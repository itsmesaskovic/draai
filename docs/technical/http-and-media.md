# HTTP server and media serving

> How DRAAI's engine serves the UI, the JSON API, and audio files to Sonos /
> Cast speakers over plain HTTP — no cloud, no accounts.

## Purpose

DRAAI's engine is a single-process HTTP server. It does three jobs on one
port:

1. Serves the web UI (`player_ui.html` or a built-in fallback page).
2. Exposes a JSON API under `/api/*` that the UI (and any phone on the same
   Wi-Fi) uses to control playback, manage the library, and read status.
3. Streams local audio files to speakers under `/media/<id>/<name>`, with
   HTTP `Range` support — Sonos will not play or seek a track without it.

This doc is grounded in `draai/server.py` and `draai/media.py`. Every claim
below has a `file:line` anchor; re-check the anchor if the code has moved.

## Where it lives

- `draai/server.py` — the `Handler` class (`BaseHTTPRequestHandler`
  subclass): all routing, the `/api/*` endpoints, `/media` serving, the
  built-in QR encoder, and the built-in fallback `PAGE`.
- `draai/media.py` — `local_ip_facing()`, `media_url()`, `find_tool()`.
- `draai/__main__.py:94-104` — binds the `ThreadingHTTPServer`, trying ports
  `PREFERRED_PORT..PREFERRED_PORT+19` (`draai/constants.py:4`,
  `PREFERRED_PORT = 8765`) and stores the winning port in
  `state.server_port` (`draai/state.py:18`).
- `draai/library.py:315` — where the track id (sha1 of path) is computed at
  scan time.
- `draai/backends.py:349-472` — where a speaker's reported `TrackURI` is
  parsed back into a track id (`get_status`, `browse_queue`).
- `draai/player_ui.html:552-554,981-986` — where the UI renders the QR /
  guest-access box; `:1414,1417` — where it persists prefs via
  `/api/prefs` instead of `localStorage`.

## Key concepts

### Server startup and port fallback

`main()` in `draai/__main__.py:94-104` loops `port` from `PREFERRED_PORT`
(8765) through `PREFERRED_PORT + 19` and tries
`ThreadingHTTPServer(("0.0.0.0", port), Handler)`, catching `OSError` (port
in use) and moving to the next. The first port that binds wins; it's stored
in the module-level `state.server_port` (a plain int reassigned at runtime —
see the docstring at `draai/state.py:4-5`). If none of the 20 ports are
free, the process exits with an error (`draai/__main__.py:99-101`).
`ThreadingHTTPServer` means each request runs on its own thread — one slow
speaker call doesn't block the UI or other speakers.

`Handler` (`draai/server.py:286-291`) sets `protocol_version = "HTTP/1.1"`
(needed for keep-alive and correct `Content-Length`/Range handling) and
silences per-request logging via an overridden `log_message`.

### Routing structure

`do_GET` (`draai/server.py:312-482`) parses the URL path with
`urllib.parse.urlparse` and dispatches with a long `if/elif` chain matching
exact paths (`/api/state`) or prefixes (`path.startswith("/api/status")`)
for endpoints that take query-string arguments. Two helpers are used
everywhere: `send_json()` (`:294-300`, sets `Content-Type:
application/json` + `Content-Length`, writes the body) and `read_json()`
(`:302-309`, reads and parses the POST body, capped at 10 MB, returns `{}`
on any failure so a bad body never raises).

`do_POST` (`draai/server.py:493-695`) reads the JSON body once via
`read_json()`, then dispatches the same way. The whole dispatch body is
wrapped in one `try/except`: a `RuntimeError` (raised deliberately by
backend calls for expected failures, e.g. "Pick a speaker first") maps to
HTTP 502; any other `Exception` maps to HTTP 500 with the message
interpolated into "Unexpected problem: %s" (`draai/server.py:692-695`) —
this is the one place a raw exception string can reach the client, but it's
still wrapped in a plain sentence, not a stack trace.

`do_HEAD` (`draai/server.py:484-491`) only special-cases `/media/`
(delegates to `serve_media(path, head=True)`); everything else gets a bare
200 with `Content-Length: 0`.

### The `/api/*` surface (as implemented, not aspirational)

GET endpoints (`draai/server.py:312-482`):

