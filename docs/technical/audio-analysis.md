# Audio analysis

> Purpose: explain the ffmpeg-based waveform/loudness pipeline — how it
> decodes, caches, and serves the data the UI uses to draw the waveform
> and drive the live visualizer, and how the app degrades when ffmpeg
> isn't installed.

## Purpose

`draai/analysis.py` turns an audio file into a small JSON blob: a
scaled waveform (`peaks`) plus four parallel envelopes (`amp`, `low`,
`mid`, `high`) sampled every 100ms. The dock/fullscreen player uses
this to paint the waveform bar and to drive the live "energy" CSS
variables (`--e-amp`, `--e-low`, `--e-mid`, `--e-high`) that make the
UI pulse in time with the music. It is a pure enhancement: nothing
about playback, queueing, or grouping depends on it.

## Where it lives

- `draai/analysis.py` — the whole pipeline: decode, envelope, cache,
  concurrency bookkeeping.
- `draai/media.py:26` (`find_tool`) — locates `ffmpeg` on `PATH` or in
  the two common Homebrew install dirs; returns `None` if missing.
- `draai/server.py:393-395` — the one HTTP endpoint, `GET /api/analysis`.
- `draai/server.py:553,563,597` — `prefetch_analysis()` calls fired
  from `/api/play`, `/api/enqueue`, and `/api/playlist_load`.
- `player_ui.html:690-741` — UI consumer: polling, peak scaling,
  per-frame energy sampling, canvas waveform draw.
- Cache dir: `~/Library/Application Support/SonosMP3Player/analysis/`
  (`ANALYSIS_DIR`, built from `CONFIG_DIR` in `draai/constants.py:17-19`
  joined with `"analysis"` in `draai/analysis.py:12`).

## Key concepts

### ffmpeg is optional, detected at runtime

`_analyze()` calls `find_tool("ffmpeg")` (`draai/analysis.py:71`); if
it returns `None` the job fails with a human-readable
`RuntimeError("ffmpeg is not installed (brew install ffmpeg)")`
(`draai/analysis.py:72-73`), caught by the outer `try/except` and
stored in `analysis_state` as `"error:<msg>"`
(`draai/analysis.py:105-107`). No ffmpeg check happens at startup —
the first time a track is analyzed is the first time the absence of
ffmpeg is discovered, and it is discovered per-track, not globally.

### The decode: streamed, constant memory

`_stream_envelope()` (`draai/analysis.py:28-65`) shells out to ffmpeg
per band:

```
ffmpeg -v error -t 43200 -i <path> -ac 1 -ar 8000 [-af <filter>] -f s16le -
```

- Mono (`-ac 1`), 8kHz (`-ar 8000` — `ANALYSIS_SR`,
  `draai/analysis.py:13`), raw signed 16-bit little-endian PCM on
  stdout. 8kHz is plenty for a loudness envelope; it keeps the ffmpeg
  process and the read loop cheap.
- Capped at `ANALYSIS_MAX_SEC = 43200` seconds (12 hours,
  `draai/analysis.py:15`) via `-t`, so a pathologically long file
  can't run forever.
- The Python side never buffers the whole decode. It reads 64KB chunks
  from `proc.stdout`, appends to a byte buffer, and peels off
  windows of `ANALYSIS_STEP = 0.1` seconds (`draai/analysis.py:14`)
  — `win_bytes = SR * STEP * 2` bytes per window (2 bytes/sample) —
  as soon as enough bytes have accumulated (`draai/analysis.py:42-56`).
  Each window collapses to a single `max(abs(sample))` value
  (`draai/analysis.py:56`), so RAM usage is independent of track
  length: "a 10-hour set never exists in RAM as raw audio, only as
  its 100ms loudness envelope" (comment at `draai/analysis.py:31-32`).
- The subprocess's stderr is discarded (`stderr=subprocess.DEVNULL`,
  `draai/analysis.py:41`); ffmpeg decode errors surface only as "could
  not decode audio" if the envelope ends up empty
  (`draai/analysis.py:63-64`), not as detailed diagnostics.

### Four passes: full band + three filtered bands

