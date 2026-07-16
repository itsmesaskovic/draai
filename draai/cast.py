"""Google Cast (CASTV2) backend: mDNS discovery, protobuf-over-TLS,
CastSession, and cast control + engine-managed queue."""
import json
import random
import socket
import ssl
import struct
import threading
import time

from draai.constants import AUDIO_EXTS
from draai.state import tracks_by_id
from draai.media import media_url
from draai.util import sec_to_hms


def _cast_varint(n):
    out = b""
    while True:
        b = n & 0x7F; n >>= 7
        out += bytes([b | 0x80]) if n else bytes([b])
        if not n:
            return out

def _cast_fv(fnum, val):
    return _cast_varint(fnum << 3) + _cast_varint(val)

def _cast_fl(fnum, data):
    if isinstance(data, str):
        data = data.encode()
    return _cast_varint((fnum << 3) | 2) + _cast_varint(len(data)) + data

def cast_frame(namespace, payload, source="sender-0", dest="receiver-0"):
    """Encode a CASTV2 CastMessage: 4-byte big-endian length prefix + protobuf."""
    m  = _cast_fv(1, 0)                       # protocol_version = CASTV2_1_0
    m += _cast_fl(2, source)                  # source_id
    m += _cast_fl(3, dest)                    # destination_id
    m += _cast_fl(4, namespace)               # namespace
    m += _cast_fv(5, 0)                       # payload_type = STRING
    m += _cast_fl(6, json.dumps(payload))     # payload_utf8
    return struct.pack(">I", len(m)) + m

def _cast_rvarint(data, i):
    shift = res = 0
    while True:
        b = data[i]; i += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            return res, i
        shift += 7

def cast_parse_frame(msg):
    """Decode a CastMessage body (WITHOUT the 4-byte length prefix)."""
    i = 0; f = {}
    while i < len(msg):
        tag, i = _cast_rvarint(msg, i)
        fnum, wt = tag >> 3, tag & 7
        if wt == 0:
            f[fnum], i = _cast_rvarint(msg, i)
        elif wt == 2:
            ln, i = _cast_rvarint(msg, i); f[fnum] = msg[i:i + ln]; i += ln
        else:
            break
    dec = lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else ""
    return {"source": dec(f.get(2, b"")), "dest": dec(f.get(3, b"")),
            "namespace": dec(f.get(4, b"")), "payload": dec(f.get(6, b""))}


# ------------------- Google Cast: mDNS (multicast DNS) discovery -------------------

def _dns_encode_name(name):
    out = b""
    for part in name.split("."):
        out += bytes([len(part)]) + part.encode()
    return out + b"\x00"

def _dns_parse_name(data, i):
    parts = []
    jumped = False
    start_i = i
    while True:
        ln = data[i]
        if ln & 0xC0 == 0xC0:                       # compression pointer
            ptr = ((ln & 0x3F) << 8) | data[i + 1]
            if not jumped:
                start_i = i + 2
            i = ptr
            jumped = True
            continue
        if ln == 0:
            i += 1
            break
        parts.append(data[i + 1:i + 1 + ln].decode("utf-8", "replace"))
        i += 1 + ln
    return ".".join(parts), (start_i if jumped else i)

def _mdns_parse_records(data, i, count, out):
    for _ in range(count):
        _name, i = _dns_parse_name(data, i)
        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", data[i:i + 10])
        i += 10
        rdata = data[i:i + rdlen]
        if rtype == 12:                             # PTR
            tgt, _ = _dns_parse_name(data, i); out["ptr"].setdefault(_name, tgt)
        elif rtype == 33:                           # SRV
            _pri, _wt, port = struct.unpack(">HHH", rdata[:6])
            tgt, _ = _dns_parse_name(data, i + 6); out["srv"][_name] = (tgt, port)
        elif rtype == 1:                            # A
            out["a"][_name] = socket.inet_ntoa(rdata[:4])
        elif rtype == 16:                           # TXT
            txt = {}; j = 0
            while j < len(rdata):
                l = rdata[j]; j += 1
                kv = rdata[j:j + l].decode("utf-8", "replace"); j += l
                if "=" in kv:
                    k, v = kv.split("=", 1); txt[k] = v
            out["txt"][_name] = txt
        i += rdlen
    return i

