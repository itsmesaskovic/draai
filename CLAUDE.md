# CLAUDE.md — context for working on DRAAI

DRAAI plays local music on Sonos / IKEA Symfonisk speakers. One
dependency-free Python engine + one self-contained HTML interface.
Currently at v1.2.0, live at github.com/itsmesaskovic/draai.

## Hard rules (do not break these)

1. **`sonos_player.py` stays ONE file, Python stdlib ONLY.** No pip
   imports, no splitting into modules. This is the product's distribution
   story. Optional tools (ffmpeg, yt-dlp) are *detected* at runtime via
   `find_tool()`, never required.
2. **`player_ui.html` stays ONE file.** All CSS/JS inline. No CDNs, no
   web fonts, no localStorage/sessionStorage (preferences persist via the
   engine: GET/POST `/api/prefs`). Must work offline.
3. **No cloud, no accounts, no telemetry, ever.**
4. **No site-specific downloader code.** The yt-dlp integration hands the
   URL to the user's own yt-dlp installation; downloads land in
   `<first folder>/Imported/` with `--embed-metadata --embed-thumbnail`.
5. Errors shown to users must be human sentences, not stack traces.
6. User-facing copy ships in English: short, warm, plain. App name is
   DRAAI (Dutch: "spin/play").

## Architecture in 60 seconds

- Engine = HTTP server on :8765 (falls back +1..+19). Serves the UI
  (player_ui.html if present next to the script, else built-in PAGE),
  a JSON API under /api/*, and media files under /media/<id>/<name>
  **with HTTP Range support** (Sonos needs it).
- Speakers are controlled with hand-rolled UPnP/SOAP (`soap_call()`).
  Discovery: SSDP M-SEARCH + `GetZoneGroupState` topology parsing
  (handles stereo pairs `Invisible="1"`, `<Satellite>` children, multi-unit
  rooms; bonded units get `fixed: true` and must not be ungrouped).
- Track identity = first 16 hex chars of sha1(file path). Media URLs embed
  it; `GetPositionInfo`'s TrackURI is parsed back to a track id — this is
  how now-playing highlighting and resume work. Don't break URL shape.
- Analysis (needs ffmpeg): streamed decode → 100ms loudness envelope +
  low/mid/high bands, cached as JSON in
  ~/Library/Application Support/SonosMP3Player/analysis/ with a version
  field `v` (`ANALYSIS_VERSION`) — bump it to invalidate all caches.
- Resume: positions of tracks >10 min stored in positions.json, seek on
  play. UI prefs (theme/group/sort) live in config.json under "ui".

## Sonos protocol gotchas (paid for in blood)

- Queue metadata REQUIRES the `<desc id="cdudn">RINCON_AssociatedZPUDN
  </desc>` element in DIDL-Lite or real hardware silently drops titles
  and shows raw filenames. See `didl_for()`.
- `ReorderTracksInQueue`: `InsertBefore` is counted in PRE-removal
  positions → moving down needs `to + 1`. See `queue_move()` and the
  mock in tests that encodes real semantics.
- Group volume = GroupRenderingControl on the coordinator; per-device
  volume = RenderingControl on each member's own IP.
- Join group: `SetAVTransportURI x-rincon:<coordinatorUUID>` on the
  member. Leave: `BecomeCoordinatorOfStandaloneGroup`.
- The engine retries discovery at startup (network may be slow) and the
  UI polls /api/state until rooms appear.

## Interface gotchas

- The UI began as a Claude Design export; class names are terse. There
  HAVE been collisions: `.spin` and `.disc` already exist in the design —
  vinyl deck uses `.vdisc`/`.vspin` instead. grep before adding classes.
- Album-palette pipeline: `paletteFromImage` → `setPaletteTarget` (accents
  clamped to a luminance FLOOR of 0.55 for dark) → lerped in `tick()` →
  `applyPal` (in light theme accents get a luminance CAP of 0.45 and
  washes become light tints via `adaptForTheme`; fullscreen `#np` always
  stays dark). Light mode hides `#bgwrap` (the blurred orbs painted wrong
  in light — don't re-enable without visual pixel-testing).
- Fullscreen is `#np.open`. Media keys use a silent looping <audio> +
  mediaSession, armed on first pointerdown.
- The built-in fallback UI lives in the engine's PAGE string — keep it
  working (it's the zero-file experience) but HALCYON-level features go
  in player_ui.html only.

## Testing

- `python3 tests/test_draai.py` — 15 tests, no network/speakers/ffmpeg
  needed. SoapMock implements real Sonos queue semantics. Always run
  before committing; add tests for new engine behavior.
- UI changes: verify by serving locally with a mocked `soap_call` (see
  test suite for the pattern) and clicking through, or against the real
  speaker at home. `--version` flag exists; version constant is
  `__version__` in the engine.
- Real-hardware checks matter for: queue reorder, grouping, anything DIDL.

## Release process

main is always stable; short-lived branches + PRs for contributors; the maintainer
commits directly for small things. Tag releases: `gh release create
vX.Y.Z --title ... --notes ...`. Bump `__version__` first. CI
(.github/workflows/tests.yml) runs the suite on pushes and PRs.

## Roadmap (agreed with the maintainer)

- v1.2 candidates: EQ presets (engine get_eq/set_eq exist; presets =
  named combos in config), coordinator badge, saved group presets,
  one-tap party mode, drop-files-onto-window import.
- v2: Chromecast/Google Cast backend (CASTV2, keep dependency-free),
  same UI, speakers become a second backend type. Sonos+Cast cannot play
  in sync — don't promise it.
- Explicitly rejected: chapters on the waveform (built, then removed as
  confusing — it's in git history), Sonos native playlists (opaque state;
  we use M3U files in <first folder>/Playlists/), multi-select via
  long-press only (use Select button + modifier clicks).
