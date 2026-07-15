# DRAAI v2 — Google Cast (Chromecast) backend design

**Date:** 2026-07-15
**Scope:** `sonos_player.py` (engine) + `player_ui.html` (small UI additions). Adds Google Cast as a second speaker backend alongside Sonos.
**Status:** Feasibility **proven** by a real-hardware spike (`scratchpad/cast_spike.py`); this doc is the design, `.ai/plans/2026-07-15-draai-v2-cast-backend.md` is the build plan.

## Goal

Let DRAAI target Google Cast devices (Chromecast, Chromecast Audio, Cast groups) exactly like it targets Sonos — same UI, same library, same media server — under the existing hard rules (one stdlib-only engine file, one offline UI file, no cloud). The maintainer's priority device is the **Chromecast Audio** for hi-res lossless.

## What the spike proved (so this is not speculative)

Against the maintainer's real devices ("CC Woonkamer" TV Chromecast, "CC Audio Zolder" Chromecast Audio), ~250 lines of pure stdlib:
- **mDNS discovery** — hand-rolled multicast DNS (`_googlecast._tcp.local`, PTR→SRV→A→TXT with name compression). No `zeroconf`.
- **CASTV2 protocol** — hand-rolled protobuf `CastMessage` (6 fields, varint + length-delimited) + stdlib `ssl` (unverified context — every Cast device presents its own self-signed cert; this is the universal, correct approach for local Cast). Flow: TLS `:8009` → CONNECT (`tp.connection`) → PING/PONG (`tp.heartbeat`) → LAUNCH app `CC1AD845` (Default Media Receiver) → read RECEIVER_STATUS for the app `transportId` → CONNECT to it → LOAD (`media`) a DRAAI `/media/<id>/<name>` URL. The device played the track; `currentTime` advanced. Volume via receiver `SET_VOLUME` (0.0–1.0).
- **Media layer reused with zero changes** — the Chromecast fetched DRAAI's existing Range-capable HTTP URL directly.

## Hi-res / format decisions (from research)

