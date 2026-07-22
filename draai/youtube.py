"""YouTube import via the user's own yt-dlp (optional)."""
import itertools
import os
import re
import shutil
import subprocess
import tempfile
import threading

from draai import state
from draai.state import yt_jobs, config
from draai.media import find_tool
from draai.library import scan_all


YT_URL_RE = re.compile(r"^https?://\S+$", re.I)

yt_counter = itertools.count(1)

# A full DJ set can be an hour of audio; 30 min was too tight and its failure left
# litter behind. An hour of wall-clock covers a long download on a modest connection.
YT_TIMEOUT = 3600


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
            # Download into a throwaway temp dir, and only move the FINISHED mp3 into
            # the library. yt-dlp writes the thumbnail (and any partial audio) to disk
            # early, before the audio download finishes — so a failure or timeout used
            # to leave those orphans, most visibly a stray .jpg, sitting in Imported/.
            # Staging in temp means nothing partial ever reaches the library, and the
            # temp dir is always removed, success or fail.
            tmp = tempfile.mkdtemp(prefix="draai-yt-")
            try:
                r = subprocess.run(
                    [ytdlp, "--no-playlist", "-x", "--audio-format", "mp3",
                     "--audio-quality", "0", "--ffmpeg-location", ffmpeg,
                     "--embed-metadata", "--embed-thumbnail",
                     "--convert-thumbnails", "jpg",
                     "-o", os.path.join(tmp, "%(title)s.%(ext)s"), url],
                    capture_output=True, text=True, timeout=YT_TIMEOUT)
                if r.returncode != 0:
                    lines = [l for l in (r.stderr or r.stdout).strip().splitlines()
                             if l.strip()]
                    raise RuntimeError(lines[-1] if lines else "Download failed")
                mp3s = [f for f in os.listdir(tmp) if f.lower().endswith(".mp3")]
                if not mp3s:
                    raise RuntimeError("No audio file was produced.")
                for f in mp3s:
                    dst = os.path.join(outdir, f)
                    shutil.move(os.path.join(tmp, f), dst)  # only the finished mp3
            finally:
                shutil.rmtree(tmp, ignore_errors=True)      # never litter, any outcome
            scan_all()
            job["status"] = "done"
            job["detail"] = ("Added “%s” to your library "
                             "(Imported folder)" % (job["title"] or "track"))
        except subprocess.TimeoutExpired:
            job["status"] = "error"
            job["error"] = ("The download took too long and was stopped "
                            "(over %d minutes). Very long videos can exceed "
                            "this." % (YT_TIMEOUT // 60))
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)

    threading.Thread(target=run, daemon=True).start()
    return job_id


# ----------------------------------------------------------------------------
# Audio analysis (needs ffmpeg): waveform peaks + band energy over time
# ----------------------------------------------------------------------------




from draai.analysis import (_scale, _stream_envelope, _analyze, get_analysis, prefetch_analysis)   # re-export during the split
