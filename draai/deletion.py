"""Permanent deletion of library files (POST /api/delete).

There is no Trash: a deleted file is gone. So the endpoint is the one
loopback-only, Origin-checked route in the engine (`delete_allowed`), and
every file is re-checked before removal: it must still be in the library,
must not be playing on any speaker, and its real path must sit inside one of
the configured music folders.
"""
import os

from draai import analysis, backends, cast
from draai.state import (config, listening, listening_lock, positions, positions_lock,
                         speakers, state_lock, trackinfo, trackinfo_lock, tracks,
                         tracks_by_id)
from draai.listening import save_listening_soon, save_trackinfo_soon, status_and_observe

GUARD_ERROR = "Deleting only works in DRAAI on this Mac."
LOOPBACK_IPS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
MAX_IDS = 500


def _hostname(host):
    """'127.0.0.1:8765' -> '127.0.0.1', '[::1]:8765' -> '::1'."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else ""
    return host.rsplit(":", 1)[0] if ":" in host else host


def delete_allowed(client_ip, headers, port):
    """None if this request may delete files, else the human error sentence."""
    if client_ip not in LOOPBACK_IPS:
        return GUARD_ERROR
    if _hostname(headers.get("Host")) not in LOOPBACK_HOSTS:
        return GUARD_ERROR
    origin = headers.get("Origin")
    if origin is not None and origin not in (
            "http://localhost:%d" % port, "http://127.0.0.1:%d" % port,
            "http://[::1]:%d" % port):
        return GUARD_ERROR
    if not (headers.get("Content-Type") or "").startswith("application/json"):
        return GUARD_ERROR
    return None


def playing_track_ids():
    """Ids PLAYING or TRANSITIONING on any speaker group, asked fresh. Never raises."""
    playing = set()
    try:
        with state_lock:
            groups = list(speakers)
        for spk in groups:
            try:
                if spk.get("backend") == "cast":
                    sess = cast.cast_sessions.get(spk.get("ip"))
                    if sess is None or not getattr(sess, "_running", False):
                        continue           # not something DRAAI is playing
                st = status_and_observe(spk) or {}
                if st.get("state") in ("PLAYING", "TRANSITIONING") and st.get("track_id"):
                    playing.add(st["track_id"])
            except Exception:
                pass
    except Exception:
        pass
    return playing


def _inside_folders(real):
    for folder in config.get("folders") or []:
        root = os.path.realpath(os.path.expanduser(folder))
        if real.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def delete_tracks(ids):
    """Delete each id's file for good; returns {"deleted": n, "failed": [...]}."""
    deleted, failed = 0, []
    playing = playing_track_ids()
    for tid in ids:
        tid = str(tid)
        with state_lock:
            track = tracks_by_id.get(tid)
        if not track:
            failed.append({"id": tid, "title": "",
                           "error": "That track is no longer in your library."})
            continue
        title = track.get("title") or os.path.basename(track.get("path") or "")
        if tid in playing:
            failed.append({"id": tid, "title": title,
                           "error": "That set is playing right now — stop it first, then delete."})
            continue
        real = os.path.realpath(track["path"])
        if not _inside_folders(real):
            failed.append({"id": tid, "title": title,
                           "error": "That file is outside your music folders, so DRAAI won't delete it."})
            continue
        try:
            os.remove(real)
        except OSError as e:
            failed.append({"id": tid, "title": title,
                           "error": "Couldn't delete “%s”: %s." % (title, e.strerror or e)})
            continue
        deleted += 1
        _forget(tid)
    if deleted:
        backends.save_positions_soon()
        save_listening_soon()
        save_trackinfo_soon()
    return {"deleted": deleted, "failed": failed}


def _forget(tid):
    """Drop every trace of a deleted track from memory and the analysis cache."""
    with state_lock:
        tracks[:] = [t for t in tracks if t.get("id") != tid]
        tracks_by_id.pop(tid, None)
    with positions_lock:
        positions.pop(tid, None)
    with listening_lock:
        listening.pop(tid, None)
    with trackinfo_lock:
        trackinfo.pop(tid, None)
    try:
        os.remove(os.path.join(analysis.ANALYSIS_DIR, tid + ".json"))
    except OSError:
        pass
