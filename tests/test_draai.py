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
ENGINE = os.path.join(HERE, "..", "sonos_player.py")
if not os.path.isfile(ENGINE):
    ENGINE = os.path.join(HERE, "sonos_player.py")   # flat layout fallback

spec = importlib.util.spec_from_file_location("draai", ENGINE)
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)


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
            self.queue.append(args["EnqueuedURI"])
            return "<ok/>"
        if action == "RemoveAllTracksFromQueue":
            self.queue = []
            return "<ok/>"
        if action == "Browse":
            items = "".join(
                '<item id="Q:0/%d"><dc:title>t%d</dc:title></item>' % (i + 1, i + 1)
                for i in range(len(self.queue)))
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
        sp.soap_call = self.soap
        sp.ssdp_discover = lambda timeout=3.0: {"192.168.1.50"}
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

    # -- helpers -----------------------------------------------------------------------

    def test_time_helpers(self):
        self.assertEqual(sp.sec_to_hms(3725), "1:02:05")
        self.assertEqual(sp.hms_to_sec("1:02:05"), 3725)
        self.assertEqual(sp.hms_to_sec("bogus"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
