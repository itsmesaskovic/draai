# DRAAI v2 — Google Cast backend — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add Google Cast (Chromecast / Chromecast Audio / Cast groups) as a second speaker backend in DRAAI, same UI and media server as Sonos, under the one-file/stdlib-only rules.

**Architecture:** Every device gets a `backend` field (`"sonos"`/`"cast"`). The ~15 procedural control functions gain a one-line `if backend=="cast"` guard dispatching to sibling `cast_*` functions. Cast needs a persistent TLS+heartbeat connection per device (`CastSession`) — unlike stateless Sonos SOAP — and an engine-managed queue (the Default Media Receiver is single-media). The existing Range HTTP media server, `media_url`, per-codec Content-Type, art, analysis, resume, and the whole UI are reused unchanged. Cast groups appear as ordinary Cast devices → synced multi-room for free.

**Tech stack:** Python stdlib only (`socket`, `struct`, `ssl`, `json`, `threading`) in `sonos_player.py`; inline JS/CSS in `player_ui.html`. Design doc: `docs/superpowers/specs/2026-07-15-draai-v2-cast-backend-design.md`. Proven protocol recipe: `scratchpad/cast_spike.py`.

## Global Constraints

- `sonos_player.py` stays **ONE file, stdlib ONLY** — no pip, no new modules. `player_ui.html` stays **ONE offline file**. (CLAUDE.md hard rules 1–2.)
- No cloud/accounts/telemetry. No transcoding. Errors shown to users are **human sentences**, not stack traces (rule 5). Copy is short/warm English (rule 6).
- The **Sonos path must remain byte-for-byte behaviorally unchanged** — `python3 tests/test_draai.py` (SoapMock, 15 tests incl. real queue semantics) stays green after every task.
- Cast protocol facts (verbatim from the proven spike): device port **8009**, TLS **unverified context** (per-device self-signed certs — isolate to Cast sockets only), Default Media Receiver app id **`CC1AD845`**, namespaces `urn:x-cast:com.google.cast.tp.connection` / `.tp.heartbeat` / `.receiver` / `.media`, mDNS service **`_googlecast._tcp.local`** on **224.0.0.251:5353**, volume **0.0–1.0 float**.
- Cast lossless = **FLAC / WAV only** (no ALAC, no AIFF); FLAC up to **96kHz/24-bit**; DRAAI **passes bytes through** (no re-encode).
- **Commits are the maintainer's** — each task ends at a verified checkpoint; do not commit.
- Real-hardware Cast targets on the maintainer's LAN: **CC Audio Zolder** and **CC Woonkamer** (IPs are DHCP — discover, don't hardcode).

---

## Phase 1 — Cast wire primitives (pure, unit-tested, no hardware)

### Task 1: CastMessage protobuf codec

**Files:** Modify `sonos_player.py` (new section near the networking helpers). Test: `tests/test_draai.py`.

**Interfaces produced:** `cast_frame(namespace, payload_dict, source, dest) -> bytes` (4-byte length prefix + protobuf); `cast_parse_frame(msg_bytes) -> {"source","dest","namespace","payload"}`.

- [ ] **Step 1: Write failing tests** in `tests/test_draai.py`:
```python
def test_cast_frame_roundtrip():
    import json
    f = sp.cast_frame("urn:x-cast:com.google.cast.receiver",
                      {"type": "LAUNCH", "appId": "CC1AD845"}, "sender-0", "receiver-0")
    # 4-byte big-endian length prefix
    import struct
    n = struct.unpack(">I", f[:4])[0]
    assert n == len(f) - 4
    d = sp.cast_parse_frame(f[4:])
    assert d["namespace"] == "urn:x-cast:com.google.cast.receiver"
    assert d["source"] == "sender-0" and d["dest"] == "receiver-0"
    assert json.loads(d["payload"]) == {"type": "LAUNCH", "appId": "CC1AD845"}
```

- [ ] **Step 2: Run — expect FAIL** (`cast_frame` undefined): `python3 tests/test_draai.py -k cast_frame`

- [ ] **Step 3: Implement** (verbatim from the spike — proven wire format):
```python
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
    d = lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else ""
    return {"source": d(f.get(2, b"")), "dest": d(f.get(3, b"")),
            "namespace": d(f.get(4, b"")), "payload": d(f.get(6, b""))}
```

- [ ] **Step 4: Run — expect PASS.**  - [ ] **Step 5: Checkpoint** (leave staged).

