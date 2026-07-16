"""Sonos backend + backend dispatch: SSDP/SOAP discovery, zone groups,
playback/queue/volume/EQ/grouping control (routing cast vs Sonos),
resume positions, and the room model."""
import json
import os
import re
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
from html import escape as xml_escape, unescape as xml_unescape
from xml.etree import ElementTree

from draai import state, cast
from draai.state import (speakers, tracks, tracks_by_id, config, positions,
                         positions_lock, _positions_dirty, state_lock, enqueue_generation)
from draai.constants import (AUDIO_EXTS, QUEUE_CAP, RESUME_MIN_TRACK, RESUME_MIN_POS,
                             POSITIONS_PATH, CONFIG_DIR)
from draai.util import sec_to_hms, hms_to_sec
from draai.media import media_url
from draai.cast import (cast_play_tracks, cast_get_status, cast_set_volume,
                        cast_seek_to, cast_set_shuffle, cast_browse_queue,
                        cast_enqueue_tracks, cast_queue_move, cast_queue_jump,
                        cast_queue_remove, cast_set_room_volume, cast_cmd, cast_discover)


SSDP_ADDR = ("239.255.255.250", 1900)


SSDP_ST = "urn:schemas-upnp-org:device:ZonePlayer:1"


def ssdp_discover(timeout=3.0):
    """Return a set of Sonos device IPs found via SSDP multicast."""
    msg = "\r\n".join([
        "M-SEARCH * HTTP/1.1",
        "HOST: 239.255.255.250:1900",
        'MAN: "ssdp:discover"',
        "MX: 2",
        "ST: " + SSDP_ST,
        "", "",
    ]).encode()
    ips = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                        struct.pack("B", 2))
        sock.settimeout(timeout)
        for _ in range(2):  # send twice; UDP is lossy
            try:
                sock.sendto(msg, SSDP_ADDR)
            except OSError:
                pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                sock.settimeout(max(0.1, deadline - time.time()))
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            except OSError:
                break
            text = data.decode("utf-8", "replace")
            if "Sonos" in text or "ZonePlayer" in text.replace(" ", ""):
                ips.add(addr[0])
    finally:
        sock.close()
    return ips


def refresh_speakers():
    """Discover Sonos (SSDP + zone topology) and Google Cast (mDNS) devices.
    Every device carries a `backend` field. Returns (list, error)."""
    sonos, sonos_err = [], None
    ips = ssdp_discover()
    for ip in config.get("manual_ips", []):
        ips.add(ip)
    for ip in sorted(ips):
        try:
            groups = get_zone_groups(ip)
            if groups:
                sonos = groups
                break
        except Exception as e:
            sonos_err = str(e)
    for g in sonos:
        g["backend"] = "sonos"
    try:
        cast = cast_discover()
    except Exception:
        cast = []
    merged = sonos + cast
    with state_lock:
        speakers[:] = merged
    if merged:
        return merged, None
    if ips and sonos_err:
        return [], "Found devices but could not read their status: %s" % sonos_err
    return [], ("No speakers found. Make sure this Mac is on the same "
                "Wi-Fi network as the speakers, then press Rescan. "
                "You can also add a speaker by its IP address.")