def _mdns_query_packet():
    header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    question = _dns_encode_name("_googlecast._tcp.local") + struct.pack(">HH", 12, 1)
    return header + question

def _mdns_parse_packet(data, out):
    _id, _flags, qd, an, ns, ar = struct.unpack(">HHHHHH", data[:12])
    i = 12
    for _ in range(qd):
        _, i = _dns_parse_name(data, i); i += 4
    i = _mdns_parse_records(data, i, an, out)
    i = _mdns_parse_records(data, i, ns, out)
    i = _mdns_parse_records(data, i, ar, out)
    return out


def cast_discover(timeout=4.0):
    """Discover Google Cast devices via mDNS. Returns device dicts with
    backend='cast'. Never raises — returns [] on any network error."""
    devices = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except Exception:
            pass
        s.bind(("", 5353))
        mreq = struct.pack("4s4s", socket.inet_aton("224.0.0.251"),
                           socket.inet_aton("0.0.0.0"))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        pkt = _mdns_query_packet()
        s.sendto(pkt, ("224.0.0.251", 5353))
        rec = {"ptr": {}, "srv": {}, "a": {}, "txt": {}}
        s.settimeout(0.6)
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, _ = s.recvfrom(9000)
            except socket.timeout:
                try:
                    s.sendto(pkt, ("224.0.0.251", 5353))
                except Exception:
                    pass
                continue
            try:
                _mdns_parse_packet(data, rec)
            except Exception:
                continue
        try:
            s.close()
        except Exception:
            pass
        seen = set()
        for inst in rec["srv"]:
            if "_googlecast._tcp" not in inst:
                continue
            tgt, port = rec["srv"][inst]
            ip = rec["a"].get(tgt)
            if not ip or ip in seen:
                continue
            seen.add(ip)
            txt = rec["txt"].get(inst, {})
            name = txt.get("fn", inst.split(".")[0])
            uuid = "CAST_" + (txt.get("id") or tgt)
            is_group = ("group" in txt.get("md", "").lower()) or ("nid" in txt)
            devices.append({"uuid": uuid, "name": name, "ip": ip, "port": port,
                            "backend": "cast", "is_group": is_group,
                            "members": [{"uuid": uuid, "name": name, "ip": ip}]})
    except Exception:
        pass
    return devices


NS_CONN = "urn:x-cast:com.google.cast.tp.connection"
NS_HB   = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECV = "urn:x-cast:com.google.cast.receiver"
NS_MED  = "urn:x-cast:com.google.cast.media"
CAST_APP = "CC1AD845"   # Default Media Receiver

cast_sessions = {}          # ip -> CastSession
cast_sessions_lock = threading.Lock()

