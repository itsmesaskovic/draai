#!/usr/bin/env python3
"""
DRAAI — play your local music on Sonos / IKEA Symfonisk speakers.

Your library. Your network. Your control.

No dependencies: uses only the Python standard library.
Run:   python3 sonos_player.py
Then a control panel opens in your browser. Keep this running while music plays
(the speakers stream the files from this little server).

How it works:
  * Finds speakers on your Wi-Fi via SSDP (the UPnP discovery protocol).
  * Reads group topology from the speakers (handles stereo pairs / groups).
  * Serves your audio files over HTTP so the speaker can fetch them.
  * Controls playback with plain UPnP/SOAP calls (no Sonos account, no cloud).
"""

import itertools
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from hashlib import sha1
from html import escape as xml_escape, unescape as xml_unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree

APP_NAME = "DRAAI"
__version__ = "1.0.0"
PREFERRED_PORT = 8765
AUDIO_EXTS = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".ogg": "audio/ogg",
}
QUEUE_CAP = 500  # max tracks sent to a speaker queue in one go

CONFIG_DIR = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "SonosMP3Player"
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------

state_lock = threading.Lock()
tracks = []            # list of dicts: {id, path, title, folder, ext}
tracks_by_id = {}      # id -> track dict
speakers = []          # list of dicts: {uuid, name, ip}
config = {"folders": [os.path.join(os.path.expanduser("~"), "Music")],
          "manual_ips": [], "last_speaker": None}
server_port = PREFERRED_PORT


def load_config():
    global config
    try:
        with open(CONFIG_PATH, "r") as f:
            saved = json.load(f)
    except Exception:
        return
    if isinstance(saved.get("folders"), list) and saved["folders"]:
        config["folders"] = saved["folders"]
    elif saved.get("folder"):               # migrate old single-folder config
        config["folders"] = [saved["folder"]]
    for k in ("manual_ips", "last_speaker"):
        if k in saved:
            config[k] = saved[k]


def save_config():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Tag parsing (pure stdlib): title / artist / album / embedded cover art
# ----------------------------------------------------------------------------

def _syncsafe(b):
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _id3_text(body):
    if not body:
        return ""
    enc, raw = body[0], body[1:]
    try:
        if enc == 0:
            s = raw.decode("latin-1")
        elif enc == 1:
            s = raw.decode("utf-16")
        elif enc == 2:
            s = raw.decode("utf-16-be")
        else:
            s = raw.decode("utf-8")
    except Exception:
        s = raw.decode("latin-1", "replace")
    return s.strip("\x00").strip()


def _null_split(data, wide):
    """Split at a null terminator (double-null for UTF-16). Returns (rest)."""
    if wide:
        i = 0
        while i + 1 < len(data):
            if data[i] == 0 and data[i + 1] == 0:
                return data[i + 2:]
            i += 2
        return b""
    i = data.find(b"\x00")
    return data[i + 1:] if i >= 0 else b""


def _tags_mp3(path, want_art):
    with open(path, "rb") as f:
        head = f.read(10)
        if len(head) < 10 or head[:3] != b"ID3":
            return {}
        ver, flags = head[3], head[5]
        size = _syncsafe(head[6:10])
        if size <= 0 or size > 32 * 1024 * 1024:
            return {}
        data = f.read(size)
    pos = 0
    if flags & 0x40:  # extended header
        if len(data) < 4:
            return {}
        eh = _syncsafe(data[0:4]) if ver >= 4 else int.from_bytes(data[0:4], "big") + 4
        pos = eh
    out = {}
    if ver == 2:
        idlen, hdrlen = 3, 6
        names = {b"TT2": "title", b"TP1": "artist", b"TAL": "album"}
        art_id = b"PIC"
    else:
        idlen, hdrlen = 4, 10
        names = {b"TIT2": "title", b"TPE1": "artist", b"TALB": "album"}
        art_id = b"APIC"
    while pos + hdrlen <= len(data):
        fid = data[pos:pos + idlen]
        if fid.strip(b"\x00") == b"" or not fid.isalnum() and b"\xa9" not in fid:
            break
        if ver == 2:
            fsize = int.from_bytes(data[pos + 3:pos + 6], "big")
        elif ver >= 4:
            fsize = _syncsafe(data[pos + 4:pos + 8])
        else:
            fsize = int.from_bytes(data[pos + 4:pos + 8], "big")
        body = data[pos + hdrlen:pos + hdrlen + fsize]
        pos += hdrlen + fsize
        if fsize <= 0:
            break
        key = names.get(fid)
        if key and key not in out:
            out[key] = _id3_text(body)
        elif fid == art_id and "art" not in out:
            out["has_art"] = True
            if want_art:
                try:
                    enc = body[0]
                    if ver == 2:  # PIC: enc + 3-byte format + type + desc + data
                        rest = body[5:]
                    else:  # APIC: enc + mime\0 + type + desc + data
                        rest = _null_split(body[1:], False)
                        rest = rest[1:]  # picture type byte
                    rest = _null_split(rest, enc in (1, 2))
                    if rest[:3] == b"\xff\xd8\xff":
                        out["art"] = ("image/jpeg", rest)
                    elif rest[:4] == b"\x89PNG":
                        out["art"] = ("image/png", rest)
                except Exception:
                    pass
    return out


def _mp4_children(data, pos, end):
    """Yield (type, body_start, body_end) for boxes in data[pos:end]."""
    while pos + 8 <= end:
        size = int.from_bytes(data[pos:pos + 4], "big")
        btype = data[pos + 4:pos + 8]
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
            hdr = 16
        if size < hdr or pos + size > end:
            return
        yield btype, pos + hdr, pos + size
        pos += size


def _tags_mp4(path, want_art):
    # find the moov box at top level without reading the whole (huge) file
    moov = None
    with open(path, "rb") as f:
        f.seek(0, 2)
        fsize = f.tell()
        pos = 0
        while pos + 8 <= fsize:
            f.seek(pos)
            hdr = f.read(16)
            if len(hdr) < 8:
                break
            size = int.from_bytes(hdr[0:4], "big")
            btype = hdr[4:8]
            hlen = 8
            if size == 1:
                size = int.from_bytes(hdr[8:16], "big")
                hlen = 16
            if size < hlen:
                break
            if btype == b"moov":
                if size > 64 * 1024 * 1024:
                    return {}
                f.seek(pos + hlen)
                moov = f.read(size - hlen)
                break
            pos += size
    if moov is None:
        return {}
    out = {}
    names = {b"\xa9nam": "title", b"\xa9ART": "artist", b"\xa9alb": "album"}
    for t1, s1, e1 in _mp4_children(moov, 0, len(moov)):
        if t1 != b"udta":
            continue
        for t2, s2, e2 in _mp4_children(moov, s1, e1):
            if t2 != b"meta":
                continue
            for t3, s3, e3 in _mp4_children(moov, s2 + 4, e2):  # meta: 4-byte ver
                if t3 != b"ilst":
                    continue
                for t4, s4, e4 in _mp4_children(moov, s3, e3):
                    for t5, s5, e5 in _mp4_children(moov, s4, e4):
                        if t5 != b"data" or e5 - s5 <= 8:
                            continue
                        payload = moov[s5 + 8:e5]
                        key = names.get(t4)
                        if key and key not in out:
                            out[key] = payload.decode("utf-8", "replace").strip()
                        elif t4 == b"covr":
                            out["has_art"] = True
                            if want_art:
                                if payload[:3] == b"\xff\xd8\xff":
                                    out["art"] = ("image/jpeg", payload)
                                elif payload[:4] == b"\x89PNG":
                                    out["art"] = ("image/png", payload)
    return out


