"""HTTP server: the Handler (JSON API + media/UI serving) and QR helper."""
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

from draai import __version__, state
from draai.constants import AUDIO_EXTS
from draai.state import config, speakers, state_lock, tracks, tracks_by_id, yt_jobs
from draai.config import save_config
from draai.media import local_ip_facing
from draai.library import get_art, scan_all, scan_folder
from draai.analysis import get_analysis, prefetch_analysis
from draai.youtube import YT_URL_RE, start_youtube_job, yt_available
from draai.playlists import delete_playlist, list_playlists, load_playlist, save_playlist
from draai.cast import cast_cmd
from draai.backends import (avt, browse_queue, enqueue_tracks, get_eq, get_rooms,
    get_status, group_join, group_leave, play_tracks, queue_jump, queue_move,
    queue_remove, refresh_speakers, seek_to, set_eq, set_room_volume, set_shuffle,
    set_sleep, set_volume, speaker_by_uuid)


def _load_ui():
    """The web UI: an external player_ui.html in the cwd wins (handy for live
    editing); else the copy embedded in the package (works inside a .pyz);
    else the built-in PAGE fallback."""
    ext = os.path.join(os.getcwd(), "player_ui.html")
    if os.path.isfile(ext):
        try:
            with open(ext, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    try:
        import importlib.resources as res
        return res.files("draai").joinpath("player_ui.html").read_text("utf-8")
    except Exception:
        return PAGE



def reveal_in_finder(track_id):
    """Open Finder at a library track's location. Path-guarded."""
    with state_lock:
        t = tracks_by_id.get(track_id or "")
    if not t:
        raise RuntimeError("Unknown track.")
    real = os.path.realpath(t["path"])
    roots = [os.path.realpath(os.path.expanduser(f))
             for f in config.get("folders", [])]
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        raise RuntimeError("That file is outside the library.")
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", real], timeout=10)
        else:
            subprocess.run(["xdg-open", os.path.dirname(real)], timeout=10)
    except Exception as e:
        raise RuntimeError("Could not open the file browser: %s" % e)


_QR_TOTAL = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134}


_QR_EC = {1: 7, 2: 10, 3: 15, 4: 20, 5: 26}


_QR_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30]}


_GF_EXP = [0] * 512


_GF_LOG = [0] * 256


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
            data = _load_ui().encode("utf-8")
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
                        "added": t.get("added", 0),
                        "dir": os.path.dirname(t["path"]),
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
            self.send_json({"url": "http://%s:%d" % (ip, state.server_port)})
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
        elif path == "/api/playlists":
            self.send_json({"playlists": list_playlists()})
        elif path == "/api/prefs":
            self.send_json(config.get("ui", {}))
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
                n = enqueue_tracks(spk, ids,
                                   play_next=bool(body.get("next")))
                prefetch_analysis(ids)
                self.send_json({"queued": n})
            elif path == "/api/queue_move":
                spk = speaker_by_uuid(body.get("speaker") or "")
                if not spk:
                    self.send_json({"error": "Pick a speaker first."}, 400)
                    return
                queue_move(spk, body.get("from") or 0, body.get("to") or 0)
                self.send_json({"ok": True})
            elif path == "/api/playlist_save":
                spk = speaker_by_uuid(body.get("speaker") or "")
                if not spk:
                    self.send_json({"error": "Pick a speaker first."}, 400)
                    return
                n = save_playlist(spk, body.get("name") or "")
                self.send_json({"saved": n,
                                "playlists": list_playlists()})
            elif path == "/api/playlist_load":
                spk = speaker_by_uuid(body.get("speaker") or "")
                if not spk:
                    self.send_json({"error": "Pick a speaker first."}, 400)
                    return
                ids, total = load_playlist(body.get("name") or "")
                if not ids:
                    self.send_json({"error": "None of that playlist's "
                                    "files are in the library."}, 404)
                    return
                mode = body.get("mode") or "play"
                if mode == "play":
                    n = play_tracks(spk, ids)
                elif mode == "next":
                    n = enqueue_tracks(spk, ids, play_next=True)
                else:
                    n = enqueue_tracks(spk, ids)
                prefetch_analysis(ids)
                self.send_json({"queued": n, "found": len(ids),
                                "total": total})
            elif path == "/api/playlist_delete":
                delete_playlist(body.get("name") or "")
                self.send_json({"playlists": list_playlists()})
            elif path == "/api/prefs":
                ui = config.setdefault("ui", {})
                for k, v in (body or {}).items():
                    if v is None:
                        ui.pop(k, None)
                    else:
                        ui[k] = v
                save_config()
                self.send_json(ui)
            elif path == "/api/reveal":
                reveal_in_finder(body.get("id") or "")
                self.send_json({"ok": True})
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
                if spk.get("backend") == "cast" and action in (
                        "pause", "resume", "stop", "next", "prev", "clearqueue"):
                    cast_cmd(spk, action)
                elif action == "pause":
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
                    if spk.get("backend") == "cast":
                        raise RuntimeError("Sleep timer isn't available on Chromecast.")
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