class CastSession:
    def __init__(self, ip, port=8009, sock_factory=None, autostart=True):
        self.ip, self.port = ip, port
        self._sock_factory = sock_factory
        self.sock = None
        self.buf = b""
        self.send_lock = threading.Lock()
        self.transport = None
        self._session_id = None
        self._media_session_id = None
        self._req = 0
        self._advance_cb = None      # called when a track finishes (queue auto-advance)
        self._running = False
        self.status = {"state": "STOPPED", "title": "", "position": "0:00:00",
                       "duration": "0:00:00", "volume": 30, "id": None}
        self._connect()
        if autostart:
            self._start_threads()

    def _connect(self):
        if self._sock_factory:
            self.sock = self._sock_factory(self.ip, self.port)
        else:
            raw = socket.create_connection((self.ip, self.port), timeout=6)
            self.sock = ssl._create_unverified_context().wrap_socket(raw)  # Cast: per-device self-signed cert
        self.buf = b""
        self._send(NS_CONN, {"type": "CONNECT"})
        self._send(NS_HB, {"type": "PING"})

    def _send(self, ns, payload, dest="receiver-0"):
        with self.send_lock:
            self.sock.sendall(cast_frame(ns, payload, "sender-0", dest))

    def _next_req(self):
        self._req += 1
        return self._req

    def _recv_frame(self):
        while len(self.buf) < 4:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("cast connection closed")
            self.buf += chunk
        ln = struct.unpack(">I", self.buf[:4])[0]
        while len(self.buf) < 4 + ln:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("cast connection closed")
            self.buf += chunk
        msg, self.buf = self.buf[4:4 + ln], self.buf[4 + ln:]
        return cast_parse_frame(msg)

    def _handle(self, m):
        ns = m.get("namespace")
        try:
            p = json.loads(m.get("payload") or "{}")
        except Exception:
            return
        if ns == NS_HB:
            if p.get("type") == "PING":
                try:
                    self._send(NS_HB, {"type": "PONG"})
                except Exception:
                    pass
            return
        if ns == NS_RECV and p.get("type") == "RECEIVER_STATUS":
            st = p.get("status", {})
            for a in st.get("applications", []):
                if a.get("appId") == CAST_APP:
                    self.transport = a.get("transportId")
                    self._session_id = a.get("sessionId")
            vol = st.get("volume", {}).get("level")
            if vol is not None:
                self.status["volume"] = int(round(vol * 100))
            return
        if ns == NS_MED and p.get("type") == "MEDIA_STATUS":
            for stt in p.get("status", []):
                if stt.get("mediaSessionId") is not None:
                    self._media_session_id = stt.get("mediaSessionId")
                ps = stt.get("playerState")
                if ps == "PLAYING":
                    self.status["state"] = "PLAYING"
                elif ps == "PAUSED":
                    self.status["state"] = "PAUSED_PLAYBACK"
                elif ps in ("IDLE", "BUFFERING"):
                    self.status["state"] = "TRANSITIONING" if ps == "BUFFERING" else self.status["state"]
                ct = stt.get("currentTime")
                if ct is not None:
                    self.status["position"] = sec_to_hms(ct)
                dur = stt.get("media", {}).get("duration")
                if dur:
                    self.status["duration"] = sec_to_hms(dur)
                if ps == "IDLE" and stt.get("idleReason") == "FINISHED":
                    if self._advance_cb:
                        try:
                            self._advance_cb()
                        except Exception:
                            pass
            return

    def _rx_loop(self):
        while self._running:
            try:
                m = self._recv_frame()
            except Exception:
                if not self._running:
                    break
                time.sleep(1)
                try:
                    self._connect()
                except Exception:
                    pass
                continue
            self._handle(m)

    def _hb_loop(self):
        # Poll media status every ~1s so currentTime advances (Cast does not
        # push periodic position updates); PING for heartbeat every ~5s.
        n = 0
        while self._running:
            time.sleep(1)
            n += 1
            try:
                if self.transport:
                    self._send(NS_MED, {"type": "GET_STATUS",
                                        "requestId": self._next_req()},
                               dest=self.transport)
                if n % 5 == 0:
                    self._send(NS_HB, {"type": "PING"})
            except Exception:
                pass

    def _start_threads(self):
        self._running = True
        threading.Thread(target=self._rx_loop, daemon=True).start()
        threading.Thread(target=self._hb_loop, daemon=True).start()

    def launch_media_receiver(self, timeout=10):
        self._send(NS_RECV, {"type": "LAUNCH", "appId": CAST_APP, "requestId": self._next_req()})
        end = time.time() + timeout
        while time.time() < end and not self.transport:
            time.sleep(0.1)
        return self.transport

    def _ensure_app(self):
        if not self.transport:
            self.launch_media_receiver()
        if self.transport:
            self._send(NS_CONN, {"type": "CONNECT"}, dest=self.transport)
        return self.transport

    def media_load(self, url, content_type, meta):
        t = self._ensure_app()
        self._send(NS_MED, {"type": "LOAD", "requestId": self._next_req(), "autoplay": True,
                            "media": {"contentId": url, "streamType": "BUFFERED",
                                      "contentType": content_type,
                                      "metadata": dict(meta, metadataType=3)}}, dest=t)

    def media_cmd(self, mtype, **kw):
        payload = {"type": mtype, "requestId": self._next_req()}
        if self._media_session_id is not None:
            payload["mediaSessionId"] = self._media_session_id   # PAUSE/PLAY/STOP/SEEK require it
        payload.update(kw)
        self._send(NS_MED, payload, dest=self.transport)

    def set_volume(self, level):
        level = max(0.0, min(1.0, level))
        self._send(NS_RECV, {"type": "SET_VOLUME", "volume": {"level": level},
                             "requestId": self._next_req()})
        self.status["volume"] = int(round(level * 100))

    def close(self):
        self._running = False
        try:
            self.sock.close()
        except Exception:
            pass


