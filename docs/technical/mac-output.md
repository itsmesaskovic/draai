# "This Mac" outputs (backend `local`)

**Purpose:** every audio output of the Mac that runs the engine (built-in
speakers, monitors over DisplayPort/HDMI, USB headsets) is a pickable room,
next to the Sonos and Cast rooms. Playback is done by the engine, so it keeps
going with the browser tab closed and the phone remote can control it.

**Where it lives:** `draai/local.py` (enumeration + `LocalPlayer`),
dispatch in `draai/backends.py` (`backend == "local"` branches,
`refresh_local_speakers()`), `/api/cmd` + `/api/state` in `draai/server.py`,
UI in `ui/90-boot.html` (`renderRooms`, `refreshState`) and `remote.html`.

## Enumeration

`_enumerate_coreaudio()` calls CoreAudio through `ctypes` (stdlib only, loaded
lazily so other platforms never fail; any error yields `[]`):

- `AudioObjectGetPropertyData` on the system object (id 1) with selector
  `dev#` gives the device ids; the **position in that array is the device
  index** ffmpeg's `audiotoolbox` wants (inputs included).
- Per device: `lnam` (name), `uid ` (stable UID, CFString), and `slay` on scope
  `outp` (an `AudioBufferList`: UInt32 count, buffers start at offset 8, 16
  bytes each, first UInt32 = channels). Output-capable = channels > 0.
- `list_outputs()` filters to outputs and numbers duplicates (`LG HDR 4K`,
  `LG HDR 4K (2)`). Speaker dicts are `uuid = "MAC_" + uid`, `ip = None`.
- Only when `sys.platform == "darwin"` and ffmpeg is found; otherwise no rooms
  and no error.

**Hot-plug:** `/api/state` calls `refresh_local_speakers()` (milliseconds) which
splices the `local` entries after the Sonos/Cast ones. The UI re-fetches state
every ~10 s. The ffmpeg device index is looked up again right before every
spawn, never cached.

**TTL cache:** `local_discover()` re-enumerates CoreAudio at most once per
`LOCAL_REFRESH_TTL` (3 s, monotonic clock), so a polling UI is cheap. Rescan
(`refresh_speakers`) passes `force=True`. `_available()` caches its
`find_tool("ffmpeg")` answer for 30 s. Each real enumeration also runs
`reap_missing()`: a PLAYING/PAUSED player whose UID is gone is halted with "The
<device> is no longer connected." (never on an empty enumeration, which may be
a CoreAudio error).

## Playback

ffmpeg reads the track file directly (no HTTP):

    ffmpeg -nostdin -hide_banner -loglevel error -ss <start> -i <path> -vn
           -af volume=<v/100> -f audiotoolbox -audio_device_index <n>
           -progress pipe:1 -

- A reader thread parses `out_time_us=` from `-progress`; position = start
  offset + out time. Each spawn bumps a generation counter, so output from a
  process we killed is ignored.
- Clean exit advances the in-memory queue (Cast semantics); end of queue is
  `STOPPED`. A non-zero exit within 1 s skips the file with "Couldn't play that
  file — skipped it."; if every track fails in a row it stops.
- **Pause = kill + remember position; resume and seek = respawn with `-ss`**
  (about half a second of hiccup).
- **Volume** is DRAAI's own (ffmpeg `volume` filter, 0-100, default 30,
  `config["local_volume"][uuid]`). Changes respawn at the current position,
  debounced 300 ms. The device's hardware volume and the system default output
  are never touched.
- Resume positions use `note_position()` from `get_status` and the usual
  `RESUME_MIN_POS` rule on start. Sleep timer = engine timer that stops.
- **Stall watchdog:** a hung ffmpeg (device unplugged) may never exit. A
  per-spawn watchdog thread kills the process if PLAYING and `out_time_us` has
  not advanced for `STALL_SECS` (5 s), then runs the normal non-zero-exit path
  (device gone -> "no longer connected", else skip). It is tied to the
  generation counter, so pause/seek/volume respawns retire it.
- **Duration probe:** library tracks carry no duration, so when a track
  starts with an unknown one, a background thread runs
  `ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1`
  (10 s timeout; `_probe_duration`, cached in `_durations`). Status then
  reports `duration`, and `observe` -> `note_track` stores it as usual. No
  analysis-cache fallback: `local.py` may not import `analysis`.
- Config: `set_volume` mutates `config["local_volume"]` under
  `config.config_lock`, which `save_config` also holds.
- Exit: `local_shutdown()` (atexit + Ctrl+C path) kills every child.

## Error sentences

- `Playing on this Mac needs ffmpeg.`
- `The <device> is no longer connected.` (stop, once; no respawn loop)
- `Couldn't play that file — skipped it.`
- `Outputs on this Mac can't be grouped.`
- `The equalizer isn't available for outputs on this Mac.`

The status `error` field is toasted once by the UI.

## Selection rule

A Mac output is **never auto-selected**: at boot the UI (and the phone remote)
picks `last_speaker` if present, else the first non-local room. A DJ set must
not start on the laptop speakers just because Sonos was slow to answer.
Warm-up in `__main__` keeps retrying until a Sonos/Cast device answers.

## Why not AirPlay / Apple TV

AirPlay receivers are not exposed as CoreAudio devices (macOS routes them
internally) and AirPlay 2 needs crypto the stdlib lacks. Out of scope.

## Gotchas

- The `slay` buffer list has an 8-byte header on 64-bit (pointer alignment),
  not 4.
- ffmpeg's `-list_devices` shows `(null)` for some monitor names; names come
  from CoreAudio, only the index from the array position.
- Tests replace `draai.local._enumerate_coreaudio`, `_available` and `_spawn`;
  nothing in the suite touches CoreAudio or ffmpeg.
