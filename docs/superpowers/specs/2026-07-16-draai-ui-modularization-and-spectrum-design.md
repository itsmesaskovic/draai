# DRAAI UI modularization + Spectrum mode — design

**Date:** 2026-07-16
**Status:** Approved for planning
**Topic:** Split the growing single `player_ui.html` source into `ui/` partials assembled into one self-contained served document (mirroring the backend `draai/` package → one `draai.pyz`), then add the Spectrum analyzer as the first new full-screen mode on the modular structure.

## Purpose

`player_ui.html` is 1,685 lines and gaining a third full-screen mode. The shared engine (render loop, clock, energy, canvas helpers, commands) is already factored, but each full-screen mode still spreads its CSS + markup + JS + wiring across one long file. Just as the backend split from one `sonos_player.py` into a `draai/` package that *ships* as one `draai.pyz`, the UI should split into `ui/` source partials that *serve* as one self-contained `player_ui.html`. This makes each mode a self-contained unit and adding one a small, local change — which Spectrum then demonstrates.

## Goals

- **`ui/` source partials** — the head/shell, shared CSS/markup/JS ("core"), and one **triplet (css + markup + js) per full-screen mode** — plus a pure-stdlib **assembler** that concatenates them into the single self-contained `player_ui.html` document (no external requests, offline, drop-in override all preserved).
- **Byte-identical invariant:** the assembler's output equals the current committed `player_ui.html` exactly — a zero-behavior-change refactor, de-risked the same way the backend split was (prove no behavior change, then build on it).
- **Server-side assembly** in `_load_ui` so **clone→run and edit→reload both work with no build step** — resolving the one real asymmetry with the backend (a browser can't run a self-contained UI straight from parts). `build.py` bakes the assembled file into the `.pyz`.
- **Adding a mode = a triplet + a manifest entry.** Demonstrated by folding in **Spectrum** (Deliverable 2), including a small shared `openMode` switching helper so mode-switching is DRY.
- Amend CLAUDE.md rule #2 to describe the assembled model.

## Non-goals

- **No behavior or visual change from the modularization** — byte-identical output is the gate.
- **No external requests, CDNs, web fonts, npm, or bundler.** Assembly is pure Python stdlib; the browser still receives ONE inline self-contained offline document, still overridable by dropping a single `player_ui.html`.
- No JS test harness (the UI still has none; verification is the byte-identical diff + Browser-pane).
- Spectrum's visual design is already settled from the reference (`HALCYON` spectrum export); this spec folds it onto the new structure rather than re-deriving it.
- No change to the built-in `PAGE` fallback, the `remote.html` slim remote, or the engine beyond `_load_ui` assembly.

## Background (verified 2026-07-16)

- `_load_ui()` (`draai/server.py:27-42`): serve `cwd/player_ui.html` (override) → packaged `draai/player_ui.html` via `importlib.resources` (the `.pyz`) → `PAGE`.
- `build.py:25-27`: copies the top-level `player_ui.html` into the staged package for the `.pyz`. `draai/player_ui.html` is a git-ignored build artifact (`.gitignore:7`); the **top-level `player_ui.html` is the committed source today**.
- `player_ui.html`: 1,685 lines, 40 `/* ---- section ---- */` landmarks; `<style>` ends ~line 478, markup ~478-716, `<script>` ~717-1685. Full-screen sections `#np` (579) and `#amp` (630); shared JS engine `tick`/`energyAt`/`sm`/`mkCanvas`/`sizeCanvas`/`drawWave`/`cmd`/`applyStatus`/`openNp`/`openAmp` (813-1002).
- Precedent: the backend split (`draai/` package, `build.py` via stdlib `zipapp`) — same "modular source, single distributable" pattern.

## Design

### 1. `ui/` layout

An **ordered manifest** of partial files that concatenate verbatim to reproduce the document. Partials carry the exact substrings of the current file (byte-identical). Grouping:

```
ui/
  manifest.txt          # ordered list of partial paths (the assembly order)
  00-head.html          # <!doctype> … <head> … <style>   (shell open + CSS-open)
  css/
    10-base.css         # tokens, resets, scrollbars, ic
    20-sidebar.css  21-main.css  22-dock.css
    30-fullscreen-core.css        # .np/.artstage/kinetic shared fullscreen chrome
    40-light-theme.css  41-popovers.css  42-vinyl.css  49-responsive.css
  40-body.html          # </style></head><body>   (CSS-close + body-open)
  body/
    50-main-ui.html     # sidebar + main + dock markup
    59-popovers.html
  70-script.html        # <script> "use strict" …   (body-... + script-open)
  js/
    80-core.js          # $, state A, api, utils, palette, artwork,
                        #   analysis+visualizer (tick/energyAt/sm/canvas/drawWave),
                        #   status, polling, commands, rooms, library, eq, sleep,
                        #   youtube, qr, idle, events, refresh, folders, selection,
                        #   playlists, queue, theme, vinyl, media-keys, boot
  99-foot.html          # </script></body></html>
  modes/
    np/     np.css     np.html     np.js        # Now Playing triplet
    amp/    amp.css    amp.html    amp.js        # Amplifier triplet
```

- **Mode triplet convention:** each `ui/modes/<name>/` holds `<name>.css`, `<name>.html`, `<name>.js`. The manifest lists each part at the right insertion point (mode CSS after `fullscreen-core.css`, before `responsive.css`; mode markup after the main-UI body / before popovers; mode JS after `core.js`). Adding a mode = drop the folder + 3 manifest lines.
- **Granularity is chosen for byte-identical + mode isolation**, not maximal splitting — `core.js` stays one file (the shared engine + main UI is genuinely one cohesive unit; splitting it further buys nothing and risks the invariant). The *modes* are what we isolate.
- The exact carve boundaries are set during implementation so the concatenation reproduces the current bytes.

### 2. Assembler (pure stdlib, shared by engine + build)

- New `draai/ui.py` (stdlib only): `assemble_ui(ui_dir) -> str` reads `manifest.txt` and concatenates the listed partials verbatim. No templating engine, no placeholders — plain ordered concatenation (the shell fragments are themselves partials in the list), which makes byte-identical trivial and the code ~15 lines.
- **`_load_ui` resolution (new order):**
  1. `cwd/player_ui.html` exists → serve it (**override**, unchanged).
  2. else a source `ui/` dir is found (package-relative: `<repo>/ui`, i.e. `dirname(dirname(server.__file__))/ui`) → `assemble_ui(ui_dir)` → serve. (This is the dev / run-from-source path — no build step.)
  3. else packaged `draai/player_ui.html` via `importlib.resources` → serve (**the `.pyz` baked file**).
  4. else `PAGE`.
- **`build.py`:** assemble `ui/` → write `draai/player_ui.html` into the staged package (baked), so the `.pyz` needs no runtime assembly and step 3 serves it. (`ui/` need not ship in the `.pyz`.)
- The **top-level `player_ui.html` is deleted from the repo and git-ignored** — `ui/` partials become the committed source of truth (no drift, mirroring why `draai/player_ui.html` is ignored). Nothing writes a top-level `player_ui.html` in dev; the override slot stays free for a user's own file.
- Assembly runs per request in dev (reading ~20 small files + concat is a few ms, matching today's per-request file read); optional mtime cache is a later nicety, not required.

### 3. Byte-identical invariant + verification

- Before carving, snapshot the current committed `player_ui.html`.
- Carve into `ui/` partials; `assemble_ui(ui/)` must equal the snapshot **byte-for-byte** (`diff` empty). This is the refactor's gate — proof of zero behavior change.
- Durable guard (`tests/test_draai.py`): assert `assemble_ui` produces a document that opens with `<!doctype html`, closes `</html>`, and contains a sentinel marker from **each** region (base CSS token, the `#np`/`#amp` sections, the `tick`/`boot` JS) — so a broken manifest or missing partial fails CI even without ffmpeg/browser. (A committed checksum of the assembled output is an option but would need updating on every intentional UI edit; the marker test is lower-friction.)
- `_load_ui` behavior test: serving assembles the same content (a live-server GET `/` contains the sentinels), and an external `cwd/player_ui.html` still overrides.

### 4. Deliverable 2 — fold in Spectrum (the payoff)

On the modular structure, Spectrum is a new triplet `ui/modes/spectrum/spectrum.{css,html,js}` + 3 manifest lines, reusing the shared engine. Its design (from the reference, reconciled to DRAAI):

- **Canvas of ~56 vertical bars** (`SPEC_N=56`); `bandCurve(f)` builds an FFT-style curve from the three bands via **Gaussian lobes** at f≈0.05/0.42/0.9 (low/mid/high) × rolloff `1−0.28f`; deterministic per-bar time+energy jitter so it reads like bins, gated on `playing && !REDUCE` — never random, never a fake signal, driven by `sm` via the playback clock.
- **Per-bar ballistics** attack ~45 ms / decay ~160 ms + **peak-hold caps** that hover and fall slowly; bars flat when paused / no analysis (and `sm` is already 0 under `prefers-reduced-motion`, so reduced-motion "no animation" falls out).
- **Look:** accent→white gradient turning red-hot near the top, faint mirrored reflection under the baseline, peak-cap ticks; always dark, teal `#5EEAD4`.
- **Furniture:** room name, title/artist, a `.wave` seek bar (click-to-seek free via the existing `$$(".wave")` handler), transport prev/play/next.
- **Mode switching:** introduce a shared **`openMode(id, sizers)`** helper (closes the other full-screen layers, opens the target, `rAF`-sizes its canvases); `openNp`/`openAmp`/`openSpec` and the `F`/`A`/`S`/`Esc` keys delegate to it. Full header mesh (NP↔Amp↔Spec buttons on each), a dock `specBtn`, `applyStatus` drives `#specSpeaker/#specTitle/#specArtist/#specPlay`, `drawSpec` hooked into `tick`, `resize` re-sizes its canvases. Desktop-only (`@media max-width:820px` hides `#spec` + `specBtn`).

The `openMode` refactor also folds `openNp`/`openAmp` onto the shared helper (still byte-compatible in behavior; this lands in Deliverable 2, after the byte-identical refactor is proven).

### 5. CLAUDE.md

Amend rule #2: the served `player_ui.html` is **assembled by the engine/`build.py` from `ui/` partials** (one triplet per full-screen mode + shared `core`); never hand-edit a built `player_ui.html` — edit the partials. It is still ONE self-contained offline document to the browser, still overridable by dropping your own `player_ui.html`. Keep all the existing rules (inline, no CDNs/fonts/storage, offline).

## Testing

- **Refactor gate:** `assemble_ui(ui/)` byte-identical to the pre-refactor `player_ui.html` (one-time `diff`).
- **Durable:** the marker/sentinel assembly test + the `_load_ui` serve/override test in `tests/test_draai.py`; full suite green (no ffmpeg/browser needed for these).
- **`.pyz`:** `python3 build.py` → the `.pyz` serves the baked assembled UI (`/` returns the full document).
- **Browser-pane (desktop):** the assembled UI works identically to before (spot-check main UI + NP + Amp); then Spectrum verified in DEMO — bars with the FFT shape + ballistics + peak caps, flat when paused, red-hot near top, mode switching + `F`/`A`/`S`/`Esc`, desktop-only. No real playback.

## Risks

- **Carve precision:** byte-identical requires exact boundaries — mechanical but fiddly; the `diff` gate catches any error immediately, so it's low-risk in practice.
- **Engine scope:** `_load_ui` gains ~15 lines of stdlib assembly (light UI templating in the engine). Kept minimal and in its own `draai/ui.py`.
- **`ui/` location detection** across run-from-repo vs `.pyz` vs an odd cwd: resolved by a package-relative path (step 2) with the packaged-file fallback (step 3); specify and test both.
- **Two-phase risk:** do the modularization (byte-identical, zero behavior change) as its own phase and prove it before Spectrum, so any Spectrum issue can't be confused with a refactor regression.
