"""Library scan: tag parsing (ID3/MP4/FLAC), album art, track model."""
import os
from hashlib import sha1

from draai import state
from draai.state import tracks, tracks_by_id, config, art_cache, state_lock
from draai.constants import AUDIO_EXTS, ART_CACHE_MAX


def _syncsafe(b):
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _id3_text(body):
    if not body:
        return ""
    enc, raw = body[0], body[1:]
    try:
        if enc == 0:
            s = raw.decode("latin-1")
        elif enc == 1:
            s = raw.decode("utf-16")
        elif enc == 2:
            s = raw.decode("utf-16-be")
        else:
            s = raw.decode("utf-8")
    except Exception:
        s = raw.decode("latin-1", "replace")
    return s.strip("\x00").strip()


def _null_split(data, wide):
    """Split at a null terminator (double-null for UTF-16). Returns (rest)."""
    if wide:
        i = 0
        while i + 1 < len(data):
            if data[i] == 0 and data[i + 1] == 0:
                return data[i + 2:]
            i += 2
        return b""
    i = data.find(b"\x00")
    return data[i + 1:] if i >= 0 else b""


def _tags_mp3(path, want_art):
    with open(path, "rb") as f:
        head = f.read(10)
        if len(head) < 10 or head[:3] != b"ID3":
            return {}
        ver, flags = head[3], head[5]
        size = _syncsafe(head[6:10])
        if size <= 0 or size > 32 * 1024 * 1024:
            return {}
        data = f.read(size)
    pos = 0
    if flags & 0x40:  # extended header
        if len(data) < 4:
            return {}
        eh = _syncsafe(data[0:4]) if ver >= 4 else int.from_bytes(data[0:4], "big") + 4
        pos = eh
    out = {}
    if ver == 2:
        idlen, hdrlen = 3, 6
        names = {b"TT2": "title", b"TP1": "artist", b"TAL": "album"}
        art_id = b"PIC"
    else:
        idlen, hdrlen = 4, 10
        names = {b"TIT2": "title", b"TPE1": "artist", b"TALB": "album"}
        art_id = b"APIC"
    while pos + hdrlen <= len(data):
        fid = data[pos:pos + idlen]
        if fid.strip(b"\x00") == b"" or not fid.isalnum() and b"\xa9" not in fid:
            break
        if ver == 2:
            fsize = int.from_bytes(data[pos + 3:pos + 6], "big")
        elif ver >= 4:
            fsize = _syncsafe(data[pos + 4:pos + 8])
        else:
            fsize = int.from_bytes(data[pos + 4:pos + 8], "big")
        body = data[pos + hdrlen:pos + hdrlen + fsize]
        pos += hdrlen + fsize
        if fsize <= 0:
            break
        key = names.get(fid)
        if key and key not in out:
            out[key] = _id3_text(body)
        elif fid == art_id and "art" not in out:
            out["has_art"] = True
            if want_art:
                try:
                    enc = body[0]
                    if ver == 2:  # PIC: enc + 3-byte format + type + desc + data
                        rest = body[5:]
                    else:  # APIC: enc + mime\0 + type + desc + data
                        rest = _null_split(body[1:], False)
                        rest = rest[1:]  # picture type byte
                    rest = _null_split(rest, enc in (1, 2))
                    if rest[:3] == b"\xff\xd8\xff":
                        out["art"] = ("image/jpeg", rest)
                    elif rest[:4] == b"\x89PNG":
                        out["art"] = ("image/png", rest)
                except Exception:
                    pass
    return out


def _mp4_children(data, pos, end):
    """Yield (type, body_start, body_end) for boxes in data[pos:end]."""
    while pos + 8 <= end:
        size = int.from_bytes(data[pos:pos + 4], "big")
        btype = data[pos + 4:pos + 8]
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
            hdr = 16
        if size < hdr or pos + size > end:
            return
        yield btype, pos + hdr, pos + size
        pos += size


def _tags_mp4(path, want_art):
    # find the moov box at top level without reading the whole (huge) file
    moov = None
    with open(path, "rb") as f:
        f.seek(0, 2)
        fsize = f.tell()
        pos = 0
        while pos + 8 <= fsize:
            f.seek(pos)
            hdr = f.read(16)
            if len(hdr) < 8:
                break
            size = int.from_bytes(hdr[0:4], "big")
            btype = hdr[4:8]
            hlen = 8
            if size == 1:
                size = int.from_bytes(hdr[8:16], "big")
                hlen = 16
            if size < hlen:
                break
            if btype == b"moov":
                if size > 64 * 1024 * 1024:
                    return {}
                f.seek(pos + hlen)
                moov = f.read(size - hlen)
                break
            pos += size
    if moov is None:
        return {}
    out = {}
    names = {b"\xa9nam": "title", b"\xa9ART": "artist", b"\xa9alb": "album"}
    for t1, s1, e1 in _mp4_children(moov, 0, len(moov)):
        if t1 != b"udta":
            continue
        for t2, s2, e2 in _mp4_children(moov, s1, e1):
            if t2 != b"meta":
                continue
            for t3, s3, e3 in _mp4_children(moov, s2 + 4, e2):  # meta: 4-byte ver
                if t3 != b"ilst":
                    continue
                for t4, s4, e4 in _mp4_children(moov, s3, e3):
                    for t5, s5, e5 in _mp4_children(moov, s4, e4):
                        if t5 != b"data" or e5 - s5 <= 8:
                            continue
                        payload = moov[s5 + 8:e5]
                        key = names.get(t4)
                        if key and key not in out:
                            out[key] = payload.decode("utf-8", "replace").strip()
                        elif t4 == b"covr":
                            out["has_art"] = True
                            if want_art:
                                if payload[:3] == b"\xff\xd8\xff":
                                    out["art"] = ("image/jpeg", payload)
                                elif payload[:4] == b"\x89PNG":
                                    out["art"] = ("image/png", payload)
    return out