def _tags_flac(path, want_art):
    out = {}
    with open(path, "rb") as f:
        if f.read(4) != b"fLaC":
            return {}
        while True:
            hdr = f.read(4)
            if len(hdr) < 4:
                break
            last = hdr[0] & 0x80
            btype = hdr[0] & 0x7F
            size = int.from_bytes(hdr[1:4], "big")
            if btype == 4 and size < 16 * 1024 * 1024:  # VORBIS_COMMENT
                blk = f.read(size)
                try:
                    p = 4 + int.from_bytes(blk[0:4], "little")  # skip vendor
                    n = int.from_bytes(blk[p:p + 4], "little")
                    p += 4
                    for _ in range(min(n, 256)):
                        ln = int.from_bytes(blk[p:p + 4], "little")
                        p += 4
                        kv = blk[p:p + ln].decode("utf-8", "replace")
                        p += ln
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            k = k.upper()
                            if k == "TITLE" and "title" not in out:
                                out["title"] = v.strip()
                            elif k == "ARTIST" and "artist" not in out:
                                out["artist"] = v.strip()
                            elif k == "ALBUM" and "album" not in out:
                                out["album"] = v.strip()
                except Exception:
                    pass
            elif btype == 6 and size < 32 * 1024 * 1024:  # PICTURE
                out["has_art"] = True
                if want_art and "art" not in out:
                    blk = f.read(size)
                    try:
                        p = 4
                        ml = int.from_bytes(blk[p:p + 4], "big")
                        mime = blk[p + 4:p + 4 + ml].decode("latin-1")
                        p += 4 + ml
                        dl = int.from_bytes(blk[p:p + 4], "big")
                        p += 4 + dl + 16  # desc + w/h/depth/colors
                        n = int.from_bytes(blk[p:p + 4], "big")
                        out["art"] = (mime or "image/jpeg",
                                      blk[p + 4:p + 4 + n])
                    except Exception:
                        pass
                else:
                    f.seek(size, 1)
            else:
                f.seek(size, 1)
            if last:
                break
    return out


def read_tags(path, want_art=False):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            return _tags_mp3(path, want_art)
        if ext in (".m4a", ".aac", ".mp4"):
            return _tags_mp4(path, want_art)
        if ext == ".flac":
            return _tags_flac(path, want_art)
    except Exception:
        pass
    return {}


art_cache = {}          # track id -> (mime, bytes); simple bounded cache
ART_CACHE_MAX = 64


def get_art(track):
    tid = track["id"]
    if tid in art_cache:
        return art_cache[tid]
    tags = read_tags(track["path"], want_art=True)
    art = tags.get("art")
    if art:
        if len(art_cache) >= ART_CACHE_MAX:
            art_cache.pop(next(iter(art_cache)))
        art_cache[tid] = art
    return art


# ----------------------------------------------------------------------------
# Music library
# ----------------------------------------------------------------------------

def scan_all():
    """Rescan every configured folder into one library. Returns (count, err)."""
    global tracks, tracks_by_id
    roots = [os.path.expanduser(f.strip()) for f in config.get("folders", [])]
    roots = [r for r in roots if r]
    multi = len(roots) > 1
    merged = []
    missing = []
    for root in roots:
        if not os.path.isdir(root):
            missing.append(root)
            continue
        merged.extend(_scan_root(root, multi))
    merged.sort(key=lambda t: (t["folder"].lower(), t["title"].lower()))
    with state_lock:
        tracks = merged
        tracks_by_id = {t["id"]: t for t in merged}
    err = ("These folders do not exist: " + ", ".join(missing)) if missing else None
    return len(merged), err


def scan_folder(folder):
    """Back-compat single-folder scan: replaces the folder list."""
    folder = os.path.expanduser(folder.strip())
    if not os.path.isdir(folder):
        return 0, "That folder does not exist: %s" % folder
    config["folders"] = [folder]
    return scan_all()


def _scan_root(folder, multi):
    """Collect audio files under one root."""
    found = []
    base = os.path.basename(folder.rstrip("/")) or folder
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(files):
            ext = os.path.splitext(name)[1].lower()
            if ext not in AUDIO_EXTS or name.startswith("."):
                continue
            path = os.path.join(root, name)
            tid = sha1(path.encode("utf-8", "surrogateescape")).hexdigest()[:16]
            rel = os.path.relpath(root, folder)
            rel = "" if rel == "." else rel
            if multi:   # prefix with the root's name so origins stay clear
                rel = base + ("/" + rel if rel else "")
            tags = read_tags(path)
            found.append({
                "id": tid,
                "path": path,
                "title": tags.get("title") or os.path.splitext(name)[0],
                "artist": tags.get("artist", ""),
                "album": tags.get("album", ""),
                "has_art": bool(tags.get("has_art")),
                "folder": rel,
                "ext": ext,
            })
    return found


# ----------------------------------------------------------------------------
# Sonos discovery (SSDP + zone group topology)
# ----------------------------------------------------------------------------

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
    """Discover speakers and read group topology. Returns (list, error)."""
    global speakers
    ips = ssdp_discover()
    for ip in config.get("manual_ips", []):
        ips.add(ip)
    if not ips:
        return [], ("No speakers found. Make sure this Mac is on the same "
                    "Wi-Fi network as the speakers, then press Rescan. "
                    "You can also add a speaker by its IP address.")
    last_err = None
    for ip in sorted(ips):
        try:
            groups = get_zone_groups(ip)
            if groups:
                with state_lock:
                    speakers = groups
                return groups, None
        except Exception as e:
            last_err = str(e)
    return [], "Found devices but could not read their status: %s" % last_err


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


# ----------------------------------------------------------------------------
# Sonos control (UPnP/SOAP)
# ----------------------------------------------------------------------------

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


def local_ip_facing(speaker_ip):
    """The IP of the interface this Mac uses to reach the speaker."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((speaker_ip, 1400))
        return s.getsockname()[0]
    finally:
        s.close()


def media_url(track, speaker_ip):
    host = local_ip_facing(speaker_ip)
    name = urllib.parse.quote(track["title"] + track["ext"])
    return "http://%s:%d/media/%s/%s" % (host, server_port, track["id"], name)


def speaker_by_uuid(uuid):
    with state_lock:
        for s in speakers:
            if s["uuid"] == uuid:
                return dict(s)
    return None


enqueue_generation = [0]  # bump to cancel a background enqueue in progress


def play_tracks(spk, ids):
    """Replace the speaker queue with `ids` and start playing the first."""
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


# -- position memory: resume long tracks where you left off -------------------

POSITIONS_PATH = os.path.join(CONFIG_DIR, "positions.json")
RESUME_MIN_TRACK = 600      # only remember tracks longer than 10 min
RESUME_MIN_POS = 90         # ...and positions past 1:30
positions = {}
positions_lock = threading.Lock()
_positions_dirty = [False]


def load_positions():
    global positions
    try:
        with open(POSITIONS_PATH) as f:
            positions = json.load(f)
    except Exception:
        positions = {}


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
    avt(spk["ip"], "SetPlayMode",
        {"NewPlayMode": "SHUFFLE_NOREPEAT" if on else "NORMAL"})


def hms_to_sec(t):
    try:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + int(float(s))
    except Exception:
        return 0


def sec_to_hms(n):
    n = max(0, int(n))
    return "%d:%02d:%02d" % (n // 3600, (n % 3600) // 60, n % 60)


def seek_to(spk, seconds):
    avt(spk["ip"], "Seek", {"Unit": "REL_TIME", "Target": sec_to_hms(seconds)})


def get_transport_state(ip):
    try:
        body = avt(ip, "GetTransportInfo")
        m = re.search(r"<CurrentTransportState>([^<]*)</CurrentTransportState>",
                      body)
        return m.group(1) if m else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


# -- speaker queue -----------------------------------------------------------

CD = ("/MediaServer/ContentDirectory/Control",
      "urn:schemas-upnp-org:service:ContentDirectory:1")


def browse_queue(spk):
    """Return (items, total) for the speaker's current queue."""
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
            items.append({"no": i,
                          "title": xml_unescape(t.group(1)) if t
                          else "Track %d" % i})
    total = int(tm.group(1)) if tm else len(items)
    return items, total


def queue_jump(spk, no):
    ip, uuid = spk["ip"], spk["uuid"]
    avt(ip, "SetAVTransportURI", {
        "CurrentURI": "x-rincon-queue:%s#0" % uuid, "CurrentURIMetaData": ""})
    avt(ip, "Seek", {"Unit": "TRACK_NR", "Target": int(no)})
    avt(ip, "Play", {"Speed": 1})


def queue_remove(spk, no):
    avt(spk["ip"], "RemoveTrackFromQueue",
        {"ObjectID": "Q:0/%d" % int(no), "UpdateID": 0})


def enqueue_tracks(spk, ids):
    """Append tracks to the speaker's queue (without replacing it)."""
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


# -- yt-dlp import (optional; needs the separate yt-dlp + ffmpeg tools) ------
# This app does not include or download any site-specific software. If the
# user has installed yt-dlp themselves, links are handed to it as-is.

YT_URL_RE = re.compile(r"^https?://\S+$", re.I)

yt_jobs = {}
yt_counter = itertools.count(1)


