# DRAAI slim phone remote — design

**Date:** 2026-07-16
**Status:** Approved for planning
**Topic:** A lightweight, phone-optimized remote served at `/remote`, separate from the full desktop UI.

## Purpose

Give the maintainer a fast, focused "couch surfing" remote on their phone:
see what's playing, drive transport, manage the queue, browse/search the
library to add songs, and switch room + volume — without shipping the full
~200 KB desktop app to the phone. Reachable over the local network via the
existing QR flow.

## Goals

- A self-contained `remote.html` served at `GET /remote`, following the same
  discipline as `player_ui.html`: single file, all CSS/JS inline, no CDNs, no
  web fonts, no local/sessionStorage, works offline, prefs (if any) via the
  engine.
- Controls **and** library browse/search/add ("couch surfing").
- Uses only the existing `/api/*` surface — no new control endpoints.
- Works for both Sonos and Google Cast speakers (the engine's `cmd`/`enqueue`
  already branch on `backend`).
- The phone QR is fixed (it's hidden today) and points at `/remote`.

## Non-goals (stays desktop-only, in `player_ui.html`)

EQ, sleep timer, speaker **grouping setup** (join/leave), YouTube import,
waveform/analysis visuals, album-palette theming, the vinyl deck, folder
management, playlist save/load. The remote can *switch which existing room* it
drives, but does not build or edit groups.

## Featureset

1. **Now playing** — cover art, title, artist, progress (elapsed / remaining)
   with a seek scrubber, play/pause.
2. **Transport** — previous · play/pause · next, plus a shuffle toggle.
3. **Up next (queue)** — the current queue as a list: current item highlighted;
   tap a row to jump to it; per-row **play-next** (move up to next), **move up**,
   and **remove**.
4. **Browse & search** — scroll the library, search by text, tap **＋** on a
   track to add it to the queue (and a "play next" affordance).
5. **Room** — a header chip showing the active room/group, tap to switch which
   room is controlled; a volume slider for that room.

## Architecture

```
GET /            -> player_ui.html   (full desktop app, unchanged)
GET /remote      -> remote.html      (this: slim phone remote)
GET /api/*       -> shared engine API (unchanged)
```

- **`remote.html`** lives at the repo root next to `player_ui.html` (source of
  truth). `build.py` copies it into the package so it ships inside `draai.pyz`.
  The packaged copy is git-ignored (build artifact), exactly like
  `draai/player_ui.html`.
- **Serving:** `server.py` gains `_load_remote()` mirroring `_load_ui()` —
  resolution order: external `remote.html` in cwd → packaged copy via
  `importlib.resources` → a minimal built-in fallback string (a plain "open the
  full app" note is acceptable; the remote is an enhancement, not the zero-file
  baseline). `do_GET` serves it for `path == "/remote"` (and `/remote/`).
- **QR / access:** the phone entry point should open the remote. `/api/access`
  currently returns `{"url": "http://<ip>:<port>"}`; extend it (or the QR text)
  so the phone lands on `/remote`. The simplest honest change: `/api/access`
  returns both the base and a `remote` URL, and the QR encodes the `/remote`
  one. Also fix the existing bug where `#qrWrap` in `player_ui.html` is built
  by `loadAccess()` but never un-hidden (`.hidden{display:none!important}` is
  never removed) — so the QR actually appears in the desktop fullscreen view.

## UI layout

One screen, dark, phone-first. A bottom tab bar switches three segments; a
persistent now-playing + transport bar is pinned above the tabs so control is
always one tap regardless of segment.

```
┌─────────────────────────────┐
│ [ Living Room ▾ ]   🔊 ▭▭▭──│  room chip (switch) + volume
├─────────────────────────────┤
│   segment content            │
│   NOW      → big art, title, │
│             artist, scrubber │
│   UP NEXT  → queue rows with │
│             ▲ play-next ⤒ ✕  │
│   BROWSE   → search + list,  │
│             tap ＋ to enqueue│
├─────────────────────────────┤
│  ◄◄    ▮▮    ►►       🔀      │  persistent transport
│  ── NOW ── UP NEXT ── BROWSE │  bottom tabs
└─────────────────────────────┘
```

Notes:
- The persistent mini-transport always shows the current track + play/pause and
  prev/next; tapping the mini-art jumps to the NOW segment.
- Reorder uses tap buttons (**move up**, **play-next**, **remove**), not drag —
  drag is unreliable on touch and touch-drag-reorder is out of scope for v1.

## Data flow and endpoint mapping

Client holds `speaker` (the active room's coordinator uuid) in memory. Rooms
come from `GET /api/state` (the `speakers` array). Now-playing and the queue are
**per-speaker** and polled on an interval (~1 s, matching the desktop UI):
`GET /api/status?speaker=<uuid>` and `GET /api/queue?speaker=<uuid>`.
`GET /api/tracks?q=` drives browse/search. All writes carry `speaker`.

**Exact contract (verified in the live handlers, 2026-07-16):**

| Action | Call | Shape |
|---|---|---|
| rooms / speakers | `GET /api/state` | `{speakers:[{uuid,name,backend,...}], last_speaker, ...}` |
| now playing | `GET /api/status?speaker=<uuid>` | `{state, title, position, duration, volume, track_no, track_id}` |
| queue | `GET /api/queue?speaker=<uuid>` | `{items:[{no,id,title,...}], total}` — `no` is **1-based** (`enumerate(..., 1)`) |
| library + search | `GET /api/tracks?q=<text>` | `{tracks:[{id,title,artist,album,has_art,added,dir,folder}], total}` (cap 3000) |
| cover art | `GET /api/art?id=<track_id>` | image bytes |
| play/pause | `POST /api/cmd {speaker, action:"resume"|"pause"}` | `{ok:true}` |
| next / prev | `POST /api/cmd {speaker, action:"next"|"prev"}` | `{ok:true}` |
| seek | `POST /api/cmd {speaker, action:"seek", value:<sec>}` | `{ok:true}` |
| shuffle | `POST /api/cmd {speaker, action:"shuffle", value:<bool>}` | `{ok:true}` |
| room volume | `POST /api/room_volume {speaker, value:0..100}` | `{ok:true}` |
| add / play-next | `POST /api/enqueue {speaker, ids:[...], next:<bool>}` | `{queued:<n>}` |
| jump to queue item | `POST /api/queue_jump {speaker, no}` | `{ok:true}` — `no` 1-based, send `item.no` |
| move in queue | `POST /api/queue_move {speaker, from, to}` | `{ok:true}` — both 1-based |
| remove from queue | `POST /api/queue_remove {speaker, no}` | `{ok:true}` — 1-based, send `item.no` |

**Index bases matter:** `browse_queue` items carry a **1-based** `no` (it uses
`enumerate(..., 1)`), and `queue_jump` (Sonos `TRACK_NR`), `queue_move`, and
`queue_remove` are **all 1-based** — send `item.no` unchanged. `queue_move`'s
pre-removal `to+1` correction for downward moves is handled inside the engine;
the client sends plain 1-based source/target positions. "Move up" is
`queue_move(from=item.no, to=item.no-1)` (guard `item.no > 1`); "play next" is
`queue_move(from=item.no, to=track_no+1)`; the current row is highlighted when
`item.no === status.track_no`.

**Now-playing metadata:** `get_status` returns only `title` + `track_id` (not
artist/album/art). The remote builds an `id → track` map from `GET /api/tracks`
and looks up `track_id` for artist/album, and uses `GET /api/art?id=<track_id>`
for the cover — the same approach the desktop UI uses.

The implementer should mirror how `player_ui.html` already calls each of these
endpoints (its `loadQueue`, transport `data-cmd` buttons, `enqueue`, and
`room_volume` code) to guarantee identical request shapes and semantics.

## Active room model

- Rooms come from `GET /api/state` (the same room/coordinator list the desktop
  UI uses). Each room is represented by its coordinator uuid.
- The room chip lists rooms; selecting one sets the client's `speaker`. All
  subsequent reads (now playing, queue) reflect that room and all writes target
  it.
- Grouping (join/leave) is **not** offered here; the remote only selects among
  rooms that already exist. If a room is a group, its coordinator is the target
  (same convention as the engine).
- Empty state: if no room is selected / none found, show a "Pick a room" prompt
  and disable transport.

## Theming

Reuse the fixed accent token (`#5EEAD4`) and the dark palette for visual
consistency with the desktop app, but a fresh, minimal stylesheet sized for
touch (large tap targets, no hover-only affordances). No album palette, no
canvas/waveform, no vinyl deck. Respects `viewport-fit=cover` for notches.

## Error and empty states

- API errors: the engine returns human-readable `{"error": "..."}` strings;
  surface them as a brief toast. Never show raw JSON or stack traces.
- Engine unreachable (poll fails): a quiet "Can't reach DRAAI — same Wi-Fi?"
  banner, auto-recovering when polling succeeds again.
- No room selected: "Pick a room" prompt, transport disabled.
- Empty queue: a gentle "Nothing queued — add something from Browse."

## Testing

- **Engine (in `tests/test_draai.py`, no network/speakers):**
  - `GET /remote` serves the remote HTML (assert a known marker string in the
    body; assert 200).
  - `_load_remote()` resolution order (external cwd copy wins; falls back to
    packaged; falls back to the built-in string).
  - `/api/access` returns a `/remote` URL of the expected shape.
- **Build:** `python3 build.py` includes `remote.html` in `draai.pyz`; running
  the `.pyz` serves `/remote`.
- **UI:** verify by serving locally and driving the Browser pane at a mobile
  viewport (375×812): segment switching, now-playing render, queue reorder
  buttons issue the right calls, browse/search + add, room switch + volume.
  **No real-device playback** — assert behavior via the mocked API / network
  panel, not by starting audio on hardware.
- Full suite stays green: `python3 tests/test_draai.py`.

## Risks and notes

- **`player_ui.html` ONE-file rule** is preserved — `remote.html` is a separate
  surface, consistent with the existing `PAGE` fallback + `player_ui.html`
  split. This is a deliberate third UI surface, not a violation.
- **Duplicated API glue:** the remote reimplements a little of the desktop UI's
  fetch/poll helpers. Accepted cost of the slim-payload goal; keep the remote's
  copy small and self-contained.
- **No auth:** the remote inherits the same open-LAN access as the rest of the
  app. This is out of scope here and tracked separately (the queued "access
  tier" task). The remote must not imply restriction it doesn't enforce.
- **Field-name drift:** confirm every request body against the live handlers at
  build time; this spec lists endpoints, not a frozen contract.
```