### Task 2: mDNS query + DNS response parser

**Files:** Modify `sonos_player.py`. Test: `tests/test_draai.py`.

**Interfaces produced:** `_dns_parse_name(data, i) -> (name, next_i)` (handles 0xC0 compression); `_mdns_parse_packet(data) -> {"ptr":{}, "srv":{}, "a":{}, "txt":{}}`; `_mdns_query_packet() -> bytes`.

- [ ] **Step 1: Write failing test** using a captured `_googlecast._tcp` response fixture (hand-craft a minimal packet with a compression pointer). Test asserts the parser extracts an A record IP and a TXT `fn=` value. (Build the fixture bytes in the test from the same encoder as `_mdns_query_packet` + appended answer records so it is self-contained — no network.)
```python
def test_mdns_parse_name_compression():
    # "_googlecast._tcp.local" then a pointer back to it
    import struct
    base = sp._dns_encode_name("_googlecast._tcp.local")
    blob = b"\x00\x00" + base + b"\xc0\x02"        # pointer at offset 2
    name, i = sp._dns_parse_name(blob, 2)
    assert name == "_googlecast._tcp.local"
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** the parser + query builder (verbatim from the spike: `_enc_name`→`_dns_encode_name`, `_parse_name`→`_dns_parse_name`, `_parse_records`→`_mdns_parse_records`, and `_mdns_query_packet` building the PTR/QTYPE=12 question). Copy the spike's `mdns_discover` body split into: `_mdns_query_packet()`, and a socket loop `cast_discover(timeout)` (Phase 2). The pure parsing functions are what this task ships + tests.

- [ ] **Step 4: Run — expect PASS.**  - [ ] **Step 5: Checkpoint.**

---

## Phase 2 — Cast discovery integrated

### Task 3: `cast_discover()` + merge into the device list with a `backend` field

**Files:** Modify `sonos_player.py` (`refresh_speakers`, `speaker_by_uuid`, `get_rooms`/`/api/state` builder). Test: `tests/test_draai.py`.

**Interfaces:** `cast_discover(timeout=4.0) -> [ {"uuid":"CAST_<id>","name":<fn>,"ip":...,"port":8009,"backend":"cast","is_group":bool,"members":[self]} ]`. Sonos devices gain `"backend":"sonos"`. `speaker_by_uuid` returns whichever.

- [ ] **Step 1: Write failing test:** monkeypatch `sp.cast_discover` to return one fake Cast device and `sp.ssdp_discover` to return none; assert the merged `/api/state` (or `get_rooms`) contains the Cast device with `backend=="cast"` and that `speaker_by_uuid("CAST_x")` resolves it. Also assert existing SoapMock Sonos tests still stamp `backend=="sonos"`.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement.**
  - `cast_discover`: open the multicast socket (from the spike's `mdns_discover` socket setup: `SO_REUSEADDR`+`SO_REUSEPORT`, bind `("",5353)`, `IP_ADD_MEMBERSHIP` 224.0.0.251, send `_mdns_query_packet()`, collect for `timeout`, re-query on idle), parse with `_mdns_parse_packet`, build device dicts. `uuid = "CAST_" + txt.get("id", srv_target)`. `is_group = txt.get("md","")` contains "Group" or txt has `nid`. `name = txt.get("fn", instance)`. Wrap the whole thing in try/except → return `[]` on any socket error (so a Cast-less LAN degrades cleanly).
  - In `refresh_speakers`: stamp `backend="sonos"` on Sonos devices; run `cast_discover()` (in a thread, joined with a timeout, alongside SSDP) and extend the device list. Keep the startup retry behavior.
  - `speaker_by_uuid`: unchanged if it already scans the merged list; else include Cast devices.

- [ ] **Step 4: Run — expect PASS** (Cast merged; Sonos unaffected).  - [ ] **Step 5: Real-hardware check:** start the engine, confirm `/api/state` lists "CC Audio Zolder" with `backend:"cast"` alongside the Sonos rooms.  - [ ] **Step 6: Checkpoint.**

---

## Phase 3 — CastSession (connection, heartbeat, cached status)

### Task 4: `CastSession` class + registry (CastMock-tested)

**Files:** Modify `sonos_player.py`. Test: `tests/test_draai.py` (add a `CastMock` transport).

**Interfaces produced:**
- `class CastSession:` with `connect()`, `send(namespace, payload, dest=None)`, `launch_media_receiver() -> transportId`, `media_load(url, content_type, meta)`, `media_cmd(type, **kw)` (PLAY/PAUSE/STOP/SEEK), `set_volume(level)`, and a cached `self.status = {"playerState","currentTime","duration","curId","volume"}` kept fresh by a background receiver loop; PING/PONG heartbeat thread; `close()`.
- `cast_session(spk) -> CastSession` — registry `cast_sessions[spk["ip"]]`, lazy-create + reconnect.
- A seam for tests: `CastSession(ip, sock_factory=...)` so a `CastMock` socket can be injected (feeds framed CastMessages, records sent frames).

- [ ] **Step 1: Write failing tests** with a `CastMock` (in-memory duplex that frames/unframes via `cast_frame`/`cast_parse_frame`): assert that after `launch_media_receiver()` the session sends a `LAUNCH CC1AD845` frame and, fed a `RECEIVER_STATUS` with an app, returns its `transportId`; assert a fed `MEDIA_STATUS` updates `session.status["playerState"]` and `currentTime`; assert a `PING` from the mock yields a `PONG` send.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** (structure from the spike's `Conn` + `run_cast`, refactored into the class): TLS via `ssl._create_unverified_context()` **only here**; length-framed send/recv under a `threading.Lock`; a daemon receiver thread parsing frames and updating `self.status` (from `MEDIA_STATUS`) / `self.transport` (from `RECEIVER_STATUS`) and replying PONG to PINGs; a daemon heartbeat thread (PING every 5s); reconnect on socket error. Never block the HTTP server threads (control calls send + return; status is read from the cache).

- [ ] **Step 4: Run — expect PASS.**  - [ ] **Step 5: Checkpoint.**

---

## Phase 4 — Backend dispatch + Cast control + engine-managed queue

### Task 5: Backend dispatch guards (Sonos unchanged)

**Files:** Modify `sonos_player.py` (the ~15 control functions). Test: `tests/test_draai.py`.

- [ ] **Step 1:** Add to the TOP of each of `play_tracks, set_volume, seek_to, get_status, get_transport_state, set_shuffle, browse_queue, enqueue_tracks, queue_move, queue_jump, queue_remove, set_room_volume` the guard:
```python
    if spk.get("backend") == "cast":
        return cast_<name>(spk, <same args>)
