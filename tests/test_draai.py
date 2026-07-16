#!/usr/bin/env python3
"""DRAAI test suite.

Run from the repository root:   python3 tests/test_draai.py

Self-contained: no speakers, no network, no ffmpeg needed. Tagged audio
files are hand-crafted in memory and the Sonos SOAP layer is mocked.
"""

import importlib.util
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)                    # so `import draai` (the package) resolves

import types
import draai
import draai.state, draai.constants, draai.util, draai.config, draai.media
import draai.library, draai.analysis, draai.youtube, draai.cast
import draai.backends, draai.playlists, draai.server

# `sp` = a flat namespace of the package's public API for the tests' sp.<name>
# reads/calls. Function MOCKS are patched on the real module they live in
# (e.g. draai.backends.soap_call), since that's where the callers resolve them.
sp = types.SimpleNamespace(__version__=draai.__version__)
for _m in (draai.state, draai.constants, draai.util, draai.config, draai.media,
           draai.library, draai.analysis, draai.youtube, draai.cast,
           draai.backends, draai.playlists, draai.server):
    for _n in dir(_m):
        if not _n.startswith("__"):
            setattr(sp, _n, getattr(_m, _n))
import http.server
sp.ThreadingHTTPServer = http.server.ThreadingHTTPServer   # for the live-server API test


# ----------------------------------------------------------------------------
# hand-crafted media files (no ffmpeg required)
# ----------------------------------------------------------------------------

def make_mp3(path, title="Song", artist="Artist", album="Album",
             art=b"\x89PNG\r\n\x1a\nFAKEPNGDATA"):
    """Minimal ID3v2.3 tag + dummy audio bytes."""
    def frame(fid, body):
        return fid + struct.pack(">I", len(body)) + b"\x00\x00" + body

    def text(fid, value):
        return frame(fid, b"\x03" + value.encode("utf-8"))

    frames = text(b"TIT2", title) + text(b"TPE1", artist) + text(b"TALB", album)
    if art:
        frames += frame(b"APIC", b"\x03image/png\x00\x03\x00" + art)
    size = len(frames)
    ss = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F,
                (size >> 7) & 0x7F, size & 0x7F])
    with open(path, "wb") as f:
        f.write(b"ID3\x03\x00\x00" + ss + frames)
        f.write(b"\xff\xfb" + b"\x00" * 4096)   # fake mpeg frames


def make_flac(path, title="FSong", artist="FArtist", album="FAlbum"):
    """Minimal FLAC: vorbis comment block only (marked as last)."""
    comments = ["TITLE=" + title, "ARTIST=" + artist, "ALBUM=" + album]
    body = struct.pack("<I", 4) + b"test"
    body += struct.pack("<I", len(comments))
    for c in comments:
        e = c.encode("utf-8")
        body += struct.pack("<I", len(e)) + e
    with open(path, "wb") as f:
        f.write(b"fLaC")
        f.write(bytes([0x80 | 4]) + len(body).to_bytes(3, "big") + body)


ZONE_XML = """<ZoneGroupState><ZoneGroups>
<ZoneGroup Coordinator="RINCON_A" ID="RINCON_A:1">
 <ZoneGroupMember UUID="RINCON_A" Location="http://192.168.1.50:1400/x.xml" ZoneName="Living Room">
  <Satellite UUID="RINCON_S" Location="http://192.168.1.52:1400/x.xml" ZoneName="Living Room"/>
 </ZoneGroupMember>
 <ZoneGroupMember UUID="RINCON_B" Location="http://192.168.1.51:1400/x.xml" ZoneName="Living Room" Invisible="1"/>
</ZoneGroup>
<ZoneGroup Coordinator="RINCON_C" ID="RINCON_C:2">
 <ZoneGroupMember UUID="RINCON_C" Location="http://192.168.1.53:1400/x.xml" ZoneName="Kitchen"/>
</ZoneGroup>
</ZoneGroups></ZoneGroupState>"""