def find_tool(name):
    p = shutil.which(name)
    if p:
        return p
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        c = os.path.join(d, name)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def yt_available():
    missing = [n for n in ("yt-dlp", "ffmpeg") if not find_tool(n)]
    return {"available": not missing, "missing": missing}


def start_youtube_job(url):
    ytdlp, ffmpeg = find_tool("yt-dlp"), find_tool("ffmpeg")
    if not ytdlp or not ffmpeg:
        raise RuntimeError("yt-dlp and ffmpeg are needed for this. In "
                           "Terminal run:  brew install yt-dlp ffmpeg")
    job_id = str(next(yt_counter))
    yt_jobs[job_id] = {"status": "working",
                       "detail": "Fetching video info…",
                       "title": "", "error": ""}

    def run():
        job = yt_jobs[job_id]
        try:
            r = subprocess.run(
                [ytdlp, "--no-playlist", "--print", "title", url],
                capture_output=True, text=True, timeout=90)
            if r.returncode == 0 and r.stdout.strip():
                job["title"] = r.stdout.strip().splitlines()[0]
                job["detail"] = "Downloading: %s" % job["title"]
            else:
                job["detail"] = "Downloading…"
            folders = config.get("folders", [])
            base = folders[0] if folders else os.path.expanduser("~/Music")
            outdir = os.path.join(base, "Imported")
            os.makedirs(outdir, exist_ok=True)
            r = subprocess.run(
                [ytdlp, "--no-playlist", "-x", "--audio-format", "mp3",
                 "--audio-quality", "0", "--ffmpeg-location", ffmpeg,
                 "--embed-metadata", "--embed-thumbnail",
                 "--convert-thumbnails", "jpg",
                 "-o", os.path.join(outdir, "%(title)s.%(ext)s"), url],
                capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                lines = [l for l in (r.stderr or r.stdout).strip().splitlines()
                         if l.strip()]
                raise RuntimeError(lines[-1] if lines else "Download failed")
            scan_all()
            job["status"] = "done"
            job["detail"] = ("Added “%s” to your library "
                             "(Imported folder)" % (job["title"] or "track"))
        except subprocess.TimeoutExpired:
            job["status"] = "error"
            job["error"] = "The download took too long and was stopped."
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)

    threading.Thread(target=run, daemon=True).start()
    return job_id


# ----------------------------------------------------------------------------
# Audio analysis (needs ffmpeg): waveform peaks + band energy over time
# ----------------------------------------------------------------------------

ANALYSIS_DIR = os.path.join(CONFIG_DIR, "analysis")
ANALYSIS_SR = 8000
ANALYSIS_STEP = 0.1          # seconds per frame
ANALYSIS_MAX_SEC = 43200     # analyze up to 12 hours of audio
ANALYSIS_VERSION = 2         # bump to invalidate older (25-min-capped) caches
PEAK_BUCKETS = 240

analysis_state = {}          # id -> "pending" | "error:<msg>"
analysis_lock = threading.Lock()


def _scale(values, peak):
    """Scale raw envelope values to 0..100 against a shared peak."""
    return [min(100, round(100 * v / peak)) for v in values]


def _stream_envelope(ffmpeg, path, afilter=None):
    """Decode with ffmpeg and reduce to a max-abs envelope while streaming.

    Constant memory regardless of track length — a 10-hour set never
    exists in RAM as raw audio, only as its 100ms loudness envelope.
    """
    import array
    cmd = [ffmpeg, "-v", "error", "-t", str(ANALYSIS_MAX_SEC), "-i", path,
           "-ac", "1", "-ar", str(ANALYSIS_SR)]
    if afilter:
        cmd += ["-af", afilter]
    cmd += ["-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    win_bytes = int(ANALYSIS_SR * ANALYSIS_STEP) * 2
    out, buf = [], b""
    try:
        while True:
            chunk = proc.stdout.read(1 << 16)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= win_bytes:
                seg, buf = buf[:win_bytes], buf[win_bytes:]
                a = array.array("h")
                a.frombytes(seg)
                if sys.byteorder == "big":
                    a.byteswap()
                out.append(max(max(a), -min(a), 1))
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()
    if not out:
        raise RuntimeError("could not decode audio")
    return out


def _analyze(track):
    tid = track["id"]
    try:
        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is not installed (brew install ffmpeg)")
        raw_amp = _stream_envelope(ffmpeg, track["path"])
        peak = max(raw_amp) if raw_amp else 1
        amp = _scale(raw_amp, peak)
        # bands share the full-band peak so relative loudness is preserved
        low = _scale(_stream_envelope(ffmpeg, track["path"],
                                      "lowpass=f=250"), peak)
        mid = _scale(_stream_envelope(ffmpeg, track["path"],
                                      "highpass=f=250,lowpass=f=2000"), peak)
        high = _scale(_stream_envelope(ffmpeg, track["path"],
                                       "highpass=f=2000"), peak)
        frames = len(amp)
        # waveform peaks: bucket the amp envelope down to PEAK_BUCKETS
        peaks = []
        if frames:
            per = max(1, frames // PEAK_BUCKETS)
            for i in range(0, frames, per):
                peaks.append(max(amp[i:i + per]))
            peaks = peaks[:PEAK_BUCKETS]
        data = {
            "status": "ready",
            "v": ANALYSIS_VERSION,
            "duration": round(frames * ANALYSIS_STEP, 1),
            "step": ANALYSIS_STEP,
            "peaks": peaks,
            "amp": amp, "low": low, "mid": mid[:frames], "high": high[:frames],
        }
        os.makedirs(ANALYSIS_DIR, exist_ok=True)
        with open(os.path.join(ANALYSIS_DIR, tid + ".json"), "w") as f:
            json.dump(data, f)
        with analysis_lock:
            analysis_state.pop(tid, None)
    except Exception as e:
        with analysis_lock:
            analysis_state[tid] = "error:%s" % e


def get_analysis(tid):
    """Return analysis dict, or {"status": "pending"/"error"} and kick a job."""
    path = os.path.join(ANALYSIS_DIR, tid + ".json")
    if os.path.isfile(path):
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("v") == ANALYSIS_VERSION:
                return data
            os.remove(path)   # stale cache (e.g. 25-min-capped): re-analyze
        except Exception:
            pass
    with state_lock:
        track = tracks_by_id.get(tid)
    if not track:
        return {"status": "error", "error": "unknown track"}
    with analysis_lock:
        st = analysis_state.get(tid)
        if st is None:
            analysis_state[tid] = "pending"
            threading.Thread(target=_analyze, args=(track,),
                             daemon=True).start()
            return {"status": "pending"}
        if st == "pending":
            return {"status": "pending"}
        return {"status": "error", "error": st[6:]}


def prefetch_analysis(ids, limit=3):
    for tid in ids[:limit]:
        threading.Thread(target=get_analysis, args=(tid,),
                         daemon=True).start()


# ----------------------------------------------------------------------------
# Speaker EQ + sleep timer
# ----------------------------------------------------------------------------

def get_eq(spk):
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


# ----------------------------------------------------------------------------
# Multi-room grouping
# ----------------------------------------------------------------------------

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
    zone = zone_by_uuid(member_uuid)
    if not zone:
        raise RuntimeError("Unknown room.")
    avt(zone["ip"], "SetAVTransportURI",
        {"CurrentURI": "x-rincon:%s" % coordinator_uuid,
         "CurrentURIMetaData": ""})
    time.sleep(0.5)
    return refresh_speakers()


def group_leave(member_uuid):
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
    zone = zone_by_uuid(member_uuid)
    if not zone:
        raise RuntimeError("Unknown room.")
    soap_call(zone["ip"], RC[0], RC[1], "SetVolume",
              {"InstanceID": 0, "Channel": "Master",
               "DesiredVolume": max(0, min(100, int(value)))})


# ----------------------------------------------------------------------------
# QR code (pure stdlib) — byte mode, EC level L, versions 1-5
# ----------------------------------------------------------------------------

_QR_TOTAL = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134}
_QR_EC = {1: 7, 2: 10, 3: 15, 4: 20, 5: 26}
_QR_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30]}

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    _GF_EXP[_i] = _x
    _GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _rs_ec(data, n_ec):
    # generator polynomial: product of (x + α^i), i = 0..n_ec-1 (highest first)
    gen = [1]
    for i in range(n_ec):
        new = [0] * (len(gen) + 1)
        for j, g in enumerate(gen):
            new[j] ^= g                                   # g * x
            if g:
                new[j + 1] ^= _GF_EXP[(_GF_LOG[g] + i) % 255]  # g * α^i
        gen = new
    # polynomial long division
    msg = list(data) + [0] * n_ec
    for i in range(len(data)):
        c = msg[i]
        if c:
            lc = _GF_LOG[c]
            for j in range(1, len(gen)):
                if gen[j]:
                    msg[i + j] ^= _GF_EXP[(lc + _GF_LOG[gen[j]]) % 255]
    return msg[len(data):]


