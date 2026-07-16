# Google Cast backend

> Pure-stdlib CASTV2 client (mDNS discovery + hand-rolled protobuf over TLS) that lets DRAAI
> drive Chromecast / Chromecast Audio devices with the same play/queue/volume surface used for
> Sonos — no `zeroconf`, no `pychromecast`, no `protobuf` package.

## Purpose

DRAAI's hard rule is stdlib-only, ever (`CLAUDE.md`). Adding a second speaker "brand" without a
dependency meant reimplementing the two pieces normally provided by `zeroconf` and
`pychromecast`/`protobuf`: multicast DNS service discovery, and the Cast wire protocol
(CASTV2 — a length-prefixed protobuf `CastMessage` over TLS). `draai/cast.py` does both in
~570 lines, then layers an engine-managed play queue on top because the Default Media Receiver
has no queue of its own. The v2 roadmap bullet in `CLAUDE.md` — "Chromecast/Google Cast backend,
keep dependency-free, same UI, speakers become a second backend type" — is realized entirely by
this file plus a dispatch seam in `draai/backends.py`.

## Where it lives

- `draai/cast.py` — the entire backend: protobuf encode/decode, mDNS discovery, `CastSession`
  (TLS transport + heartbeat + status cache), and the Cast play/queue/volume functions.
- `draai/backends.py` — the dispatch seam. `refresh_speakers()` merges Sonos (SSDP) and Cast
  (mDNS) devices into one list, each stamped `backend: "sonos"` or `"cast"`
  (`draai/backends.py:89`, `draai/backends.py:94`). Every control function (`play_tracks`,
  `set_volume`, `seek_to`, `get_status`, `set_shuffle`, `browse_queue`, `enqueue_tracks`,
  `queue_move`, `queue_jump`, `queue_remove`, `set_room_volume`) opens with an
  `if spk.get("backend") == "cast": return cast_<fn>(spk, …)` guard before falling through to
  the Sonos SOAP path (`draai/backends.py:250-251`, `:350-351`, `:407-408`, `:421-422`,
  `:428-429`, `:449-450`, `:479-480`, `:491-492`, `:501-502`, `:510-511`, `:682-683`).
  EQ and dynamic grouping are Sonos-only and raise a friendly `RuntimeError` for Cast devices
  (`draai/backends.py:585-586`, `:602-603`, `:639-640`, `:653-654`).
- `docs/superpowers/specs/2026-07-15-draai-v2-cast-backend-design.md` — the design doc; the
  spike behind it ran against real hardware ("CC Woonkamer" TV Chromecast, "CC Audio Zolder"
  Chromecast Audio) before this code was written.
- `tests/test_draai.py` — protobuf round-trip, mDNS name (de)compression, `CastSession`
  status/ping/finish handling via a `MockSock`, discovery-merge, EQ/grouping guards, and a full
  play/advance/queue test using a mocked `CastSession` (`tests/test_draai.py:369-529`).

## Key concepts

### mDNS discovery (`cast_discover`, `draai/cast.py:137-193`)

Cast devices advertise via multicast DNS on `224.0.0.251:5353` under the service name
`_googlecast._tcp.local`. `cast_discover()`:
1. Opens a UDP socket, joins the `224.0.0.251` multicast group, and sends a PTR query for
   `_googlecast._tcp.local` (`_mdns_query_packet`, `draai/cast.py:121-124`).
2. Listens for up to `timeout` seconds (default 4s), re-sending the query on each 0.6s read
   timeout, and parses every reply packet (`draai/cast.py:157-170`).
3. `_mdns_parse_packet` (`draai/cast.py:126-134`) walks the DNS header's question/answer/
   authority/additional counts and hands each resource record to `_mdns_parse_records`
   (`draai/cast.py:97-119`), which understands four record types: PTR (12, service instance
   name), SRV (33, target hostname + port), A (1, IPv4 address), and TXT (16, `key=value` pairs
   including `fn` = friendly name and `id` = device id).
4. Name parsing (`_dns_parse_name`, `draai/cast.py:77-95`) implements DNS name compression
   (pointer bytes with the top two bits set, `0xC0`) since mDNS responses reuse earlier labels
   by reference rather than repeating them.