def get_zone_groups(any_ip):
    """Ask one speaker for the whole household topology."""
    body = soap_call(any_ip, "/ZoneGroupTopology/Control",
                     "urn:schemas-upnp-org:service:ZoneGroupTopology:1",
                     "GetZoneGroupState", {})
    m = re.search(r"<ZoneGroupState>(.*?)</ZoneGroupState>", body, re.S)
    if not m:
        raise RuntimeError("no ZoneGroupState in response")
    xml = xml_unescape(m.group(1))
    root = ElementTree.fromstring(xml)
    groups = []
    for zg in root.iter("ZoneGroup"):
        coord_uuid = zg.get("Coordinator", "")
        members = zg.findall("ZoneGroupMember")
        coord = None
        names = []
        zone_members = []
        seen_uuids = set()

        def add_member(el, fixed):
            u = el.get("UUID", "")
            mloc = re.search(r"http://([0-9.]+)", el.get("Location", ""))
            if not u or u in seen_uuids or not mloc:
                return
            seen_uuids.add(u)
            zone_members.append({"uuid": u, "name": el.get("ZoneName", "?"),
                                 "ip": mloc.group(1), "fixed": fixed})

        for mem in members:
            if mem.get("UUID") == coord_uuid:
                coord = mem
            invisible = mem.get("Invisible") == "1"
            if not invisible:
                zname = mem.get("ZoneName", "?")
                if zname not in names:
                    names.append(zname)
            # list every physical device: visible rooms are moveable,
            # bonded units (stereo pairs, subs, surrounds) are fixed
            add_member(mem, fixed=invisible)
            for sat in mem.findall("Satellite"):
                add_member(sat, fixed=True)
        # same-named devices (pairs, multi-unit rooms): disambiguate display
        name_counts = {}
        for m in zone_members:
            name_counts[m["name"]] = name_counts.get(m["name"], 0) + 1
        seen_names = {}
        for m in zone_members:
            if name_counts[m["name"]] > 1:
                seen_names[m["name"]] = seen_names.get(m["name"], 0) + 1
                m["name"] = "%s · %s" % (m["name"],
                                         m["ip"].rsplit(".", 1)[-1])
        if coord is None or not names:
            continue
        loc = coord.get("Location", "")
        ipm = re.search(r"http://([0-9.]+)", loc)
        if not ipm:
            continue
        coord_name = coord.get("ZoneName", names[0])
        others = [n for n in names if n != coord_name]
        display = coord_name + (" + " + ", ".join(others) if others else "")
        groups.append({"uuid": coord_uuid, "name": display,
                       "ip": ipm.group(1), "members": zone_members})
    groups.sort(key=lambda g: g["name"].lower())
    return groups


AVT = ("/MediaRenderer/AVTransport/Control",
       "urn:schemas-upnp-org:service:AVTransport:1")


RC = ("/MediaRenderer/RenderingControl/Control",
      "urn:schemas-upnp-org:service:RenderingControl:1")


GRC = ("/MediaRenderer/GroupRenderingControl/Control",
       "urn:schemas-upnp-org:service:GroupRenderingControl:1")


