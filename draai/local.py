"""This Mac's own audio outputs as DRAAI rooms (backend "local").

Outputs are enumerated through CoreAudio via ctypes (stdlib only) and played
with the optional ffmpeg's `audiotoolbox` output, one child process per room.
Never touches the system default output or the device's hardware volume.
"""
import sys

from draai.media import find_tool

_FFMPEG_MISSING = "Playing on this Mac needs ffmpeg."


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
