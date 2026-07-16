"""Playlists as plain .m3u files in the first library folder."""
import os
import re

from draai.state import config, tracks, tracks_by_id, state_lock
from draai.backends import browse_queue


def playlists_dir():
    folders = config.get("folders", [])
    base = folders[0] if folders else os.path.expanduser("~/Music")
    return os.path.join(base, "Playlists")


def safe_playlist_name(name):
    name = re.sub(r"[/\\:\x00-\x1f]", "-", (name or "").strip())[:80]
    return name or "Playlist"


def list_playlists():
    out = []
    d = playlists_dir()
    if os.path.isdir(d):
        for f in sorted(os.listdir(d), key=str.lower):
            if f.lower().endswith(".m3u"):
                try:
                    n = sum(1 for line in open(os.path.join(d, f),
                                               encoding="utf-8",
                                               errors="replace")
                            if line.strip() and not line.startswith("#"))
                except Exception:
                    n = 0
                out.append({"name": f[:-4], "count": n})
    return out


def save_playlist(spk, name):
    items, _total = browse_queue(spk)
    entries = []
    with state_lock:
        for it in items:
            t = tracks_by_id.get(it.get("id") or "")
            if t:
                entries.append((t["title"], t["path"]))
    if not entries:
        raise RuntimeError("The queue has no tracks from your library "
                           "to save.")
    d = playlists_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, safe_playlist_name(name) + ".m3u")
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for title, p in entries:
            f.write("#EXTINF:-1,%s\n%s\n" % (title, p))
    return len(entries)


def load_playlist(name):
    """Return (ids_present_in_library, total_lines)."""
    path = os.path.join(playlists_dir(), safe_playlist_name(name) + ".m3u")
    if not os.path.isfile(path):
        raise RuntimeError("No playlist named “%s”." % name)
    ids, total = [], 0
    with state_lock:
        by_path = {t["path"]: t["id"] for t in tracks}
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        total += 1
        tid = by_path.get(line)
        if tid:
            ids.append(tid)
    return ids, total


def delete_playlist(name):
    path = os.path.join(playlists_dir(), safe_playlist_name(name) + ".m3u")
    if os.path.isfile(path):
        os.remove(path)