5. After the listen window, `cast_discover` joins PTR → SRV → A → TXT by instance name to build
   device dicts: `{"uuid": "CAST_<txt id or hostname>", "name": <fn>, "ip", "port", "backend":
   "cast", "is_group": <bool from TXT "md"/"nid">, "members": [...]}` (`draai/cast.py:175-190`).
   `is_group` flags a Cast speaker group (built in Google Home) — DRAAI treats it as one more
   device, since a Cast group already syncs playback natively.
6. The whole function is wrapped in a bare `try/except: pass` and returns `[]` on any network
   error — discovery must never raise into `refresh_speakers()`.

### The hand-rolled protobuf wire format (`draai/cast.py:17-66`)

CASTV2's `CastMessage` proto has (for DRAAI's purposes) six fields — protocol_version(1),
source_id(2), destination_id(3), namespace(4), payload_type(5), payload_utf8(6) — which is
small and stable enough that hand-encoding beats vendoring a `.proto` compiler or the `protobuf`
pip package (the hard stdlib-only rule leaves no other option). Protobuf's wire format is
tag+value pairs, where the tag packs the field number and wire type
(`(field_num << 3) | wire_type`):
- **Varint** (wire type 0, used for `protocol_version` and `payload_type`, both always 0):
  `_cast_varint` (`draai/cast.py:17-23`) emits 7 bits per byte, high bit set on every byte except
  the last (base-128 continuation encoding). `_cast_fv` (`:25-26`) is "field varint": tag varint
  + value varint.
- **Length-delimited** (wire type 2, used for the three string fields and payload):
  `_cast_fl` (`:28-31`) emits tag varint + length varint + raw bytes. The JSON payload
  (`json.dumps(payload)`) goes in field 6 this way — Cast's `payload_utf8` is just a UTF-8 JSON
  blob, so no nested proto schema is needed for the actual command bodies.
- `cast_frame` (`:33-41`) assembles a full message and prepends a 4-byte big-endian length
  prefix (`struct.pack(">I", len(m))`) — CASTV2 frames every message with its length outside the
  protobuf body, since protobuf itself is not self-delimiting on a stream socket.
- Decoding is symmetric: `_cast_rvarint` (`:43-49`) reads a varint back out, and
  `cast_parse_frame` (`:52-66`) walks tag/value pairs, branching on wire type (0 = varint via
  `_cast_rvarint`, 2 = length-delimited slice), and only decodes the fields DRAAI actually reads
  (source, dest, namespace, payload) — it stops at any other wire type rather than trying to skip
  it generically, since none of the fields DRAAI sends/receives need that.

### Transport: TLS on 8009 with an unverified context

`CastSession._connect` (`draai/cast.py:224-232`) opens a plain TCP socket to
`(ip, 8009)` and wraps it with `ssl._create_unverified_context().wrap_socket(raw)`
(`draai/cast.py:229`). Every Cast device presents its own self-signed certificate with no shared
CA, so there is nothing a normal `ssl.create_default_context()` could verify against — using the
unverified context is the standard, correct approach for local Cast connections (matching what
the design spec calls out explicitly), not a shortcut. This is scoped to Cast sockets only; no
other part of the engine touches `ssl`.

### Namespaces and message flow