| Path | Line | Purpose |
|---|---|---|
| `/` | 314 | Serves the UI HTML (see "UI serving" below) |
| `/favicon.ico` | 321 | Always 204, no favicon shipped |
| `/media/<id>/<name>` | 325 | Audio streaming, see "Media serving" |
| `/api/state` | 327 | Speakers, folders, track count, version, last-used speaker |
| `/api/tracks` | 338 | Library search (`?q=`), returns up to 3000 matches |
| `/api/status` | 359 | One speaker's transport state, position, volume, current track id |
| `/api/queue` | 367 | That speaker's current play queue |
| `/api/art` | 378 | Embedded album art blob for a track id |
| `/api/analysis` | 393 | Cached loudness/band analysis for a track id |
| `/api/rooms` | 396 | Grouping topology visible to a speaker |
| `/api/eq` | 403 | Current bass/treble/loudness for a speaker |
| `/api/access` | 413 | LAN URL other devices can use to reach this server (guest mode) |
| `/api/qr.svg` | 422 | Renders an SVG QR code for arbitrary `?text=` (used for the guest URL) |
| `/api/browse` | 438 | Filesystem directory listing, for the folder picker |
| `/api/playlists` | 468 | List saved M3U playlists |
| `/api/prefs` | 470 | GET UI prefs (theme/group/sort/...) persisted server-side |
| `/api/yt_available` | 472 | Whether yt-dlp/ffmpeg are installed |
| `/api/yt_status` | 474 | Poll a running yt-dlp import job |

POST endpoints (`draai/server.py:493-695`), each returns JSON, wrapped by
the `try/except` described above:

| Path | Line | Purpose |
|---|---|---|
| `/api/rescan_speakers` | 497 | Re-run SSDP discovery |
| `/api/add_ip` | 500 | Add a speaker manually by IP (validated with a regex) |
| `/api/folder` | 511 | Legacy: replace the whole folder list with one folder |
| `/api/rescan_library` | 520 | Re-scan all configured folders |
| `/api/folders_add` | 524 | Add one folder to the library, then rescan |
| `/api/folders_remove` | 536 | Remove one folder, then rescan |
| `/api/play` | 544 | Replace the speaker's queue with `ids` and start playing |
| `/api/enqueue` | 555 | Append (or play-next) `ids` to the speaker's queue |
| `/api/queue_move` | 565 | Reorder one queue entry (`from`→`to`) |
| `/api/playlist_save` | 572 | Save the speaker's current queue as a named M3U |
| `/api/playlist_load` | 580 | Load a playlist: play / play-next / enqueue |
| `/api/playlist_delete` | 600 | Delete a saved playlist |
| `/api/prefs` | 603 | POST — merge/save UI prefs (`null` value deletes a key) |
| `/api/reveal` | 612 | Reveal a library file in Finder (path-guarded to library roots) |
| `/api/eq` | 615 | Set bass/treble/loudness on a speaker |
| `/api/group` | 623 | Join one speaker into another's group |
| `/api/ungroup` | 627 | Remove a speaker from its group |
| `/api/room_volume` | 630 | Set one member's per-device volume |
| `/api/queue_jump` | 635 | Jump playback to a queue position |
| `/api/queue_remove` | 642 | Remove one entry from the queue |
| `/api/youtube` | 649 | Start a yt-dlp import job for a URL |
| `/api/cmd` | 657 | Transport actions: `pause`, `resume`, `next`, `prev`, `volume`, `seek`, `clearqueue`, `sleep`, `shuffle` (dispatched by `action` in the body; Cast speakers route a subset through `cast_cmd`, `:663-665`) |

