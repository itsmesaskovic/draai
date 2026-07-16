"""Waveform / loudness analysis via ffmpeg (optional feature)."""
import json
import os
import subprocess
import sys
import threading

from draai.constants import CONFIG_DIR
from draai.media import find_tool
from draai.state import tracks_by_id, state_lock

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