```
and to `get_eq/set_eq/group_join/group_leave`:
```python
    if spk.get("backend") == "cast":
        raise RuntimeError("That control isn't available on Chromecast.")
```
- [ ] **Step 2:** Run `python3 tests/test_draai.py` — **all existing Sonos tests must still pass** (the guard is inert when `backend!="cast"`).  - [ ] **Step 3: Checkpoint.**

### Task 6: Cast control functions (single-track LOAD, Option A) + engine-managed queue

**Files:** Modify `sonos_player.py`. Test: `tests/test_draai.py` (CastMock).

**Interfaces:** `cast_play_tracks`, `cast_set_volume`, `cast_seek_to`, `cast_get_status`, `cast_get_transport_state`, `cast_set_shuffle`, `cast_browse_queue`, `cast_enqueue_tracks`, `cast_queue_move`, `cast_queue_jump`, `cast_queue_remove`, `cast_set_room_volume`. Plus module state `cast_queues[ip] = {"ids":[...], "idx":int}`.

- [ ] **Step 1: Write failing CastMock tests:** `cast_play_tracks(spk, [a,b])` LOADs the media URL for `a` (assert a LOAD frame with `contentId` = `media_url(track_a, ip)` and `contentType` from `AUDIO_EXTS`), sets `cast_queues[ip]={"ids":[a,b],"idx":0}`; a fed `MEDIA_STATUS playerState=IDLE idleReason=FINISHED` advances to `b` and LOADs it; `cast_queue_jump(spk, 2)` LOADs `b`; `cast_get_status` returns `curId=a` and the cached `currentTime`; casting an ALAC/AIFF track raises the human-sentence error.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement:**
  - `cast_play_tracks(spk, ids)`: filter to Cast-playable (reject `.m4a` ALAC and `.aiff` with the human error; a quick check: allow ext in a `CAST_OK_EXTS` set = mp3/flac/wav/aac/ogg/opus/m4a-if-aac — for `.m4a` you can't cheaply tell ALAC vs AAC, so attempt and surface the receiver's LOAD_FAILED as the friendly error), set `cast_queues[ip]`, `sess.media_load(media_url(t0, ip), AUDIO_EXTS[ext], meta)`.
  - Queue auto-advance: in `CastSession`'s receiver loop, on `playerState=="IDLE" and idleReason=="FINISHED"`, call a registered callback → advance `cast_queues[ip]["idx"]` and load next (guard end-of-queue).
  - `cast_browse_queue`: return `[{"no":i+1,"id":id,"title":tracks_by_id[id]["title"]} for i,id in enumerate(cast_queues[ip]["ids"])]` (same shape the UI expects).
  - `cast_enqueue_tracks(play_next)`: append / insert after idx.
  - `cast_queue_move/jump/remove`: reorder/select/delete in the list; `jump` reloads.
  - `cast_set_volume`/`cast_set_room_volume`: `sess.set_volume(value/100.0)`.
  - `cast_seek_to(spk, sec)`: `sess.media_cmd("SEEK", currentTime=sec)`.
  - `cast_get_status`: read `sess.status`, map to the same dict `get_status` returns for Sonos (`state`, `title`, `position`, `duration`, `volume`, current `id`).
  - `cast_set_shuffle`: shuffle the in-memory `ids` (engine-side; Cast has no shuffle flag).

- [ ] **Step 4: Run — expect PASS.**  - [ ] **Step 5: Real-hardware check (CC Audio Zolder):** from the running engine, select the Zolder room and: play a track → hear it; pause/play/seek; volume; add another track and confirm auto-advance at track end; queue view shows the engine queue. Confirm the Sonos rooms still behave identically.  - [ ] **Step 6: Checkpoint.**

---

## Phase 5 — UI polish

### Task 7: Backend badge, hide Sonos-only controls for Cast, friendly cast errors

**Files:** Modify `player_ui.html`.

- [ ] **Step 1:** In `renderRooms`, add a small badge when `g.backend==="cast"` (e.g. a "Cast" chip next to the name; Sonos rooms unchanged). `/api/state` now includes `backend` per device.
- [ ] **Step 2:** When the selected speaker `A.speaker.backend==="cast"`, hide the EQ button/deck and the Sonos room-grouping affordance (the `data-group` control), since those raise "not available on Chromecast". Keep everything else (play/queue/volume/sleep/search/import) working.
- [ ] **Step 3:** Surface cast LOAD failures as a toast with the engine's human sentence (the API already returns human-sentence errors; ensure the UI shows them for the play/enqueue path).
- [ ] **Step 4: Real-hardware check:** Zolder room shows the Cast badge, no EQ/grouping; casting an ALAC file shows the friendly toast; a Sonos room is unchanged.  - [ ] **Step 5: Checkpoint.**

---

## Phase 6 — (Later refinement) near-gapless via Cast queue preload

**Deferred.** Replace per-track LOAD with the Cast media queue (`QUEUE_LOAD` + `QueueItem.preloadTime`) so the receiver pre-buffers the next track. Only pursue if track-gap is bothersome; baseline (Phase 4) ships first.

---

## Self-Review

**Spec coverage:** unified `backend` field (T3) · dispatch (T5) · CastSession/heartbeat/status (T4) · mDNS discovery (T2/T3) · protobuf codec (T1) · Cast control + engine queue (T6) · groups-as-devices (free via T3 discovery) · hi-res FLAC pass-through (reused media server; ALAC/AIFF rejection in T6) · UI badge/hide/errors (T7) · Sonos untouched (T5 guard + tests each task) · no cross-backend sync (never grouped across backends). ✓

**Placeholder scan:** the protobuf codec is complete verbatim code; mDNS/CastSession/control reference the *proven spike* (`scratchpad/cast_spike.py`) as the exact source and describe the refactor precisely — the implementer copies proven code, not invents it. The one soft spot is ALAC-vs-AAC detection in `.m4a` (can't tell cheaply) — resolved explicitly by attempting LOAD and surfacing the receiver's failure as the human-sentence error. ✓

**Type/name consistency:** `cast_frame`/`cast_parse_frame` (T1) used by `CastSession` (T4) and control (T6); `cast_discover` device dict shape (T3) consumed by `speaker_by_uuid`/dispatch; `cast_get_status` returns the SAME dict shape as Sonos `get_status`; `cast_browse_queue` returns the SAME shape the queue UI already renders. ✓

**Scope:** Phases 1–2 are pure/no-hardware and independently testable; 3–4 need CastMock + real hardware; 5 is UI. Each phase yields working, testable software. Phase 6 is explicitly deferred. ✓
