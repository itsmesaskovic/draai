# Architecture and packaging

> How the `draai/` package is put together — module map, import DAG, entry points, packaging into `draai.pyz`, and where state lives on disk.

## Purpose

DRAAI ships as one dependency-free Python package (`draai/`) plus one
self-contained HTML UI (`player_ui.html`). This doc describes how the
package's ~13 modules divide responsibility, how they're allowed to depend
on each other, how the two entry points (`python3 -m draai` and the
packaged `draai.pyz`) boot the app, and where runtime state (config,
resume positions, analysis cache) is persisted. It exists so an agent can
add or move code without breaking the import DAG or the packaging step.

## Where it lives

Module map (all under `draai/`), one line each:

| Module | Responsibility |
|---|---|
| `draai/__init__.py:1` | Package root; holds `__version__` only. |
| `draai/__main__.py` | CLI entry point: flag handling, port scan, banner, warmup thread, autostart install/uninstall. |
| `draai/constants.py` | Leaf module: app name, port, audio extensions, storage paths, queue/resume tuning constants. |
| `draai/state.py` | Leaf module: shared mutable runtime state (`tracks`, `speakers`, `config`, locks, caches) — see `draai/state.py:1`. |
| `draai/util.py` | Leaf module: `hms_to_sec` / `sec_to_hms` time-string helpers — `draai/util.py:4`, `draai/util.py:12`. |
| `draai/media.py` | `find_tool()` (ffmpeg/yt-dlp discovery), `local_ip_facing()`, `media_url()` — `draai/media.py:10`, `draai/media.py:20`, `draai/media.py:26`. |
| `draai/config.py` | Load/save `config.json` — `draai/config.py:9`, `draai/config.py:24`. |
| `draai/library.py` | Filesystem scan, ID3/MP4/FLAC tag + art parsing, track model (`scan_all`, `read_tags`, `get_art`) — `draai/library.py:274`, `draai/library.py:241`, `draai/library.py:257`. |
| `draai/analysis.py` | ffmpeg-based loudness/band envelope analysis, disk cache — `draai/analysis.py:68`, `draai/analysis.py:110`. |
| `draai/playlists.py` | M3U playlists as files in `<first folder>/Playlists/` — `draai/playlists.py:37`, `draai/playlists.py:58`. |
| `draai/youtube.py` | Hands URLs to the user's own yt-dlp; imports audio into `<first folder>/Imported/` — `draai/youtube.py:24`. |
| `draai/cast.py` | Google Cast (CASTV2) backend: mDNS discovery, TLS session, cast-specific play/queue/volume — `draai/cast.py:137`, `draai/cast.py:205`. |
| `draai/backends.py` | Sonos backend (SSDP/SOAP/UPnP) + the backend dispatch layer that routes each call to Sonos or Cast, resume positions — `draai/backends.py:1-3`. |
| `draai/server.py` | HTTP `Handler` (JSON API, media serving with Range, UI serving), QR generator, built-in `PAGE` fallback — `draai/server.py:1`, `draai/server.py:286`. |

