# DRAAI — split the engine into a package, ship as one `.pyz` (Option C)

> **For agentic workers:** execute phase-by-phase; the 23-test suite + a booting app are the safety net — run `python3 tests/test_draai.py` after EVERY step, and it must stay green. Steps use `- [ ]`.

**Goal:** Turn the 3,381-line `sonos_player.py` into a modular `draai/` package (stdlib-only), and ship a single runnable `draai.pyz` that contains the engine *and* the UI. Behavior identical; distribution goes from two files to one.

**Architecture:** A leaf `state.py` (mutable runtime state) + `constants.py` (read-only), imported by everyone. Topic modules (`config, media, library, analysis, playlists, youtube, sonos, cast, backends, server`) form a clean DAG. `__main__.py` is the entry point. `zipapp` bundles it to `draai.pyz`. During the transition, the shrinking `sonos_player.py` re-exports from the new modules so callers keep working; at the end it becomes a 3-line shim.

## Global Constraints

- **stdlib only** — the constraint that stays. No pip, ever.
- Behavior must not change; `python3 tests/test_draai.py` (**23 tests**) stays green after every step, and the app must still boot + discover + play.
- The single-file *distribution* is preserved via `draai.pyz` (this REPLACES CLAUDE.md hard-rule #1's "one file, no splitting" — update that rule in the final phase).
- Commits are the maintainer's; each phase ends at a verified checkpoint on the `v1.2-cast-backend` branch (or a child branch).

## The state strategy (the crux — keep it simple)

Only 6 module-level names are ever reassigned: `config, tracks, tracks_by_id, speakers, positions` (containers) and `server_port` (an int).
- **Convert the 5 container rebinds to in-place mutation** so the objects are never replaced → every module can `from .state import config, tracks, tracks_by_id, speakers, positions` and reference them by bare name UNCHANGED:
  - `load_config`: build `merged`, then `config.clear(); config.update(merged)` (instead of `config = merged`).
  - `scan_all`: `tracks[:] = new_list`; `tracks_by_id.clear(); tracks_by_id.update(new_map)`.
  - `refresh_speakers`: `speakers[:] = merged`.
  - `load_positions`: `positions.clear(); positions.update(loaded)`.
- **`server_port`** stays in `state` and is accessed as `state.server_port` (only 4 sites: `media_url`, the access-url handler, `main` set, `main` url). Everyone else imports containers by name.
- Locks/dicts/lists mutated in place (`state_lock, cast_sessions, cast_queues, positions_lock, yt_jobs, art_cache, enqueue_generation, _positions_dirty, *_lock`) → plain `from .state import …`, no reference changes.

## Module map (target)

```
draai/__init__.py       __version__
draai/constants.py      all UPPER_CASE consts + PAGE fallback string
draai/state.py          the mutable globals (6 reassigned → mutated in place; rest as-is)
draai/config.py         load_config, save_config
draai/media.py          media_url, local_ip_facing, Range file serving helper
draai/library.py        scan_all/scan_folder/_scan_root, read_tags/_tags_flac/_syncsafe, get_art
draai/analysis.py       _stream_envelope, _analyze, get_analysis, prefetch_analysis, _scale
draai/playlists.py      playlists_dir, list/save/load/delete_playlist, safe_playlist_name
draai/youtube.py        find_tool, yt_available, start_youtube_job
draai/sonos.py          ssdp_discover, soap_call, avt, didl_for, get_zone_groups, sonos control+queue+eq+grouping
draai/cast.py           mDNS + CASTV2 codec + CastSession + cast_discover + cast control+queue
draai/backends.py       device model + dispatch: refresh_speakers, speaker_by_uuid, play_tracks/get_status/… routers
draai/server.py         HTTP handler + /api/* routing + UI serving
draai/__main__.py       main(), arg parse, autostart, entry
draai/player_ui.html    the UI, embedded as package data
build.py                zipapp → draai.pyz
(sonos_player.py)       REMOVED — no shim/facade. Entry is `python3 -m draai` / `draai.pyz`.
                        Renaming the entry is user-facing/breaking → land this in a NEW version (2.0.0).
```
DAG (import direction): `state, constants` ← everything; `sonos, cast` ← `state, constants, media`; `backends` ← `sonos, cast`; `server` ← `backends + all`; `__main__` ← `server`.

---

## Phase 1 — Scaffold + constants + state (foundation)

- [ ] Create `draai/__init__.py` (`__version__ = "..."` — move from the engine).
- [ ] Create `draai/constants.py`: move every UPPER_CASE constant (APP_NAME, PREFERRED_PORT, AUDIO_EXTS, QUEUE_CAP, CONFIG_DIR/PATH, SSDP_*, AVT/RC/GRC/CD, NS_*, CAST_APP, CAST_BAD_EXTS, POSITIONS_PATH, RESUME_*, ANALYSIS_*, _QR_*, _GF_*, YT_URL_RE, PAGE, etc.).
- [ ] Create `draai/state.py`: move the mutable globals; keep them as containers. `server_port` lives here too.
- [ ] In `sonos_player.py`: at top, `from draai import state` + `from draai.constants import *` + `from draai.state import config, tracks, tracks_by_id, speakers, positions, state_lock, cast_sessions, cast_sessions_lock, cast_queues, cast_queues_lock, positions_lock, _positions_dirty, yt_jobs, art_cache, enqueue_generation`. Remove the now-moved definitions.
- [ ] Apply the 4 in-place-mutation conversions + the `server_port`→`state.server_port` at its 4 sites (see state strategy).
- [ ] **Verify:** `python3 tests/test_draai.py` → 23 OK; `python3 sonos_player.py` boots and serves `/api/state`. Checkpoint.

## Phase 2 — Extract leaf utility modules (one at a time, tests after each)

For each of `config.py, media.py, library.py, analysis.py, playlists.py, youtube.py`:
- [ ] Move its functions into the new module; add `from . import state` / `from .constants import …` / `from .media import media_url` as needed.
- [ ] In `sonos_player.py`, replace the moved defs with a re-export: `from .<mod> import <names>` (so existing callers in the hub keep working).
- [ ] **Verify 23 tests green** + app boots. Checkpoint per module.

## Phase 3 — Extract backends

- [ ] `draai/cast.py` FIRST (most self-contained: mDNS, codec, CastSession, cast control+queue). Re-export from hub. Verify.
- [ ] `draai/sonos.py` (SSDP/SOAP/zone/control). Re-export. Verify.
- [ ] `draai/backends.py` (dispatch: `refresh_speakers`, `speaker_by_uuid`, and the `play_tracks/get_status/set_volume/seek_to/browse_queue/enqueue_tracks/queue_*/set_room_volume` routers that `if backend=="cast"` to cast else sonos). Verify.

## Phase 4 — Extract server + entry, embed the UI

- [ ] `draai/server.py`: the HTTP handler class + `/api/*` routing. Change UI serving to read `player_ui.html` as package data (`importlib.resources.files("draai").joinpath("player_ui.html").read_text()`), keeping a "prefer an external `player_ui.html` in cwd if present" fallback for live dev.
- [ ] `draai/__main__.py`: `main()`, argparse (`--version`), autostart install/uninstall, entry. `PAGE` stays in `constants.py`.
- [ ] **DELETE `sonos_player.py`** (maintainer decision: no shim, no facade — the entry is the package). Migrate the test loader to `import draai` + reference the package's modules (draai/__init__.py may re-export a flat public API for convenience). Update README run command → `python3 -m draai` (and `draai.pyz`).
- [ ] Copy `player_ui.html` into `draai/player_ui.html` (build step will keep it in sync).
- [ ] **Verify:** `python3 -m draai` boots, serves the UI, discovers Sonos+Cast, plays. 23 tests green.

## Phase 5 — Build + docs

- [ ] `build.py` (stdlib `zipapp`): copy `player_ui.html` → `draai/player_ui.html`, `zipapp.create_archive("draai", "draai.pyz", interpreter="/usr/bin/env python3", main="draai.__main__:main")`.
- [ ] **Verify one-file distribution:** `python3 build.py && python3 draai.pyz` → serves UI + discovers + plays (real-hardware smoke on Zolder).
- [ ] Update **CLAUDE.md hard-rule #1** ("modular `draai/` package; ships as `draai.pyz`; stdlib-only stays; no splitting → replaced by DAG discipline") and the README run command (`python3 draai.pyz`).

## Risks / watch-items

- **Circular imports** — obey the DAG (state/constants are leaves; server is the root). If a cycle appears, a function is in the wrong module.
- **The `from .state import config` rule** depends on the in-place-mutation conversions — if any rebind slips back in, readers go stale. Grep for `global ` after Phase 1: only `server_port` (via `state.server_port =`) should remain conceptually; container rebinds gone.
- **UI loading under zipapp** — `__file__`/"html next to script" changes; the package-data read + external fallback handles it. Test both the `.pyz` and `python -m draai` paths.
- **Re-export shims** keep the hub working mid-refactor; they're removed as `server.py` takes over the imports in Phase 4. Harmless if a few linger.
- Test file imports change from `importlib.spec_from_file_location('sp','sonos_player.py')` to importing `draai` modules — update the loader in Phase 1 or keep loading the shim (which imports the package) until Phase 4.