Four Cast namespaces are used, each a `urn:x-cast:com.google.cast.*` string
(`draai/cast.py:196-199`):
- `NS_CONN` (`tp.connection`) — `CONNECT` to open a logical channel to a destination (the
  receiver, or later the launched app's `transportId`).
- `NS_HB` (`tp.heartbeat`) — `PING`/`PONG` keepalive.
- `NS_RECV` (`receiver`) — `LAUNCH` an app, `GET_STATUS`/`RECEIVER_STATUS`, `SET_VOLUME`.
- `NS_MED` (`media`) — `LOAD`, `PLAY`, `PAUSE`, `STOP`, `SEEK`, `GET_STATUS`/`MEDIA_STATUS`.

`CAST_APP = "CC1AD845"` (`draai/cast.py:200`) is the Default Media Receiver app id — the generic
Cast receiver that plays a URL DRAAI hands it, with no custom receiver app needed.

Connection sequence, driven by `CastSession.__init__` → `_connect`:
1. TLS connect, then immediately send `CONNECT` on `tp.connection` and `PING` on `tp.heartbeat`
   to `receiver-0` (`draai/cast.py:231-232`).
2. Two background daemon threads start (`_start_threads`, `:337-340`): `_rx_loop` continuously
   reads framed messages and dispatches them to `_handle`; `_hb_loop` sends `PING` roughly every
   5s and polls media status roughly every 1s (see Position tracking below).
3. `_handle` (`draai/cast.py:257-303`) answers incoming `PING` with `PONG`, and on
   `RECEIVER_STATUS` extracts the launched app's `transportId`/`sessionId` when its `appId`
   matches `CAST_APP`, plus the receiver-level volume.

### CastSession lifecycle: connect → launch → LOAD → control

- `launch_media_receiver` (`draai/cast.py:342-347`) sends `LAUNCH` with `appId=CAST_APP` on the
  receiver channel and polls (100ms ticks, up to 10s) until `self.transport` is set by the
  `RECEIVER_STATUS` handler above.
- `_ensure_app` (`:349-354`) lazily launches if needed, then sends a fresh `CONNECT` addressed to
  the app's own `transportId` — the receiver-level connection and the app-level connection are
  separate logical channels in Cast, both required.
- `media_load` (`:356-361`) sends `LOAD` on `media` to the app transport with `autoplay: true`,
  a `contentId` (DRAAI's `/media/<id>/<name>` URL), `contentType` (from `AUDIO_EXTS`), and
  `streamType: "BUFFERED"`.
- `media_cmd` (`:363-368`) is the shared helper for `PLAY`/`PAUSE`/`STOP`/`SEEK`: it always
  stamps a fresh `requestId` and, critically, includes `mediaSessionId` whenever one is known.
- `set_volume` (`:370-374`) sends `SET_VOLUME` on the receiver channel with a `0.0-1.0` level.
- `close` (`:376-381`) flips `_running = False` and closes the socket; `_rx_loop` treats any recv
  error as a signal to sleep 1s and try `_connect()` again if still running (`:305-317`), giving
  the session basic reconnect resilience.
- `cast_session(spk)` (`:384-392`) is the module-level lazy singleton: one `CastSession` per
  device IP, kept in `cast_sessions = {ip: CastSession}` under `cast_sessions_lock`, recreated if
  missing or no longer `_running`.

### Position tracking: why the heartbeat loop also polls media status

The Default Media Receiver does not push periodic `currentTime` updates on its own — a
`MEDIA_STATUS` broadcast only arrives on state transitions (load, play, pause, finish) unless
something explicitly asks. So `_hb_loop` (`draai/cast.py:320-335`) sends `GET_STATUS` on the
`media` namespace to the app transport roughly once per second (every loop tick), and sends the
heartbeat `PING` only every 5th tick (`n % 5 == 0`). Each `GET_STATUS` reply comes back through
`_handle`'s `MEDIA_STATUS` branch (`:280-303`), which updates `self.status["position"]` from
`stt["currentTime"]`. Without this poll, DRAAI's progress bar / resume position would freeze
between real state transitions — this loop is what makes the Cast playhead advance visibly in
the UI, exactly like Sonos's own periodic `GetPositionInfo` polling does for that backend.

### `mediaSessionId`: required on PAUSE/STOP/SEEK

`MEDIA_STATUS` events carry a `mediaSessionId` identifying the currently loaded media item;
`_handle` captures it into `self._media_session_id` whenever present (`draai/cast.py:282-283`).
`media_cmd` (`:363-368`) attaches it to every outgoing `PAUSE`/`PLAY`/`STOP`/`SEEK` payload when
known. This is load-bearing: the receiver silently ignores media control commands that omit or
mismatch the current `mediaSessionId` (a known CASTV2 behavior, and the reason this field is
threaded through rather than left off).

### Codec / hi-res matrix

- **Chromecast Audio is the hi-res target device** (per the design spec and `CLAUDE.md`'s
  roadmap). DRAAI serves the original file byte-for-byte via the existing Range-capable
  `/media/<id>/<name>` endpoint — the Cast device decodes it directly, no transcoding.
  `AUDIO_EXTS` (`draai/constants.py:5-14`) supplies the `Content-Type` per extension: `.mp3` →
  `audio/mpeg`, `.m4a` → `audio/mp4`, `.aac` → `audio/aac`, `.flac` → `audio/flac`, `.wav` →
  `audio/wav`, `.aiff`/`.aif` → `audio/aiff`, `.ogg` → `audio/ogg`.
- **What plays cleanly**: FLAC (including hi-res up to 96kHz/24-bit on Chromecast Audio), WAV,
  MP3, AAC — all pass through untouched, matching Cast's documented supported-media list.
- **What does NOT play**: AIFF is blocked pre-emptively. `CAST_BAD_EXTS = {".aiff", ".aif"}`
  (`draai/cast.py:401`) is checked by `_cast_playable_ids` (`:404-409`), which filters those
  extensions out of any id list before queueing and raises a human-sentence `RuntimeError`
  ("That file can't play on Chromecast (AIFF/Apple Lossless) — convert it to FLAC.") if nothing
  playable remains.
- **ALAC is the one gap the extension check can't catch**: Apple Lossless files also use `.m4a`
  — indistinguishable by extension from AAC `.m4a`, which *does* play on Cast — so
  `CAST_BAD_EXTS` cannot filter them out in advance. The code comment at `draai/cast.py:401`
  notes this is "surfaced at LOAD" instead: an ALAC file will be sent to the device and fail
  there rather than being caught client-side. This is a known, accepted gap, not an oversight —
  worth flagging to anyone extending this code that pre-LOAD ALAC detection would need to sniff
  file contents, not just the extension.

### Engine-managed queue

The Default Media Receiver has no device-side queue (unlike Sonos, which owns a real
`AVTransport` queue), so DRAAI keeps one in memory per Cast device IP:
`cast_queues = {ip: {"ids": [...], "idx": int}}` (`draai/cast.py:399-401`).
- `cast_play_tracks` (`:435-442`) filters to playable ids, replaces the queue, wires
  `sess._advance_cb` to `_cast_advance`, and LOADs index 0.
- `_cast_advance` (`:425-432`) is called by `CastSession._handle` when `MEDIA_STATUS` reports
  `playerState == "IDLE"` with `idleReason == "FINISHED"` (`draai/cast.py:297-302`) — i.e. the
  track played to completion — and LOADs the next index if one exists. This is DRAAI's
  auto-advance for Cast, playing the same architectural role Sonos's device-side queue plays
  natively.
- `cast_enqueue_tracks`, `cast_browse_queue`, `cast_queue_jump`, `cast_queue_remove`,
  `cast_queue_move` (`:445-505`) all operate on this in-memory list; because DRAAI itself LOADed
  the current track, the now-playing id is known directly from `q["idx"]` — no TrackURI parsing
  is needed (unlike Sonos, where now-playing identity round-trips through `GetPositionInfo`).
- `cast_set_shuffle` (`:519-529`) reshuffles everything after the currently-playing index in
  place, keeping the current track at position 0 of the new order.
- `cast_cmd` (`:548-571`) maps the shared `/api/cmd` transport actions (`pause`, `resume`,
  `stop`, `next`, `prev`, `clearqueue`) onto `media_cmd` calls plus queue index changes; any
  other action raises "That action isn't available on Chromecast."

### The backend seam and the sync limit

`refresh_speakers()` in `draai/backends.py` runs Sonos SSDP discovery and `cast_discover()`
independently, stamps each result's `backend` field, and concatenates them into one `speakers`
list (`draai/backends.py:88-96`) — from the UI's perspective there is exactly one `/api/state`
list, backend-agnostic by construction. Every control endpoint in `backends.py` is a thin
dispatcher: check `spk["backend"]`, call the Sonos SOAP path or delegate to the matching
`cast_*` function. Cast and Sonos devices can each be driven independently and simultaneously,
but **Sonos and Cast cannot play in sync with each other** — they are different protocols with
independent clocks and no shared timing reference, and DRAAI does not attempt to bridge that (see
`CLAUDE.md`'s v2 roadmap note and the design spec's "Non-goals" section). A pre-made Cast
**speaker group** (built in Google Home) is the one case that does sync natively, since Cast
groups already agree on timing at the protocol level before DRAAI ever sees them — DRAAI just
treats the group as one more `backend: "cast"` device.

## Gotchas

- **Unverified TLS is intentional, not a bug** — every Cast device's cert is self-signed with no
  shared CA; `ssl._create_unverified_context()` is correct here and must stay scoped to
  `draai/cast.py` only.
- **`mediaSessionId` must be threaded through** on PAUSE/STOP/SEEK or the receiver silently
  drops the command — there's no error, playback just doesn't respond. See
  `draai/cast.py:363-368`.
- **The heartbeat loop is doing double duty**: it's not just a keepalive, it's also the only
  reason `currentTime` advances in the UI. Don't "simplify" it into a pure PING loop.
- **ALAC `.m4a` cannot be filtered client-side** — extension-based filtering
  (`CAST_BAD_EXTS`) can't distinguish it from AAC `.m4a`; it fails at LOAD time instead
  (`draai/cast.py:401`).
- **Cast groups sync, cross-backend does not** — never build a feature that implies Sonos and
  Cast play together; the design spec calls this out as a non-goal explicitly.
- **EQ and dynamic (runtime) grouping are Sonos-only** and must keep raising a friendly
  `RuntimeError` for Cast devices, not silently no-op or crash
  (`draai/backends.py:585-586`, `:602-603`, `:639-640`, `:653-654`).
- **This backend was verified against real Chromecast + Chromecast Audio hardware** during the
  spike behind the design doc (`docs/superpowers/specs/2026-07-15-draai-v2-cast-backend-design.md`).
  Do not re-run against real hardware or play audio to "re-verify" this doc — the test suite
  (`tests/test_draai.py:369-529`, `MockSock`-based) and code reading are sufficient for changes;
  actual playback verification is real-hardware-only and off-limits for an agent session (no
  unprompted playback).

## References

- `draai/cast.py:17-66` — hand-rolled protobuf varint/length-delimited encode+decode, `cast_frame`/`cast_parse_frame`.
- `draai/cast.py:71-134` — mDNS name (de)compression and record parsing (`PTR`/`SRV`/`A`/`TXT`).
- `draai/cast.py:137-193` — `cast_discover`, the `_googlecast._tcp.local` multicast query loop.
- `draai/cast.py:196-232` — namespaces, `CAST_APP`, `CastSession._connect` (TLS + unverified context).
- `draai/cast.py:257-303` — `_handle`: PING/PONG, RECEIVER_STATUS → transportId, MEDIA_STATUS → position/state/finish.
- `draai/cast.py:305-340` — `_rx_loop` (read+reconnect) and `_hb_loop` (heartbeat + media status poll).
- `draai/cast.py:342-374` — `launch_media_receiver`, `_ensure_app`, `media_load`, `media_cmd`, `set_volume`.
- `draai/cast.py:399-529` — engine-managed queue (`cast_queues`, `cast_play_tracks`, `_cast_advance`, browse/jump/remove/move/shuffle).
- `draai/cast.py:401-409` — `CAST_BAD_EXTS` / `_cast_playable_ids` (AIFF blocked; ALAC `.m4a` not detectable by extension).
- `draai/cast.py:532-571` — `cast_get_status`, `cast_cmd` (shared transport action mapping).
- `draai/backends.py:73-100` — `refresh_speakers`: SSDP + `cast_discover` merge, `backend` stamping.
- `draai/backends.py:250-683` — per-endpoint `backend == "cast"` dispatch guards (play, status, volume, shuffle, seek, queue ops, EQ/grouping rejection).
- `tests/test_draai.py:369-529` — protobuf/mDNS unit tests, `CastSession` mock-socket tests, discovery merge, EQ/grouping guard tests, full play/advance/queue test.
- `docs/superpowers/specs/2026-07-15-draai-v2-cast-backend-design.md` — design doc and real-hardware spike notes.
- `CLAUDE.md` — v2 roadmap bullet (Cast backend, dependency-free, Sonos+Cast cannot sync).
