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
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)                    # so `import draai` (the package) resolves

import types
import draai
import draai.state, draai.constants, draai.util, draai.config, draai.media
import draai.library, draai.analysis, draai.importer, draai.cast
import draai.backends, draai.playlists, draai.server, draai.listening, draai.local

# `sp` = a flat namespace of the package's public API for the tests' sp.<name>
# reads/calls. Function MOCKS are patched on the real module they live in
# (e.g. draai.backends.soap_call), since that's where the callers resolve them.
sp = types.SimpleNamespace(__version__=draai.__version__)
for _m in (draai.state, draai.constants, draai.util, draai.config, draai.media,
           draai.library, draai.analysis, draai.importer, draai.cast,
           draai.backends, draai.playlists, draai.server, draai.listening,
           draai.local):
    for _n in dir(_m):
        if not _n.startswith("__"):
            setattr(sp, _n, getattr(_m, _n))
import http.server
sp.ThreadingHTTPServer = http.server.ThreadingHTTPServer   # for the live-server API test


# ----------------------------------------------------------------------------
# hand-crafted media files (no ffmpeg required)
# ----------------------------------------------------------------------------

def make_mp3(path, title="Song", artist="Artist", album="Album",
             art=b"\x89PNG\r\n\x1a\nFAKEPNGDATA", purl=None, purl_utf8=False):
    """Minimal ID3v2.3 tag + dummy audio bytes. `purl` adds a yt-dlp style
    TXXX:purl source-link frame (latin-1, after the cover, like yt-dlp;
    `purl_utf8` writes it with encoding byte 0x03 instead)."""
    def frame(fid, body):
        return fid + struct.pack(">I", len(body)) + b"\x00\x00" + body

    def text(fid, value):
        return frame(fid, b"\x03" + value.encode("utf-8"))

    frames = text(b"TIT2", title) + text(b"TPE1", artist) + text(b"TALB", album)
    if art:
        frames += frame(b"APIC", b"\x03image/png\x00\x03\x00" + art)
    if purl is not None:
        if purl_utf8:
            frames += frame(b"TXXX", b"\x03purl\x00" + purl.encode("utf-8"))
        else:
            frames += frame(b"TXXX", b"\x00purl\x00" + purl.encode("latin-1"))
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


