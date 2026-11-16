"""Time listened per track, measured by the engine itself.

A daemon watcher samples each speaker group's transport every
LISTEN_INTERVAL seconds (no browser needed), and /api/status feeds the same
observer whenever the UI polls. Every sample goes through `observe()`, which
credits the position delta since the previous sample of the same speaker.

Crediting rule (per speaker, keyed by group uuid):
- Only a PLAYING sample of a DRAAI track (a `/media/<id>/` URL) credits.
- Same track as the previous sample: credit `pos - prev_pos` when
  0 < delta <= wall-clock elapsed + LISTEN_SLACK. A larger jump is a seek
  forward, a negative one a seek back: both credit nothing.
- Track changed since the previous sample: credit the new track's position
  if it is <= elapsed + slack (it started within the window, so all of it
  was heard); otherwise nothing. The previous track's unseen tail (at most
  one interval) is not credited.
- No previous sample (first sight, engine just started), or the previous
  sample is older than LISTEN_STALE: credit nothing, just remember it.
- Paused/stopped samples credit nothing but are remembered, so the time
  played after a resume is counted from the paused position.
- A sample taken before the newest one already recorded (the watcher and a
  UI poll racing) is dropped, so the same span is never counted twice.
"""
import json
import os
import threading
import time

from draai import cast
from draai.state import (listening, listening_lock, _listening_dirty, speakers, state_lock,
                         trackinfo, trackinfo_lock, _trackinfo_dirty)
from draai.constants import CONFIG_DIR, LISTENING_PATH, LISTEN_INTERVAL, TRACKINFO_PATH
from draai.util import hms_to_sec
from draai.backends import get_status

LISTEN_SLACK = 5     # seconds of tolerance on top of wall-clock elapsed
LISTEN_STALE = 120   # older previous sample: don't trust the delta

_last = {}           # speaker key -> (track_id, pos_sec, sample_time)
_last_lock = threading.Lock()
_path = [LISTENING_PATH]   # mutable so tests can redirect it
_info_path = [TRACKINFO_PATH]


def load_listening(path=None):
    try:
        with open(path or _path[0]) as f:
            loaded = json.load(f)
        clean = {str(k): int(v) for k, v in loaded.items() if int(v) > 0}
    except Exception:
        clean = {}
    with listening_lock:
        listening.clear()
        listening.update(clean)


def write_listening(path=None):
    path = path or _path[0]
    os.makedirs(os.path.dirname(path) or CONFIG_DIR, exist_ok=True)
    with listening_lock:
        data = dict(listening)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def save_listening_soon():
    if _listening_dirty[0]:
        return
    _listening_dirty[0] = True

    def flush():
        time.sleep(10)
        _listening_dirty[0] = False
        try:
            write_listening()
        except Exception:
            pass
    threading.Thread(target=flush, daemon=True).start()


def credit(tid, seconds):
    seconds = int(seconds)
    if not tid or seconds <= 0:
        return 0
    with listening_lock:
        listening[tid] = listening.get(tid, 0) + seconds
    save_listening_soon()
    return seconds


def listened_snapshot():
    with listening_lock:
        return dict(listening)


# -- per-track duration and last-seen time (trackinfo.json) -------------------
# A PLAYING or PAUSED_PLAYBACK sample of a DRAAI track records the track's
# duration and the wall-clock time it was last seen on a speaker; the Home
# screen's Continue-listening shelf reads both through /api/tracks. To keep
# trackinfo.json from being rewritten on every poll, an entry only changes
# (and a save is only scheduled) when the duration changes or `seen` moves on
# by more than SEEN_RESOLUTION seconds.

SEEN_RESOLUTION = 60


def load_trackinfo(path=None):
    clean = {}
    try:
        with open(path or _info_path[0]) as f:
            loaded = json.load(f)
        items = loaded.items() if isinstance(loaded, dict) else ()
    except Exception:
        items = ()
    for k, v in items:
        try:                               # one bad entry must not cost the rest
            clean[str(k)] = {"dur": int(v.get("dur", 0)), "seen": float(v.get("seen", 0))}
        except Exception:
            pass
    with trackinfo_lock:
        trackinfo.clear()
        trackinfo.update(clean)


