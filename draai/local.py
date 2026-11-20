"""This Mac's own audio outputs as DRAAI rooms (backend "local").

Outputs are enumerated through CoreAudio via ctypes (stdlib only) and played
with the optional ffmpeg's `audiotoolbox` output, one child process per room.
Never touches the system default output or the device's hardware volume.
"""
import random
import subprocess
import sys
import threading
import time

from draai import config as _config
from draai.constants import RESUME_MIN_POS
from draai.media import find_tool
from draai.state import config, tracks_by_id, trackinfo, positions, positions_lock
from draai.util import sec_to_hms

_FFMPEG_MISSING = "Playing on this Mac needs ffmpeg."
_SKIPPED = "Couldn't play that file \u2014 skipped it."
_CANT_CMD = "That action isn't available for outputs on this Mac."
DEFAULT_VOLUME = 30
VOLUME_DEBOUNCE = 0.3       # seconds; a dragged slider restarts ffmpeg once
FAST_FAIL_SECS = 1.0        # non-zero exit this soon after spawn = unplayable file


def _available():
    return sys.platform == "darwin" and find_tool("ffmpeg") is not None


def _enumerate_coreaudio():
    """Every CoreAudio device in `dev#` order:
    [{"index", "uid", "name", "out_channels"}]. [] on any error."""
    if sys.platform != "darwin":
        return []
    try:
        import ctypes
        import ctypes.util
        import struct

        CA = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
        CF = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))

        def fourcc(s):
            return struct.unpack(">I", s.encode())[0]

        class Addr(ctypes.Structure):
            _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                        ("elem", ctypes.c_uint32)]

        CA.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(Addr), ctypes.c_uint32,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        CA.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        CA.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(Addr), ctypes.c_uint32,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        CA.AudioObjectGetPropertyData.restype = ctypes.c_int32
        CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                          ctypes.c_long, ctypes.c_uint32]
        CF.CFStringGetCString.restype = ctypes.c_bool
        CF.CFRelease.argtypes = [ctypes.c_void_p]

        glob, outp = fourcc("glob"), fourcc("outp")

        def get(obj, sel, scope=glob):
            a = Addr(fourcc(sel), scope, 0)
            size = ctypes.c_uint32(0)
            if CA.AudioObjectGetPropertyDataSize(obj, ctypes.byref(a), 0, None,
                                                 ctypes.byref(size)) != 0:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if CA.AudioObjectGetPropertyData(obj, ctypes.byref(a), 0, None,
                                             ctypes.byref(size), buf) != 0:
                return None
            return buf.raw[:size.value]

        def cfstring(obj, sel):
            raw = get(obj, sel)
            if not raw or len(raw) < 8:
                return ""
            ref = ctypes.c_void_p.from_buffer_copy(raw[:8]).value
            if not ref:
                return ""
            out = ctypes.create_string_buffer(1024)
            ok = CF.CFStringGetCString(ref, out, 1024, 0x08000100)
            CF.CFRelease(ref)
            return out.value.decode("utf-8", "replace") if ok else ""

        raw = get(1, "dev#")
        if not raw:
            return []
        ids = struct.unpack("<%dI" % (len(raw) // 4), raw[:len(raw) // 4 * 4])
        devs = []
        for i, dev in enumerate(ids):
            lay = get(dev, "slay", outp)
            channels = 0
            if lay and len(lay) >= 4:
                n = struct.unpack("<I", lay[:4])[0]
                for b in range(n):
                    off = 8 + b * 16    # buffers start 8-aligned (mData pointer)
                    if off + 4 <= len(lay):
                        channels += struct.unpack("<I", lay[off:off + 4])[0]
            devs.append({"index": i, "uid": cfstring(dev, "uid "),
                         "name": cfstring(dev, "lnam"), "out_channels": channels})
        return devs
    except Exception:
        return []


def list_outputs():
    """Output-capable devices: [{"uid", "name", "index"}], names de-duplicated
    (" (2)", " (3)" in enumeration order)."""
    seen = {}
    outs = []
    for d in _enumerate_coreaudio():
        if d.get("out_channels", 0) <= 0 or not d.get("uid"):
            continue
        name = d.get("name") or "Audio output"
        n = seen.get(name, 0) + 1
        seen[name] = n
        outs.append({"uid": d["uid"], "index": d["index"],
                     "name": name if n == 1 else "%s (%d)" % (name, n)})
    return outs


def device_index(uid):
    """Fresh lookup: position of this UID among ALL CoreAudio devices (the
    index ffmpeg's audiotoolbox wants), or None if it's gone."""
    for d in _enumerate_coreaudio():
        if d.get("uid") == uid:
            return d["index"]
    return None


def local_discover():
    if not _available():
        return []
    out = []
    for o in list_outputs():
        u = "MAC_" + o["uid"]
        out.append({"uuid": u, "name": o["name"], "ip": None,
                    "backend": "local", "is_group": False,
                    "members": [{"uuid": u, "name": o["name"], "ip": None}]})
    return out


# ----------------------------------------------------------------------------
# playback
# ----------------------------------------------------------------------------

def _spawn(argv):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)


def build_argv(ffmpeg, path, start, volume, index):
    return [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-ss", "%.3f" % start, "-i", path, "-vn",
            "-af", "volume=%.3f" % (volume / 100.0),
            "-f", "audiotoolbox", "-audio_device_index", str(index),
            "-progress", "pipe:1", "-"]


players = {}
_players_lock = threading.Lock()


def has_player(uuid):
    return uuid in players


def _volume_for(uuid):
    try:
        return max(0, min(100, int(config.get("local_volume", {}).get(uuid, DEFAULT_VOLUME))))
    except Exception:
        return DEFAULT_VOLUME


class LocalPlayer:
    """One Mac output: an in-memory queue plus at most one ffmpeg child."""

    def __init__(self, spk):
        self.uuid = spk["uuid"]
        self.name = spk["name"]
        self.uid = self.uuid[len("MAC_"):]
        self.lock = threading.RLock()
        self.ids = []
        self.idx = 0
        self.state = "STOPPED"
        self.pos = 0.0              # seconds into the current track
        self.start = 0.0            # offset the current ffmpeg was started at
        self.proc = None
        self.gen = 0                # bumped on every spawn/kill; stale readers do nothing
        self.spawned_at = 0.0
        self.fail_streak = 0
        self.error = None
        self.vol_timer = None
        self.sleep_timer = None

    # -- process control ------------------------------------------------------

    def _kill(self):
        self.gen += 1
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass

    def _halt(self, error=None):
        self._kill()
        self.state = "STOPPED"
        self.pos = 0.0
        if error:
            self.error = error

    def _track(self):
        if self.ids and 0 <= self.idx < len(self.ids):
            return tracks_by_id.get(self.ids[self.idx])
        return None

    def _spawn_current(self, start):
        """(Re)start ffmpeg on the current track at `start` seconds."""
        self._kill()
        t = self._track()
        if not t:
            self.state = "STOPPED"
            return
        if not _available():
            return self._halt(_FFMPEG_MISSING)
        index = device_index(self.uid)          # fresh every time: hot-plug safe
        if index is None:
            return self._halt("The %s is no longer connected." % self.name)
        argv = build_argv(find_tool("ffmpeg") or "ffmpeg", t["path"], start,
                          _volume_for(self.uuid), index)
        try:
            proc = _spawn(argv)
        except Exception:
            return self._halt(_FFMPEG_MISSING)
        self.proc = proc
        self.start = start
        self.pos = start
        self.state = "PLAYING"
        self.spawned_at = time.monotonic()
        gen = self.gen
        threading.Thread(target=self._reader, args=(gen, proc), daemon=True).start()

    def _reader(self, gen, proc):
        try:
            for line in proc.stdout:
                if line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    with self.lock:
                        if gen == self.gen:
                            self.pos = self.start + max(0, us) / 1e6
            rc = proc.wait()
        except Exception:
            rc = proc.returncode if proc.returncode is not None else 1
        self._on_exit(gen, rc)

    def _on_exit(self, gen, rc):
        with self.lock:
            if gen != self.gen:
                return                          # we killed it / a newer spawn owns the state
            self.proc = None
            if rc == 0:
                self.fail_streak = 0
                return self._advance()
            if device_index(self.uid) is None:
                return self._halt("The %s is no longer connected." % self.name)
            if time.monotonic() - self.spawned_at < FAST_FAIL_SECS:
                self.fail_streak += 1
                self.error = _SKIPPED
                if self.fail_streak >= len(self.ids):
                    return self._halt()
                return self._advance()
            self.error = _SKIPPED
            self._advance()

    def _advance(self):
        if self.idx + 1 < len(self.ids):
            self._play_index(self.idx + 1)
        else:
            self._halt()

    def _play_index(self, i, start=None):
        self.idx = i
        if start is None:
            start = 0.0
            t = self._track()
            if t:
                with positions_lock:
                    saved = positions.get(t["id"], 0)
                if saved > RESUME_MIN_POS:
                    start = max(0.0, saved - 5.0)
        self._spawn_current(start)

    # -- volume / sleep timers --------------------------------------------------

    def _respawn_now(self):
        with self.lock:
            self.vol_timer = None
            if self.state == "PLAYING":
                self._spawn_current(self.pos)

    def set_volume(self, value):
        v = max(0, min(100, int(float(value))))
        config.setdefault("local_volume", {})[self.uuid] = v
        _config.save_config()
        with self.lock:
            if self.vol_timer:
                self.vol_timer.cancel()
            if self.state == "PLAYING":
                self.vol_timer = threading.Timer(VOLUME_DEBOUNCE, self._respawn_now)
                self.vol_timer.daemon = True
                self.vol_timer.start()

    def set_sleep(self, minutes):
        with self.lock:
            if self.sleep_timer:
                self.sleep_timer.cancel()
                self.sleep_timer = None
            if minutes and float(minutes) > 0:
                self.sleep_timer = threading.Timer(float(minutes) * 60, self.stop)
                self.sleep_timer.daemon = True
                self.sleep_timer.start()

    # -- transport ------------------------------------------------------------

    def play(self, ids):
        with self.lock:
            self.ids = list(ids)
            self.fail_streak = 0
            self.error = None
            self._play_index(0)

    def pause(self):
        with self.lock:
            if self.state == "PLAYING":
                self._kill()
                self.state = "PAUSED_PLAYBACK"

    def resume(self):
        with self.lock:
            if self.state == "PAUSED_PLAYBACK":
                self.error = None
                self._spawn_current(self.pos)

    def stop(self):
        with self.lock:
            self._halt()

    def seek(self, seconds):
        with self.lock:
            seconds = max(0.0, float(seconds))
            if self.state == "PLAYING":
                self._spawn_current(seconds)
            elif self.state == "PAUSED_PLAYBACK":
                self.pos = seconds              # applied on resume

    def jump(self, i):
        with self.lock:
            if 0 <= i < len(self.ids):
                self.fail_streak = 0
                self.error = None
                self._play_index(i)

    def clear(self):
        with self.lock:
            self._halt()
            self.ids = []
            self.idx = 0

    def enqueue(self, ids, play_next=False):
        with self.lock:
            if play_next:
                pos = self.idx + 1
                self.ids[pos:pos] = ids
            else:
                self.ids.extend(ids)

    def remove(self, i):
        with self.lock:
            if not (0 <= i < len(self.ids)):
                return
            del self.ids[i]
            if i < self.idx:
                self.idx -= 1
            elif i == self.idx:
                if self.ids:
                    self._play_index(min(self.idx, len(self.ids) - 1))
                else:
                    self.idx = 0
                    self._halt()

    def move(self, a, b):
        with self.lock:
            if not (0 <= a < len(self.ids)) or not (0 <= b < len(self.ids)):
                return
            cur = self.ids[self.idx] if self.ids else None
            self.ids.insert(b, self.ids.pop(a))
            if cur is not None:
                self.idx = self.ids.index(cur)

    def shuffle(self):
        with self.lock:
            if not self.ids:
                return
            cur = self.ids[self.idx]
            rest = [x for i, x in enumerate(self.ids) if i != self.idx]
            random.shuffle(rest)
            self.ids = [cur] + rest
            self.idx = 0

    def shutdown(self):
        with self.lock:
            for t in (self.vol_timer, self.sleep_timer):
                if t:
                    t.cancel()
            self._kill()
            self.state = "STOPPED"


# ----------------------------------------------------------------------------
# module API (mirrors cast.py's cast_* functions)
# ----------------------------------------------------------------------------

def _player(spk):
    with _players_lock:
        p = players.get(spk["uuid"])
        if p is None:
            p = players[spk["uuid"]] = LocalPlayer(spk)
        return p


def _playable_ids(ids):
    return [tid for tid in ids if tid in tracks_by_id]


def local_play_tracks(spk, ids):
    if not _available():
        raise RuntimeError(_FFMPEG_MISSING)
    ids = _playable_ids(ids)
    if not ids:
        raise RuntimeError("There's nothing to play there.")
    p = _player(spk)
    p.play(ids)
    return len(ids)


def local_enqueue_tracks(spk, ids, play_next=False):
    ids = _playable_ids(ids)
    p = players.get(spk["uuid"])
    if not p or not p.ids:
        return local_play_tracks(spk, ids)
    p.enqueue(ids, play_next)


def local_browse_queue(spk):
    p = players.get(spk["uuid"])
    ids = list(p.ids) if p else []
    items = []
    for i, tid in enumerate(ids):
        t = tracks_by_id.get(tid)
        items.append({"no": i + 1, "id": tid, "title": (t["title"] if t else tid)})
    return items, len(items)


def local_queue_jump(spk, no):
    p = players.get(spk["uuid"])
    if p:
        p.jump(int(no) - 1)


def local_queue_remove(spk, no):
    p = players.get(spk["uuid"])
    if p:
        p.remove(int(no) - 1)


def local_queue_move(spk, from_no, to_no):
    p = players.get(spk["uuid"])
    if p:
        p.move(int(from_no) - 1, int(to_no) - 1)


def local_set_volume(spk, value):
    _player(spk).set_volume(value)


def local_seek_to(spk, seconds):
    p = players.get(spk["uuid"])
    if p:
        p.seek(seconds)


def local_set_shuffle(spk, on):
    p = players.get(spk["uuid"])
    if p and on:
        p.shuffle()


def local_set_sleep(spk, minutes):
    _player(spk).set_sleep(minutes)


def local_cmd(spk, action, value=None):
    p = players.get(spk["uuid"])
    if action == "clearqueue":
        if p:
            p.clear()
        return
    if p is None:
        if action in ("pause", "resume", "stop", "next", "prev"):
            return
        raise RuntimeError(_CANT_CMD)
    if action == "pause":
        p.pause()
    elif action == "resume":
        p.resume()
    elif action == "stop":
        p.stop()
    elif action == "next":
        with p.lock:
            if p.idx + 1 < len(p.ids):
                p.jump(p.idx + 1)
    elif action == "prev":
        with p.lock:
            if p.idx - 1 >= 0:
                p.jump(p.idx - 1)
    else:
        raise RuntimeError(_CANT_CMD)


def _duration_sec(tid):
    t = tracks_by_id.get(tid) or {}
    d = t.get("duration")
    if isinstance(d, (int, float)) and d > 0:
        return float(d)
    d = (trackinfo.get(tid) or {}).get("dur")
    if isinstance(d, (int, float)) and d > 0:
        return float(d)
    return 0.0


def local_get_status(spk):
    p = players.get(spk["uuid"])
    if p is None:
        return {"state": "STOPPED", "title": "", "position": "0:00:00", "duration": "",
                "volume": _volume_for(spk["uuid"]), "track_no": None,
                "track_id": None, "pos": 0.0, "error": None}
    with p.lock:
        t = p._track()
        tid = p.ids[p.idx] if t else None
        dur = _duration_sec(tid) if tid else 0.0
        return {"state": p.state,
                "title": t["title"] if t else "",
                "position": sec_to_hms(p.pos),
                "duration": sec_to_hms(dur) if dur else "",
                "volume": _volume_for(p.uuid),
                "track_no": (p.idx + 1) if p.ids else None,
                "track_id": tid,
                "pos": round(p.pos, 3),
                "error": p.error}


def local_shutdown():
    """Kill every ffmpeg child (engine exit) so nothing keeps playing."""
    for p in list(players.values()):
        try:
            p.shutdown()
        except Exception:
            pass