class SoapMock:
    """Stands in for a Sonos household."""

    def __init__(self):
        self.queue = []
        self.calls = []
        self.volumes = {}

    def __call__(self, ip, path, service, action, args):
        self.calls.append((ip, action, dict(args)))
        if action == "GetZoneGroupState":
            esc = ZONE_XML.replace("<", "&lt;").replace(">", "&gt;")
            return "<ZoneGroupState>%s</ZoneGroupState>" % esc
        if action == "AddURIToQueue":
            uri = args["EnqueuedURI"]
            pos = int(args.get("DesiredFirstTrackNumberEnqueued", 0))
            if int(args.get("EnqueueAsNext", 0)) == 1 and pos > 0:
                self.queue.insert(pos - 1, uri)
            else:
                self.queue.append(uri)
            return "<ok/>"
        if action == "ReorderTracksInQueue":
            # real-Sonos semantics: InsertBefore counted pre-removal
            s = int(args["StartingIndex"]); n = int(args["NumberOfTracks"])
            ib = int(args["InsertBefore"])
            moved = self.queue[s - 1:s - 1 + n]
            del self.queue[s - 1:s - 1 + n]
            adj = ib - n if ib > s else ib
            for i, m in enumerate(moved):
                self.queue.insert(adj - 1 + i, m)
            return "<ok/>"
        if action == "RemoveAllTracksFromQueue":
            self.queue = []
            return "<ok/>"
        if action == "Browse":
            items = "".join(
                '<item id="Q:0/%d"><dc:title>t%d</dc:title><res>%s</res></item>'
                % (i + 1, i + 1, u)
                for i, u in enumerate(self.queue))
            esc = items.replace("<", "&lt;").replace(">", "&gt;")
            return ("<Result>%s</Result><TotalMatches>%d</TotalMatches>"
                    % (esc, len(self.queue)))
        if action == "SetVolume":
            self.volumes[ip] = int(args["DesiredVolume"])
            return "<ok/>"
        if action in ("GetVolume", "GetGroupVolume"):
            return "<CurrentVolume>%d</CurrentVolume>" % self.volumes.get(ip, 25)
        if action == "GetTransportInfo":
            return "<CurrentTransportState>STOPPED</CurrentTransportState>"
        if action == "GetPositionInfo":
            return ("<Track>1</Track><TrackDuration>0:03:00</TrackDuration>"
                    "<RelTime>0:01:00</RelTime><TrackURI></TrackURI>"
                    "<TrackMetaData></TrackMetaData>")
        if action == "GetBass":
            return "<CurrentBass>0</CurrentBass>"
        if action == "GetTreble":
            return "<CurrentTreble>0</CurrentTreble>"
        if action == "GetLoudness":
            return "<CurrentLoudness>1</CurrentLoudness>"
        return "<ok/>"


class DraaiTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.soap = SoapMock()
        draai.backends.soap_call = self.soap
        draai.backends.ssdp_discover = lambda timeout=3.0: {"192.168.1.50"}
        draai.backends.cast_discover = lambda timeout=4.0: []
        sp.config["folders"] = [self.tmp]
        sp.config["manual_ips"] = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- tags -----------------------------------------------------------------

    def test_mp3_tags_and_art(self):
        p = os.path.join(self.tmp, "a.mp3")
        make_mp3(p, "Titel", "Artiest", "Plaat")
        t = sp.read_tags(p)
        self.assertEqual(t["title"], "Titel")
        self.assertEqual(t["artist"], "Artiest")
        self.assertEqual(t["album"], "Plaat")
        self.assertTrue(t["has_art"])
        art = sp.read_tags(p, want_art=True)["art"]
        self.assertEqual(art[0], "image/png")
        self.assertTrue(art[1].startswith(b"\x89PNG"))

    def test_flac_tags(self):
        p = os.path.join(self.tmp, "b.flac")
        make_flac(p, "FT", "FA", "FB")
        t = sp.read_tags(p)
        self.assertEqual((t["title"], t["artist"], t["album"]),
                         ("FT", "FA", "FB"))

    # -- library --------------------------------------------------------------

    def test_scan_multiple_folders(self):
        second = tempfile.mkdtemp()
        try:
            make_mp3(os.path.join(self.tmp, "one.mp3"), "One")
            os.makedirs(os.path.join(second, "Sub"))
            make_mp3(os.path.join(second, "Sub", "two.mp3"), "Two")
            sp.config["folders"] = [self.tmp, second]
            n, err = sp.scan_all()
            self.assertEqual(n, 2)
            self.assertIsNone(err)
            folders = sorted(t["folder"] for t in sp.tracks)
            # multi-root scans prefix tracks with their root's name
            self.assertTrue(any("/Sub" in f or f.endswith("Sub") for f in folders))
        finally:
            shutil.rmtree(second, ignore_errors=True)

    # -- topology ---------------------------------------------------------------

    def test_zone_groups_members_and_fixed(self):
        groups, err = sp.refresh_speakers()
        self.assertIsNone(err)
        self.assertEqual(len(groups), 2)
        living = [g for g in groups if g["uuid"] == "RINCON_A"][0]
        self.assertEqual(len(living["members"]), 3)   # coord + bonded + satellite
        fixed = {m["uuid"]: m["fixed"] for m in living["members"]}
        self.assertFalse(fixed["RINCON_A"])
        self.assertTrue(fixed["RINCON_B"])
        self.assertTrue(fixed["RINCON_S"])
        # duplicate names got disambiguated
        names = [m["name"] for m in living["members"]]
        self.assertEqual(len(names), len(set(names)))

    # -- playback ----------------------------------------------------------------

    def test_play_sends_didl_with_desc(self):
        make_mp3(os.path.join(self.tmp, "c.mp3"), "C")
        sp.scan_all()
        tid = sp.tracks[0]["id"]
        sp.play_tracks({"uuid": "RINCON_A", "ip": "192.168.1.50"}, [tid])
        adds = [c for c in self.soap.calls if c[1] == "AddURIToQueue"]
        self.assertEqual(len(adds), 1)
        self.assertIn("RINCON_AssociatedZPUDN", adds[0][2]["EnqueuedURIMetaData"])

    def test_room_volume(self):
        sp.refresh_speakers()
        sp.set_room_volume("RINCON_B", 60)
        self.assertEqual(self.soap.volumes["192.168.1.51"], 60)

    # -- resume memory -------------------------------------------------------------

    def test_positions(self):
        sp.CONFIG_DIR = self.tmp
        sp.POSITIONS_PATH = os.path.join(self.tmp, "positions.json")
        with sp.positions_lock:
            sp.positions.clear()
        sp.note_position("t1", 3600, 36000)   # remember
        sp.note_position("t2", 30, 36000)     # too early: forget
        sp.note_position("t3", 200, 300)      # short track: ignore
        sp.note_position("t4", 35900, 36000)  # nearly done: forget
        with sp.positions_lock:
            self.assertEqual(dict(sp.positions), {"t1": 3600})

    # -- QR ------------------------------------------------------------------------

    def test_qr_matrix_structure(self):
        m = sp._qr_matrix("http://192.168.1.2:8765")
        n = len(m)
        self.assertIn(n, (21, 25, 29, 33, 37))
        for row in m:
            self.assertEqual(len(row), n)
            for v in row:
                self.assertIn(v, (0, 1))
        # finder pattern corners
        for r, c in ((0, 0), (0, n - 7), (n - 7, 0)):
            self.assertEqual(m[r][c], 1)
            self.assertEqual(m[r + 3][c + 3], 1)   # center of the eye

    # -- HTTP API --------------------------------------------------------------------

    def test_api_roundtrip(self):
        make_mp3(os.path.join(self.tmp, "d.mp3"), "D-Track", "D-Artist")
        sp.scan_all()
        sp.refresh_speakers()
        httpd = sp.ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def get(p):
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d%s" % (port, p), timeout=5) as r:
                    return json.load(r)

            st = get("/api/state")
            self.assertEqual(st["track_count"], 1)
            self.assertEqual(len(st["speakers"]), 2)
            self.assertIn("version", st)
            tr = get("/api/tracks")
            self.assertEqual(tr["tracks"][0]["title"], "D-Track")
            self.assertTrue(tr["tracks"][0]["has_art"])
            br = get("/api/browse?path=" + urllib.parse.quote(self.tmp))
            self.assertEqual(br["path"], os.path.realpath(self.tmp))
        finally:
            httpd.shutdown()

    def test_remote_page_served(self):
        httpd = sp.ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/remote" % port, timeout=5) as r:
                body = r.read().decode("utf-8")
                self.assertEqual(r.status, 200)
                self.assertIn('data-remote="1"', body)
        finally:
            httpd.shutdown()

    def test_access_returns_remote_url(self):
        httpd = sp.ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/access" % port, timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
                self.assertIn("remote", data)
                self.assertTrue(data["remote"].endswith("/remote"))
        finally:
            httpd.shutdown()

    # -- v1.1: queue management ---------------------------------------------------------

    def _seed_queue(self, titles):
        for t in titles:
            make_mp3(os.path.join(self.tmp, t + ".mp3"), t)
        sp.scan_all()
        ids = {t["title"]: t["id"] for t in sp.tracks}
        sp.play_tracks({"uuid": "RINCON_A", "ip": "192.168.1.50"},
                       [ids[t] for t in titles])
        import time
        time.sleep(0.3)   # background enqueue of the tail
        return ids

    def _queue_ids(self):
        items, _ = sp.browse_queue({"uuid": "RINCON_A", "ip": "192.168.1.50"})
        return [it["id"] for it in items]

    def test_queue_move_semantics(self):
        ids = self._seed_queue(["A", "B", "C", "D", "E"])
        spk = {"uuid": "RINCON_A", "ip": "192.168.1.50"}
        sp.queue_move(spk, 5, 2)   # move up
        self.assertEqual(self._queue_ids(),
                         [ids[x] for x in ["A", "E", "B", "C", "D"]])
        sp.queue_move(spk, 2, 4)   # move down
        self.assertEqual(self._queue_ids(),
                         [ids[x] for x in ["A", "B", "C", "E", "D"]])

    def test_enqueue_play_next(self):
        ids = self._seed_queue(["A", "B", "C"])
        spk = {"uuid": "RINCON_A", "ip": "192.168.1.50"}
        # mock reports track 1 as current -> block lands at positions 2..3
        sp.enqueue_tracks(spk, [ids["C"], ids["B"]], play_next=True)
        self.assertEqual(self._queue_ids(),
                         [ids[x] for x in ["A", "C", "B", "B", "C"]])

    def test_playlist_roundtrip(self):
        ids = self._seed_queue(["A", "B"])
        spk = {"uuid": "RINCON_A", "ip": "192.168.1.50"}
        n = sp.save_playlist(spk, "Mix/1")
        self.assertEqual(n, 2)
        self.assertEqual(sp.list_playlists(), [{"name": "Mix-1", "count": 2}])
        got, total = sp.load_playlist("Mix-1")
        self.assertEqual((len(got), total), (2, 2))
        sp.delete_playlist("Mix-1")
        self.assertEqual(sp.list_playlists(), [])

    def test_reveal_guard(self):
        make_mp3(os.path.join(self.tmp, "x.mp3"), "X")
        sp.scan_all()
        with sp.state_lock:
            sp.tracks_by_id["f" * 16] = {"id": "f" * 16, "path": "/etc/passwd"}
        with self.assertRaises(RuntimeError):
            sp.reveal_in_finder("f" * 16)
        with self.assertRaises(RuntimeError):
            sp.reveal_in_finder("0" * 16)

    def test_added_timestamp(self):
        make_mp3(os.path.join(self.tmp, "y.mp3"), "Y")
        sp.scan_all()
        self.assertTrue(all(t.get("added", 0) > 0 for t in sp.tracks))

    # -- helpers -----------------------------------------------------------------------

    def test_time_helpers(self):
        self.assertEqual(sp.sec_to_hms(3725), "1:02:05")
        self.assertEqual(sp.hms_to_sec("1:02:05"), 3725)
        self.assertEqual(sp.hms_to_sec("bogus"), 0)



    def test_cast_frame_roundtrip(self):
        f = sp.cast_frame("urn:x-cast:com.google.cast.receiver",
                          {"type": "LAUNCH", "appId": "CC1AD845"}, "sender-0", "receiver-0")
        n = struct.unpack(">I", f[:4])[0]
        self.assertEqual(n, len(f) - 4)               # 4-byte big-endian length prefix
        d = sp.cast_parse_frame(f[4:])
        self.assertEqual(d["namespace"], "urn:x-cast:com.google.cast.receiver")
        self.assertEqual(d["source"], "sender-0")
        self.assertEqual(d["dest"], "receiver-0")
        self.assertEqual(json.loads(d["payload"]), {"type": "LAUNCH", "appId": "CC1AD845"})

    def test_mdns_name_compression(self):
        # "_googlecast._tcp.local" then a compression pointer back to offset 2
        base = sp._dns_encode_name("_googlecast._tcp.local")
        blob = b"\x00\x00" + base + b"\xc0\x02"        # pointer -> offset 2
        name, i = sp._dns_parse_name(blob, 2)
        self.assertEqual(name, "_googlecast._tcp.local")
        # the pointer that follows resolves to the same name
        name2, _ = sp._dns_parse_name(blob, 2 + len(base))
        self.assertEqual(name2, "_googlecast._tcp.local")

    def test_mdns_query_packet_is_ptr_question(self):
        pkt = sp._mdns_query_packet()
        qd = struct.unpack(">H", pkt[4:6])[0]          # qdcount
        self.assertEqual(qd, 1)
        self.assertIn(b"_googlecast", pkt)
        # QTYPE=PTR(12) QCLASS=IN(1) at the end
        qtype, qclass = struct.unpack(">HH", pkt[-4:])
        self.assertEqual(qtype, 12)
        self.assertEqual(qclass, 1)

    def test_castsession_handles_status_ping_finish(self):
        sent = []
        class MockSock:
            def sendall(self, b): sent.append(b)
            def recv(self, n): raise AssertionError("recv not used in this test")
            def close(self): pass
        sess = sp.CastSession("1.2.3.4", sock_factory=lambda ip, port: MockSock(), autostart=False)
        # _connect() already sent CONNECT + PING
        types0 = [json.loads(sp.cast_parse_frame(b[4:])["payload"]).get("type") for b in sent]
        self.assertIn("CONNECT", types0)
        # RECEIVER_STATUS -> transportId + volume captured
        rs = {"type": "RECEIVER_STATUS", "status": {
            "applications": [{"appId": "CC1AD845", "transportId": "tr-9", "sessionId": "s-1"}],
            "volume": {"level": 0.5}}}
        sess._handle({"namespace": sp.NS_RECV, "payload": json.dumps(rs)})
        self.assertEqual(sess.transport, "tr-9")
        self.assertEqual(sess.status["volume"], 50)
        # MEDIA_STATUS -> playerState + position
        ms = {"type": "MEDIA_STATUS", "status": [{"playerState": "PLAYING",
              "currentTime": 12.0, "media": {"duration": 200.0}}]}
        sess._handle({"namespace": sp.NS_MED, "payload": json.dumps(ms)})
        self.assertEqual(sess.status["state"], "PLAYING")
        self.assertEqual(sess.status["position"], sp.sec_to_hms(12.0))
        # PING -> PONG
        sent.clear()
        sess._handle({"namespace": sp.NS_HB, "payload": json.dumps({"type": "PING"})})
        pong = [json.loads(sp.cast_parse_frame(b[4:])["payload"]).get("type") for b in sent]
        self.assertIn("PONG", pong)
        # FINISHED -> advance callback fires
        fired = []
        sess._advance_cb = lambda: fired.append(1)
        sess._handle({"namespace": sp.NS_MED, "payload": json.dumps(
            {"type": "MEDIA_STATUS", "status": [{"playerState": "IDLE", "idleReason": "FINISHED"}]})})
        self.assertEqual(fired, [1])

    def test_backend_field_and_merge(self):
        orig = (draai.backends.ssdp_discover, draai.backends.get_zone_groups, draai.backends.cast_discover)
        try:
            draai.backends.ssdp_discover = lambda: {"10.0.0.5"}
            draai.backends.get_zone_groups = lambda ip: [{"uuid": "RINCON_x", "name": "Living",
                "ip": "10.0.0.5", "members": [{"uuid": "RINCON_x", "name": "Living", "ip": "10.0.0.5"}]}]
            draai.backends.cast_discover = lambda timeout=4.0: [{"uuid": "CAST_abc", "name": "Zolder",
                "ip": "10.0.0.9", "port": 8009, "backend": "cast", "is_group": False,
                "members": [{"uuid": "CAST_abc", "name": "Zolder", "ip": "10.0.0.9"}]}]
            merged, err = sp.refresh_speakers()
            self.assertIsNone(err)
            by = {s["uuid"]: s for s in merged}
            self.assertEqual(by["RINCON_x"]["backend"], "sonos")
            self.assertEqual(by["CAST_abc"]["backend"], "cast")
            self.assertEqual(draai.backends.speaker_by_uuid("CAST_abc")["backend"], "cast")
        finally:
            draai.backends.ssdp_discover, draai.backends.get_zone_groups, draai.backends.cast_discover = orig

    def test_no_devices_message(self):
        orig = (draai.backends.ssdp_discover, draai.backends.cast_discover)
        try:
            draai.backends.ssdp_discover = lambda: set()
            draai.backends.cast_discover = lambda timeout=4.0: []
            # config manual_ips may add entries; ensure empty for this assertion
            mi = sp.config.get("manual_ips")
            sp.config["manual_ips"] = []
            merged, err = sp.refresh_speakers()
            self.assertEqual(merged, [])
            self.assertIn("No speakers found", err)
        finally:
            draai.backends.ssdp_discover, draai.backends.cast_discover = orig
            sp.config["manual_ips"] = mi if mi is not None else []

    def test_cast_guard_eq_and_grouping_raise(self):
        cast_spk = {"backend": "cast", "uuid": "CAST_x", "ip": "1.2.3.4",
                    "name": "Zolder", "members": [{"uuid": "CAST_x", "name": "Zolder", "ip": "1.2.3.4"}]}
        with self.assertRaises(RuntimeError):
            sp.get_eq(cast_spk)
        with self.assertRaises(RuntimeError):
            sp.set_eq(cast_spk, bass=1)
        # group_join resolves member by uuid -> make it findable as cast
        orig = draai.backends.speaker_by_uuid
        try:
            draai.backends.speaker_by_uuid = lambda u: cast_spk if u == "CAST_x" else None
            with self.assertRaises(RuntimeError):
                sp.group_join("CAST_x", "CAST_y")
            with self.assertRaises(RuntimeError):
                sp.group_leave("CAST_x")
        finally:
            draai.backends.speaker_by_uuid = orig

    def test_cast_play_and_queue(self):
        sent = []
        class MockSock:
            def sendall(self, b): sent.append(b)
            def recv(self, n): raise AssertionError("no recv")
            def close(self): pass
        ip = "9.9.9.9"
        sess = sp.CastSession(ip, sock_factory=lambda i, p: MockSock(), autostart=False)
        sess.transport = "tr-1"; sess._running = True
        sp.cast_sessions[ip] = sess
        a, b = "a" * 16, "b" * 16
        sp.tracks_by_id[a] = {"id": a, "title": "TA", "artist": "X", "ext": ".flac", "path": "/a.flac"}
        sp.tracks_by_id[b] = {"id": b, "title": "TB", "artist": "X", "ext": ".mp3", "path": "/b.mp3"}
        spk = {"ip": ip, "port": 8009, "backend": "cast", "uuid": "CAST_x"}
        try:
            sent.clear()
            sp.cast_play_tracks(spk, [a, b])
            self.assertEqual(sp.cast_queues[ip]["ids"], [a, b])
            self.assertEqual(sp.cast_queues[ip]["idx"], 0)
            load = [json.loads(sp.cast_parse_frame(x[4:])["payload"]) for x in sent]
            load = [p for p in load if p.get("type") == "LOAD"][0]
            self.assertEqual(load["media"]["contentType"], "audio/flac")
            self.assertIn("/media/" + a + "/", load["media"]["contentId"])
            # queue view shape
            items, total = sp.cast_browse_queue(spk)
            self.assertEqual(total, 2)
            self.assertEqual(items[0], {"no": 1, "id": a, "title": "TA"})
            # auto-advance loads b
            sent.clear()
            sp._cast_advance(ip)
            self.assertEqual(sp.cast_queues[ip]["idx"], 1)
            load2 = [json.loads(sp.cast_parse_frame(x[4:])["payload"]) for x in sent]
            load2 = [p for p in load2 if p.get("type") == "LOAD"][0]
            self.assertEqual(load2["media"]["contentType"], "audio/mpeg")
            # status shape
            stt = sp.cast_get_status(spk)
            self.assertEqual(stt["track_no"], 2)
            self.assertEqual(stt["track_id"], b)
            # AIFF rejected
            sp.tracks_by_id["c" * 16] = {"id": "c" * 16, "title": "C", "ext": ".aiff", "path": "/c.aiff"}
            with self.assertRaises(RuntimeError):
                sp.cast_play_tracks(spk, ["c" * 16])
        finally:
            sp.cast_sessions.pop(ip, None); sp.cast_queues.pop(ip, None)
            for k in (a, b, "c" * 16): sp.tracks_by_id.pop(k, None)

    @unittest.skipUnless(sp.find_tool("ffmpeg"), "ffmpeg not installed")
    def test_stereo_analysis_channels(self):
        import wave, struct
        path = os.path.join(self.tmp, "pan.wav")
        sr, n = 8000, 8000  # 1 second
        with wave.open(path, "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
            frames = b"".join(struct.pack("<hh", 30000, 2000) for _ in range(n))  # L loud, R quiet
            w.writeframes(frames)
        d = sp._analyze({"id": "pan", "path": path})
        self.assertEqual(d["step"], 0.03)
        self.assertIn("ampL", d); self.assertIn("ampR", d)
        self.assertEqual(len(d["ampL"]), len(d["amp"]))
        self.assertEqual(len(d["ampR"]), len(d["amp"]))
        self.assertLessEqual(len(d["peaks"]), 240)
        # left channel is much louder than right
        self.assertGreater(sum(d["ampL"]), sum(d["ampR"]) * 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