This list was read directly out of the `if/elif` chains, not inferred —
treat it as the source of truth over any prose summary elsewhere
(including this repo's `CLAUDE.md`, which only sketches categories).

### Media serving with `Range` support

`serve_media(path, head)` (`draai/server.py:698-752`) is the handler for
both `GET /media/<id>/<name>` (`:325-326`) and `HEAD /media/<id>/<name>`
(`:486-487`).

1. Split the path on `/`; `parts[2]` is the track id (`:699-700`). The
   trailing `<name>` segment is never read server-side — it exists only so
   the URL looks sane to a human/log and so some clients infer a filename;
   lookup is entirely by id via `tracks_by_id.get(tid)` (`:701-702`).
2. If the id is unknown or the file no longer exists on disk, respond 404
   with `Content-Length: 0` (`:703-707`).
3. Look up file size and MIME type from `AUDIO_EXTS`
   (`draai/constants.py:5-13`), defaulting to
   `application/octet-stream` for an unrecognized extension
   (`draai/server.py:709`).
4. Default range is the whole file: `start=0`, `end=size-1`, `status=200`
   (`:710-711`).
5. If a `Range` header is present, match it with
   `re.match(r"bytes=(\d*)-(\d*)$", ...)` (`:714`). Three shapes are
   handled:
   - `bytes=<start>-<end>` — explicit start and end.
   - `bytes=<start>-` — from `start` to EOF (`end` stays `size-1`).
   - `bytes=-<n>` — suffix range, last `n` bytes
     (`start = max(0, size - n)`, `:720-722`).
   If `start >= size` the response is `416 Range Not Satisfiable` with a
   `Content-Range: bytes */<size>` header (`:723-728`). Otherwise
   `status` becomes `206` (`:729`).
6. Always sets `Accept-Ranges: bytes` (`:733`) so speakers know they're
   allowed to ask for ranges at all, and `Content-Length` to the actual
   number of bytes in this response, not the whole file (`:730,734`). Only
   a `206` gets a `Content-Range: bytes <start>-<end>/<size>` header
   (`:735-737`).
7. For `HEAD`, stop after headers (`:739-740`). For `GET`, stream the file
   in 64 KiB chunks starting at `start`, stopping once `length` bytes have
   been written (`:741-750`). `BrokenPipeError`/`ConnectionResetError` are
   caught and swallowed (`:751-752`) — a speaker closing the connection
   mid-stream because it seeked or skipped is normal, not an error worth
   surfacing.

Sonos issues Range requests routinely (to seek, and sometimes just to probe
duration), so without this the file plays once from byte 0 and skipping /
resuming breaks silently.

### Track identity: the id is the contract

- **Generation**: at library scan time,
  `sha1(path.encode("utf-8", "surrogateescape")).hexdigest()[:16]`
  (`draai/library.py:315`) — the first 16 hex characters of the sha1 of the
  absolute file path. This is a stable, collision-resistant, and
  filesystem-independent id that survives tag edits (it's path-based, not
  content-based).
- **Embedding**: `media_url(track, speaker_ip)` (`draai/media.py:20-23`)
  builds `http://<host>:<port>/media/<id>/<urlencoded title+ext>` — the id
  is in the path, not a query string, so it survives any proxying/caching
  a speaker's firmware might do.
- **Recovery**: Sonos's `GetPositionInfo` SOAP response includes a
  `<TrackURI>`; the engine regexes it for `/media/([0-9a-f]{16})/` to
  recover the id (`draai/backends.py:378-382` in `get_status`, and again
  in `browse_queue` at `draai/backends.py:464-470` for every queue entry).
  This recovered id is what drives now-playing highlighting in the UI
  (`out["track_no"]`/`out["track_id"]` in `/api/status`) and the resume
  feature (`note_position(um.group(1), pos, dur)`,
  `draai/backends.py:379-384`, only for tracks longer than
  `RESUME_MIN_TRACK` — `draai/constants.py:24`).

**Do not change the URL shape** (`/media/<16-hex-id>/<name>`) — both
`get_status` and `browse_queue` depend on that exact regex to map a
speaker's queue back to library tracks. This is called out explicitly in
`CLAUDE.md`'s "Sonos protocol gotchas" and confirmed by reading the regex
in `draai/backends.py:380,466`.

### `local_ip_facing`: which IP the speaker fetches from

`local_ip_facing(speaker_ip)` (`draai/media.py:10-17`) opens a UDP
(`SOCK_DGRAM`) socket, calls `connect()` to the speaker's IP on port 1400
(Sonos's control port — chosen just because it's a real port on the
speaker, not because any packet is actually sent; UDP `connect()` doesn't
transmit), then reads back `getsockname()[0]` — the local interface address
the OS would use to route to that speaker. This is the standard
"connect a UDP socket, don't send anything, read the local address"
trick for discovering the outbound-facing IP without needing hardcoded
interface names, and it's correct even on machines with multiple
interfaces/VPNs, because it asks the routing table for the specific
destination rather than guessing.

`media_url()` calls this per-track/per-speaker to build the `host` part of
the media URL (`draai/media.py:20-23`), and `/api/access`
(`draai/server.py:413-421`) uses the same function (against the first known
speaker's IP, or `8.8.8.8` if none, falling back to `127.0.0.1` on error)
to build the URL shown to guests.

### Guest mode and the QR code

There is no separate authentication or reduced-permission "guest" role in
the code — `/api/access` and `/api/qr.svg` exist purely to make it *easy*
for a phone on the same Wi-Fi to open the exact same full-control UI a
laptop would see. "Guest mode" is UI copy
(`draai/player_ui.html:554`: *"Scan to be the DJ — Guests on the Wi-Fi can
queue songs from their phone."*), not an access-control feature; anyone who
loads the URL gets the same control as the host machine. This is worth
knowing before assuming there's an authorization boundary to preserve.

- `GET /api/access` (`draai/server.py:413-421`) returns
  `{"url": "http://<local_ip_facing>:<server_port>"}` — the address other
  devices on the LAN should use to reach this same server.
- `GET /api/qr.svg?text=<url>` (`draai/server.py:422-437`) renders that URL
  (or any string ≤120 chars) as a scannable SVG QR code, generated by a
  hand-rolled QR encoder (`_qr_matrix`, `qr_svg`, `draai/server.py:66-283`
  — Reed-Solomon error correction, finder/alignment/timing patterns, mask
  selection by penalty score — all stdlib, no `qrcode` package). This is
  the same "no pip, ever" rule applied to a whole barcode format.
- The UI fetches both and renders the QR box at
  `draai/player_ui.html:552-554,981-986`: it tries `/api/qr.svg?text=...`
  as an `<img>` first, and falls back to a client-side `fauxQR()` if that
  image fails to load (`:984`).

### UI serving: external file, packaged copy, or built-in fallback

`GET /` calls `_load_ui()` (`draai/server.py:27-42`), which tries three
sources in order:

1. `./player_ui.html` in the process's current working directory — wins if
   present, read fresh on every request (no caching), which is what makes
   live-editing the UI file practical during development
   (`draai/server.py:31-37`).
2. The copy of `player_ui.html` packaged inside the `draai` package via
   `importlib.resources` (`:38-40`) — this is what resolves when running
   from the zipped `draai.pyz` where there's no loose file next to the
   script.
3. The built-in `PAGE` string constant (`draai/server.py:755-1282`, a
   complete standalone HTML/CSS/JS document) if both of the above fail —
   this is the "zero-file experience" fallback: a minimal but fully
   functional player (speaker list, folder scan, search, queue, transport
   controls, YouTube import) that works even if `player_ui.html` is
   missing or unreadable. Per `CLAUDE.md`, new HALCYON-level UI features
   belong only in `player_ui.html`, not `PAGE` — `PAGE` just has to keep
   working.

### `/api/prefs`: server-side UI preference persistence

Because `player_ui.html` must work with no `localStorage`/`sessionStorage`
(`CLAUDE.md` hard rule #2), UI preferences round-trip through the engine
instead:

- `GET /api/prefs` (`draai/server.py:470-471`) returns
  `config.get("ui", {})` — whatever's stored under the `"ui"` key in
  `config.json`.
- `POST /api/prefs` (`draai/server.py:603-611`) merges the posted object
  into `config["ui"]`: any key with a `None` value is deleted
  (`ui.pop(k, None)`), otherwise it's set, then `save_config()` persists it
  to disk and the merged object is echoed back.
- The UI debounces writes 400ms after a change and posts
  `{theme, group, sort, dir, collapsed}` (`draai/player_ui.html:1414`), and
  loads them once at startup (`:1417`). This is the mechanism behind
  theme/group/sort persisting across restarts without any client storage.

## Gotchas

- **Never change the `/media/<id>/<name>` URL shape.** Two independent
  regexes (`draai/backends.py:380` in `get_status`, `:466` in
  `browse_queue`) depend on `/media/` immediately followed by exactly 16
  hex characters and a `/`. Changing the prefix, the id length, or adding
  extra path segments before the id silently breaks now-playing detection
  and resume for every in-flight session — there's no error, the id just
  stops matching and `track_id`/`out["id"]` become `None`.
- **The `<name>` segment of a media URL is cosmetic.** `serve_media` reads
  only `parts[2]` (the id) and ignores whatever filename follows
  (`draai/server.py:699-702`). Don't rely on it for anything server-side.
- **Range support is not optional.** Any regression that drops the `Range`
  header handling, the `Accept-Ranges: bytes` header, or 206 responses will
  make playback start-only (no seeking) or break outright on real Sonos
  hardware — this class of bug won't show up in `tests/test_draai.py`
  unless a test specifically exercises `serve_media` with a `Range`
  header, so treat manual/real-hardware verification as required for any
  change here (`CLAUDE.md`: "Real-hardware checks matter for: queue
  reorder, grouping, anything DIDL" — Range serving deserves the same
  caution even though it's not DIDL).
- **Track ids are derived from the absolute path, not file content.**
  Moving/renaming a file changes its id (a new scan assigns a new sha1);
  the resume/position and "now playing" mappings for the old id become
  orphaned (harmless, just stale) rather than migrated.
- **`local_ip_facing` requires network reachability to the speaker's
  subnet**, not just any local IP — on a Mac with multiple active
  interfaces (Wi-Fi + Ethernet + VPN) it correctly picks the one that
  actually routes to the speaker, but if no route exists the UDP `connect`
  can raise, which is why callers wrap it in `try/except` and fall back to
  `"127.0.0.1"` (`draai/server.py:417-420`) — a URL that will never work
  for a second device, so treat that fallback as a "speakers not found
  yet" signal, not a real address to hand out.
- **"Guest mode" grants full control, not read-only access.** Don't build
  around an assumption of a restricted guest permission set — none exists;
  see "Guest mode and the QR code" above.
- **`do_POST`'s blanket exception handler can leak exception text to the
  client** (`draai/server.py:694-695`, `"Unexpected problem: %s" % e`).
  This satisfies CLAUDE.md rule #5 ("human sentences, not stack tracebacks")
  only if `str(e)` itself is human-readable — raising bare/low-level
  exceptions (e.g. from `os` or `socket` calls) inside a POST handler
  without wrapping them in a `RuntimeError` with a clear message will leak
  Python-flavored text to the UI's error toast.
- **The built-in `PAGE` fallback and `player_ui.html` are two separate UIs**
  that must both keep working; a change to the JSON shape of any `/api/*`
  endpoint has to be compatible with both consumers (`PAGE`'s inline
  `<script>`, `draai/server.py:928-1279`, and `player_ui.html`'s script).

## References

- `draai/server.py:27-42` — `_load_ui()` (external → packaged → `PAGE`
  fallback)
- `draai/server.py:66-283` — hand-rolled QR encoder (`_qr_matrix`,
  `qr_svg`)
- `draai/server.py:286-291` — `Handler` class, `HTTP/1.1`, quiet logging
- `draai/server.py:294-309` — `send_json` / `read_json` helpers
- `draai/server.py:312-482` — `do_GET` full routing table
- `draai/server.py:413-421` — `/api/access`
- `draai/server.py:422-437` — `/api/qr.svg`
- `draai/server.py:470-471` — `GET /api/prefs`
- `draai/server.py:484-491` — `do_HEAD`
- `draai/server.py:493-695` — `do_POST` full routing table + error mapping
- `draai/server.py:603-611` — `POST /api/prefs`
- `draai/server.py:698-752` — `serve_media` (Range parsing, 206/416,
  chunked streaming)
- `draai/server.py:755-1282` — built-in fallback `PAGE`
- `draai/media.py:10-17` — `local_ip_facing`
- `draai/media.py:20-23` — `media_url`
- `draai/media.py:26-34` — `find_tool` (ffmpeg/yt-dlp discovery, not
  required at import time)
- `draai/library.py:315` — track id = `sha1(path)[:16]`
- `draai/backends.py:349-386` — `get_status`, TrackURI → track id recovery,
  resume note
- `draai/backends.py:447-472` — `browse_queue`, TrackURI → track id
  recovery per queue entry
- `draai/constants.py:4-13` — `PREFERRED_PORT`, `AUDIO_EXTS`
- `draai/constants.py:24-25` — `RESUME_MIN_TRACK`, `RESUME_MIN_POS`
- `draai/state.py:4-5,18` — `state.server_port`
- `draai/__main__.py:94-104` — port-fallback bind loop
- `draai/player_ui.html:552-554,981-986` — QR/guest-access UI
- `draai/player_ui.html:1414,1417` — prefs debounce-save / load-on-start