def qr_decode(mat):
    """Decode a DRAAI-generated QR matrix the way a scanner does: rebuild the
    function-module map, read the format info for the mask, un-mask, read the
    data bits in zigzag order, and return (rs_syndromes_zero, decoded_text).
    A conformant scanner accepts the symbol iff all RS syndromes are zero."""
    size = len(mat)
    ver = (size - 17) // 4
    M = [[None] * size for _ in range(size)]

    def setr(r, c, pat):
        for dr, row in enumerate(pat):
            for dc, v in enumerate(row):
                if 0 <= r + dr < size and 0 <= c + dc < size:
                    M[r + dr][c + dc] = v

    fnd = [[1] * 7, [1, 0, 0, 0, 0, 0, 1], [1, 0, 1, 1, 1, 0, 1],
           [1, 0, 1, 1, 1, 0, 1], [1, 0, 1, 1, 1, 0, 1],
           [1, 0, 0, 0, 0, 0, 1], [1] * 7]
    for r, c in ((0, 0), (0, size - 7), (size - 7, 0)):
        setr(r, c, fnd)
    for i in range(8):
        for r, c in ((7, i), (i, 7), (7, size - 8 + i), (i, size - 8),
                     (size - 8, i), (size - 8 + i, 7)):
            if 0 <= r < size and 0 <= c < size and M[r][c] is None:
                M[r][c] = 0
    ap = [[1] * 5, [1, 0, 0, 0, 1], [1, 0, 1, 0, 1], [1, 0, 0, 0, 1], [1] * 5]
    for r in sp._QR_ALIGN[ver]:
        for c in sp._QR_ALIGN[ver]:
            if M[r][c] is None:
                setr(r - 2, c - 2, ap)
    for i in range(8, size - 8):
        if M[6][i] is None:
            M[6][i] = 1 - (i % 2)
        if M[i][6] is None:
            M[i][6] = 1 - (i % 2)
    M[size - 8][8] = 1
    fp = []
    for i in range(9):
        if i != 6:
            fp += [(8, i), (i, 8)]
    for i in range(8):
        fp += [(size - 1 - i, 8), (8, size - 1 - i)]
    for r, c in fp:
        if M[r][c] is None:
            M[r][c] = 0
    func = [[M[r][c] is not None for c in range(size)] for r in range(size)]
    pos_a = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
             (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    fmt = 0
    for r, c in pos_a:
        fmt = (fmt << 1) | mat[r][c]
    mask = ((fmt ^ 0x5412) >> 10) & 0b111
    masks = [lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0,
             lambda r, c: c % 3 == 0, lambda r, c: (r + c) % 3 == 0,
             lambda r, c: (r // 2 + c // 3) % 2 == 0,
             lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
             lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
             lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0]
    bits = []
    col, up = size - 1, True
    while col > 0:
        if col == 6:
            col -= 1
        for r in (range(size - 1, -1, -1) if up else range(size)):
            for c in (col, col - 1):
                if not func[r][c]:
                    bits.append(mat[r][c] ^ (1 if masks[mask](r, c) else 0))
        up = not up
        col -= 2
    code = [int("".join(map(str, bits[i:i + 8])), 2)
            for i in range(0, (len(bits) // 8) * 8, 8)]
    n_data = sp._QR_TOTAL[ver] - sp._QR_EC[ver]
    n_ec = sp._QR_EC[ver]
    code = code[:n_data + n_ec]
    syn_zero = True
    for j in range(n_ec):
        acc = 0
        for k, b in enumerate(code):
            if b:
                acc ^= sp._GF_EXP[(sp._GF_LOG[b] + (len(code) - 1 - k) * j) % 255]
        if acc:
            syn_zero = False
    b = "".join(format(x, "08b") for x in code[:n_data])
    cnt = int(b[4:12], 2)
    txt = bytes(int(b[12 + i * 8:20 + i * 8], 2) for i in range(cnt))
    return syn_zero, txt.decode("utf-8", "replace")


def _pulse_train(step, period, duration, spike=90, base=10):
    """Synthetic low/mid/high envelopes: a sharp one-frame spike in `low`
    every `period` seconds, flat elsewhere. Returns (low, mid, high, true_times).

    The first pulse is placed at t=period, not t=0: onset novelty is a
    frame-to-frame diff, so a transient sitting exactly on frame 0 has no
    prior frame to rise from and can never be detected — the same is true
    of any real recording (nothing has a beat at t=0 with zero pre-roll).
    """
    n = int(round(duration / step))
    low = [base] * n
    mid = [base] * n
    high = [base] * n
    true_times = []
    t = period
    while t < duration:
        idx = int(round(t / step))
        if idx < n:
            low[idx] = spike
            true_times.append(idx * step)
        t += period
    return low, mid, high, true_times


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


FAKE_DEVS = [
    {"index": 0, "uid": "LG-A", "name": "LG HDR 4K", "out_channels": 2},
    {"index": 1, "uid": "LG-B", "name": "LG HDR 4K", "out_channels": 2},
    {"index": 2, "uid": "JABRA-OUT", "name": "Jabra Link 370", "out_channels": 2},
    {"index": 3, "uid": "JABRA-IN", "name": "Jabra Link 370", "out_channels": 0},
    {"index": 4, "uid": "BuiltInMicrophoneDevice", "name": "MacBook Pro Microphone", "out_channels": 0},
    {"index": 5, "uid": "BuiltInSpeakerDevice", "name": "MacBook Pro Speakers", "out_channels": 2},
]


class FakeProc:
    """Stand-in for an ffmpeg child. `lines` are fed to the progress reader;
    with `rc` given it exits straight away, otherwise it runs until finish()
    or kill()."""
    def __init__(self, argv, lines=(), rc=None):
        import queue
        self.argv = argv
        self.returncode = None
        self.killed = False
        self._q = queue.Queue()
        for ln in lines:
            self._q.put(ln)
        if rc is not None:
            self.finish(rc)
        self.stdout = self._iter()

    def _iter(self):
        while True:
            ln = self._q.get()
            if ln is None:
                return
            yield ln

    def feed(self, line):
        self._q.put(line)

    def finish(self, rc):
        if self.returncode is None:
            self.returncode = rc
            self._q.put(None)

    def kill(self):
        self.killed = True
        self.finish(-9)

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode


def wait_for(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class DraaiTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.soap = SoapMock()
        draai.backends.soap_call = self.soap
        draai.backends.ssdp_discover = lambda timeout=3.0: {"192.168.1.50"}
        draai.backends.cast_discover = lambda timeout=4.0: []
        # never touch real CoreAudio / ffmpeg
        draai.local._enumerate_coreaudio = lambda: []
        draai.local._available = lambda: True
        draai.local.players.clear()
        sp.config["local_volume"] = {}
        # config saves go to the temp dir, never the real app-support dir
        self._cfg_paths = (draai.config.CONFIG_PATH, draai.config.CONFIG_DIR)
        draai.config.CONFIG_DIR = self.tmp
        draai.config.CONFIG_PATH = os.path.join(self.tmp, "config.json")
        sp.config["folders"] = [self.tmp]
        sp.config["manual_ips"] = []
        # never let a debounced save reach the real config dir
        draai.listening._path[0] = os.path.join(self.tmp, "listening.json")
        draai.listening._info_path[0] = os.path.join(self.tmp, "trackinfo.json")

    def tearDown(self):
        draai.local.local_shutdown()
        draai.local.players.clear()
        draai.config.CONFIG_PATH, draai.config.CONFIG_DIR = self._cfg_paths
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

    def test_qr_reed_solomon(self):
        # GF(256) tables must be populated (the bug: they were all zeros, so
        # every QR shipped with all-zero EC and would not decode).
        self.assertFalse(all(x == 0 for x in sp._GF_EXP))
        self.assertTrue(all(sp._GF_LOG[sp._GF_EXP[i]] == i for i in range(255)))
        # canonical reference vector (Thonky "HELLO WORLD", 10 EC codewords)
        data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64,
                236, 17, 236, 17, 236, 17]
        self.assertEqual(sp._rs_ec(data, 10),
                         [196, 35, 39, 119, 235, 215, 231, 226, 93, 23])

    def test_qr_decodes(self):
        # Round-trip: a scanner un-masks, reads, and checks RS syndromes. For
        # several lengths (versions 1-4) the symbol must carry zero errors and
        # decode back to the exact URL.
        for url in ("http://a.b/x",
                    "http://192.168.1.10:8765/remote",
                    "http://192.168.100.200:8799/remote?x=1",
                    "http://192.168.100.200:8799/remote?token=abcdef1234567890"):
            syn_zero, decoded = qr_decode(sp._qr_matrix(url))
            self.assertTrue(syn_zero, "RS syndromes nonzero for %r" % url)
            self.assertEqual(decoded, url)

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
            self.assertEqual(st["install"], "source")
            tr = get("/api/tracks")
            self.assertEqual(tr["tracks"][0]["title"], "D-Track")
            self.assertTrue(tr["tracks"][0]["has_art"])
            br = get("/api/browse?path=" + urllib.parse.quote(self.tmp))
            self.assertEqual(br["path"], os.path.realpath(self.tmp))
        finally:
            httpd.shutdown()

    def test_install_kind_detects_homebrew(self):
        """A Homebrew install lives under <prefix>/Cellar/draai/<ver>/ — the
        engine then defers autostart and updates to brew."""
        from draai.install import install_kind
        self.assertEqual(install_kind(
            "/opt/homebrew/Cellar/draai/2.7.0/libexec/draai.pyz/draai/__init__.py"),
            "homebrew")
        self.assertEqual(install_kind(
            "/usr/local/Cellar/draai/2.7.0/libexec/draai.pyz/draai/__init__.py"),
            "homebrew")
        self.assertEqual(install_kind("/Users/x/Downloads/draai.pyz/draai/__init__.py"), "pyz")
        self.assertEqual(install_kind("/Users/x/src/draai/draai/__init__.py"), "source")
        self.assertEqual(install_kind(), "source")   # this test run

    def test_install_autostart_defers_to_brew(self):
        import io, contextlib
        from draai import install
        home = tempfile.mkdtemp()
        old_home, old_kind = os.environ.get("HOME"), install.install_kind
        os.environ["HOME"] = home
        install.install_kind = lambda path=None: "homebrew"
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                install.install_autostart()
            self.assertIn("brew services start draai", out.getvalue())
            self.assertFalse(os.path.exists(os.path.join(
                home, "Library", "LaunchAgents", "com.draai.player.plist")))
        finally:
            install.install_kind = old_kind
            os.environ["HOME"] = old_home
            shutil.rmtree(home, ignore_errors=True)

    def test_running_engine_detection(self):
        """`draai` with DRAAI already running opens that one instead of
        starting a second engine on the next port."""
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from draai.install import running_engine

        def serve(body):
            class H(BaseHTTPRequestHandler):
                def do_GET(self):
                    data = json.dumps(body).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                def log_message(self, *a):
                    pass
            srv = HTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            return srv

        draai_srv = serve({"version": "9.9.9", "speakers": []})
        other_srv = serve({"hello": "world"})
        try:
            dp, op = draai_srv.server_address[1], other_srv.server_address[1]
            self.assertEqual(running_engine([dp]), "http://localhost:%d" % dp)
            self.assertIsNone(running_engine([op]))
            self.assertEqual(running_engine([op, dp]), "http://localhost:%d" % dp)
        finally:
            draai_srv.shutdown()
            other_srv.shutdown()
        self.assertIsNone(running_engine([dp]))   # nothing listening any more

    def test_manifest_and_icons_served(self):
        """Chrome's 'Install DRAAI' needs a manifest with real PNG icons."""
        httpd = sp.ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def get(p):
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d%s" % (port, p), timeout=10) as r:
                    return r.headers.get("Content-Type"), r.read()
            ctype, body = get("/manifest.webmanifest")
            self.assertEqual(ctype, "application/manifest+json")
            m = json.loads(body)
            self.assertEqual(m["name"], "DRAAI")
            self.assertEqual(m["display"], "standalone")
            for icon in m["icons"]:
                ctype, png = get(icon["src"])
                self.assertEqual(ctype, "image/png")
                self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
                w, h = struct.unpack(">II", png[16:24])
                self.assertEqual("%dx%d" % (w, h), icon["sizes"])
            with self.assertRaises(urllib.error.HTTPError):
                get("/icon-64.png")
            ui = sp._load_ui()
            self.assertIn('rel="manifest" href="/manifest.webmanifest"', ui)
        finally:
            httpd.shutdown()

    def test_tool_install_runs_brew_for_missing_tools(self):
        """'Install yt-dlp' in the UI runs `brew install` for exactly the
        missing tools, without Homebrew's cleanup (it could remove the Python
        the running engine uses), and reports done once they are found."""
        imp = draai.importer
        installed = {"ffmpeg"}
        calls = []

        def fake_find(name):
            if name == "brew":
                return "/opt/homebrew/bin/brew"
            return "/opt/homebrew/bin/" + name if name in installed else None

        def fake_run(cmd, **kw):
            calls.append((cmd, kw.get("env", {})))
            installed.update(cmd[2:])
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        old_find, old_run = imp.find_tool, imp.subprocess.run
        imp.find_tool, imp.subprocess.run = fake_find, fake_run
        try:
            y = imp.yt_available()
            self.assertEqual(y["missing"], ["yt-dlp"])
            self.assertTrue(y["can_install"])
            job = imp.start_tool_install()
            for _ in range(100):
                if imp.install_status()["status"] != "working":
                    break
                time.sleep(0.02)
            self.assertEqual(imp.install_status()["status"], "done")
            self.assertEqual(calls[0][0], ["/opt/homebrew/bin/brew", "install", "yt-dlp"])
            self.assertEqual(calls[0][1].get("HOMEBREW_NO_INSTALL_CLEANUP"), "1")
            self.assertTrue(imp.yt_available()["available"])
            self.assertIn(job["status"], ("working", "done"))
        finally:
            imp.find_tool, imp.subprocess.run = old_find, old_run

    def test_tool_install_failure_is_a_sentence(self):
        imp = draai.importer
        old_find, old_run = imp.find_tool, imp.subprocess.run
        imp.find_tool = lambda n: "/opt/homebrew/bin/brew" if n == "brew" else None
        imp.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
            returncode=1, stdout="", stderr="Error: something broke\n")
        try:
            imp.start_tool_install()
            for _ in range(100):
                if imp.install_status()["status"] != "working":
                    break
                time.sleep(0.02)
            st = imp.install_status()
            self.assertEqual(st["status"], "error")
            self.assertIn("brew install", st["error"])
            self.assertNotIn("Traceback", st["error"])
            imp.find_tool = lambda n: None          # no Homebrew at all
            with self.assertRaises(RuntimeError):
                imp.start_tool_install()
        finally:
            imp.find_tool, imp.subprocess.run = old_find, old_run

    def test_tool_install_only_from_this_mac(self):
        httpd = sp.ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/install_tools" % port, data=b"{}",
                headers={"Content-Type": "application/json", "Host": "draai.test"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 403)
            self.assertIn("on this Mac", json.load(cm.exception)["error"])
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

    def test_discovery_interfaces_skip_vpn_tunnels(self):
        from draai.util import parse_ipv4_addrs
        mac = ("lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384\n"
               "\tinet 127.0.0.1 netmask 0xff000000\n"
               "en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
               "\tinet 192.168.178.155 netmask 0xffffff00 broadcast 192.168.178.255\n"
               "ipsec0: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280\n"
               "\tinet 192.168.31.245 --> 192.168.31.245 netmask 0xffffff00\n"
               "utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380\n"
               "\tinet 10.8.0.2 --> 10.8.0.1 netmask 0xffffffff\n"
               "en7: flags=8863<UP,BROADCAST,RUNNING,MULTICAST> mtu 1500\n"
               "\tinet 169.254.3.4 netmask 0xffff0000\n")
        self.assertEqual(parse_ipv4_addrs(mac), ["192.168.178.155"])
        linux = ("1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n"
                 "    inet 127.0.0.1/8 scope host lo\n"
                 "2: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
                 "    inet 192.168.1.20/24 brd 192.168.1.255 scope global wlan0\n"
                 "5: tun0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1500\n"
                 "    inet 10.8.0.2/24 scope global tun0\n")
        self.assertEqual(parse_ipv4_addrs(linux), ["192.168.1.20"])

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

    def test_cast_status_exact_position_extrapolated(self):
        # Cast reports a float currentTime about once a second; the engine must hand the
        # UI that precise value moved forward to "now", not a stale whole second.
        class MockSock:
            def sendall(self, b): pass
            def close(self): pass
        sess = sp.CastSession("1.2.3.7", sock_factory=lambda ip, port: MockSock(), autostart=False)
        clock = [1000.0]
        orig = draai.cast.time.monotonic
        draai.cast.time.monotonic = lambda: clock[0]
        try:
            sess._handle({"namespace": sp.NS_MED, "payload": json.dumps({"type": "MEDIA_STATUS",
                "status": [{"playerState": "PLAYING", "currentTime": 12.4, "media": {"duration": 200.0}}]})})
            sp.cast_sessions["1.2.3.7"] = sess
            clock[0] += 0.85
            st = sp.cast_get_status({"ip": "1.2.3.7"})
            self.assertAlmostEqual(st["pos"], 13.25, places=3)
            self.assertEqual(st["position"], "0:00:13")
            sess._handle({"namespace": sp.NS_MED, "payload": json.dumps({"type": "MEDIA_STATUS",
                "status": [{"playerState": "PAUSED", "currentTime": 20.5}]})})
            clock[0] += 5
            self.assertAlmostEqual(sp.cast_get_status({"ip": "1.2.3.7"})["pos"], 20.5, places=3)
        finally:
            draai.cast.time.monotonic = orig
            sp.cast_sessions.pop("1.2.3.7", None)

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
        import draai.analysis as _A
        self._old_adir = _A.ANALYSIS_DIR; _A.ANALYSIS_DIR = self.tmp
        self.addCleanup(lambda: setattr(_A, "ANALYSIS_DIR", self._old_adir))
        path = os.path.join(self.tmp, "pan.wav")
        sr, n = 8000, 8000  # 1 second
        with wave.open(path, "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
            frames = b"".join(struct.pack("<hh", 30000, 2000) for _ in range(n))  # L loud, R quiet
            w.writeframes(frames)
        d = sp._analyze({"id": "pan", "path": path})
        self.assertIsNotNone(d)
        self.assertEqual(d["step"], 0.03)
        self.assertIn("ampL", d); self.assertIn("ampR", d)
        self.assertEqual(len(d["ampL"]), len(d["amp"]))
        self.assertEqual(len(d["ampR"]), len(d["amp"]))
        self.assertLessEqual(len(d["peaks"]), 240)
        # left channel is much louder than right
        self.assertGreater(sum(d["ampL"]), sum(d["ampR"]) * 3)

    # -- beat / section detection (pure functions, no ffmpeg needed) ----------

    def test_beat_detection_120bpm(self):
        # production step (0.03s) — 0.5s is 16.667 frames at this step, i.e.
        # NOT an integer frame count. That mismatch is exactly what makes
        # this a meaningful test: a detector that snaps the beat period to
        # an integer frame count will drift over a long track (120 BPM
        # would read as 117.6 BPM, and the grid slips out of phase by up to
        # half a beat). Run long enough (300s / 600 beats) for drift to
        # show up if it's there.
        step = sp.ANALYSIS_STEP
        low, mid, high, true_times = _pulse_train(step, 0.5, 300.0)
        beats, bpm = sp.detect_beats(low, mid, high, step)
        self.assertIsNotNone(bpm)
        self.assertAlmostEqual(bpm, 120.0, delta=2)
        self.assertTrue(beats)
        errs = sorted(min(abs(tt - b) for b in beats) for tt in true_times)
        median_err = errs[len(errs) // 2]
        max_err = errs[-1]
        self.assertLess(median_err, 0.04)
        self.assertLess(max_err, 0.08, "beat grid drifted out of phase: max_err=%r" % max_err)

    def test_beat_detection_octave_guard(self):
        step = sp.ANALYSIS_STEP
        low, mid, high, _ = _pulse_train(step, 0.5, 30.0)
        beats, bpm = sp.detect_beats(low, mid, high, step)
        self.assertIsNotNone(bpm)
        self.assertGreater(abs(bpm - 240), 10)
        self.assertGreater(abs(bpm - 60), 10)

    def test_beat_detection_fast_tempos_not_halved(self):
        # 150-190 BPM (hardstyle/hardcore territory) must not be reported
        # at half tempo. The octave-guard band now extends to 180 BPM, but
        # 175 and 190 BPM specifically also exercise a second failure mode:
        # at a 0.03s step their true period lands close to a half-integer
        # frame count, so quantization jitter suppresses the raw
        # single-cycle autocorrelation and the two-cycle (half-tempo) lag
        # scores higher even though it's inside the preferred band too —
        # only a sub-harmonic check (comparing the winner against its own
        # half-lag) catches that, not the band/ratio widening alone.
        step = sp.ANALYSIS_STEP
        for true_bpm in (160, 175, 190):
            low, mid, high, _ = _pulse_train(step, 60.0 / true_bpm, 60.0)
            beats, bpm = sp.detect_beats(low, mid, high, step)
            self.assertIsNotNone(bpm, "no bpm detected for %r BPM train" % true_bpm)
            self.assertAlmostEqual(bpm, true_bpm, delta=3,
                                   msg="%r BPM train detected as %r (halved?)" % (true_bpm, bpm))

    def test_beat_grid_ignores_offbeat_onsets(self):
        # Regression: the grid fit used to index onsets sequentially with
        # `max(1, round(gap/period))`, so an onset half a beat after the last one was
        # charged a WHOLE beat. On a real frenchcore track 178 such off-beat hits
        # inflated the assigned span to 868 beats against ~809 of music, dragging a
        # correct 199.7 BPM up to 216.2. Every pre-existing test passed throughout,
        # because they all used clean pulse trains with nothing on the off-beat.
        period = 0.30                       # 200 BPM
        onsets = []
        for i in range(400):
            onsets.append(round(i * period, 4))
            if i % 3 == 0:                  # a third of beats also carry an off-beat hit
                onsets.append(round(i * period + period / 2, 4))
        onsets.sort()
        phase, fitted = sp._fit_beat_grid(onsets, period)
        self.assertAlmostEqual(fitted, period, delta=0.005,
                               msg="off-beat onsets inflated the fitted tempo: %.1f BPM"
                                   % (60.0 / fitted))

    def test_beat_grid_cannot_redecide_tempo(self):
        # The fit polishes frame quantization; it must never overrule the tempo that
        # seeded it. Feed onsets that imply a very different period and confirm the
        # result stays within the +/-2% clamp.
        seed = 0.50
        onsets = [round(i * 0.30, 4) for i in range(300)]   # implies 0.30, not 0.50
        phase, fitted = sp._fit_beat_grid(onsets, seed)
        self.assertGreaterEqual(fitted, seed * 0.97)
        self.assertLessEqual(fitted, seed * 1.03)

    def test_tempo_corroboration_rescues_soft_peak(self):
        # A real 200 BPM frenchcore track produced the RIGHT period (0.3005s = 199.7
        # BPM) and was discarded for scoring 1.204 against the 1.3 gate, because a
        # third of its onsets sit on the off-beat and flatten the autocorrelation peak.
        # A soft peak corroborated by the median inter-onset interval is trustworthy.
        onsets = [round(i * 0.30, 3) for i in range(200)]        # steady 200 BPM
        self.assertTrue(sp._tempo_trustworthy(0.3005, 1.20, onsets))
        # ...but corroboration must not become a back door: a period that disagrees
        # with the onsets stays rejected however close to the gate it scores. This is
        # the case that broke on real files when the gate was simply lowered.
        self.assertFalse(sp._tempo_trustworthy(0.225, 1.29, onsets))
        # and nothing rescues a peak below the floor
        self.assertFalse(sp._tempo_trustworthy(0.3005, 1.00, onsets))
        # a sharp peak needs no corroboration at all
        self.assertTrue(sp._tempo_trustworthy(0.4, 1.9, []))

    def test_beat_detection_slow_end_not_doubled(self):
        # Guards the slow half of the range against being reported at double tempo.
        step = sp.ANALYSIS_STEP
        for true_bpm in (60, 75, 90, 105, 125):
            low, mid, high, _ = _pulse_train(step, 60.0 / true_bpm, 60.0)
            beats, bpm = sp.detect_beats(low, mid, high, step)
            self.assertIsNotNone(bpm, "no bpm detected for %r BPM train" % true_bpm)
            self.assertAlmostEqual(bpm, true_bpm, delta=3,
                                   msg="%r BPM train detected as %r (doubled?)" % (true_bpm, bpm))

    def test_beat_detection_uptempo_not_third_timed(self):
        # 187-195 BPM: near the 0.30s search floor, the true lag falls
        # between the 10- and 11-frame lags (both very coarse), while a 3x
        # harmonic lands on a finely-represented longer lag and can
        # outscore the fundamental — a divide-by-3 error, not the
        # divide-by-2 error the other fast-tempo test exercises.
        step = sp.ANALYSIS_STEP
        for true_bpm in (187, 193, 195):
            low, mid, high, _ = _pulse_train(step, 60.0 / true_bpm, 60.0)
            beats, bpm = sp.detect_beats(low, mid, high, step)
            self.assertIsNotNone(bpm, "no bpm detected for %r BPM train" % true_bpm)
            self.assertAlmostEqual(bpm, true_bpm, delta=4,
                                   msg="%r BPM train detected as %r (1/3 tempo?)" % (true_bpm, bpm))

    def test_beat_detection_slow_tempo_not_doubled(self):
        step = sp.ANALYSIS_STEP
        low, mid, high, _ = _pulse_train(step, 60.0 / 70, 60.0)
        beats, bpm = sp.detect_beats(low, mid, high, step)
        self.assertIsNotNone(bpm)
        self.assertAlmostEqual(bpm, 70.0, delta=3)

    def test_sections_quiet_then_loud(self):
        step = 0.03
        duration = 60.0
        n = int(round(duration / step))
        half = n // 2
        amp = [20] * half + [90] * (n - half)
        low = list(amp); mid = list(amp); high = list(amp)
        sections = sp.detect_sections(amp, low, mid, high, step)
        self.assertEqual(sections[0]["t"], 0.0)
        # a boundary near the midpoint (30s), within +/-4s
        self.assertTrue(any(abs(sec["t"] - 30.0) <= 4 for sec in sections[1:]),
                        "no boundary near the midpoint: %r" % sections)
        self.assertGreater(sections[-1]["energy"], sections[0]["energy"])

    def test_beats_and_sections_degenerate_input(self):
        beats, bpm = sp.detect_beats([], [], [], 0.03)
        self.assertEqual(beats, [])
        self.assertIsNone(bpm)
        # very short (a handful of frames) must not raise either
        beats, bpm = sp.detect_beats([10, 12, 11], [10, 11, 10], [10, 10, 11], 0.03)
        self.assertEqual(beats, [])
        self.assertIsNone(bpm)

        sections = sp.detect_sections([], [], [], [], 0.03)
        self.assertEqual(sections, [{"t": 0.0, "energy": 0.0}])
        sections = sp.detect_sections([50, 51], [50, 51], [50, 51], [50, 51], 0.03)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["t"], 0.0)

    def test_analysis_payload_shape(self):
        # Floor, not equality. Retuning any beat/section constant REQUIRES bumping the
        # version, because get_analysis serves cached JSON whenever v matches — so a
        # detection fix silently never reaches already-analysed tracks. An equality
        # assertion here just breaks on every legitimate bump without catching that;
        # a floor keeps the version moving forward and stays honest about why.
        self.assertIsInstance(sp.ANALYSIS_VERSION, int)
        self.assertGreaterEqual(sp.ANALYSIS_VERSION, 5)
        step = 0.03
        n = int(round(10.0 / step))
        amp = [50] * n
        beats, bpm = sp.detect_beats(amp, amp, amp, step)
        sections = sp.detect_sections(amp, amp, amp, amp, step)
        self.assertIsInstance(beats, list)
        self.assertTrue(bpm is None or isinstance(bpm, float))
        self.assertIsInstance(sections, list)
        self.assertTrue(all("t" in s and "energy" in s for s in sections))

    def test_ui_assembles(self):
        from draai.ui import assemble_ui
        html = assemble_ui(os.path.join(ROOT, "ui"))
        self.assertTrue(html.lstrip().lower().startswith("<!doctype html"))
        self.assertIn("</html>", html)
        for marker in ('id="np"', 'id="amp"', 'function tick', 'function boot', '--ac:'):
            self.assertIn(marker, html)

    # -- privacy / security invariants (see docs/technical/security-and-privacy.md) --

    # -- time listened -------------------------------------------------------------

    def _listen_reset(self):
        L = draai.listening
        L._path[0] = os.path.join(self.tmp, "listening.json")
        with sp.listening_lock:
            sp.listening.clear()
        with L._last_lock:
            L._last.clear()
        L._info_path[0] = os.path.join(self.tmp, "trackinfo.json")
        with sp.trackinfo_lock:
            sp.trackinfo.clear()
        with sp.positions_lock:
            sp.positions.clear()
        return L

    def test_observe_records_trackinfo(self):
        L = self._listen_reset()
        A, tid = {"uuid": "A", "ip": "1.1.1.1"}, "a" * 16
        L.observe(A, {"state": "PLAYING", "track_id": tid,
                      "position": "0:10:00", "duration": "1:00:00"}, now=0)
        info = L.trackinfo_snapshot()[tid]
        self.assertEqual(info["dur"], 3600)
        self.assertGreater(info["seen"], 0)
        first_seen = info["seen"]
        with L.trackinfo_lock:
            L.trackinfo[tid]["seen"] = first_seen - 100    # pretend it was a while ago
        L.observe(A, {"state": "PAUSED_PLAYBACK", "track_id": tid,
                      "position": "0:10:00", "duration": "1:00:00"}, now=15)
        self.assertGreaterEqual(L.trackinfo_snapshot()[tid]["seen"], first_seen)
        L.observe(A, {"state": "PLAYING", "track_id": tid,
                      "position": "0:10:15", "duration": ""}, now=30)
        self.assertEqual(L.trackinfo_snapshot()[tid]["dur"], 3600)  # kept
        L.observe(A, {"state": "PLAYING", "track_id": None,
                      "position": "0:01:00", "duration": "0:05:00"}, now=45)
        self.assertEqual(list(L.trackinfo_snapshot()), [tid])       # not ours: ignored
        # stopped / transitioning samples record nothing
        other = "d" * 16
        for st in ("STOPPED", "TRANSITIONING"):
            L.observe(A, {"state": st, "track_id": other,
                          "position": "0:00:00", "duration": "1:00:00"}, now=60)
        self.assertNotIn(other, L.trackinfo_snapshot())

    def test_note_track_only_saves_on_real_change(self):
        L = self._listen_reset()
        tid = "e" * 16
        self.assertTrue(L.note_track(tid, 3600, 1000.0))             # new entry
        self.assertFalse(L.note_track(tid, 3600, 1030.0))            # seen within 60 s: no churn
        self.assertFalse(L.note_track(tid, 0, 1060.0))               # unknown dur, 60 s: still no
        self.assertEqual(L.trackinfo_snapshot()[tid], {"dur": 3600, "seen": 1000.0})
        self.assertTrue(L.note_track(tid, 3600, 1061.0))             # moved on > 60 s
        self.assertTrue(L.note_track(tid, 3700, 1062.0))             # duration changed
        self.assertEqual(L.trackinfo_snapshot()[tid], {"dur": 3700, "seen": 1062.0})

    def test_trackinfo_roundtrip_and_corrupt(self):
        L = self._listen_reset()
        L.note_track("b" * 16, 5400, 1700000000.0)
        L.note_track("c" * 16, 0, 1700000500.0)
        L.write_trackinfo()
        with sp.trackinfo_lock:
            sp.trackinfo.clear()
        L.load_trackinfo(L._info_path[0])
        self.assertEqual(L.trackinfo_snapshot(), {
            "b" * 16: {"dur": 5400, "seen": 1700000000.0},
            "c" * 16: {"dur": 0, "seen": 1700000500.0}})
        with open(L._info_path[0], "w") as f:
            f.write("not json")
        L.load_trackinfo()
        self.assertEqual(L.trackinfo_snapshot(), {})
        # one malformed entry is skipped; the good ones survive
        with open(L._info_path[0], "w") as f:
            json.dump({"b" * 16: {"dur": 5400, "seen": 1700000000.0},
                       "x" * 16: {"dur": "long", "seen": 1.0},
                       "y" * 16: "garbage",
                       "c" * 16: {"dur": 60}}, f)
        L.load_trackinfo()
        self.assertEqual(L.trackinfo_snapshot(), {
            "b" * 16: {"dur": 5400, "seen": 1700000000.0},
            "c" * 16: {"dur": 60, "seen": 0.0}})

    def test_load_positions_drops_bad_values(self):
        path = os.path.join(self.tmp, "positions.json")
        with open(path, "w") as f:
            json.dump({"a" * 16: 1930, "b" * 16: "soon", "c" * 16: None,
                       "d" * 16: "1200", "e" * 16: [1]}, f)
        draai.backends.load_positions(path)
        with sp.positions_lock:
            got = dict(sp.positions)
        self.assertEqual(got, {"a" * 16: 1930, "d" * 16: 1200})
        with open(path, "w") as f:
            f.write("not json")
        draai.backends.load_positions(path)
        self.assertEqual(sp.positions, {})

    def test_api_tracks_resume_dur_seen(self):
        L = self._listen_reset()
        make_mp3(os.path.join(self.tmp, "p.mp3"), "P-Set", "P-DJ")
        make_mp3(os.path.join(self.tmp, "q.mp3"), "Q-Set", "Q-DJ")
        sp.scan_all()
        ids = {t["title"]: t["id"] for t in sp.tracks}
        id1 = ids["P-Set"]
        with sp.positions_lock:
            sp.positions[id1] = 1930
        L.note_track(id1, 5400, 1700000000.0)
        httpd = sp.ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/tracks" % port, timeout=5) as r:
                got = {t["title"]: t for t in json.load(r)["tracks"]}
            p, q = got["P-Set"], got["Q-Set"]
            self.assertEqual((p["resume"], p["dur"], p["seen"]), (1930, 5400, 1700000000.0))
            self.assertEqual((q["resume"], q["dur"], q["seen"]), (0, 0, 0))
            self.assertEqual(p["artist"], "P-DJ")      # existing fields unchanged
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_read_tags_source_url(self):
        url = "https://www.youtube.com/watch?v=abc"
        p = os.path.join(self.tmp, "v.mp3")
        big = b"\x89PNG\r\n\x1a\n" + b"\x01" * (3 * 1024 * 1024)  # large cover first
        make_mp3(p, "V-Set", purl=url, art=big)
        self.assertEqual(sp.read_tags(p).get("source_url"), url)
        p2 = os.path.join(self.tmp, "n.mp3")
        make_mp3(p2, "N-Set")
        self.assertEqual(sp.read_tags(p2).get("source_url", ""), "")
        sp.scan_all()
        by = {t["title"]: t for t in sp.tracks}
        self.assertEqual(by["V-Set"]["source_url"], url)
        self.assertEqual(by["N-Set"]["source_url"], "")

    def test_read_tags_source_url_utf8(self):
        url = "https://www.youtube.com/watch?v=abc&t=\u00e9"
        p = os.path.join(self.tmp, "u.mp3")
        make_mp3(p, "U-Set", purl=url, purl_utf8=True)
        self.assertEqual(sp.read_tags(p).get("source_url"), url)

    def test_source_url_rejects_non_http(self):
        for bad in ("javascript:x", "-oops"):
            p = os.path.join(self.tmp, "bad.mp3")
            make_mp3(p, "B-Set", purl=bad)
            self.assertEqual(sp.read_tags(p).get("source_url", ""), "")
            sp.scan_all()
            self.assertEqual({t["title"]: t for t in sp.tracks}["B-Set"]["source_url"], "")

    def test_api_tracks_has_video(self):
        url = "https://www.youtube.com/watch?v=abc"
        make_mp3(os.path.join(self.tmp, "v.mp3"), "V-Set", purl=url)
        make_mp3(os.path.join(self.tmp, "n.mp3"), "N-Set")
        sp.scan_all()
        httpd = sp.ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/tracks" % port, timeout=5) as r:
                raw = r.read().decode("utf-8")
            got = {t["title"]: t for t in json.loads(raw)["tracks"]}
            self.assertIs(got["V-Set"]["has_video"], True)
            self.assertIs(got["N-Set"]["has_video"], False)
            self.assertNotIn("youtube.com", raw)
            self.assertNotIn("source_url", raw)
        finally:
            httpd.shutdown()
            httpd.server_close()

    # -- set video (yt-dlp stream resolver) ------------------------------------------

    def _fake_ytdlp(self, expire=None, fail=False, info=None):
        """Write an executable stand-in for yt-dlp that prints fixed JSON and
        counts its invocations; point video.find_tool at it. Never the real one."""
        import time
        from draai import video
        with video._cache_lock:
            video._cache.clear()
        exp = int(time.time()) + 7200 if expire is None else expire
        fmts = [
            {"url": "https://cdn.example/a", "vcodec": "none", "acodec": "opus",
             "protocol": "https", "height": None, "ext": "webm"},
            {"url": "https://cdn.example/h.m3u8", "vcodec": "avc1", "acodec": "none",
             "protocol": "m3u8_native", "height": 1080, "fps": 30, "ext": "mp4"},
            {"url": "https://cdn.example/2160?expire=%d&x=1" % exp, "vcodec": "av01.0.12M.08",
             "acodec": "none", "protocol": "https", "height": 2160, "fps": 60, "ext": "mp4",
             "format_id": "401"},
            {"url": "https://cdn.example/1080", "vcodec": "vp9", "acodec": "none",
             "protocol": "https", "height": 1080, "fps": 30, "ext": "webm"},
            {"url": "https://cdn.example/720", "vcodec": "avc1.4d401f", "acodec": None,
             "protocol": "https", "height": 720, "ext": "mp4"},
        ]
        count = os.path.join(self.tmp, "ytdlp-calls")
        script = os.path.join(self.tmp, "fake-yt-dlp")
        with open(script, "w") as f:
            f.write("#!%s\nimport sys\nopen(%r, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                    % (sys.executable, count))
            if fail:
                f.write("sys.exit(1)\n")
            else:
                f.write("print(%r)\n" % json.dumps({"formats": fmts} if info is None else info))
        os.chmod(script, 0o755)
        video.find_tool = lambda name: script if name == "yt-dlp" else None
        self.addCleanup(setattr, video, "find_tool", draai.media.find_tool)

        def calls():
            if not os.path.exists(count):
                return []
            with open(count) as fh:
                return fh.read().splitlines()
        return video, calls

    def _video_track(self, tid, url="https://www.youtube.com/watch?v=abc"):
        sp.tracks_by_id[tid] = {"id": tid, "title": "Set", "path": "/set.mp3",
                                "source_url": url}
        self.addCleanup(sp.tracks_by_id.pop, tid, None)

    def test_video_streams_filters_and_sorts(self):
        video, calls = self._fake_ytdlp()
        self._video_track("v" * 16)
        streams = video.video_streams("v" * 16)
        self.assertEqual([s["height"] for s in streams], [2160, 1080, 720])
        for s in streams:
            self.assertEqual(set(s), {"url", "height", "fps", "vcodec", "ext"})
            self.assertTrue(s["url"].startswith("https://cdn.example/"))
        self.assertEqual(streams[1], {"url": "https://cdn.example/1080", "height": 1080,
                                      "fps": 30, "vcodec": "vp9", "ext": "webm"})
        self.assertEqual(streams[2]["fps"], 0)
        self.assertEqual(calls(), ["-J --no-playlist -- https://www.youtube.com/watch?v=abc"])

    def test_video_streams_cached(self):
        video, calls = self._fake_ytdlp()
        self._video_track("v" * 16)
        a = video.video_streams("v" * 16)
        b = video.video_streams("v" * 16)
        self.assertEqual(a, b)
        self.assertEqual(len(calls()), 1)

    def test_video_cache_expires(self):
        import time
        video, calls = self._fake_ytdlp(expire=int(time.time()) + 300)
        self._video_track("v" * 16)
        video.video_streams("v" * 16)
        video.video_streams("v" * 16)
        self.assertEqual(len(calls()), 2)

    def test_video_errors(self):
        video, calls = self._fake_ytdlp()
        self._video_track("n" * 16, url="")
        for tid in ("n" * 16, "0" * 16):
            with self.assertRaises(video.VideoError) as cm:
                video.video_streams(tid)
            self.assertEqual(cm.exception.status, 404)
            self.assertEqual(str(cm.exception), "This set has no video link.")
        self.assertEqual(calls(), [])
        self._video_track("v" * 16)
        video.find_tool = lambda name: None
        with self.assertRaises(video.VideoError) as cm:
            video.video_streams("v" * 16)
        self.assertEqual((str(cm.exception), cm.exception.status),
                         ("Install yt-dlp to see set videos.", 404))
        video, calls = self._fake_ytdlp(fail=True)
        with self.assertRaises(video.VideoError) as cm:
            video.video_streams("v" * 16)
        self.assertEqual((str(cm.exception), cm.exception.status),
                         ("Couldn't load the video for this set.", 502))
        self.assertEqual(len(calls()), 1)

    def test_video_malformed_ytdlp_output(self):
        for info in ({"formats": 5},
                     {"formats": [{"url": "https://cdn.example/n", "vcodec": "vp9",
                                   "acodec": "none", "protocol": "https", "height": 720,
                                   "fps": float("nan"), "ext": "webm"}]},
                     {"formats": [{"url": "https://cdn.example/i", "vcodec": "vp9",
                                   "acodec": "none", "protocol": "https", "height": 720,
                                   "fps": float("inf"), "ext": "webm"}]}):
            video, calls = self._fake_ytdlp(info=info)
            self._video_track("v" * 16)
            with self.assertRaises(video.VideoError) as cm:
                video.video_streams("v" * 16)
            self.assertEqual((str(cm.exception), cm.exception.status),
                             ("Couldn't load the video for this set.", 502))

    def test_api_video_route(self):
        video, calls = self._fake_ytdlp()
        self._video_track("v" * 16)
        httpd = sp.ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d/api/video?id=" % port
            with urllib.request.urlopen(base + "v" * 16, timeout=10) as r:
                body = json.loads(r.read().decode("utf-8"))
            self.assertEqual([s["height"] for s in body["streams"]], [2160, 1080, 720])
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "0" * 16, timeout=10)
            self.assertEqual(cm.exception.code, 404)
            self.assertEqual(json.loads(cm.exception.read().decode("utf-8")),
                             {"error": "This set has no video link."})
            cm.exception.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_listening_delta_crediting(self):
        L = self._listen_reset()
        A, B = {"uuid": "A", "ip": "1.1.1.1"}, {"uuid": "B", "ip": "1.1.1.2"}
        t1, t2, t3 = "1" * 16, "2" * 16, "3" * 16

        def st(tid, pos, state="PLAYING"):
            return {"state": state, "track_id": tid, "position": sp.sec_to_hms(pos)}
        self.assertEqual(L.observe(A, st(t1, 100), now=0), 0)      # first sight
        self.assertEqual(L.observe(B, st(t1, 10), now=1), 0)       # other speaker
        self.assertEqual(L.observe(A, st(t1, 115), now=15), 15)    # normal tick
        self.assertEqual(L.observe(B, st(t1, 24), now=16), 14)     # tracked separately
        self.assertEqual(L.observe(A, st(t1, 400), now=30), 0)     # seek forward
        self.assertEqual(L.observe(A, st(t1, 415), now=45), 15)
        self.assertEqual(L.observe(A, st(t1, 300), now=60), 0)     # seek back
        self.assertEqual(L.observe(A, st(t1, 300, "PAUSED_PLAYBACK"), now=75), 0)
        self.assertEqual(L.observe(A, st(t1, 300, "PAUSED_PLAYBACK"), now=90), 0)
        self.assertEqual(L.observe(A, st(t1, 305), now=105), 5)    # resumed
        self.assertEqual(L.observe(A, st(t1, 290), now=100), 0)    # older sample lost a race
        self.assertEqual(L.observe(A, st(t2, 8), now=120), 8)      # new track began in window
        self.assertEqual(L.observe(A, st(t3, 500), now=135), 0)    # jumped into a track
        self.assertEqual(L.observe(A, st(t3, 515), now=400), 0)    # stale gap
        self.assertEqual(L.observe(A, {"state": "PLAYING", "track_id": None,
                                       "position": "0:01:00"}, now=415), 0)  # not ours
        self.assertEqual(L.observe(A, st(t3, 530), now=430), 0)    # forgotten -> first sight
        with sp.listening_lock:
            self.assertEqual(dict(sp.listening), {t1: 15 + 14 + 15 + 5, t2: 8})

    def test_listening_persistence_roundtrip(self):
        L = self._listen_reset()
        L.credit("a" * 16, 3600)
        L.credit("a" * 16, 15)
        L.credit("b" * 16, 0)          # nothing credited, nothing stored
        L.write_listening()
        with sp.listening_lock:
            sp.listening.clear()
        L.load_listening()
        self.assertEqual(L.listened_snapshot(), {"a" * 16: 3615})
        with open(L._path[0], "w") as f:
            f.write("{not json")
        L.load_listening()
        self.assertEqual(L.listened_snapshot(), {})

    def test_listening_watch_once_is_safe(self):
        L = self._listen_reset()
        tid = "4" * 16
        asked = []

        def fake_status(spk):
            asked.append(spk["uuid"])
            if spk["uuid"] == "BAD":
                raise OSError("speaker unreachable")
            return {"state": "PLAYING", "track_id": tid, "position": "0:00:30"}
        groups = [{"uuid": "BAD", "ip": "1.1.1.3", "backend": "sonos"},
                  {"uuid": "OK", "ip": "1.1.1.4", "backend": "sonos"},
                  {"uuid": "CAST_none", "ip": "1.1.1.5", "backend": "cast"}]
        old_status, old_speakers = L.get_status, list(sp.speakers)
        L.get_status = fake_status
        sp.speakers[:] = groups
        try:
            # last-seen stamps use the real monotonic clock, so must "now"
            # (a fixed value only works on hosts with a long uptime)
            t = time.monotonic()
            L.watch_once(now=t)        # must not raise; cast w/o session skipped
            self.assertEqual(asked, ["BAD", "OK"])
            asked.clear()
            L.watch_once(now=t + 1)    # OK was just sampled: skipped
            self.assertEqual(asked, ["BAD"])
        finally:
            L.get_status = old_status
            sp.speakers[:] = old_speakers

    def test_api_most_played(self):
        L = self._listen_reset()
        make_mp3(os.path.join(self.tmp, "x.mp3"), "X-Set", "X-DJ")
        make_mp3(os.path.join(self.tmp, "y.mp3"), "Y-Set", "Y-DJ")
        make_mp3(os.path.join(self.tmp, "z.mp3"), "Z-Set", "Z-DJ")
        sp.scan_all()
        ids = {t["title"]: t["id"] for t in sp.tracks}
        L.credit(ids["X-Set"], 600)
        L.credit(ids["Y-Set"], 24000)
        L.credit("f" * 16, 99999)      # no longer in the library: left alone, not listed
        httpd = sp.ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def get(p):
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d%s" % (port, p), timeout=5) as r:
                    return json.load(r)
            mp = get("/api/most_played")
            self.assertEqual([t["title"] for t in mp["tracks"]], ["Y-Set", "X-Set"])
            self.assertEqual(mp["tracks"][0]["listened"], 24000)
            self.assertEqual(mp["tracks"][0]["artist"], "Y-DJ")
            self.assertEqual(mp["total"], 2)
            one = get("/api/most_played?limit=1")
            self.assertEqual([t["id"] for t in one["tracks"]], [ids["Y-Set"]])
            tr = {t["title"]: t["listened"] for t in get("/api/tracks")["tracks"]}
            self.assertEqual(tr, {"X-Set": 600, "Y-Set": 24000, "Z-Set": 0})
        finally:
            httpd.shutdown()


    # -- permanent delete (POST /api/delete) ------------------------------------------

    def _delete_env(self):
        """Two scanned dummy sets, a live server, analysis + saves redirected."""
        import draai.deletion
        L = self._listen_reset()
        make_mp3(os.path.join(self.tmp, "keep.mp3"), "Keep-Set", "K-DJ")
        make_mp3(os.path.join(self.tmp, "gone.mp3"), "Gone-Set", "G-DJ")
        sp.scan_all()
        ids = {t["title"]: t["id"] for t in sp.tracks}
        old = (draai.analysis.ANALYSIS_DIR, draai.backends.save_positions_soon,
               draai.deletion.playing_track_ids, list(sp.speakers))
        draai.analysis.ANALYSIS_DIR = os.path.join(self.tmp, "analysis")
        draai.backends.save_positions_soon = lambda: None    # never write the real positions.json
        sp.speakers[:] = []

        def restore():
            (draai.analysis.ANALYSIS_DIR, draai.backends.save_positions_soon,
             draai.deletion.playing_track_ids, spk) = old
            sp.speakers[:] = spk
        self.addCleanup(restore)
        httpd = sp.ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
        port = httpd.server_address[1]
        old_port = draai.state.server_port
        draai.state.server_port = port                  # as __main__ does after binding
        self.addCleanup(setattr, draai.state, "server_port", old_port)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)             # cleanups run LIFO: shutdown first
        self.addCleanup(httpd.shutdown)
        return L, ids, port

    def _post_delete(self, port, ids, headers):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/delete" % port,
            data=json.dumps({"ids": ids}).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.load(e)

    def _good_headers(self, port):
        return {"Content-Type": "application/json",
                "Origin": "http://127.0.0.1:%d" % port}

    def test_delete_guard_rejects(self):
        import draai.deletion
        _, ids, port = self._delete_env()
        victim = os.path.join(self.tmp, "gone.mp3")
        bad = [
            {"Content-Type": "application/json", "Origin": "http://evil.test"},
            {"Content-Type": "application/json", "Host": "evil.test:%d" % port},
            {"Content-Type": "text/plain", "Origin": "http://127.0.0.1:%d" % port},
        ]
        for headers in bad:
            code, body = self._post_delete(port, [ids["Gone-Set"]], headers)
            self.assertEqual(code, 403, headers)
            self.assertEqual(body["error"], "Deleting only works in DRAAI on this Mac.")
            self.assertTrue(os.path.exists(victim), headers)
            self.assertIn(ids["Gone-Set"], sp.tracks_by_id)
        good = {"Host": "127.0.0.1:%d" % port, **self._good_headers(port)}
        self.assertIsNone(draai.deletion.delete_allowed("127.0.0.1", good, port))
        self.assertEqual(draai.deletion.delete_allowed("192.168.1.9", good, port),
                         "Deleting only works in DRAAI on this Mac.")
        for host in ("localhost:%d" % port, "[::1]:%d" % port):
            self.assertIsNone(draai.deletion.delete_allowed(
                "::1", dict(good, Host=host, Origin="http://[::1]:%d" % port), port))
        no_host = {k: v for k, v in good.items() if k != "Host"}
        self.assertEqual(draai.deletion.delete_allowed("127.0.0.1", no_host, port),
                         "Deleting only works in DRAAI on this Mac.")
        self.assertEqual(draai.deletion.delete_allowed("127.0.0.1", dict(good, Origin="null"), port),
                         "Deleting only works in DRAAI on this Mac.")

    def test_delete_removes_file_and_state(self):
        L, ids, port = self._delete_env()
        gone, keep = ids["Gone-Set"], ids["Keep-Set"]
        with sp.positions_lock:
            sp.positions[gone] = 1200
            sp.positions[keep] = 900
        L.credit(gone, 600)
        L.credit(keep, 300)
        L.note_track(gone, 3600, 1700000000.0)
        L.note_track(keep, 3600, 1700000000.0)
        os.makedirs(draai.analysis.ANALYSIS_DIR)
        for tid in (gone, keep):
            with open(os.path.join(draai.analysis.ANALYSIS_DIR, tid + ".json"), "w") as f:
                f.write("{}")
        # the id twice: de-duplicated, so the second copy isn't a "no longer in library" failure
        code, body = self._post_delete(port, [gone, gone], self._good_headers(port))
        self.assertEqual((code, body), (200, {"deleted": 1, "failed": []}))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "gone.mp3")))
        self.assertNotIn(gone, sp.tracks_by_id)
        self.assertNotIn(gone, [t["id"] for t in sp.tracks])
        self.assertNotIn(gone, sp.positions)
        self.assertNotIn(gone, sp.listening)
        self.assertNotIn(gone, sp.trackinfo)
        self.assertFalse(os.path.exists(os.path.join(draai.analysis.ANALYSIS_DIR, gone + ".json")))
        # the other set is untouched
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "keep.mp3")))
        self.assertIn(keep, sp.tracks_by_id)
        self.assertEqual((sp.positions[keep], sp.listening[keep]), (900, 300))
        self.assertIn(keep, sp.trackinfo)
        self.assertTrue(os.path.exists(os.path.join(draai.analysis.ANALYSIS_DIR, keep + ".json")))
        # unknown id: refused, nothing else happens
        code, body = self._post_delete(port, ["0" * 16], self._good_headers(port))
        self.assertEqual(body["deleted"], 0)
        self.assertEqual(body["failed"][0]["error"], "That track is no longer in your library.")

    def test_playing_track_ids(self):
        import draai.deletion as D
        old = (D.status_and_observe, list(sp.speakers))
        self.addCleanup(lambda: (setattr(D, "status_and_observe", old[0]),
                                 sp.speakers.__setitem__(slice(None), old[1])))
        asked = []

        def fake(spk):
            asked.append(spk["uuid"])
            if spk["uuid"] == "boom":
                raise OSError("unreachable")
            return {"P": {"state": "PLAYING", "track_id": "a" * 16},
                    "T": {"state": "TRANSITIONING", "track_id": "b" * 16},
                    "Z": {"state": "PAUSED_PLAYBACK", "track_id": "c" * 16}}[spk["uuid"]]
        D.status_and_observe = fake
        sp.speakers[:] = [{"uuid": "P", "ip": "1.1.1.1"}, {"uuid": "T", "ip": "1.1.1.2"},
                          {"uuid": "Z", "ip": "1.1.1.3"}, {"uuid": "boom", "ip": "1.1.1.4"},
                          {"uuid": "C", "ip": "1.1.1.5", "backend": "cast"}]
        self.assertEqual(D.playing_track_ids(), {"a" * 16, "b" * 16})
        self.assertNotIn("C", asked)           # Cast without a live session: skipped

    def test_delete_refuses_playing_track(self):
        import draai.deletion
        _, ids, port = self._delete_env()
        id1 = ids["Gone-Set"]
        draai.deletion.playing_track_ids = lambda: {id1}
        code, body = self._post_delete(port, [id1], self._good_headers(port))
        self.assertEqual(body["deleted"], 0)
        self.assertEqual(body["failed"][0]["id"], id1)
        self.assertIn("playing right now", body["failed"][0]["error"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "gone.mp3")))
        self.assertIn(id1, sp.tracks_by_id)

    def test_delete_refuses_outside_folders(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        target = os.path.join(outside, "elsewhere.mp3")
        make_mp3(target, "Outside-Set", "O-DJ")
        os.symlink(target, os.path.join(self.tmp, "link.mp3"))
        _, ids, port = self._delete_env()
        tid = ids["Outside-Set"]
        code, body = self._post_delete(port, [tid], self._good_headers(port))
        self.assertEqual(body["deleted"], 0)
        self.assertIn("outside your music folders", body["failed"][0]["error"])
        self.assertTrue(os.path.exists(target))
        self.assertIn(tid, sp.tracks_by_id)

    def test_engine_imports_only_stdlib(self):
        """Supply-chain guard: the engine must import only the Python standard
        library — no pip dependency, ever. A third-party package in the import
        graph is exactly what could exfiltrate a user's data, so adding one must
        turn the suite red rather than ship silently."""
        import ast
        pkg = os.path.dirname(draai.__file__)
        roots = set()
        for fn in sorted(os.listdir(pkg)):
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(pkg, fn), encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), fn)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        roots.add(a.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots.add(node.module.split(".")[0])
        roots.discard("draai")
        stdlib = getattr(sys, "stdlib_module_names", None)
        offenders = []
        for name in sorted(roots):
            if stdlib is not None and name in stdlib:
                continue
            try:
                mod = importlib.import_module(name)
            except ImportError:
                offenders.append(name + " (unresolvable)")
                continue
            path = getattr(mod, "__file__", "") or ""
            if "site-packages" in path or "dist-packages" in path:
                offenders.append("%s -> %s" % (name, path))
        self.assertEqual(offenders, [], "non-stdlib imports in engine: %r" % offenders)

    def test_ui_loads_nothing_from_the_internet(self):
        """No-phone-home guard: the served UI must load nothing external — no
        CDN, web font, script, or stylesheet — and every request it makes goes
        to the local engine. This is what keeps 'the browser never talks to the
        internet' true as the UI grows."""
        import re
        from draai.ui import assemble_ui
        ui = assemble_ui(os.path.join(ROOT, "ui"))
        # nothing is loaded over TLS from a remote host, and no external-asset patterns
        self.assertNotIn("https://", ui)
        for pat in ("@import", "<script src", "url(http", 'src="http', "src='http"):
            self.assertNotIn(pat, ui, "external-resource pattern %r found in UI" % pat)
        # <link> only for the app manifest and icons, served by the local engine
        for tag in re.findall(r"<link\b[^>]*>", ui):
            rel = re.search(r'rel="([^"]+)"', tag)
            href = re.search(r'href="([^"]+)"', tag)
            self.assertTrue(rel and rel.group(1) in ("manifest", "icon", "apple-touch-icon"),
                            "unexpected <link> in UI: %r" % tag)
            self.assertTrue(href and href.group(1).startswith("/")
                            and not href.group(1).startswith("//"),
                            "<link> to a non-local URL: %r" % tag)
        # every fetch() targets a relative URL (the local engine), never an absolute host
        for target in re.findall(r"""fetch\(\s*[`"']([^`"']*)""", ui):
            self.assertTrue(target.startswith("/") or target.startswith("."),
                            "fetch() to a non-relative URL: %r" % target)
        # the only http:// tokens permitted are XML namespaces and offline placeholders
        for url in re.findall(r"http://[^\s\"'`)>]+", ui):
            ok = url.startswith("http://www.w3.org/") or "draai.local" in url or "192.168." in url
            self.assertTrue(ok, "unexpected http:// URL in UI: %r" % url)

    def test_root_serves_assembled_ui(self):
        httpd = sp.ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5) as r:
                body = r.read().decode("utf-8")
                self.assertIn('id="amp"', body)
                self.assertIn("function tick", body)
        finally:
            httpd.shutdown()

    # -- this Mac's own outputs (local backend) -------------------------------

    def test_local_outputs_filter_and_dedupe(self):
        draai.local._enumerate_coreaudio = lambda: FAKE_DEVS
        names = [o["name"] for o in draai.local.list_outputs()]
        self.assertEqual(names, ["LG HDR 4K", "LG HDR 4K (2)", "Jabra Link 370", "MacBook Pro Speakers"])

    def test_local_discover_speaker_shape(self):
        draai.local._enumerate_coreaudio = lambda: FAKE_DEVS
        sp_ = {s["uuid"]: s for s in draai.local.local_discover()}
        j = sp_["MAC_JABRA-OUT"]
        self.assertEqual((j["backend"], j["ip"], j["is_group"]), ("local", None, False))
        self.assertEqual(j["members"], [{"uuid": "MAC_JABRA-OUT", "name": "Jabra Link 370", "ip": None}])
        self.assertEqual(draai.local.device_index("JABRA-OUT"), 2)   # index counts inputs too
        self.assertIsNone(draai.local.device_index("GONE"))

    def test_local_discover_empty_without_ffmpeg(self):
        draai.local._enumerate_coreaudio = lambda: FAKE_DEVS
        draai.local._available = lambda: False
        self.assertEqual(draai.local.local_discover(), [])

    # -- local playback ---------------------------------------------------------

    def _local_setup(self, devs=None, procs_rc=None, n_tracks=2):
        """Fake enumerator + recording spawner. Returns (spk, ids, spawned)."""
        draai.local._enumerate_coreaudio = lambda: (devs if devs is not None else FAKE_DEVS)
        spawned = []

        def fake_spawn(argv):
            rc = procs_rc(len(spawned)) if procs_rc else None
            pr = FakeProc(argv, rc=rc)
            spawned.append(pr)
            return pr
        draai.local._spawn = fake_spawn
        self.addCleanup(setattr, draai.local, "_spawn", draai.local.__dict__["_spawn"])
        ids = []
        for i in range(n_tracks):
            tid = ("%016x" % (0xA0 + i))
            sp.tracks_by_id[tid] = {"id": tid, "title": "T%d" % i, "ext": ".mp3",
                                    "path": "/m/t%d.mp3" % i}
            ids.append(tid)
        self.addCleanup(lambda: [sp.tracks_by_id.pop(t, None) for t in ids])
        spk = {"uuid": "MAC_JABRA-OUT", "name": "Jabra Link 370", "ip": None,
               "backend": "local", "is_group": False}
        return spk, ids, spawned

    def test_local_build_argv(self):
        self.assertEqual(
            draai.local.build_argv("ffmpeg", "/m/a.mp3", 12.5, 30, 2),
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-ss", "12.500", "-i", "/m/a.mp3", "-vn", "-af", "volume=0.300",
             "-f", "audiotoolbox", "-audio_device_index", "2",
             "-progress", "pipe:1", "-"])

    def test_local_play_spawns_on_jabra_index(self):
        spk, ids, spawned = self._local_setup()
        draai.local.local_play_tracks(spk, ids)
        self.assertEqual(len(spawned), 1)
        a = spawned[0].argv
        self.assertEqual(a[a.index("-audio_device_index") + 1], "2")
        self.assertIn("/m/t0.mp3", a)
        st = draai.local.local_get_status(spk)
        self.assertEqual((st["state"], st["track_no"]), ("PLAYING", 1))

    def test_local_advance_on_clean_exit(self):
        spk, ids, spawned = self._local_setup()
        draai.local.local_play_tracks(spk, ids)
        spawned[0].finish(0)
        self.assertTrue(wait_for(lambda: len(spawned) == 2))
        self.assertIn("/m/t1.mp3", spawned[1].argv)
        spawned[1].finish(0)
        self.assertTrue(wait_for(lambda: draai.local.local_get_status(spk)["state"] == "STOPPED"))

    def test_local_pause_resume_respawns_at_position(self):
        spk, ids, spawned = self._local_setup()
        draai.local.local_play_tracks(spk, ids)
        spawned[0].feed("out_time_us=61000000\n")
        self.assertTrue(wait_for(lambda: draai.local.local_get_status(spk)["position"] == "0:01:01"))
        draai.local.local_cmd(spk, "pause")
        self.assertTrue(spawned[0].killed)
        st = draai.local.local_get_status(spk)
        self.assertEqual((st["state"], st["position"]), ("PAUSED_PLAYBACK", "0:01:01"))
        time.sleep(0.05)
        self.assertEqual(len(spawned), 1)           # our own kill must not advance
        draai.local.local_cmd(spk, "resume")
        self.assertEqual(len(spawned), 2)
        a = spawned[1].argv
        self.assertEqual(a[a.index("-ss") + 1], "61.000")
        self.assertIn("/m/t0.mp3", a)

    def test_local_volume_debounced(self):
        spk, ids, spawned = self._local_setup()
        draai.local.local_play_tracks(spk, ids)
        for v in range(40, 50):
            draai.local.local_set_volume(spk, v)
        time.sleep(0.45)
        self.assertEqual(len(spawned), 2)
        self.assertIn("volume=0.490", spawned[1].argv)
        self.assertEqual(sp.config["local_volume"]["MAC_JABRA-OUT"], 49)

    def test_local_device_gone_stops_with_sentence(self):
        devs = [d for d in FAKE_DEVS if d["uid"] != "JABRA-OUT"]
        spk, ids, spawned = self._local_setup(devs=devs)
        draai.local.local_play_tracks(spk, ids)
        st = draai.local.local_get_status(spk)
        self.assertEqual(spawned, [])
        self.assertEqual(st["state"], "STOPPED")
        self.assertEqual(st["error"], "The Jabra Link 370 is no longer connected.")

    def test_local_unplugged_mid_track_stops_once(self):
        spk, ids, spawned = self._local_setup()
        draai.local.local_play_tracks(spk, ids)
        draai.local._enumerate_coreaudio = lambda: [d for d in FAKE_DEVS if d["uid"] != "JABRA-OUT"]
        spawned[0].finish(1)
        self.assertTrue(wait_for(lambda: draai.local.local_get_status(spk)["state"] == "STOPPED"))
        time.sleep(0.05)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(draai.local.local_get_status(spk)["error"],
                         "The Jabra Link 370 is no longer connected.")

    def test_local_all_failing_tracks_stop(self):
        spk, ids, spawned = self._local_setup(procs_rc=lambda n: 1)
        draai.local.local_play_tracks(spk, ids)
        self.assertTrue(wait_for(lambda: draai.local.local_get_status(spk)["state"] == "STOPPED"))
        time.sleep(0.05)
        self.assertEqual(len(spawned), 2)
        self.assertEqual(draai.local.local_get_status(spk)["error"],
                         "Couldn't play that file \u2014 skipped it.")

    def test_local_queue_semantics(self):
        spk, ids, spawned = self._local_setup(n_tracks=3)
        a, b, c = ids
        draai.local.local_play_tracks(spk, [a, b])
        draai.local.local_enqueue_tracks(spk, [c], play_next=True)
        self.assertEqual([i["id"] for i in draai.local.local_browse_queue(spk)[0]], [a, c, b])
        p = draai.local.players["MAC_JABRA-OUT"]
        draai.local.local_queue_move(spk, 1, 3)             # move the playing track down
        self.assertEqual((p.ids, p.idx), ([c, b, a], 2))
        draai.local.local_queue_remove(spk, 1)              # remove before idx
        self.assertEqual((p.ids, p.idx), ([b, a], 1))
        self.assertEqual(len(spawned), 1)                   # none of that respawned

    def test_local_resume_saved_position(self):
        spk, ids, spawned = self._local_setup()
        with sp.positions_lock:
            sp.positions[ids[0]] = 600
        self.addCleanup(sp.positions.clear)
        draai.local.local_play_tracks(spk, ids)
        a = spawned[0].argv
        self.assertEqual(a[a.index("-ss") + 1], "595.000")

    def test_local_shutdown_kills_children(self):
        spk, ids, spawned = self._local_setup()
        draai.local.local_play_tracks(spk, ids)
        draai.local.local_shutdown()
        self.assertTrue(all(p.killed for p in spawned))

    # -- local backend wiring ---------------------------------------------------

    def test_local_refresh_splices_without_touching_sonos(self):
        son = {"uuid": "RINCON_A", "name": "Kantoor", "ip": "192.168.1.50", "backend": "sonos"}
        sp.speakers[:] = [son]
        self.addCleanup(sp.speakers.clear)
        draai.local._enumerate_coreaudio = lambda: FAKE_DEVS
        draai.backends.refresh_local_speakers()
        self.assertEqual(sp.speakers[0], son)
        self.assertEqual([s["backend"] for s in sp.speakers], ["sonos"] + ["local"] * 4)
        draai.local._enumerate_coreaudio = lambda: [d for d in FAKE_DEVS if d["uid"] != "JABRA-OUT"]
        draai.backends.refresh_local_speakers()
        self.assertEqual(sp.speakers[0], son)
        self.assertNotIn("MAC_JABRA-OUT", [s["uuid"] for s in sp.speakers])
        self.assertEqual(len(sp.speakers), 4)

    def test_local_only_is_not_no_speakers(self):
        draai.backends.ssdp_discover = lambda timeout=3.0: set()
        draai.local._enumerate_coreaudio = lambda: FAKE_DEVS
        found, err = draai.backends.refresh_speakers()
        self.assertEqual((len(found), err), (4, None))

    def test_local_dispatch_play_and_cmd(self):
        make_mp3(os.path.join(self.tmp, "l.mp3"), "L-Track")
        sp.scan_all()
        spk, _ids, spawned = self._local_setup(n_tracks=0)
        sp.speakers[:] = [dict(spk, members=[])]
        self.addCleanup(sp.speakers.clear)
        draai.server.prefetch_analysis = lambda ids: None
        tid = sp.tracks[0]["id"]
        httpd = sp.ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def post(path, body):
                req = urllib.request.Request(
                    "http://127.0.0.1:%d%s" % (port, path), data=json.dumps(body).encode(),
                    headers=self._good_headers(port))
                with urllib.request.urlopen(req, timeout=5) as r:
                    return json.load(r)

            def status():
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/api/status?speaker=MAC_JABRA-OUT" % port, timeout=5) as r:
                    return json.load(r)
            post("/api/play", {"speaker": "MAC_JABRA-OUT", "ids": [tid]})
            self.assertEqual(len(spawned), 1)
            self.assertEqual(status()["state"], "PLAYING")
            post("/api/cmd", {"speaker": "MAC_JABRA-OUT", "action": "pause"})
            self.assertEqual(status()["state"], "PAUSED_PLAYBACK")
            self.assertEqual(post("/api/cmd", {"speaker": "MAC_JABRA-OUT", "action": "sleep", "value": 5}),
                             {"ok": True})
            draai.local.players["MAC_JABRA-OUT"].set_sleep(0)      # don't leave the timer running
            self.assertEqual(self.soap.calls, [])
        finally:
            httpd.shutdown()

    def test_local_eq_and_group_refuse_with_sentence(self):
        spk = {"uuid": "MAC_JABRA-OUT", "name": "Jabra Link 370", "ip": None, "backend": "local"}
        with self.assertRaises(RuntimeError) as cm:
            draai.backends.get_eq(spk)
        self.assertEqual(str(cm.exception), "The equalizer isn't available for outputs on this Mac.")
        sp.speakers[:] = [dict(spk, members=[])]
        self.addCleanup(sp.speakers.clear)
        with self.assertRaises(RuntimeError) as cm:
            draai.backends.group_join("MAC_JABRA-OUT", "RINCON_X")
        self.assertEqual(str(cm.exception), "Outputs on this Mac can't be grouped.")

    def test_local_status_notes_position(self):
        spk, ids, spawned = self._local_setup()
        sp.tracks_by_id[ids[0]]["duration"] = 3600
        draai.local.local_play_tracks(spk, ids)
        spawned[0].feed("out_time_us=700000000\n")
        self.assertTrue(wait_for(lambda: draai.local.local_get_status(spk)["position"] == "0:11:40"))
        self.addCleanup(sp.positions.clear)
        st = draai.backends.get_status(dict(spk, backend="local"))
        self.assertEqual(st["track_id"], ids[0])
        self.assertEqual(sp.positions[ids[0]], 700)

    def test_watcher_skips_idle_local(self):
        L = draai.listening
        spk = {"uuid": "MAC_JABRA-OUT", "name": "Jabra", "ip": None, "backend": "local"}
        old_status, old_speakers = L.get_status, list(sp.speakers)
        L.get_status = lambda s: self.fail("asked an idle Mac output")
        sp.speakers[:] = [spk]
        try:
            L.watch_once(now=time.monotonic())
        finally:
            L.get_status = old_status
            sp.speakers[:] = old_speakers


if __name__ == "__main__":
    unittest.main(verbosity=2)