`draai/player_ui.html` also lives inside the package directory as the
bundled copy of the top-level UI (see [`_load_ui()` resolution
order](#_load_ui-resolution-order) below) — it's an asset, not a Python
module.

## Key concepts

### The import DAG

Leaves (no intra-package imports, or only import other leaves):
`draai/constants.py` (stdlib only), `draai/util.py` (no imports at all),
`draai/state.py` (imports only `draai.constants`).

Everything else forms a DAG rooted at `draai/server.py` and
`draai/__main__.py`:

```
constants, util, state           (leaves)
   ↑
media, config                    (media.py:7 imports draai.state; config.py:6 imports draai.state)
   ↑
library                          (library.py:5-7 imports draai.state, draai.constants)
   ↑
analysis, cast                   (analysis.py:8-10; cast.py:11-14 — cast has no dependency on backends)
   ↑
youtube                          (youtube.py:8-11 imports state, media, library, and re-exports from analysis)
   ↑
backends                         (backends.py:16-26 imports state, constants, util, media, cast)
   ↑
playlists                        (playlists.py:5-6 imports state, backends — for browse_queue)
   ↑
server                           (server.py:11-24 imports nearly everything above)
   ↑
__main__                         (root: imports state, constants, config, library, backends, server)
```

Verified by grepping every `import`/`from` line in the package (no
`draai.server` or `draai.__main__` import appears anywhere except in
`__main__.py` itself) — confirmed 2026-07-16 with
`grep -n "^import\|^from" draai/*.py`. **Keep it a DAG**: a new module may
only import from modules strictly below it in this chain; never add an
import that would let `state.py` or `constants.py` reach upward, and never
let `server.py`/`__main__.py` be imported by anything else (they're roots,
per `CLAUDE.md`'s hard rule 1).

One wrinkle: `draai/youtube.py:82` re-exports analysis internals
(`_scale, _stream_envelope, _analyze, get_analysis, prefetch_analysis`)
with the comment `# re-export during the split` — nothing in the package
currently imports those names from `draai.youtube` (server.py imports them
straight from `draai.analysis`, `server.py:17`), so this looks like
leftover scaffolding from the `sonos_player.py` → `draai/` split that can
likely be deleted; see [Gotchas](#gotchas).

### Entry points

**`python3 -m draai`** runs `draai/__main__.py:main()` directly.
**`draai.pyz`** is a zipapp with `main="draai.__main__:main"`
(`build.py:31`) — same function, different launch mechanism.
`__main__.py`'s own `_launch_args()` (`draai/__main__.py:18-29`)
distinguishes the two at runtime by checking whether `sys.argv[0]` ends in
`.pyz`.

`main()` flow (`draai/__main__.py:80-136`):
1. `--version` → print `APP_NAME __version__` and return (`:81-83`).
2. `--install-autostart` / `--uninstall-autostart` → run and return
   (`:84-89`).
3. `load_config()` (`draai/config.py:9`) then `load_positions()`
   (`draai/backends.py:309`) — populate `state.config` and
   `state.positions` from disk before the server exists.
4. Port scan: try `ThreadingHTTPServer(("0.0.0.0", port), Handler)` for
   `port` in `range(PREFERRED_PORT, PREFERRED_PORT + 20)`
   (`draai/__main__.py:94-100`); `PREFERRED_PORT` is `8765`
   (`draai/constants.py:4`). First bind that succeeds wins; the chosen
   port is stored in `state.server_port`. If all 20 ports are taken, print
   an error and `sys.exit(1)` (`:101-103`).
5. Print the banner (control-panel URL, "keep this window open", the
   macOS incoming-connections hint, Ctrl+C to quit) — `:105-118`.
6. Start a **daemon warmup thread** (`draai/__main__.py:121-129`): calls
   `scan_all()` (`draai/library.py:274`) once, then retries
   `refresh_speakers()` (`draai/backends.py:73`) up to 4 times with a
   4-second sleep between attempts, stopping early if speakers are found.
   This exists because SSDP discovery is often slow right after the Mac's
   network stack comes up at boot.
7. Unless `--headless` is in `sys.argv`, a one-shot `threading.Timer(0.8,
   ...)` opens the control panel in the default browser via
   `webbrowser.open(url)` (`:131-132`). `--headless` is what the launchd
   autostart plist passes so login doesn't pop a browser window.
8. `httpd.serve_forever()`, with `KeyboardInterrupt` caught to print
   "Bye!" and exit cleanly (`:133-136`).

### Autostart

`install_autostart()` (`draai/__main__.py:32-63`) writes a launchd plist
to `~/Library/LaunchAgents/com.draai.player.plist` with
`RunAtLoad=true`, `KeepAlive=true`, and stdout/stderr redirected to
`~/Library/Logs/draai.log`. The `ProgramArguments` array is built from
`_launch_args()` (`:18-29`):
- If launched from a `.pyz` (argv0 ends in `.pyz`): `[python, argv0,
  "--headless"]`, working directory = the `.pyz`'s own directory.
- Otherwise (running as `python3 -m draai`): `[python, "-m", "draai",
  "--headless"]`, working directory = the parent of the `draai/` package
  directory (so `-m draai` resolves from there).

After writing the plist, it best-effort `launchctl unload`s then `load
-w`s it (`:55-59`) so a re-install takes effect immediately.
`uninstall_autostart()` (`:66-77`) unloads and deletes the plist file.

### `build.py`: staging the zipapp

`build.py:18-35` builds `draai.pyz` by:
1. Creating a temp staging directory (`tempfile.mkdtemp()`).
2. `shutil.copytree`-ing the whole `draai/` package into
   `<stage>/draai`, excluding `__pycache__`/`*.pyc` (`build.py:22-23`).
3. Copying the **top-level** `player_ui.html` (the source of truth,
   living next to `build.py`) into `<stage>/draai/player_ui.html`
   (`build.py:25-27`) — this is what keeps the packaged UI in sync with
   whatever's being edited at the repo root; the copy inside
   `draai/player_ui.html` in the source tree is what ships and what
   `_load_ui()` finds via `importlib.resources` at runtime.
4. `zipapp.create_archive(stage, target="draai.pyz",
   interpreter="/usr/bin/env python3", main="draai.__main__:main")`
   (`build.py:29-31`) — because `stage` contains a `draai/` directory (not
   the module files directly), `import draai.X` resolves correctly inside
   the zip, and `zipapp`'s `main=` parameter generates a `__main__.py` at
   the zip root that calls `draai.__main__.main()`.
5. `chmod 755` the output so `./draai.pyz` is directly executable
   (`build.py:32`).

### `_load_ui()` resolution order

`draai/server.py:27-42`, called from `Handler.do_GET` for path `/`
(`draai/server.py:314-320`):
1. `player_ui.html` in the **current working directory** — read straight
   from disk. This is what makes live-editing the UI possible: run the
   engine, edit `player_ui.html` in the repo root, refresh the browser.
2. Else, the packaged copy inside the `draai` package via
   `importlib.resources.files("draai").joinpath("player_ui.html")`
   (`:39-40`) — this is what resolves inside a `.pyz` where there is no
   ordinary filesystem path to read from directly.
3. Else, the built-in `PAGE` string constant (`draai/server.py:755` to end
   of file) — a minimal, dependency-free fallback UI that still talks to
   the same `/api/*` endpoints. Per `CLAUDE.md`, new HALCYON-level UI
   features go in `player_ui.html` only; `PAGE` just needs to keep
   working as the zero-file experience.

### Storage locations

All persistent state lives under one app-support directory,
`CONFIG_DIR` (`draai/constants.py:17-19`):

```
~/Library/Application Support/SonosMP3Player/
├── config.json        # CONFIG_PATH, constants.py:20 — folders, manual_ips,
│                       #   last_speaker, ui prefs (theme/group/sort)
├── positions.json      # POSITIONS_PATH, constants.py:23 — resume positions
│                       #   for tracks longer than RESUME_MIN_TRACK (600s)
└── analysis/            # ANALYSIS_DIR, analysis.py:12 — one <track-id>.json
                          #   per analyzed track, keyed with a "v" field
                          #   (ANALYSIS_VERSION, analysis.py:16) to invalidate
                          #   stale caches on format changes
```

Note the directory name is still `SonosMP3Player` (the pre-rename app
name) even though the app is now DRAAI — changing it would silently
orphan every user's existing config/positions/analysis cache, so it's
deliberately left alone. `config.py:9-21` also migrates an old
single-`"folder"` key into the new `"folders"` list on load, for anyone
upgrading from before multi-folder support existed.

Playlists are **not** under `CONFIG_DIR` — they're `.m3u` files inside
`<first library folder>/Playlists/` (`draai/playlists.py:9-12`), so
they travel with the music, not the app.

### Threading model

- The HTTP server is a stdlib `ThreadingHTTPServer`
  (`draai/__main__.py:96`) — each request is handled on its own thread,
  so a slow speaker call (SOAP over the network, `soap_call()` at
  `draai/backends.py:184`, has an 8s `urlopen` timeout) doesn't block
  other API calls.
- `state.state_lock` (`draai/state.py:12`) guards the shared library/
  speaker containers (`tracks`, `tracks_by_id`, `speakers`, and reads of
  `config`); every module that touches those containers takes it first
  (e.g. `draai/library.py:287`, `draai/server.py:328`).
- `state.positions_lock` (`draai/state.py:23`) separately guards the
  resume-positions dict, written from playback-status polling.
- `analysis.analysis_lock` (`draai/analysis.py:20`) guards the
  in-progress/error tracking dict `analysis_state` so concurrent
  `/api/analysis` requests for the same track don't spawn duplicate
  ffmpeg jobs (`draai/analysis.py:126-135`).
- Background work is always a `daemon=True` thread so it never blocks
  process exit: the boot warmup (`draai/__main__.py:129`), per-track
  analysis jobs (`draai/analysis.py:130`), YouTube import jobs
  (`draai/youtube.py:71`), and the queue-fill-in-background half of
  `play_tracks()` (`draai/backends.py:305`, guarded by
  `enqueue_generation` so a newer play request cancels a stale one still
  filling the queue).

### Pure-stdlib, optional tools

The whole package imports nothing outside the Python standard library —
confirmed by the import grep above (every non-`draai.*` import is a
stdlib module: `json`, `os`, `socket`, `ssl`, `struct`, `threading`,
`urllib.*`, `xml.etree`, `http.server`, etc.). External tools (`ffmpeg`,
`yt-dlp`) are never imported as Python packages; they're located on disk
at runtime by `find_tool()` (`draai/media.py:26-34`, checks `shutil.which`
then falls back to `/opt/homebrew/bin` and `/usr/local/bin`) and invoked
as subprocesses. Their absence degrades a feature (no waveform analysis,
no YouTube import) rather than breaking startup — `analysis.py:71-73` and
`youtube.py:19-21,25-28` both surface a human-readable "brew install ..."
message instead of raising an import error.

## Gotchas

- **`youtube.py`'s trailing re-export is dead weight from the split.**
  `draai/youtube.py:82` re-exports five analysis symbols with the comment
  `# re-export during the split`; nothing imports them through
  `draai.youtube` today (`server.py` imports them from `draai.analysis`
  directly). It's a candidate for deletion, not a load-bearing part of
  the DAG — but don't remove it without grepping for external/test
  imports first.
- **`Handler.server_version` still says `SonosMP3Player/1.0`**
  (`draai/server.py:288`) — cosmetic, shows up in the `Server:` HTTP
  response header, harmless but a naming fossil from before the DRAAI
  rename, same family as the `SonosMP3Player` config directory name.
- **The port scan is why "is DRAAI already running?" matters.** Because
  `main()` silently tries 20 ports in a row (`draai/__main__.py:94-100`),
  a second instance doesn't fail loudly — it just binds the next free
  port and prints a different URL in its own banner. There is no
  cross-instance lock file.
- **`_load_ui()`'s cwd-first check is a live-editing convenience, not a
  security boundary.** Anyone who starts DRAAI from a directory
  containing an arbitrary `player_ui.html` gets that file served as the
  UI — expected for local development, worth remembering if this is ever
  scripted or run from an untrusted working directory.
- **`build.py` copies `player_ui.html` at build time only.** If you edit
  the top-level `player_ui.html` and only re-run `python3 -m draai`
  (not `build.py`), you're testing the live cwd copy via `_load_ui()`'s
  first branch — the packaged copy inside `draai/player_ui.html` (and
  therefore any already-built `draai.pyz`) is untouched until the next
  `python3 build.py`.
- **`CONFIG_DIR` keeps the pre-rebrand name `SonosMP3Player`
  deliberately** (`draai/constants.py:17-19`) — renaming it would orphan
  existing users' config, resume positions, and analysis cache on
  upgrade.

## References

- `draai/__init__.py:1` — `__version__`.
- `draai/__main__.py:80-136` — `main()`: flags, port scan, banner, warmup
  thread, `--headless`, browser open.
- `draai/__main__.py:18-29` — `_launch_args()`.
- `draai/__main__.py:32-77` — `install_autostart()` / `uninstall_autostart()`.
- `build.py:18-35` — `build()`: zipapp staging and archive creation.
- `draai/server.py:27-42` — `_load_ui()` resolution order.
- `draai/server.py:286-288` — `Handler` class header.
- `draai/state.py:1-25` — shared mutable state and locks.
- `draai/constants.py:17-25` — `CONFIG_DIR`, `CONFIG_PATH`,
  `POSITIONS_PATH`, resume tuning constants.
- `draai/config.py:9-30` — `load_config()` / `save_config()`.
- `draai/media.py:26-34` — `find_tool()`.
- `draai/analysis.py:12-16` — `ANALYSIS_DIR`, `ANALYSIS_VERSION`.
- `draai/backends.py:309-317` — `load_positions()`.
- `draai/youtube.py:82` — leftover re-export from the package split.