def _qr_matrix(text):
    data = text.encode("utf-8")
    ver = None
    for v in sorted(_QR_TOTAL):
        if len(data) <= _QR_TOTAL[v] - _QR_EC[v] - 2:
            ver = v
            break
    if ver is None:
        raise RuntimeError("text too long for QR")
    n_data = _QR_TOTAL[ver] - _QR_EC[ver]
    # bitstream: mode 0100, 8-bit length, data, terminator, pads
    bits = "0100" + format(len(data), "08b")
    for b in data:
        bits += format(b, "08b")
    bits += "0" * min(4, n_data * 8 - len(bits))
    while len(bits) % 8:
        bits += "0"
    codewords = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    pads = [0xEC, 0x11]
    while len(codewords) < n_data:
        codewords.append(pads[(len(codewords) - len(bits) // 8) % 2])
    codewords += _rs_ec(codewords, _QR_EC[ver])

    size = 17 + 4 * ver
    M = [[None] * size for _ in range(size)]  # None = unset data area

    def set_region(r, c, pattern):
        for dr, row in enumerate(pattern):
            for dc, v in enumerate(row):
                rr, cc = r + dr, c + dc
                if 0 <= rr < size and 0 <= cc < size:
                    M[rr][cc] = v

    finder = [[1] * 7, [1, 0, 0, 0, 0, 0, 1], [1, 0, 1, 1, 1, 0, 1],
              [1, 0, 1, 1, 1, 0, 1], [1, 0, 1, 1, 1, 0, 1],
              [1, 0, 0, 0, 0, 0, 1], [1] * 7]
    for r, c in ((0, 0), (0, size - 7), (size - 7, 0)):
        set_region(r, c, finder)
    # separators
    for i in range(8):
        for r, c in ((7, i), (i, 7), (7, size - 8 + i), (i, size - 8),
                     (size - 8, i), (size - 8 + i, 7)):
            if 0 <= r < size and 0 <= c < size and M[r][c] is None:
                M[r][c] = 0
    # alignment patterns
    ap = [[1] * 5, [1, 0, 0, 0, 1], [1, 0, 1, 0, 1],
          [1, 0, 0, 0, 1], [1] * 5]
    coords = _QR_ALIGN[ver]
    for r in coords:
        for c in coords:
            if M[r][c] is None:  # skip ones overlapping finders
                set_region(r - 2, c - 2, ap)
    # timing
    for i in range(8, size - 8):
        if M[6][i] is None:
            M[6][i] = 1 - (i % 2)
        if M[i][6] is None:
            M[i][6] = 1 - (i % 2)
    # dark module + reserve format areas
    M[size - 8][8] = 1
    fmt_pos = []
    for i in range(9):
        if i != 6:
            fmt_pos.append((8, i))
            fmt_pos.append((i, 8))
    for i in range(8):
        fmt_pos.append((size - 1 - i, 8))
        fmt_pos.append((8, size - 1 - i))
    for r, c in fmt_pos:
        if M[r][c] is None:
            M[r][c] = 0
    # place data bits (zigzag)
    bitstr = "".join(format(cw, "08b") for cw in codewords)
    bi = 0
    col = size - 1
    upward = True
    func = [[M[r][c] is not None for c in range(size)] for r in range(size)]
    while col > 0:
        if col == 6:
            col -= 1
        rng = range(size - 1, -1, -1) if upward else range(size)
        for r in rng:
            for c in (col, col - 1):
                if not func[r][c]:
                    M[r][c] = int(bitstr[bi]) if bi < len(bitstr) else 0
                    bi += 1
        upward = not upward
        col -= 2

    def penalty(mat):
        p = 0
        for rows in (mat, list(zip(*mat))):
            for row in rows:
                run, prev = 0, -1
                for v in row:
                    if v == prev:
                        run += 1
                    else:
                        if run >= 5:
                            p += 3 + run - 5
                        run, prev = 1, v
                if run >= 5:
                    p += 3 + run - 5
        for r in range(size - 1):
            for c in range(size - 1):
                if mat[r][c] == mat[r][c + 1] == mat[r + 1][c] == mat[r + 1][c + 1]:
                    p += 3
        pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
        pat2 = pat1[::-1]
        for rows in (mat, list(zip(*mat))):
            for row in rows:
                row = list(row)
                for i in range(len(row) - 10):
                    if row[i:i + 11] in (pat1, pat2):
                        p += 40
        dark = sum(sum(row) for row in mat)
        ratio = abs(100 * dark // (size * size) - 50) // 5
        return p + ratio * 10

    masks = [lambda r, c: (r + c) % 2 == 0,
             lambda r, c: r % 2 == 0,
             lambda r, c: c % 3 == 0,
             lambda r, c: (r + c) % 3 == 0,
             lambda r, c: (r // 2 + c // 3) % 2 == 0,
             lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
             lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
             lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0]

    def apply_mask(k):
        mat = [row[:] for row in M]
        for r in range(size):
            for c in range(size):
                if not func[r][c] and masks[k](r, c):
                    mat[r][c] ^= 1
        # format info: EC L (01) + mask, BCH(15,5), xor 0x5412
        f = (0b01 << 3) | k
        rem = f << 10
        for _ in range(15):
            if rem.bit_length() >= 11:
                rem ^= 0x537 << (rem.bit_length() - 11)
            else:
                break
        fmt = ((f << 10) | rem) ^ 0x5412
        fbits = [(fmt >> (14 - i)) & 1 for i in range(15)]
        pos_a = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7),
                 (8, 8), (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
        pos_b = [(size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8),
                 (size - 5, 8), (size - 6, 8), (size - 7, 8),
                 (8, size - 8), (8, size - 7), (8, size - 6), (8, size - 5),
                 (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1)]
        for i, bit in enumerate(fbits):
            mat[pos_a[i][0]][pos_a[i][1]] = bit
            mat[pos_b[i][0]][pos_b[i][1]] = bit
        mat[size - 8][8] = 1  # dark module stays dark
        return mat

    best, best_p = None, None
    for k in range(8):
        mat = apply_mask(k)
        p = penalty(mat)
        if best_p is None or p < best_p:
            best, best_p = mat, p
    return best


def qr_svg(text, module=8, border=4):
    mat = _qr_matrix(text)
    n = len(mat)
    dim = (n + 2 * border) * module
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
             'width="%d" height="%d"><rect width="100%%" height="100%%" '
             'fill="white"/>' % (dim, dim, dim, dim)]
    for r in range(n):
        for c in range(n):
            if mat[r][c]:
                parts.append('<rect x="%d" y="%d" width="%d" height="%d" '
                             'fill="black"/>'
                             % ((c + border) * module, (r + border) * module,
                                module, module))
    parts.append("</svg>")
    return "".join(parts)


# ----------------------------------------------------------------------------
# HTTP server (control panel UI + API + media)
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SonosMP3Player/1.0"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # -- helpers ------------------------------------------------------------
    def send_json(self, obj, code=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > 10_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # -- routing ------------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            # A custom UI? Drop a "player_ui.html" next to this script and
            # it replaces the built-in interface (same /api endpoints).
            custom = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "player_ui.html")
            if os.path.isfile(custom):
                try:
                    with open(custom, "r", encoding="utf-8") as f:
                        data = f.read().encode("utf-8")
                except Exception:
                    data = PAGE.encode("utf-8")
            else:
                data = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path.startswith("/media/"):
            self.serve_media(path, head=False)
        elif path == "/api/state":
            with state_lock:
                sp = list(speakers)
                n = len(tracks)
            folders = config.get("folders", [])
            self.send_json({"speakers": sp,
                            "folders": folders,
                            "folder": folders[0] if folders else "",
                            "track_count": n,
                            "version": __version__,
                            "last_speaker": config.get("last_speaker")})
        elif path == "/api/tracks":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            query = (qs.get("q", [""])[0] or "").lower().strip()
            with state_lock:
                items = tracks
                if query:
                    words = query.split()
                    items = [t for t in items
                             if all(w in (t["title"] + " " + t.get("artist", "")
                                          + " " + t.get("album", "") + " "
                                          + t["folder"]).lower()
                                    for w in words)]
                out = [{"id": t["id"], "title": t["title"],
                        "artist": t.get("artist", ""),
                        "album": t.get("album", ""),
                        "has_art": t.get("has_art", False),
                        "folder": t["folder"]} for t in items[:3000]]
                total = len(items)
            self.send_json({"tracks": out, "total": total})
        elif path.startswith("/api/status"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            uuid = qs.get("speaker", [""])[0]
            spk = speaker_by_uuid(uuid)
            if not spk:
                self.send_json({"error": "unknown speaker"}, 404)
                return
            self.send_json(get_status(spk))
        elif path.startswith("/api/queue"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            spk = speaker_by_uuid(qs.get("speaker", [""])[0])
            if not spk:
                self.send_json({"error": "unknown speaker"}, 404)
                return
            try:
                items, total = browse_queue(spk)
                self.send_json({"items": items, "total": total})
            except Exception as e:
                self.send_json({"error": str(e)}, 502)
        elif path.startswith("/api/art"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            with state_lock:
                track = tracks_by_id.get(qs.get("id", [""])[0])
            art = get_art(track) if track else None
            if not art:
                self.send_json({"error": "no artwork"}, 404)
                return
            mime, blob = art
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(blob)
        elif path.startswith("/api/analysis"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self.send_json(get_analysis(qs.get("id", [""])[0]))
        elif path.startswith("/api/rooms"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            spk = speaker_by_uuid(qs.get("speaker", [""])[0])
            if not spk:
                self.send_json({"error": "unknown speaker"}, 404)
                return
            self.send_json({"rooms": get_rooms(spk)})
        elif path.startswith("/api/eq"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            spk = speaker_by_uuid(qs.get("speaker", [""])[0])
            if not spk:
                self.send_json({"error": "unknown speaker"}, 404)
                return
            try:
                self.send_json(get_eq(spk))
            except Exception as e:
                self.send_json({"error": str(e)}, 502)
        elif path == "/api/access":
            ip = None
            with state_lock:
                sp = speakers[0]["ip"] if speakers else None
            try:
                ip = local_ip_facing(sp or "8.8.8.8")
            except Exception:
                ip = "127.0.0.1"
            self.send_json({"url": "http://%s:%d" % (ip, server_port)})
        elif path.startswith("/api/qr.svg"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            text = qs.get("text", [""])[0][:120]
            if not text:
                self.send_json({"error": "missing text"}, 400)
                return
            try:
                svg = qr_svg(text).encode("utf-8")
            except Exception as e:
                self.send_json({"error": str(e)}, 400)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg)))
            self.end_headers()
            self.wfile.write(svg)
        elif path.startswith("/api/browse"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            p = qs.get("path", [""])[0] or os.path.expanduser("~")
            p = os.path.realpath(os.path.expanduser(p))
            if not os.path.isdir(p):
                self.send_json({"error": "Not a folder: %s" % p}, 400)
                return
            dirs = []
            try:
                for name in sorted(os.listdir(p), key=str.lower)[:800]:
                    if name.startswith("."):
                        continue
                    full = os.path.join(p, name)
                    if os.path.isdir(full):
                        dirs.append({"name": name, "path": full})
            except PermissionError:
                self.send_json({"error": "No permission to open that "
                                "folder."}, 403)
                return
            shortcuts = []
            home = os.path.expanduser("~")
            for lbl, sp_ in (("Home", home),
                             ("Music", os.path.join(home, "Music")),
                             ("Downloads", os.path.join(home, "Downloads")),
                             ("External drives", "/Volumes")):
                if os.path.isdir(sp_):
                    shortcuts.append({"name": lbl, "path": sp_})
            parent = os.path.dirname(p) if p != "/" else None
            self.send_json({"path": p, "parent": parent, "dirs": dirs,
                            "shortcuts": shortcuts})
        elif path == "/api/yt_available":
            self.send_json(yt_available())
        elif path.startswith("/api/yt_status"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            job = yt_jobs.get(qs.get("id", [""])[0])
            if not job:
                self.send_json({"error": "unknown job"}, 404)
                return
            self.send_json(job)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_HEAD(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/media/"):
            self.serve_media(path, head=True)
        else:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self.read_json()
        try:
            if path == "/api/rescan_speakers":
                found, err = refresh_speakers()
                self.send_json({"speakers": found, "error": err})
            elif path == "/api/add_ip":
                ip = (body.get("ip") or "").strip()
                if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
                    self.send_json({"error": "That doesn't look like an IP "
                                    "address (e.g. 192.168.1.50)."}, 400)
                    return
                if ip not in config["manual_ips"]:
                    config["manual_ips"].append(ip)
                    save_config()
                found, err = refresh_speakers()
                self.send_json({"speakers": found, "error": err})
            elif path == "/api/folder":
                folder = body.get("folder") or ""
                n, err = scan_folder(folder)   # legacy: replaces the list
                if err:
                    self.send_json({"error": err}, 400)
                    return
                save_config()
                self.send_json({"track_count": n,
                                "folders": config["folders"]})
            elif path == "/api/rescan_library":
                n, err = scan_all()
                self.send_json({"track_count": n, "error": err,
                                "folders": config["folders"]})
            elif path == "/api/folders_add":
                folder = os.path.expanduser((body.get("folder") or "").strip())
                if not os.path.isdir(folder):
                    self.send_json({"error": "That folder does not exist: %s"
                                    % folder}, 400)
                    return
                if folder not in config["folders"]:
                    config["folders"].append(folder)
                    save_config()
                n, err = scan_all()
                self.send_json({"track_count": n, "error": err,
                                "folders": config["folders"]})
            elif path == "/api/folders_remove":
                folder = (body.get("folder") or "").strip()
                config["folders"] = [f for f in config["folders"]
                                     if f != folder]
                save_config()
                n, err = scan_all()
                self.send_json({"track_count": n, "error": err,
                                "folders": config["folders"]})
            elif path == "/api/play":
                spk = speaker_by_uuid(body.get("speaker") or "")
                if not spk:
                    self.send_json({"error": "Pick a speaker first."}, 400)
                    return
                ids = body.get("ids") or []
                n = play_tracks(spk, ids)
                config["last_speaker"] = spk["uuid"]
                save_config()
                prefetch_analysis(ids)
                self.send_json({"queued": n})
            elif path == "/api/enqueue":
                spk = speaker_by_uuid(body.get("speaker") or "")
                if not spk:
                    self.send_json({"error": "Pick a speaker first."}, 400)
                    return
                ids = body.get("ids") or []
                n = enqueue_tracks(spk, ids)
                prefetch_analysis(ids)
                self.send_json({"queued": n})
            elif path == "/api/eq":
                spk = speaker_by_uuid(body.get("speaker") or "")
                if not spk:
                    self.send_json({"error": "Pick a speaker first."}, 400)
                    return
                set_eq(spk, body.get("bass"), body.get("treble"),
                       body.get("loudness"))
                self.send_json({"ok": True})
            elif path == "/api/group":
                found, err = group_join(body.get("member") or "",
                                        body.get("coordinator") or "")
                self.send_json({"speakers": found, "error": err})
            elif path == "/api/ungroup":
                found, err = group_leave(body.get("member") or "")
                self.send_json({"speakers": found, "error": err})
            elif path == "/api/room_volume":
                set_room_volume(body.get("member") or body.get("speaker")
                                or "",
                                body.get("value", body.get("volume", 25)))
                self.send_json({"ok": True})
            elif path == "/api/queue_jump":
                spk = speaker_by_uuid(body.get("speaker") or "")
                if not spk:
                    self.send_json({"error": "Pick a speaker first."}, 400)
                    return
                queue_jump(spk, body.get("no") or 1)
                self.send_json({"ok": True})
            elif path == "/api/queue_remove":
                spk = speaker_by_uuid(body.get("speaker") or "")
                if not spk:
                    self.send_json({"error": "Pick a speaker first."}, 400)
                    return
                queue_remove(spk, body.get("no") or 0)
                self.send_json({"ok": True})
            elif path == "/api/youtube":
                url = (body.get("url") or "").strip()
                if not YT_URL_RE.match(url) or len(url) > 500:
                    self.send_json({"error": "That doesn't look like a "
                                    "link."}, 400)
                    return
                job_id = start_youtube_job(url)
                self.send_json({"job": job_id})
            elif path == "/api/cmd":
                spk = speaker_by_uuid(body.get("speaker") or "")
                if not spk:
                    self.send_json({"error": "Pick a speaker first."}, 400)
                    return
                action = body.get("action")
                if action == "pause":
                    avt(spk["ip"], "Pause")
                elif action == "resume":
                    avt(spk["ip"], "Play", {"Speed": 1})
                elif action == "next":
                    avt(spk["ip"], "Next")
                elif action == "prev":
                    avt(spk["ip"], "Previous")
                elif action == "volume":
                    set_volume(spk, body.get("value", 25))
                elif action == "seek":
                    seek_to(spk, body.get("value", 0))
                elif action == "clearqueue":
                    avt(spk["ip"], "RemoveAllTracksFromQueue")
                elif action == "sleep":
                    set_sleep(spk, body.get("value", 0))
                elif action == "shuffle":
                    set_shuffle(spk, bool(body.get("value")))
                else:
                    self.send_json({"error": "unknown action"}, 400)
                    return
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "not found"}, 404)
        except RuntimeError as e:
            self.send_json({"error": str(e)}, 502)
        except Exception as e:
            self.send_json({"error": "Unexpected problem: %s" % e}, 500)

    # -- media serving with Range support ------------------------------------
    def serve_media(self, path, head):
        parts = path.split("/")
        tid = parts[2] if len(parts) > 2 else ""
        with state_lock:
            track = tracks_by_id.get(tid)
        if not track or not os.path.isfile(track["path"]):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        size = os.path.getsize(track["path"])
        ctype = AUDIO_EXTS.get(track["ext"], "application/octet-stream")
        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:  # suffix range: last N bytes
                    n = int(m.group(2))
                    start = max(0, size - n)
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if head:
            return
        try:
            with open(track["path"], "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # speaker closed the stream (seek/skip) — normal


# ----------------------------------------------------------------------------
# The control panel page
# ----------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DRAAI</title>
<style>
  :root {
    --bg: #101418; --panel: #1a2027; --panel2: #212a33; --line: #2c3641;
    --text: #e8edf2; --dim: #93a1af; --accent: #4cc2ff; --accent2: #2a9df4;
    --good: #43d17c; --bad: #ff6b6b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    padding-bottom: 150px;
  }
  header {
    padding: 18px 22px 10px; display: flex; align-items: baseline; gap: 12px;
  }
  header h1 { font-size: 20px; margin: 0; font-weight: 700; }
  header .sub { color: var(--dim); font-size: 13px; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 0 18px; }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 12px; padding: 14px 16px; margin-bottom: 14px;
  }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  label.small { color: var(--dim); font-size: 12px; display: block; margin-bottom: 4px; }
  select, input[type=text] {
    background: var(--panel2); color: var(--text); border: 1px solid var(--line);
    border-radius: 8px; padding: 9px 11px; font-size: 14px; min-width: 0;
  }
  select { min-width: 220px; }
  input[type=text] { flex: 1; }
  button {
    background: var(--panel2); color: var(--text); border: 1px solid var(--line);
    border-radius: 8px; padding: 9px 14px; font-size: 14px; cursor: pointer;
  }
  button:hover { border-color: var(--accent2); }
  button.primary { background: var(--accent2); border-color: var(--accent2); color: #fff; font-weight: 600; }
  button.primary:hover { background: var(--accent); border-color: var(--accent); }
  #msg { display: none; margin: 0 0 14px; padding: 10px 14px; border-radius: 10px;
         background: #3a2328; border: 1px solid #64333c; color: #ffb4b4; font-size: 14px; }
  #msg.info { background: #1d3242; border-color: #2c5570; color: #bfe3ff; }
  .tracks { border-collapse: collapse; width: 100%; }
  .tracks td { padding: 8px 10px; border-top: 1px solid var(--line); cursor: pointer; }
  .tracks tr:hover td { background: var(--panel2); }
  .tracks tr.playing td { color: var(--accent); }
  td.t-folder { color: var(--dim); font-size: 12px; width: 34%; }
  td.t-one { width: 42px; text-align: right; }
  .oneBtn { border: none; background: none; color: var(--dim); font-size: 15px; padding: 4px 6px; }
  .oneBtn:hover { color: var(--accent); }
  .hint { color: var(--dim); font-size: 12.5px; margin: 8px 2px 0; }
  #count { color: var(--dim); font-size: 13px; margin-left: auto; }
  /* Now playing bar */
  #bar {
    position: fixed; left: 0; right: 0; bottom: 0;
    background: rgba(20,26,32,.97); border-top: 1px solid var(--line);
    padding: 12px 18px; backdrop-filter: blur(8px);
  }
  #bar .inner { max-width: 860px; margin: 0 auto; display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  #np { flex: 1; min-width: 180px; }
  #npTitle { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #npState { color: var(--dim); font-size: 12.5px; }
  .ctl { display: flex; gap: 8px; align-items: center; }
  .ctl button { font-size: 17px; padding: 8px 13px; border-radius: 10px; }
  #shufBtn.on { color: var(--accent); border-color: var(--accent); }
  #vol { width: 130px; accent-color: var(--accent2); }
  a.linkish { color: var(--accent); cursor: pointer; font-size: 13px; text-decoration: underline; }
  /* Seek bar */
  #seekRow { display: flex; align-items: center; gap: 10px; width: 100%; margin-bottom: 8px; }
  #seek { flex: 1; accent-color: var(--accent2); height: 4px; }
  .ttime { color: var(--dim); font-size: 12px; min-width: 38px; text-align: center;
           font-variant-numeric: tabular-nums; }
  #row2 { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; width: 100%; }
  /* Queue panel */
  .qhead { display: flex; align-items: center; gap: 10px; }
  .qhead h3 { margin: 0; font-size: 15px; }
  #qcount { color: var(--dim); font-size: 13px; }
  .qlist { border-collapse: collapse; width: 100%; margin-top: 8px; }
  .qlist td { padding: 6px 8px; border-top: 1px solid var(--line); cursor: pointer; }
  .qlist tr:hover td { background: var(--panel2); }
  .qlist tr.cur td { color: var(--accent); font-weight: 600; }
  td.q-no { width: 34px; color: var(--dim); font-size: 12px; }
  td.q-x { width: 34px; text-align: right; }
  .xBtn { border: none; background: none; color: var(--dim); font-size: 14px; padding: 2px 6px; }
  .xBtn:hover { color: var(--bad); }
  #qEmpty { color: var(--dim); font-size: 13px; margin-top: 8px; }
  .addBtn { border: none; background: none; color: var(--dim); font-size: 16px; padding: 4px 6px; }
  .addBtn:hover { color: var(--good); }
  td.t-one { width: 76px; text-align: right; white-space: nowrap; }
  #ytHint { margin-top: 8px; }
  #ytHint.working { color: var(--accent); }
  #ytHint.done { color: var(--good); }
  #ytHint.err { color: var(--bad); }
  code.cmd { background: var(--panel2); padding: 2px 7px; border-radius: 6px;
             font-size: 12.5px; color: var(--text); }
</style>
</head>
<body>
<header class="wrap"><h1>DRAAI</h1>
  <span class="sub">local music &rarr; your speakers, no cloud</span></header>
<div class="wrap">
  <div id="msg"></div>

  <div class="card">
    <div class="row">
      <div>
        <label class="small">Speaker</label>
        <select id="speaker"><option value="">Looking for speakers…</option></select>
      </div>
      <button id="rescan" style="align-self:flex-end">Rescan</button>
      <span style="align-self:flex-end"><a class="linkish" id="addIp">add by IP address</a></span>
    </div>
  </div>

  <div class="card">
    <label class="small">Music folder</label>
    <div class="row">
      <input type="text" id="folder" placeholder="~/Music">
      <button id="scan" class="primary">Scan folder</button>
    </div>
  </div>

  <div class="card" id="ytCard" style="display:none">
    <label class="small">Import via yt-dlp</label>
    <div class="row" id="ytRow">
      <input type="text" id="ytUrl" placeholder="Paste a link — yt-dlp saves the audio into your music folder">
      <button id="ytBtn" class="primary">Import</button>
    </div>
    <div class="hint" id="ytHint"></div>
  </div>

  <div class="card" id="qCard">
    <div class="qhead"><h3>Up next</h3><span id="qcount"></span>
      <button id="qClear" style="margin-left:auto;font-size:12.5px;padding:6px 10px">Clear queue</button></div>
    <table class="qlist"><tbody id="qlist"></tbody></table>
    <div id="qEmpty">The queue is empty — click a song below to start one.</div>
  </div>

  <div class="card">
    <div class="row" style="margin-bottom:8px">
      <input type="text" id="search" placeholder="Search songs…">
      <span id="count"></span>
    </div>
    <table class="tracks"><tbody id="list"></tbody></table>
    <div class="hint">Click a song to play it and everything after it (in the
      order shown). &#65291; adds it to the queue instead; &#9654; plays just
      that one song.</div>
  </div>
</div>

<div id="bar"><div class="inner">
  <div id="seekRow">
    <span class="ttime" id="tPos">0:00</span>
    <input type="range" id="seek" min="0" max="1000" value="0">
    <span class="ttime" id="tDur">0:00</span>
  </div>
  <div id="row2">
    <div id="np"><div id="npTitle">Nothing playing</div><div id="npState">&nbsp;</div></div>
    <div class="ctl">
      <button id="prevBtn" title="Previous">&#9198;</button>
      <button id="playBtn" title="Play / Pause">&#9654;</button>
      <button id="nextBtn" title="Next">&#9197;</button>
      <button id="shufBtn" title="Shuffle">&#128256;</button>
    </div>
    <div class="ctl"><span style="color:var(--dim);font-size:13px">Vol</span>
      <input type="range" id="vol" min="0" max="100" value="25"></div>
  </div>
</div></div>

<script>
"use strict";
let view = [];            // tracks currently shown, in order
let currentSpeaker = "";
let playState = "STOPPED";
let shuffleOn = false;
let statusTimer = null;
let durSec = 0;           // duration of current track in seconds
let seeking = false;      // user is dragging the seek bar
let curTrackNo = null;    // 1-based position in the speaker queue
let tickCount = 0;
let ytTimer = null;

const $ = id => document.getElementById(id);

function showMsg(text, info) {
  const el = $("msg");
  if (!text) { el.style.display = "none"; return; }
  el.textContent = text;
  el.className = info ? "info" : "";
  el.style.display = "block";
}

async function api(path, body) {
  const opts = body === undefined ? {} :
    { method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(data.error || ("Request failed: " + r.status));
  return data;
}

function fillSpeakers(list, keep) {
  const sel = $("speaker");
  sel.innerHTML = "";
  if (!list.length) {
    sel.innerHTML = '<option value="">No speakers found</option>';
    return;
  }
  for (const s of list) {
    const o = document.createElement("option");
    o.value = s.uuid; o.textContent = s.name;
    sel.appendChild(o);
  }
  if (keep && list.some(s => s.uuid === keep)) sel.value = keep;
  currentSpeaker = sel.value;
  restartStatus();
}

async function loadState() {
  const st = await api("/api/state");
  $("folder").value = st.folder || "";
  fillSpeakers(st.speakers || [], st.last_speaker);
  if (!(st.speakers || []).length) rescanSpeakers();
  if (st.track_count > 0) loadTracks();
  else if (st.folder) scanFolder();   // first run: try the default folder
}

async function rescanSpeakers(ip) {
  showMsg("Looking for speakers on your network…", true);
  try {
    const body = ip ? await api("/api/add_ip", {ip}) :
                      await api("/api/rescan_speakers", {});
    fillSpeakers(body.speakers || [], currentSpeaker);
    showMsg(body.error || "");
    if (!body.error) showMsg("");
  } catch (e) { showMsg(e.message); }
}

async function scanFolder() {
  const folder = $("folder").value.trim();
  if (!folder) return;
  showMsg("Scanning " + folder + "…", true);
  try {
    const r = await api("/api/folder", {folder});
    showMsg("");
    await loadTracks();
    if (r.track_count === 0)
      showMsg("No audio files found in that folder (looked for mp3, m4a, flac, wav, aiff, ogg).");
  } catch (e) { showMsg(e.message); }
}

async function loadTracks() {
  const q = $("search").value.trim();
  const r = await api("/api/tracks?q=" + encodeURIComponent(q));
  view = r.tracks;
  $("count").textContent = r.total + " song" + (r.total === 1 ? "" : "s");
  const tbody = $("list");
  tbody.innerHTML = "";
  const frag = document.createDocumentFragment();
  view.forEach((t, i) => {
    const tr = document.createElement("tr");
    tr.dataset.idx = i;
    const td1 = document.createElement("td");
    td1.textContent = t.title;
    const td2 = document.createElement("td");
    td2.className = "t-folder"; td2.textContent = t.folder;
    const td3 = document.createElement("td");
    td3.className = "t-one";
    td3.innerHTML =
      '<button class="addBtn" title="Add to queue">&#65291;</button>' +
      '<button class="oneBtn" title="Play only this song">&#9654;</button>';
    tr.append(td1, td2, td3);
    frag.appendChild(tr);
  });
  tbody.appendChild(frag);
}

async function playFrom(i, onlyOne) {
  if (!currentSpeaker) { showMsg("Pick a speaker first (or press Rescan)."); return; }
  const ids = onlyOne ? [view[i].id] : view.slice(i).map(t => t.id);
  showMsg("Starting playback…", true);
  try {
    await api("/api/play", {speaker: currentSpeaker, ids});
    showMsg("");
    markPlaying(view[i].title);
  } catch (e) { showMsg(e.message); }
}

function markPlaying(title) {
  $("npTitle").textContent = title || "Nothing playing";
}

async function cmd(action, value) {
  if (!currentSpeaker) { showMsg("Pick a speaker first."); return; }
  try { await api("/api/cmd", {speaker: currentSpeaker, action, value}); showMsg(""); }
  catch (e) { showMsg(e.message); }
}

async function addToQueue(i) {
  if (!currentSpeaker) { showMsg("Pick a speaker first (or press Rescan)."); return; }
  try {
    await api("/api/enqueue", {speaker: currentSpeaker, ids: [view[i].id]});
    showMsg("Added “" + view[i].title + "” to the queue.", true);
    setTimeout(() => showMsg(""), 2500);
    loadQueue();
  } catch (e) { showMsg(e.message); }
}

async function loadQueue() {
  if (!currentSpeaker) return;
  let r;
  try {
    r = await api("/api/queue?speaker=" + encodeURIComponent(currentSpeaker));
  } catch (e) { return; }  // speaker busy — try again next cycle
  const items = r.items || [];
  $("qcount").textContent = items.length ?
    items.length + " song" + (items.length === 1 ? "" : "s") : "";
  $("qEmpty").style.display = items.length ? "none" : "block";
  $("qClear").style.display = items.length ? "" : "none";
  const tbody = $("qlist");
  tbody.innerHTML = "";
  const frag = document.createDocumentFragment();
  for (const it of items) {
    const tr = document.createElement("tr");
    tr.dataset.no = it.no;
    if (it.no === curTrackNo) tr.className = "cur";
    const td1 = document.createElement("td");
    td1.className = "q-no"; td1.textContent = it.no;
    const td2 = document.createElement("td");
    td2.textContent = it.title;
    const td3 = document.createElement("td");
    td3.className = "q-x";
    td3.innerHTML = '<button class="xBtn" title="Remove from queue">&#10005;</button>';
    tr.append(td1, td2, td3);
    frag.appendChild(tr);
  }
  tbody.appendChild(frag);
}

/* -- YouTube -- */
async function initYouTube() {
  try {
    const r = await api("/api/yt_available");
    $("ytCard").style.display = "block";
    if (!r.available) {
      $("ytRow").style.display = "none";
      $("ytHint").innerHTML = "This app can hand links to " +
        "<a href='https://github.com/yt-dlp/yt-dlp' target='_blank' " +
        "style='color:inherit'>yt-dlp</a> (a separate open-source tool) to " +
        "save audio into your library. To enable it, open Terminal and run " +
        '<code class="cmd">brew install ' + r.missing.join(" ") + "</code>" +
        ", then restart this app. Only import content you have the rights to."
    }
  } catch (e) {}
}

async function startYouTube() {
  const url = $("ytUrl").value.trim();
  if (!url) return;
  $("ytBtn").disabled = true;
  setYtHint("Starting…", "working");
  try {
    const r = await api("/api/youtube", {url});
    if (ytTimer) clearInterval(ytTimer);
    ytTimer = setInterval(async () => {
      try {
        const s = await api("/api/yt_status?id=" + r.job);
        if (s.status === "working") setYtHint(s.detail, "working");
        else {
          clearInterval(ytTimer); ytTimer = null;
          $("ytBtn").disabled = false;
          if (s.status === "done") {
            setYtHint("✓ " + s.detail, "done");
            $("ytUrl").value = "";
            loadTracks();
          } else setYtHint(s.error || "Download failed.", "err");
        }
      } catch (e) {}
    }, 2000);
  } catch (e) {
    setYtHint(e.message, "err");
    $("ytBtn").disabled = false;
  }
}

function setYtHint(text, cls) {
  const el = $("ytHint");
  el.textContent = text;
  el.className = "hint " + (cls || "");
}

function restartStatus() {
  if (statusTimer) clearInterval(statusTimer);
  if (!currentSpeaker) return;
  const tick = async () => {
    try {
      const s = await api("/api/status?speaker=" + encodeURIComponent(currentSpeaker));
      playState = s.state || "UNKNOWN";
      if (s.title) $("npTitle").textContent = s.title;
      const bits = [];
      if (playState === "PLAYING") bits.push("Playing");
      else if (playState === "PAUSED_PLAYBACK") bits.push("Paused");
      else if (playState === "TRANSITIONING") bits.push("Loading…");
      else bits.push("Stopped");
      $("npState").textContent = bits.join("  ·  ");
      $("playBtn").innerHTML = playState === "PLAYING" ? "&#9646;&#9646;" : "&#9654;";
      if (typeof s.volume === "number" && document.activeElement !== $("vol"))
        $("vol").value = s.volume;
      /* seek bar */
      durSec = hmsToSec(s.duration);
      const posSec = hmsToSec(s.position);
      if (!seeking) {
        $("tPos").textContent = fmtSec(posSec);
        $("tDur").textContent = fmtSec(durSec);
        $("seek").value = durSec > 0 ? Math.round(1000 * posSec / durSec) : 0;
        $("seek").disabled = durSec === 0;
      }
      /* queue highlight + periodic refresh */
      const newNo = s.track_no || null;
      if (newNo !== curTrackNo) {
        curTrackNo = newNo;
        loadQueue();
      } else if (tickCount % 5 === 0) loadQueue();
      tickCount++;
    } catch (e) { /* speaker briefly unreachable — keep trying */ }
  };
  tick();
  loadQueue();
  statusTimer = setInterval(tick, 2000);
}

function hmsToSec(t) {
  if (!t) return 0;
  const p = t.split(":").map(Number);
  if (p.length !== 3 || p.some(isNaN)) return 0;
  return p[0] * 3600 + p[1] * 60 + p[2];
}

function fmtSec(s) {
  const m = Math.floor(s / 60), r = (s % 60).toString().padStart(2, "0");
  return m + ":" + r;
}

function fmtTime(t) {  // "0:03:25" -> "3:25"
  const p = t.split(":").map(Number);
  if (p.length !== 3 || p.some(isNaN)) return t;
  const s = p[0] * 3600 + p[1] * 60 + p[2];
  const m = Math.floor(s / 60), r = (s % 60).toString().padStart(2, "0");
  return m + ":" + r;
}

/* wiring */
$("list").addEventListener("click", e => {
  const tr = e.target.closest("tr");
  if (!tr) return;
  const i = parseInt(tr.dataset.idx, 10);
  if (e.target.closest(".addBtn")) { addToQueue(i); return; }
  playFrom(i, !!e.target.closest(".oneBtn"));
});
$("qlist").addEventListener("click", async e => {
  const tr = e.target.closest("tr");
  if (!tr || !currentSpeaker) return;
  const no = parseInt(tr.dataset.no, 10);
  try {
    if (e.target.closest(".xBtn")) {
      await api("/api/queue_remove", {speaker: currentSpeaker, no});
    } else {
      await api("/api/queue_jump", {speaker: currentSpeaker, no});
    }
    showMsg("");
    loadQueue();
  } catch (err) { showMsg(err.message); }
});
$("qClear").addEventListener("click", async () => {
  await cmd("clearqueue");
  loadQueue();
});
$("seek").addEventListener("input", () => {
  seeking = true;
  if (durSec > 0)
    $("tPos").textContent = fmtSec(Math.round($("seek").value / 1000 * durSec));
});
$("seek").addEventListener("change", () => {
  if (durSec > 0)
    cmd("seek", Math.round($("seek").value / 1000 * durSec));
  setTimeout(() => { seeking = false; }, 1200);
});
$("ytBtn").addEventListener("click", startYouTube);
$("ytUrl").addEventListener("keydown", e => { if (e.key === "Enter") startYouTube(); });
$("speaker").addEventListener("change", e => {
  currentSpeaker = e.target.value; restartStatus();
});
$("rescan").addEventListener("click", () => rescanSpeakers());
$("addIp").addEventListener("click", () => {
  const ip = window.prompt("Speaker IP address (find it in the Sonos app under " +
    "Settings → System → About My System):");
  if (ip) rescanSpeakers(ip.trim());
});
$("scan").addEventListener("click", scanFolder);
$("folder").addEventListener("keydown", e => { if (e.key === "Enter") scanFolder(); });
let searchTimer = null;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadTracks, 200);
});
$("playBtn").addEventListener("click", () =>
  cmd(playState === "PLAYING" ? "pause" : "resume"));
$("nextBtn").addEventListener("click", () => cmd("next"));
$("prevBtn").addEventListener("click", () => cmd("prev"));
$("shufBtn").addEventListener("click", () => {
  shuffleOn = !shuffleOn;
  $("shufBtn").classList.toggle("on", shuffleOn);
  cmd("shuffle", shuffleOn);
});
$("vol").addEventListener("change", e => cmd("volume", parseInt(e.target.value, 10)));

loadState();
initYouTube();
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------------

def install_autostart():
    plist_dir = os.path.join(os.path.expanduser("~"), "Library",
                             "LaunchAgents")
    plist_path = os.path.join(plist_dir, "com.draai.player.plist")
    log_path = os.path.join(os.path.expanduser("~"), "Library", "Logs",
                            "draai.log")
    script = os.path.abspath(__file__)
    python = sys.executable or "/usr/bin/python3"
    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.draai.player</string>
  <key>ProgramArguments</key>
  <array><string>%s</string><string>%s</string><string>--headless</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>%s</string>
  <key>StandardErrorPath</key><string>%s</string>
</dict></plist>
""" % (python, script, log_path, log_path)
    os.makedirs(plist_dir, exist_ok=True)
    with open(plist_path, "w") as f:
        f.write(plist)
    try:
        subprocess.run(["launchctl", "unload", plist_path],
                       capture_output=True)
        subprocess.run(["launchctl", "load", "-w", plist_path],
                       capture_output=True)
    except Exception:
        pass
    print("Installed. DRAAI now starts automatically when you log in,")
    print("running quietly in the background — no Terminal window needed.")
    print("Control panel:  http://localhost:%d" % PREFERRED_PORT)
    print("To undo:        python3 %s --uninstall-autostart" % script)


def uninstall_autostart():
    plist_path = os.path.join(os.path.expanduser("~"), "Library",
                              "LaunchAgents", "com.draai.player.plist")
    try:
        subprocess.run(["launchctl", "unload", plist_path],
                       capture_output=True)
    except Exception:
        pass
    if os.path.exists(plist_path):
        os.remove(plist_path)
        print("Removed. DRAAI no longer starts at login.")
    else:
        print("Nothing to remove — autostart was not installed.")


def main():
    global server_port
    if "--version" in sys.argv:
        print("%s %s" % (APP_NAME, __version__))
        return
    if "--install-autostart" in sys.argv:
        install_autostart()
        return
    if "--uninstall-autostart" in sys.argv:
        uninstall_autostart()
        return
    load_config()
    load_positions()

    httpd = None
    for port in range(PREFERRED_PORT, PREFERRED_PORT + 20):
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
            server_port = port
            break
        except OSError:
            continue
    if httpd is None:
        print("Could not find a free port to run on. Is the app already running?")
        sys.exit(1)

    url = "http://localhost:%d" % server_port
    print()
    print("  %s %s" % (APP_NAME, __version__))
    print("  " + "-" * len(APP_NAME))
    print("  Control panel:  %s" % url)
    print()
    print("  Keep this window open while music is playing — the speakers")
    print("  stream the files from your Mac through this app.")
    print()
    print("  If macOS asks whether Python may accept incoming network")
    print("  connections, click Allow (the speakers need it to fetch music).")
    print()
    print("  Press Ctrl+C to quit.")
    print()

    # Warm up in the background so the page has data quickly.
    def warmup():
        scan_all()
        # discovery can be slow right after boot — retry a few times
        for _ in range(4):
            found, _err = refresh_speakers()
            if found:
                break
            time.sleep(4)
    threading.Thread(target=warmup, daemon=True).start()

    if "--headless" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")


if __name__ == "__main__":
    main()