`_analyze()` (`draai/analysis.py:68-107`) calls `_stream_envelope`
**four separate times** — once unfiltered for `amp`, and three more
with an `-af` (audio filter) argument for the bands:

| band | ffmpeg `-af` filter | draai/analysis.py |
|---|---|---|
| `amp` (full) | none | 74 |
| `low` | `lowpass=f=250` | 78-79 |
| `mid` | `highpass=f=250,lowpass=f=2000` | 80-81 |
| `high` | `highpass=f=2000` | 82-83 |

So the file is decoded through ffmpeg four times per analysis job —
this is CPU/IO cost paid once, at analysis time, not per-poll.

All four envelopes are scaled by `_scale()` (`draai/analysis.py:23-25`)
against the **same peak** — the max of the raw full-band envelope
(`peak = max(raw_amp)`, `draai/analysis.py:75`) — clamped to
0..100 (`min(100, round(100 * v / peak))`). Sharing one peak across
bands is deliberate ("bands share the full-band peak so relative
loudness is preserved", comment at `draai/analysis.py:77`): a quiet
bass line stays visibly quieter than a loud vocal, rather than each
band being independently normalized to its own 0..100 range.

### Waveform peaks: bucketed for display

`peaks` is a separate, coarser series meant for the static waveform
bar, not the live energy: `amp` (0..100 per 100ms frame) is downsampled
to at most `PEAK_BUCKETS = 240` (`draai/analysis.py:17`) buckets by
taking the `max` of each bucket-sized run of frames
(`draai/analysis.py:86-91`). This keeps the waveform payload small and
resolution-independent of track length.

### Cache: JSON on disk, keyed by track id, versioned

- Directory: `ANALYSIS_DIR = CONFIG_DIR/analysis`
  (`draai/analysis.py:12`), i.e.
  `~/Library/Application Support/SonosMP3Player/analysis/`.
- File name: `<track id>.json` (`draai/analysis.py:101`), where track
  id is the sha1-based id from the rest of the engine (see
  `draai/media.py` / CLAUDE.md — first 16 hex chars of
  `sha1(file path)`), not a content hash of the audio analysis itself.
- Payload includes `"v": ANALYSIS_VERSION` (`draai/analysis.py:16,94`),
  currently `2`. `get_analysis()` checks `data.get("v") ==
  ANALYSIS_VERSION` on every read (`draai/analysis.py:117`); a mismatch
  deletes the stale file and falls through to re-analysis
  (`draai/analysis.py:119`). **Bumping `ANALYSIS_VERSION` invalidates
  every cached file lazily**, one track at a time, on next access —
  there is no bulk migration or cache-wide cleanup pass.
- The comment at `draai/analysis.py:16` records why `v` was last
  bumped: the older format capped analysis at 25 minutes.
- Full cached payload shape (`draai/analysis.py:92-99`):
  `{"status": "ready", "v": 2, "duration": <sec, 1 decimal>,
  "step": 0.1, "peaks": [...], "amp": [...], "low": [...],
  "mid": [...], "high": [...]}`. `duration` is derived from frame
  count (`frames * ANALYSIS_STEP`, `draai/analysis.py:95`), not from
  ffmpeg-reported metadata.

### Concurrency: analysis_state + analysis_lock prevent stampedes

- `analysis_state` (`draai/analysis.py:19`) is a plain dict, `track id
  -> "pending" | "error:<msg>"`, guarded by `analysis_lock`
  (`draai/analysis.py:20`, a `threading.Lock`).
- `get_analysis(tid)` (`draai/analysis.py:110-135`) is the single entry
  point, called both from the HTTP handler and from
  `prefetch_analysis`:
  1. If a fresh (`v` matches) cache file exists, return it immediately
     — no lock needed (`draai/analysis.py:113-121`).
  2. Otherwise, look up the track by id under `state_lock`
     (`draai/analysis.py:122-123`, shared with the rest of the engine's
     track index in `draai/state.py`); unknown id returns an error dict.
  3. Under `analysis_lock`: if no entry exists in `analysis_state` for
     this id, mark it `"pending"` and spawn a daemon thread running
     `_analyze(track)` (`draai/analysis.py:126-131`). If an entry
     already exists (`"pending"` or an error string), no new thread is
     started — the caller just gets the current status
     (`draai/analysis.py:132-135`).
  4. On completion, `_analyze` removes the id from `analysis_state`
     under the lock (`draai/analysis.py:103-104`) so the next
     `get_analysis` call sees the fresh cache file from step 1 instead
     of stale `"pending"` state.
  - Net effect: at most one ffmpeg job per track id runs at a time,
    regardless of how many HTTP requests or prefetch calls arrive
    concurrently for that id. There is no global concurrency cap across
    *different* track ids — each gets its own daemon thread — so
    playing/enqueueing many tracks at once can start many ffmpeg
    processes in parallel (bounded in practice by `prefetch_analysis`'s
    `limit`, see below).
- `prefetch_analysis(ids, limit=3)` (`draai/analysis.py:138-141`) only
  warms the **first 3** ids of whatever list it's given (queue order),
  each in its own thread calling `get_analysis` (which itself
  no-ops if already pending/cached). It is fire-and-forget: return
  values are discarded, nothing is awaited.

### HTTP surface

- `GET /api/analysis?id=<track id>` (`draai/server.py:393-395`) is the
  only endpoint. It just calls `get_analysis(id)` and returns the dict
  as JSON — status is one of `"ready"` (full payload), `"pending"`, or
  `"error"` (with an `"error"` message field).
- `POST /api/play`, `/api/enqueue`, `/api/playlist_load`
  (`draai/server.py:553,563,597`) each call `prefetch_analysis(ids)`
  right after queueing tracks on the speaker, so analysis for the
  first few tracks is already running (or cached) by the time the UI
  asks for it.

### UI consumption

- `loadAnalysis(id)` (`player_ui.html:691-697`) is called whenever the
  now-playing track changes (`player_ui.html:777`). It resets state,
  then polls `GET /api/analysis?id=...` every 2s
  (`setTimeout(..., 2000)`, `player_ui.html:694`) while status is
  `"pending"`, stopping once `"ready"` (or the request errors, which is
  silently swallowed — `catch(e){}`, `player_ui.html:695`).
- `buildPeaks()` (`player_ui.html:699`) converts the 0..100 `peaks`
  array to a `Float32Array` of 0..1 for canvas drawing.
- `energyAt(sec)` (`player_ui.html:700-702`) linearly interpolates
  between the two `amp`/`low`/`mid`/`high` frames straddling the
  current playback position (`step` from the payload, default 0.1s)
  to get a smooth 0..1 value per band at any timestamp — playback
  position is a locally-advanced clock (`tick()`,
  `player_ui.html:707-725`), not re-fetched from the speaker every
  frame.
- Per-frame energy is exponentially smoothed (`ek` time constant
  ~90ms, `player_ui.html:714-715`) and written to CSS custom
  properties `--e-amp/--e-low/--e-mid/--e-high` on `documentElement`
  (`player_ui.html:716-718`), which CSS elsewhere presumably reads for
  glow/pulse effects.
- `drawWave()` (`player_ui.html:730-...`) renders the bucketed `peaks`
  as bars on a `<canvas>`, coloring played vs. unplayed portions
  differently and adding a "shimmer" boost near the playhead driven by
  the smoothed `high` energy.
- If `analysis` is still `null` (never loaded / not ready), `energyAt`
  returns `null` and the tick loop falls back to all-zero energy
  (`player_ui.html:712-713`); `drawWave` falls back to a fake
  sine-ish placeholder waveform when `PEAKS` is empty
  (`player_ui.html:734`, `v=0.35+0.4*Math.abs(Math.sin(i*0.6))`) so the
  bar never looks broken before analysis lands.

### Graceful degradation without ffmpeg

- No feature-detection gate anywhere disables the UI when ffmpeg is
  absent; the pipeline is simply attempted and fails per-track.
- Without ffmpeg, every `_analyze()` call raises immediately
  (`draai/analysis.py:72-73`), `analysis_state[tid]` becomes
  `"error:ffmpeg is not installed (brew install ffmpeg)"`, and
  `get_analysis` keeps returning `{"status": "error", "error": "..."}"`
  for that id until the process restarts (the error is cached only
  in-memory in `analysis_state`, not written to disk — there is no
  JSON file, so nothing needs to expire).
- The UI's poll loop only retries while status is `"pending"`
  (`player_ui.html:694`); an `"error"` status is not explicitly handled
  in `loadAnalysis` — it simply never sets `analysis`, so the waveform
  falls back to the placeholder sine wave and the energy variables stay
  at 0. Playback, queueing, and every other feature are unaffected —
  analysis is purely additive to the visual layer.

## Gotchas

- **`ANALYSIS_VERSION` bump is the only invalidation path.** There's no
  admin endpoint or CLI flag to clear the cache; changing the envelope
  format, sample rate, band filters, or JSON shape requires bumping
  `ANALYSIS_VERSION` (`draai/analysis.py:16`) or old caches will keep
  being served as-is (their `v` still matches) with the old shape.
- **The four ffmpeg passes are not parallelized within one track.**
  `_analyze` calls `_stream_envelope` four times sequentially
  (`draai/analysis.py:74-83`) — full band, then low, then mid, then
  high. A single track's analysis time is roughly 4x one decode pass.
- **`analysis_state` is per-process, in-memory only.** Restarting the
  engine clears all `"pending"`/`"error"` bookkeeping (a genuinely
  in-flight ffmpeg subprocess would be orphaned, though in practice
  short-lived daemon threads die with the process). This also means a
  cached `"error"` status is not persisted — every fresh process retries
  ffmpeg detection per track on first request.
- **Filenames are the track id, not a hash of file content.** If the
  same file path is later replaced with different audio but keeps the
  same id-generating path, `get_analysis` will happily serve the old
  cached analysis (version match, but wrong content) — there's no
  content-hash or mtime check, only the `v` field.
- **`prefetch_analysis`'s `limit=3` is a soft warm-up, not a ceiling.**
  Explicit `GET /api/analysis?id=...` calls from the UI for any track
  (e.g. scrubbing to a track later in a long queue) still trigger
  full analysis regardless of the prefetch limit — the limit only
  bounds how many tracks get proactively warmed right after
  play/enqueue/playlist-load.
- **ffmpeg stderr is thrown away.** Decode failures (corrupt file,
  unsupported codec, permissions) surface only as the generic "could
  not decode audio" message (`draai/analysis.py:64`) when the output
  is empty — there is no way to see ffmpeg's actual error from the API
  response.

## References

- `draai/analysis.py:1-141` — full pipeline (this doc mirrors it almost
  line for line; re-read the file if these line numbers drift).
- `draai/analysis.py:8-20` — imports, constants (`ANALYSIS_DIR`,
  `ANALYSIS_SR`, `ANALYSIS_STEP`, `ANALYSIS_MAX_SEC`,
  `ANALYSIS_VERSION`, `PEAK_BUCKETS`), `analysis_state`, `analysis_lock`.
- `draai/analysis.py:28-65` — `_stream_envelope`: ffmpeg invocation and
  streaming window reduction.
- `draai/analysis.py:68-107` — `_analyze`: four-pass band decode,
  peak bucketing, JSON write, cache dir creation.
- `draai/analysis.py:110-135` — `get_analysis`: cache read + version
  check + stampede-safe job dispatch.
- `draai/analysis.py:138-141` — `prefetch_analysis`.
- `draai/media.py:26-34` — `find_tool`, shared ffmpeg/yt-dlp detection.
- `draai/constants.py:17-19` — `CONFIG_DIR` (analysis cache lives under
  `CONFIG_DIR/analysis`).
- `draai/server.py:393-395` — `GET /api/analysis` handler.
- `draai/server.py:553,563,597` — `prefetch_analysis()` call sites in
  `/api/play`, `/api/enqueue`, `/api/playlist_load`.
- `player_ui.html:690-741` — UI: polling (`loadAnalysis`), peak scaling
  (`buildPeaks`), per-frame energy sampling (`energyAt`), animation
  loop and canvas draw (`tick`, `drawWave`).
- `player_ui.html:777` — `loadAnalysis(id)` call site on track change.