def cast_session(spk):
    """Lazily create/reuse a live CastSession for a Cast device."""
    ip = spk["ip"]
    with cast_sessions_lock:
        s = cast_sessions.get(ip)
        if s is None or not s._running:
            s = CastSession(ip, spk.get("port", 8009))
            cast_sessions[ip] = s
    return s


# -- Cast play queue -----------------------------------------------------
# Chromecast has no device-side queue, so DRAAI keeps one in memory per
# Cast device and auto-advances on track end via CastSession._advance_cb.

cast_queues = {}                 # ip -> {"ids":[...], "idx":int}
cast_queues_lock = threading.Lock()
CAST_BAD_EXTS = {".aiff", ".aif"}   # Chromecast cannot decode AIFF (or ALAC .m4a — surfaced at LOAD)


def _cast_playable_ids(ids):
    good = [tid for tid in ids
            if not (tracks_by_id.get(tid) and tracks_by_id[tid].get("ext") in CAST_BAD_EXTS)]
    if not good:
        raise RuntimeError("That file can't play on Chromecast (AIFF/Apple Lossless) — convert it to FLAC.")
    return good


def _cast_load_index(ip, sess, idx):
    q = cast_queues.get(ip)
    if not q or not (0 <= idx < len(q["ids"])):
        return
    q["idx"] = idx
    t = tracks_by_id.get(q["ids"][idx])
    if not t:
        return
    ct = AUDIO_EXTS.get(t.get("ext"), "audio/mpeg")
    meta = {"title": t.get("title") or "", "artist": t.get("artist") or ""}
    sess.media_load(media_url(t, ip), ct, meta)


def _cast_advance(ip):
    q = cast_queues.get(ip)
    sess = cast_sessions.get(ip)
    if not q or not sess:
        return
    nxt = q["idx"] + 1
    if nxt < len(q["ids"]):
        _cast_load_index(ip, sess, nxt)


def cast_play_tracks(spk, ids):
    ip = spk["ip"]
    ids = _cast_playable_ids(ids)
    with cast_queues_lock:
        cast_queues[ip] = {"ids": list(ids), "idx": 0}
    sess = cast_session(spk)
    sess._advance_cb = lambda: _cast_advance(ip)
    _cast_load_index(ip, sess, 0)


def cast_enqueue_tracks(spk, ids, play_next=False):
    ip = spk["ip"]
    ids = _cast_playable_ids(ids)
    q = cast_queues.get(ip)
    if not q or not q["ids"]:
        return cast_play_tracks(spk, ids)
    with cast_queues_lock:
        if play_next:
            pos = q["idx"] + 1
            q["ids"][pos:pos] = ids
        else:
            q["ids"].extend(ids)