def soap_call(ip, path, service, action, args):
    parts = "".join("<%s>%s</%s>" % (k, xml_escape(str(v)), k)
                    for k, v in args.items())
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body><u:%s xmlns:u=\"%s\">%s</u:%s></s:Body></s:Envelope>"
        % (action, service, parts, action)
    )
    req = urllib.request.Request(
        "http://%s:1400%s" % (ip, path),
        data=envelope.encode("utf-8"),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": '"%s#%s"' % (service, action),
            "Connection": "close",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        code = re.search(r"<errorCode>(\d+)</errorCode>", detail)
        raise RuntimeError("Speaker refused %s (UPnP error %s)"
                           % (action, code.group(1) if code else e.code))


def avt(ip, action, args=None):
    base = {"InstanceID": 0}
    base.update(args or {})
    return soap_call(ip, AVT[0], AVT[1], action, base)


def didl_for(track, url):
    """Minimal DIDL-Lite metadata so the speaker shows the track title."""
    inner = (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="local-%s" parentID="-1" restricted="true">'
        "<dc:title>%s</dc:title>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        '<res protocolInfo="http-get:*:%s:*">%s</res>'
        '<desc id="cdudn" '
        'nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">'
        "RINCON_AssociatedZPUDN</desc>"
        "</item></DIDL-Lite>"
        % (track["id"], xml_escape(track["title"]),
           AUDIO_EXTS.get(track["ext"], "audio/mpeg"), xml_escape(url))
    )
    return inner


def speaker_by_uuid(uuid):
    with state_lock:
        for s in speakers:
            if s["uuid"] == uuid:
                return dict(s)
    return None


def play_tracks(spk, ids):
    """Replace the speaker queue with `ids` and start playing the first."""
    if spk.get("backend") == "cast":
        return cast_play_tracks(spk, ids)
    track_list = []
    with state_lock:
        for tid in ids[:QUEUE_CAP]:
            t = tracks_by_id.get(tid)
            if t:
                track_list.append(t)
    if not track_list:
        raise RuntimeError("Track not found — try rescanning your folder.")
    ip, uuid = spk["ip"], spk["uuid"]
    enqueue_generation[0] += 1
    gen = enqueue_generation[0]

    avt(ip, "RemoveAllTracksFromQueue")
    first = track_list[0]
    url = media_url(first, ip)
    avt(ip, "AddURIToQueue", {
        "EnqueuedURI": url,
        "EnqueuedURIMetaData": didl_for(first, url),
        "DesiredFirstTrackNumberEnqueued": 0,
        "EnqueueAsNext": 0,
    })
    avt(ip, "SetAVTransportURI", {
        "CurrentURI": "x-rincon-queue:%s#0" % uuid,
        "CurrentURIMetaData": "",
    })
    avt(ip, "Seek", {"Unit": "TRACK_NR", "Target": 1})
    avt(ip, "Play", {"Speed": 1})
    # resume long tracks (sets, audiobooks) where the listener left off
    with positions_lock:
        saved = positions.get(first["id"])
    if saved and saved > RESUME_MIN_POS:
        try:
            avt(ip, "Seek", {"Unit": "REL_TIME",
                             "Target": sec_to_hms(max(0, saved - 5))})
        except Exception:
            pass

    rest = track_list[1:]
    if rest:
        def add_rest():
            for t in rest:
                if enqueue_generation[0] != gen:
                    return  # a newer play request superseded this one
                try:
                    u = media_url(t, ip)
                    avt(ip, "AddURIToQueue", {
                        "EnqueuedURI": u,
                        "EnqueuedURIMetaData": didl_for(t, u),
                        "DesiredFirstTrackNumberEnqueued": 0,
                        "EnqueueAsNext": 0,
                    })
                except Exception:
                    return
        threading.Thread(target=add_rest, daemon=True).start()
    return len(track_list)


def load_positions():
    try:
        with open(POSITIONS_PATH) as f:
            loaded = json.load(f)
        positions.clear()
        positions.update(loaded)
    except Exception:
        positions.clear()


def save_positions_soon():
    if _positions_dirty[0]:
        return
    _positions_dirty[0] = True

    def flush():
        time.sleep(10)
        _positions_dirty[0] = False
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with positions_lock:
                data = dict(positions)
            with open(POSITIONS_PATH, "w") as f:
                json.dump(data, f)
        except Exception:
            pass
    threading.Thread(target=flush, daemon=True).start()


def note_position(tid, pos_sec, dur_sec):
    if dur_sec < RESUME_MIN_TRACK:
        return
    with positions_lock:
        if dur_sec - pos_sec < 120 or pos_sec < RESUME_MIN_POS:
            positions.pop(tid, None)   # finished (or barely started): forget
        else:
            positions[tid] = int(pos_sec)
    save_positions_soon()


def get_status(spk):
    if spk.get("backend") == "cast":
        return cast_get_status(spk)
    ip = spk["ip"]
    out = {"state": "UNKNOWN", "title": "", "position": "", "duration": "",
           "volume": None, "track_no": None, "track_id": None}
    try:
        body = avt(ip, "GetTransportInfo")
        m = re.search(r"<CurrentTransportState>([^<]*)</CurrentTransportState>",
                      body)
        if m:
            out["state"] = m.group(1)
    except Exception:
        pass
    try:
        body = avt(ip, "GetPositionInfo")
        meta = re.search(r"<TrackMetaData>(.*?)</TrackMetaData>", body, re.S)
        if meta:
            didl = xml_unescape(meta.group(1))
            tm = re.search(r"<dc:title>([^<]*)</dc:title>", didl)
            if tm:
                out["title"] = xml_unescape(tm.group(1))
        for key, tag in (("position", "RelTime"), ("duration", "TrackDuration")):
            m = re.search(r"<%s>([^<]*)</%s>" % (tag, tag), body)
            if m and m.group(1) not in ("NOT_IMPLEMENTED",):
                out[key] = m.group(1)
        m = re.search(r"<Track>(\d+)</Track>", body)
        if m:
            out["track_no"] = int(m.group(1))
        m = re.search(r"<TrackURI>([^<]*)</TrackURI>", body)
        if m:
            um = re.search(r"/media/([0-9a-f]{16})/", xml_unescape(m.group(1)))
            if um:
                out["track_id"] = um.group(1)
                pos = hms_to_sec(out["position"])
                dur = hms_to_sec(out["duration"])
                if pos > 0 and dur > 0:
                    note_position(um.group(1), pos, dur)
    except Exception:
        pass
    try:
        body = soap_call(ip, GRC[0], GRC[1], "GetGroupVolume", {"InstanceID": 0})
        m = re.search(r"<CurrentVolume>(\d+)</CurrentVolume>", body)
        if m:
            out["volume"] = int(m.group(1))
    except Exception:
        try:
            body = soap_call(ip, RC[0], RC[1], "GetVolume",
                             {"InstanceID": 0, "Channel": "Master"})
            m = re.search(r"<CurrentVolume>(\d+)</CurrentVolume>", body)
            if m:
                out["volume"] = int(m.group(1))
        except Exception:
            pass
    return out


def set_volume(spk, value):
    if spk.get("backend") == "cast":
        return cast_set_volume(spk, value)
    value = max(0, min(100, int(value)))
    ip = spk["ip"]
    try:
        soap_call(ip, GRC[0], GRC[1], "SetGroupVolume",
                  {"InstanceID": 0, "DesiredVolume": value})
    except Exception:
        soap_call(ip, RC[0], RC[1], "SetVolume",
                  {"InstanceID": 0, "Channel": "Master",
                   "DesiredVolume": value})


def set_shuffle(spk, on):
    if spk.get("backend") == "cast":
        return cast_set_shuffle(spk, on)
    avt(spk["ip"], "SetPlayMode",
        {"NewPlayMode": "SHUFFLE_NOREPEAT" if on else "NORMAL"})


def seek_to(spk, seconds):
    if spk.get("backend") == "cast":
        return cast_seek_to(spk, seconds)
    avt(spk["ip"], "Seek", {"Unit": "REL_TIME", "Target": sec_to_hms(seconds)})


def get_transport_state(ip):
    try:
        body = avt(ip, "GetTransportInfo")
        m = re.search(r"<CurrentTransportState>([^<]*)</CurrentTransportState>",
                      body)
        return m.group(1) if m else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


CD = ("/MediaServer/ContentDirectory/Control",
      "urn:schemas-upnp-org:service:ContentDirectory:1")


def browse_queue(spk):
    """Return (items, total) for the speaker's current queue."""
    if spk.get("backend") == "cast":
        return cast_browse_queue(spk)
    body = soap_call(spk["ip"], CD[0], CD[1], "Browse", {
        "ObjectID": "Q:0", "BrowseFlag": "BrowseDirectChildren",
        "Filter": "dc:title", "StartingIndex": 0,
        "RequestedCount": QUEUE_CAP, "SortCriteria": "",
    })
    m = re.search(r"<Result>(.*?)</Result>", body, re.S)
    tm = re.search(r"<TotalMatches>(\d+)</TotalMatches>", body)
    items = []
    if m:
        didl = xml_unescape(m.group(1))
        for i, im in enumerate(
                re.finditer(r"<item[^>]*>(.*?)</item>", didl, re.S), 1):
            t = re.search(r"<dc:title>([^<]*)</dc:title>", im.group(1))
            # our own media URLs carry the track id — recover it so the UI
            # and playlists can map queue entries back to library tracks
            u = re.search(r"/media/([0-9a-f]{16})/", im.group(1))
            items.append({"no": i,
                          "id": u.group(1) if u else None,
                          "title": xml_unescape(t.group(1)) if t
                          else "Track %d" % i})
    total = int(tm.group(1)) if tm else len(items)
    return items, total


def queue_move(spk, from_no, to_no):
    """Move one queue item from position `from_no` to final position `to_no`
    (both 1-based). Sonos's InsertBefore counts positions in the queue as it
    stands BEFORE removal, hence the +1 when moving downward."""
    if spk.get("backend") == "cast":
        return cast_queue_move(spk, from_no, to_no)
    from_no, to_no = int(from_no), int(to_no)
    if from_no == to_no:
        return
    insert_before = to_no if to_no < from_no else to_no + 1
    avt(spk["ip"], "ReorderTracksInQueue",
        {"StartingIndex": from_no, "NumberOfTracks": 1,
         "InsertBefore": insert_before, "UpdateID": 0})


def queue_jump(spk, no):
    if spk.get("backend") == "cast":
        return cast_queue_jump(spk, no)
    ip, uuid = spk["ip"], spk["uuid"]
    avt(ip, "SetAVTransportURI", {
        "CurrentURI": "x-rincon-queue:%s#0" % uuid, "CurrentURIMetaData": ""})
    avt(ip, "Seek", {"Unit": "TRACK_NR", "Target": int(no)})
    avt(ip, "Play", {"Speed": 1})


def queue_remove(spk, no):
    if spk.get("backend") == "cast":
        return cast_queue_remove(spk, no)
    avt(spk["ip"], "RemoveTrackFromQueue",
        {"ObjectID": "Q:0/%d" % int(no), "UpdateID": 0})


def enqueue_tracks(spk, ids, play_next=False):
    """Append tracks to the queue — or, with play_next, insert them as a
    block right after the currently playing track, order preserved."""
    if spk.get("backend") == "cast":
        return cast_enqueue_tracks(spk, ids, play_next)
    track_list = []
    with state_lock:
        for tid in ids[:QUEUE_CAP]:
            t = tracks_by_id.get(tid)
            if t:
                track_list.append(t)
    if not track_list:
        raise RuntimeError("Track not found — try rescanning your folder.")
    ip, uuid = spk["ip"], spk["uuid"]
    try:
        _, before = browse_queue(spk)
    except Exception:
        before = None

    insert_at = 0          # 0 = append at the end
    if play_next and before:
        st = get_status(spk)
        cur = st.get("track_no") or 0
        if cur > 0:
            insert_at = cur + 1
    if play_next and insert_at:
        # insert the whole block sequentially so order is preserved
        for i, t in enumerate(track_list):
            u = media_url(t, ip)
            avt(ip, "AddURIToQueue", {
                "EnqueuedURI": u,
                "EnqueuedURIMetaData": didl_for(t, u),
                "DesiredFirstTrackNumberEnqueued": insert_at + i,
                "EnqueueAsNext": 1,
            })
        return len(track_list)

    gen = enqueue_generation[0]

    first = track_list[0]
    url = media_url(first, ip)
    avt(ip, "AddURIToQueue", {
        "EnqueuedURI": url,
        "EnqueuedURIMetaData": didl_for(first, url),
        "DesiredFirstTrackNumberEnqueued": 0,
        "EnqueueAsNext": 0,
    })
    # If the queue was empty and nothing is playing, start it up.
    if before == 0 and get_transport_state(ip) != "PLAYING":
        try:
            avt(ip, "SetAVTransportURI", {
                "CurrentURI": "x-rincon-queue:%s#0" % uuid,
                "CurrentURIMetaData": ""})
            avt(ip, "Play", {"Speed": 1})
        except Exception:
            pass

    rest = track_list[1:]
    if rest:
        def add_rest():
            for t in rest:
                if enqueue_generation[0] != gen:
                    return  # a new "play" wiped the queue; stop appending
                try:
                    u = media_url(t, ip)
                    avt(ip, "AddURIToQueue", {
                        "EnqueuedURI": u,
                        "EnqueuedURIMetaData": didl_for(t, u),
                        "DesiredFirstTrackNumberEnqueued": 0,
                        "EnqueueAsNext": 0,
                    })
                except Exception:
                    return
        threading.Thread(target=add_rest, daemon=True).start()
    return len(track_list)


def get_eq(spk):
    if spk.get("backend") == "cast":
        raise RuntimeError("The equalizer isn't available on Chromecast.")
    ip = spk["ip"]
    out = {}
    for action, tag, key in (("GetBass", "CurrentBass", "bass"),
                             ("GetTreble", "CurrentTreble", "treble")):
        body = soap_call(ip, RC[0], RC[1], action, {"InstanceID": 0})
        m = re.search(r"<%s>(-?\d+)</%s>" % (tag, tag), body)
        out[key] = int(m.group(1)) if m else 0
    body = soap_call(ip, RC[0], RC[1], "GetLoudness",
                     {"InstanceID": 0, "Channel": "Master"})
    m = re.search(r"<CurrentLoudness>(\d)</CurrentLoudness>", body)
    out["loudness"] = bool(int(m.group(1))) if m else True
    return out


def set_eq(spk, bass=None, treble=None, loudness=None):
    if spk.get("backend") == "cast":
        raise RuntimeError("The equalizer isn't available on Chromecast.")
    ip = spk["ip"]
    if bass is not None:
        soap_call(ip, RC[0], RC[1], "SetBass",
                  {"InstanceID": 0,
                   "DesiredBass": max(-10, min(10, int(bass)))})
    if treble is not None:
        soap_call(ip, RC[0], RC[1], "SetTreble",
                  {"InstanceID": 0,
                   "DesiredTreble": max(-10, min(10, int(treble)))})
    if loudness is not None:
        soap_call(ip, RC[0], RC[1], "SetLoudness",
                  {"InstanceID": 0, "Channel": "Master",
                   "DesiredLoudness": 1 if loudness else 0})


def set_sleep(spk, minutes):
    minutes = max(0, min(720, int(minutes)))
    avt(spk["ip"], "ConfigureSleepTimer",
        {"NewSleepTimerDuration":
         "" if minutes == 0 else sec_to_hms(minutes * 60)})


def zone_by_uuid(zuuid):
    with state_lock:
        for g in speakers:
            for m in g.get("members", []):
                if m["uuid"] == zuuid:
                    return dict(m)
            if g["uuid"] == zuuid:
                return {"uuid": g["uuid"], "name": g["name"], "ip": g["ip"]}
    return None


def group_join(member_uuid, coordinator_uuid):
    _m = speaker_by_uuid(member_uuid)
    if _m and _m.get("backend") == "cast":
        raise RuntimeError("Grouping isn't available on Chromecast from DRAAI.")
    zone = zone_by_uuid(member_uuid)
    if not zone:
        raise RuntimeError("Unknown room.")
    avt(zone["ip"], "SetAVTransportURI",
        {"CurrentURI": "x-rincon:%s" % coordinator_uuid,
         "CurrentURIMetaData": ""})
    time.sleep(0.5)
    return refresh_speakers()


def group_leave(member_uuid):
    _m = speaker_by_uuid(member_uuid)
    if _m and _m.get("backend") == "cast":
        raise RuntimeError("Grouping isn't available on Chromecast from DRAAI.")
    zone = zone_by_uuid(member_uuid)
    if not zone:
        raise RuntimeError("Unknown room.")
    avt(zone["ip"], "BecomeCoordinatorOfStandaloneGroup")
    time.sleep(0.5)
    return refresh_speakers()


def get_rooms(spk):
    rooms = []
    for m in spk.get("members", []):
        vol = None
        try:
            body = soap_call(m["ip"], RC[0], RC[1], "GetVolume",
                             {"InstanceID": 0, "Channel": "Master"})
            mm = re.search(r"<CurrentVolume>(\d+)</CurrentVolume>", body)
            if mm:
                vol = int(mm.group(1))
        except Exception:
            pass
        rooms.append({"uuid": m["uuid"], "name": m["name"], "ip": m["ip"],
                      "volume": vol, "fixed": m.get("fixed", False)})
    return rooms


def set_room_volume(member_uuid, value):
    _m = speaker_by_uuid(member_uuid)
    if _m and _m.get("backend") == "cast":
        return cast_set_room_volume(_m, value)
    zone = zone_by_uuid(member_uuid)
    if not zone:
        raise RuntimeError("Unknown room.")
    soap_call(zone["ip"], RC[0], RC[1], "SetVolume",
              {"InstanceID": 0, "Channel": "Master",
               "DesiredVolume": max(0, min(100, int(value)))})