Authoritative Cast support: **FLAC up to 96kHz/24-bit**, plus WAV (LPCM), HE-AAC/LC-AAC, MP3, Opus, Vorbis, WebM. Sources: [Cast supported media](https://developers.google.com/cast/docs/media), [CCA spec](https://support.google.com/chromecast/answer/6279377).

- **Pass-through is ideal for the Chromecast Audio.** DRAAI already serves raw FLAC as `audio/flac` with Range. The CCA decodes FLAC ≤24/96 natively → **no transcoding, ever.** DRAAI's no-transcode model is a feature here, not a limit.
- **Not Cast-playable: ALAC and AIFF.** Cast decodes FLAC/WAV for lossless, not Apple Lossless (`.m4a` ALAC) or AIFF. DRAAI serves those to Sonos fine, but casting one must fail with a **human sentence** ("This Apple Lossless / AIFF file can't play on Chromecast — convert it to FLAC.") rather than a silent stall.
- **Device setting:** true 24/96 output requires "Full dynamic range" enabled on the CCA in the Google Home app (can drop out on weak wifi) — a user setting, documented, not a DRAAI concern.
- **Cast groups sync; Sonos↔Cast does not.** A Chromecast-Audio **speaker group** (made in Google Home) advertises as its *own* Cast device — DRAAI targets it like any speaker and gets **true synced multi-room hi-res for free** ([multi-room](https://support.google.com/chromecast/answer/10012636)). This is the one sync story that works, and it costs no extra code (a group is just another `/api/state` entry).
- **Gapless:** the Default Media Receiver isn't sample-accurate gapless, but the Cast media queue's `preloadTime` gets close ([queueing](https://developers.google.com/cast/docs/android_sender/queueing)). Deferred to a later phase; baseline is per-track LOAD.

## Architecture

**1. Unified device model.** Every device carries `backend: "sonos" | "cast"`. Sonos devices come from SSDP + `GetZoneGroupState` (unchanged, get `backend:"sonos"` stamped on). Cast devices come from mDNS (`{uuid:"CAST_<txt-id>", name, ip, port, backend:"cast", is_group}`). `/api/state` merges both lists; the UI is already backend-agnostic (renders whatever `/api/state` returns).

**2. Backend dispatch.** The engine is procedural: every control endpoint does `spk = speaker_by_uuid(uuid)` → `func(spk, …)` → `soap_call(spk["ip"], …)`. Each of the ~15 control functions gains a one-line guard at the top: `if spk.get("backend")=="cast": return cast_<fn>(spk, …)`. The Sonos path is untouched (tests stay green); Cast gets sibling `cast_*` functions.

Control surface to dispatch: `play_tracks`, `set_volume`, `seek_to`, `get_status`, `get_transport_state`, `set_shuffle`, `browse_queue`, `enqueue_tracks`, `queue_move`, `queue_jump`, `queue_remove`, `set_room_volume`. Sonos-only (return a graceful "not supported on Chromecast" for Cast): `get_eq`/`set_eq`, `group_join`/`group_leave` (Cast groups are pre-made in Google Home, not built at runtime).

**3. CastSession (the one genuinely new architectural piece).** Sonos is stateless request/response SOAP; Cast needs a **persistent TLS connection with a heartbeat** per device. A `CastSession` class owns: the TLS socket, framed CastMessage send/recv (under a lock), a background heartbeat thread (PING ~5s + PONG replies), the launched-app `transportId`, and a receiver loop that caches the latest RECEIVER/MEDIA status. A module-level `cast_sessions = {ip: CastSession}` registry lazily creates and keeps sessions alive, reconnecting on drop. `cast_get_status` reads the cached MEDIA_STATUS (`currentTime`, `playerState`, current track id) → feeds `/api/status` polling, the playhead, and resume.

**4. Engine-managed queue for Cast (baseline, Option A).** The Default Media Receiver is effectively single-media, so DRAAI holds the queue in memory per Cast device (ordered track ids + index). `cast_play_tracks` sets the list and LOADs index 0; the receiver loop watches for `playerState=IDLE, idleReason=FINISHED` and LOADs the next. `browse_queue`/`enqueue`/`queue_move`/`queue_jump`/`queue_remove` operate on this in-memory list. Now-playing track id is known directly (DRAAI LOADed it) — no TrackURI parsing needed. (Option B, Cast queue + `preloadTime` for near-gapless, is a later phase.)

**5. Reused unchanged.** Media serving with Range (`/media/<id>/<name>`), `media_url` + `local_ip_facing` (pass the Cast device IP), correct per-codec Content-Type (`AUDIO_EXTS`, already includes `audio/flac`), library scan, art, waveform analysis, positions/resume, the entire UI shell.

**6. UI additions (small).** A backend badge on each room (Sonos vs Cast); hide the EQ deck and Sonos grouping controls when the selected device is Cast; a friendly error if a user casts an ALAC/AIFF file.

## Non-goals

- No Sonos↔Cast synchronized playback (different clocks/protocols — don't promise it).
- No transcoding (pass-through only; the CCA needs none).
- No EQ or runtime group-building on Cast.
- No new dependencies; engine stays one stdlib file, UI stays one offline file.

## Risks / watch-items

- **Persistent connections in a mostly-stateless engine** — the CastSession threads + registry are new; needs clean lifecycle (reconnect, teardown, thread-safety with the HTTP server threads).
- **mDNS in the shipped engine** — binding UDP 5353 alongside the OS mDNS responder (SO_REUSEPORT); multicast may need care across interfaces.
- **CastMessage codec correctness** — pure and unit-testable with vectors; the spike proves the wire format.
- **Queue semantics divergence** — Cast's engine-managed queue won't match Sonos's device queue 1:1; keep the UI behavior consistent.
- **Unverified TLS** — correct for Cast, but isolate it to Cast sockets only (never weaken anything else).

## Phased approach (see the plan for tasks)

0. ✅ Spike (done). 1. Wire primitives (protobuf + mDNS parser, pure, unit-tested). 2. Discovery integrated + `backend` field. 3. CastSession (connection/heartbeat/status, CastMock-tested). 4. Backend dispatch + Cast control + engine-managed queue (real-hardware). 5. UI polish. 6. (Later) near-gapless via Cast queue preload.