def cast_browse_queue(spk):
    q = cast_queues.get(spk["ip"], {"ids": [], "idx": 0})
    items = []
    for i, tid in enumerate(q["ids"]):
        t = tracks_by_id.get(tid)
        items.append({"no": i + 1, "id": tid, "title": (t["title"] if t else tid)})
    return items, len(items)


def cast_queue_jump(spk, no):
    ip = spk["ip"]
    sess = cast_session(spk)
    _cast_load_index(ip, sess, int(no) - 1)


def cast_queue_remove(spk, no):
    ip = spk["ip"]
    q = cast_queues.get(ip)
    if not q:
        return
    i = int(no) - 1
    if not (0 <= i < len(q["ids"])):
        return
    with cast_queues_lock:
        del q["ids"][i]
        if i < q["idx"]:
            q["idx"] -= 1
        elif i == q["idx"]:
            # removed the playing track: load whatever now sits at idx (clamped)
            q["idx"] = min(q["idx"], len(q["ids"]) - 1)
            if q["ids"]:
                _cast_load_index(ip, cast_session(spk), q["idx"])


def cast_queue_move(spk, from_no, to_no):
    q = cast_queues.get(spk["ip"])
    if not q:
        return
    a, b = int(from_no) - 1, int(to_no) - 1
    if not (0 <= a < len(q["ids"])) or not (0 <= b < len(q["ids"])):
        return
    with cast_queues_lock:
        cur = q["ids"][q["idx"]] if q["ids"] else None
        q["ids"].insert(b, q["ids"].pop(a))
        if cur is not None:
            q["idx"] = q["ids"].index(cur)     # keep pointing at the same track


def cast_set_volume(spk, value):
    cast_session(spk).set_volume(float(value) / 100.0)


def cast_set_room_volume(spk, value):
    cast_session(spk).set_volume(float(value) / 100.0)


def cast_seek_to(spk, seconds):
    cast_session(spk).media_cmd("SEEK", currentTime=float(seconds))


def cast_set_shuffle(spk, on):
    import random
    q = cast_queues.get(spk["ip"])
    if not q or not q["ids"] or not on:
        return
    with cast_queues_lock:
        cur = q["ids"][q["idx"]]
        rest = [x for i, x in enumerate(q["ids"]) if i != q["idx"]]
        random.shuffle(rest)
        q["ids"] = [cur] + rest
        q["idx"] = 0


def cast_get_status(spk):
    ip = spk["ip"]
    sess = cast_sessions.get(ip)
    q = cast_queues.get(ip, {"ids": [], "idx": 0})
    cur_id = q["ids"][q["idx"]] if q["ids"] and 0 <= q["idx"] < len(q["ids"]) else None
    t = tracks_by_id.get(cur_id) if cur_id else None
    st = sess.status if sess else {}
    return {"state": st.get("state", "STOPPED"),
            "title": (t["title"] if t else ""),
            "position": st.get("position", "0:00:00"),
            "duration": st.get("duration", "0:00:00"),
            "volume": st.get("volume"),
            "track_no": (q["idx"] + 1) if q["ids"] else None,
            "track_id": cur_id}


def cast_cmd(spk, action, value=None):
    """Cast equivalents for the /api/cmd transport actions."""
    ip = spk["ip"]
    sess = cast_session(spk)
    if action == "pause":
        sess.media_cmd("PAUSE")
    elif action == "resume":
        sess.media_cmd("PLAY")
    elif action == "stop":
        sess.media_cmd("STOP")
    elif action == "next":
        q = cast_queues.get(ip)
        if q:
            _cast_load_index(ip, sess, q["idx"] + 1)
    elif action == "prev":
        q = cast_queues.get(ip)
        if q:
            _cast_load_index(ip, sess, q["idx"] - 1)
    elif action == "clearqueue":
        with cast_queues_lock:
            cast_queues.pop(ip, None)
        sess.media_cmd("STOP")
    else:
        raise RuntimeError("That action isn't available on Chromecast.")
