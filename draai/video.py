"""Set video: resolve a set's source link to direct video streams via the
user's own yt-dlp (optional). The browser streams the video itself; the engine
only asks yt-dlp for the stream list (`-J`, metadata only, nothing downloaded)."""
import json
import subprocess
import threading
import time
import urllib.parse

from draai.media import find_tool
from draai.state import state_lock, tracks_by_id


VIDEO_TIMEOUT = 30         # seconds for one yt-dlp metadata request
DEFAULT_TTL = 3600         # cache lifetime when stream URLs carry no expire=
EXPIRE_MARGIN = 300        # stop serving URLs this long before they expire

_cache = {}                # tid -> (streams, expires_at)
_locks = {}                # tid -> Lock, so concurrent requests run yt-dlp once
_cache_lock = threading.Lock()


class VideoError(Exception):
    """A human sentence for the UI plus the HTTP status to send with it."""

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def _streams_from(info):
    out = []
    for f in info.get("formats") or []:
        if not isinstance(f, dict):
            continue
        h = f.get("height")
        if (f.get("vcodec") in (None, "none") or f.get("acodec") not in (None, "none")
                or f.get("protocol") != "https" or type(h) is not int or h <= 0
                or not isinstance(f.get("url"), str)):
            continue
        fps = f.get("fps")
        out.append({"url": f["url"], "height": h,
                    "fps": int(fps) if isinstance(fps, (int, float)) else 0,
                    "vcodec": str(f["vcodec"]), "ext": str(f.get("ext") or "")})
    out.sort(key=lambda s: s["height"], reverse=True)
    return out


def _expires_at(streams):
    exps = []
    for s in streams:
        for v in urllib.parse.parse_qs(urllib.parse.urlsplit(s["url"]).query).get("expire", []):
            if v.isdigit():
                exps.append(int(v))
    return min(exps) - EXPIRE_MARGIN if exps else time.time() + DEFAULT_TTL


def video_streams(tid):
    """Direct video-only https streams for a set, tallest first. Raises VideoError."""
    with state_lock:
        t = tracks_by_id.get(tid)
        url = (t or {}).get("source_url") or ""
    if not url:
        raise VideoError("This set has no video link.", 404)
    with _cache_lock:
        lock = _locks.setdefault(tid, threading.Lock())
    with lock:
        with _cache_lock:
            hit = _cache.get(tid)
        if hit and time.time() < hit[1]:
            return hit[0]
        ytdlp = find_tool("yt-dlp")
        if not ytdlp:
            raise VideoError("Install yt-dlp to see set videos.", 404)
        try:
            proc = subprocess.run([ytdlp, "-J", "--no-playlist", "--", url],
                                  capture_output=True, timeout=VIDEO_TIMEOUT)
            if proc.returncode != 0:
                raise ValueError("yt-dlp exit %d" % proc.returncode)
            info = json.loads(proc.stdout.decode("utf-8", "replace"))
            if not isinstance(info, dict):
                raise ValueError("unexpected yt-dlp output")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise VideoError("Couldn't load the video for this set.", 502)
        streams = _streams_from(info)
        if not streams:
            raise VideoError("No video stream was found for this set.", 404)
        with _cache_lock:
            _cache[tid] = (streams, _expires_at(streams))
        return streams