def _tags_flac(path, want_art):
    out = {}
    with open(path, "rb") as f:
        if f.read(4) != b"fLaC":
            return {}
        while True:
            hdr = f.read(4)
            if len(hdr) < 4:
                break
            last = hdr[0] & 0x80
            btype = hdr[0] & 0x7F
            size = int.from_bytes(hdr[1:4], "big")
            if btype == 4 and size < 16 * 1024 * 1024:  # VORBIS_COMMENT
                blk = f.read(size)
                try:
                    p = 4 + int.from_bytes(blk[0:4], "little")  # skip vendor
                    n = int.from_bytes(blk[p:p + 4], "little")
                    p += 4
                    for _ in range(min(n, 256)):
                        ln = int.from_bytes(blk[p:p + 4], "little")
                        p += 4
                        kv = blk[p:p + ln].decode("utf-8", "replace")
                        p += ln
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            k = k.upper()
                            if k == "TITLE" and "title" not in out:
                                out["title"] = v.strip()
                            elif k == "ARTIST" and "artist" not in out:
                                out["artist"] = v.strip()
                            elif k == "ALBUM" and "album" not in out:
                                out["album"] = v.strip()
                except Exception:
                    pass
            elif btype == 6 and size < 32 * 1024 * 1024:  # PICTURE
                out["has_art"] = True
                if want_art and "art" not in out:
                    blk = f.read(size)
                    try:
                        p = 4
                        ml = int.from_bytes(blk[p:p + 4], "big")
                        mime = blk[p + 4:p + 4 + ml].decode("latin-1")
                        p += 4 + ml
                        dl = int.from_bytes(blk[p:p + 4], "big")
                        p += 4 + dl + 16  # desc + w/h/depth/colors
                        n = int.from_bytes(blk[p:p + 4], "big")
                        out["art"] = (mime or "image/jpeg",
                                      blk[p + 4:p + 4 + n])
                    except Exception:
                        pass
                else:
                    f.seek(size, 1)
            else:
                f.seek(size, 1)
            if last:
                break
    return out


def read_tags(path, want_art=False):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            return _tags_mp3(path, want_art)
        if ext in (".m4a", ".aac", ".mp4"):
            return _tags_mp4(path, want_art)
        if ext == ".flac":
            return _tags_flac(path, want_art)
    except Exception:
        pass
    return {}




def get_art(track):
    tid = track["id"]
    if tid in art_cache:
        return art_cache[tid]
    tags = read_tags(track["path"], want_art=True)
    art = tags.get("art")
    if art:
        if len(art_cache) >= ART_CACHE_MAX:
            art_cache.pop(next(iter(art_cache)))
        art_cache[tid] = art
    return art


# ----------------------------------------------------------------------------
# Music library
# ----------------------------------------------------------------------------

def scan_all():
    """Rescan every configured folder into one library. Returns (count, err)."""
    roots = [os.path.expanduser(f.strip()) for f in config.get("folders", [])]
    roots = [r for r in roots if r]
    multi = len(roots) > 1
    merged = []
    missing = []
    for root in roots:
        if not os.path.isdir(root):
            missing.append(root)
            continue
        merged.extend(_scan_root(root, multi))
    merged.sort(key=lambda t: (t["folder"].lower(), t["title"].lower()))
    with state_lock:
        tracks[:] = merged
        tracks_by_id.clear()
        tracks_by_id.update({t["id"]: t for t in merged})
    err = ("These folders do not exist: " + ", ".join(missing)) if missing else None
    return len(merged), err


def scan_folder(folder):
    """Back-compat single-folder scan: replaces the folder list."""
    folder = os.path.expanduser(folder.strip())
    if not os.path.isdir(folder):
        return 0, "That folder does not exist: %s" % folder
    config["folders"] = [folder]
    return scan_all()


def _scan_root(folder, multi):
    """Collect audio files under one root."""
    found = []
    base = os.path.basename(folder.rstrip("/")) or folder
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(files):
            ext = os.path.splitext(name)[1].lower()
            if ext not in AUDIO_EXTS or name.startswith("."):
                continue
            path = os.path.join(root, name)
            tid = sha1(path.encode("utf-8", "surrogateescape")).hexdigest()[:16]
            rel = os.path.relpath(root, folder)
            rel = "" if rel == "." else rel
            if multi:   # prefix with the root's name so origins stay clear
                rel = base + ("/" + rel if rel else "")
            tags = read_tags(path)
            try:
                st = os.stat(path)
                added = int(getattr(st, "st_birthtime", st.st_mtime))
            except OSError:
                added = 0
            found.append({
                "id": tid,
                "path": path,
                "added": added,
                "title": tags.get("title") or os.path.splitext(name)[0],
                "artist": tags.get("artist", ""),
                "album": tags.get("album", ""),
                "has_art": bool(tags.get("has_art")),
                "folder": rel,
                "ext": ext,
            })
    return found