def write_trackinfo(path=None):
    path = path or _info_path[0]
    os.makedirs(os.path.dirname(path) or CONFIG_DIR, exist_ok=True)
    with trackinfo_lock:
        data = {k: dict(v) for k, v in trackinfo.items()}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def save_trackinfo_soon():
    if _trackinfo_dirty[0]:
        return
    _trackinfo_dirty[0] = True

    def flush():
        time.sleep(10)
        _trackinfo_dirty[0] = False
        try:
            write_trackinfo()
        except Exception:
            pass
    threading.Thread(target=flush, daemon=True).start()


def note_track(tid, dur_sec, now_wall):
    """Remember a track's duration (when known) and when it was last seen.

    Returns True when the entry changed (and a save was scheduled)."""
    if not tid:
        return False
    dur_sec = int(dur_sec or 0)
    now_wall = float(now_wall)
    with trackinfo_lock:
        old = trackinfo.get(tid)
        if old is not None:
            dur_changed = dur_sec > 0 and dur_sec != old.get("dur", 0)
            if not dur_changed and abs(now_wall - old.get("seen", 0)) <= SEEN_RESOLUTION:
                return False
        old = old or {}
        trackinfo[tid] = {"dur": dur_sec if dur_sec > 0 else old.get("dur", 0),
                          "seen": now_wall}
    save_trackinfo_soon()
    return True


def trackinfo_snapshot():
    with trackinfo_lock:
        return {k: dict(v) for k, v in trackinfo.items()}


def _key(spk):
    return spk.get("uuid") or spk.get("ip")


def observe(spk, status, now=None):
    """Feed one status sample; returns the seconds credited (0 mostly)."""
    if now is None:
        now = time.monotonic()
    key = _key(spk)
    tid = (status or {}).get("track_id")
    playing = (status or {}).get("state") == "PLAYING"
    pos = hms_to_sec((status or {}).get("position") or "")
    with _last_lock:
        prev = _last.get(key)
        if prev and now < prev[2]:
            return 0                       # stale sample lost a race
        if not tid:
            _last.pop(key, None)
            return 0
        _last[key] = (tid, pos, now)
    if (status or {}).get("state") in ("PLAYING", "PAUSED_PLAYBACK"):
        note_track(tid, hms_to_sec((status or {}).get("duration") or ""), time.time())
    if not playing or not prev:
        return 0
    elapsed = now - prev[2]
    if elapsed <= 0 or elapsed > LISTEN_STALE:
        return 0
    bound = elapsed + LISTEN_SLACK
    delta = pos - prev[1] if prev[0] == tid else pos
    if 0 < delta <= bound:
        return credit(tid, delta)
    return 0


def status_and_observe(spk):
    """get_status + observe: what /api/status and the watcher both use."""
    t0 = time.monotonic()
    st = get_status(spk)
    try:
        observe(spk, st, t0)
    except Exception:
        pass
    return st


def last_seen(spk):
    with _last_lock:
        prev = _last.get(_key(spk))
    return prev[2] if prev else None


def watch_once(now=None):
    """One sweep over all speaker groups. Never raises."""
    if now is None:
        now = time.monotonic()
    with state_lock:
        groups = list(speakers)
    for spk in groups:
        try:
            seen = last_seen(spk)
            if seen is not None and now - seen < LISTEN_INTERVAL / 2:
                continue                   # the UI is polling it already
            if spk.get("backend") == "cast":
                sess = cast.cast_sessions.get(spk.get("ip"))
                if sess is None or not getattr(sess, "_running", False):
                    continue               # not something DRAAI is playing
            status_and_observe(spk)
        except Exception:
            pass


def start_watcher(interval=LISTEN_INTERVAL):
    def loop():
        while True:
            time.sleep(interval)
            watch_once()
    t = threading.Thread(target=loop, name="draai-listening", daemon=True)
    t.start()
    return t
