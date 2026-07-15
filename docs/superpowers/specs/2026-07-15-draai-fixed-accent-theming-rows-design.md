# DRAAI UI redesign — fixed accent, token-driven themes, new list rows

**Date:** 2026-07-15
**Scope:** `player_ui.html` only. Engine (`sonos_player.py`) is untouched.
**Reference:** Claude Design "HALCYON" export — screenshots (light songs,
dark songs, dark fullscreen NP) plus the decoded source of "HALCYON Player
update.html". The reference is a **design mockup on an older/simpler DRAAI
base**: it has none of the current file's v1.1 features (no grouping, sort,
multi-select, playlists, drag-reorder, group play-all, reveal) and it still
uses the **old blue `--ac:120 210 255` with the album-palette pipeline live**
— the teal in the screenshots is the album palette reading green art, not a
fixed accent. So the reference is the source of truth for **rows, tokens, and
theming mechanics**, NOT for the accent (that's this spec's own requirement)
and NOT a drop-in. Exact reference values are used below where noted.

## Goal

Three layered visual changes. **All existing functionality stays intact** —
folder grouping, sort, multi-select, YouTube import, rooms, EQ, sleep timer,
resume, media keys, playlists, drag-reorder. Nothing about the API, the
engine, or interaction logic changes; this is theming + row markup/CSS.

The **fullscreen Now Playing view (`#np.open`) stays visually identical** —
it keeps its per-album coloring. Only the *source* of that coloring is
decoupled from the rest of the app.

## Non-goals

- No engine changes, no new API endpoints.
- No changes to discovery, SOAP, queue semantics, analysis, or resume.
- No new dependencies, fonts, or storage. One file, offline, as today.
- No redesign of the fullscreen NP layout/animation.

---

## Part 1 — Fixed accent, decoupled from the playing track

### Today

`getArt()` → `paletteFromImage()` (or `paletteFromHash()` fallback) →
`setPaletteTarget()` sets `palT`; `tick()` lerps `pal` toward `palT` each
frame and calls `applyPal()`, which writes the **global** CSS variables
`--ac`, `--ac2`, `--bg1`, `--bg2`. Every accent in the app reads
`rgb(var(--ac))`, and the fullscreen NP view reads the same globals. So the
entire chrome re-themes per track.

### Change

1. **`--ac` becomes a fixed constant.** In `:root`:
   `--ac: 94 234 212;` (= `#5EEAD4` = `rgb(94 234 212)`). JS must **never**
   write `--ac` again. `--ac2` also becomes a fixed value in `:root` (used
   only by the small brand disc's conic gradient) — a fixed second shade,
   not per-track. Identical in both themes; `--ac`/`--ac2` are **not** in
   the light override set.

2. **The palette pipeline survives but re-points at NP-only variables.**
   Introduce `--np-ac`, `--np-ac2`, `--np-bg1`, `--np-bg2`. `applyPal()`
   writes these instead of the globals. `paletteFromImage`,
   `paletteFromHash`, `setPaletteTarget`, and the `tick()` lerp are kept
   as-is except for the variable names they target.

3. **Repoint every `.np …` selector** that currently reads `--ac`/`--ac2`/
   `--bg1`/`--bg2` to the `--np-*` equivalents. Known sites (verify by grep
   before editing — line numbers drift): the `.np .npbg .a` orbs, `.nphead
   .lbl b`, `.artstage`/`.artstage .halo`, `.npmeta .k`, `.fadetrack .rail`.
   Result: NP renders exactly as before.

4. **`adaptForTheme()` collapses to a passthrough.** Its purpose was to
   re-tone the album-driven accent/bg for light mode and to bail while NP is
   open. With a fixed chrome accent and NP-only palette vars, it no longer
   has anything to adapt. Replace its body with `return c;`. NP is kept dark
   in light theme by **token re-declaration on `.np`** (see Part 2), the
   reference's mechanism — cleaner than the old bail.

5. **Waveform (`drawWave`)** distinguishes dock vs NP by the canvas object
   (`o===npCv`), matching the reference:
   - **Dock (chrome):** played bars = fixed teal; unplayed bars = `WAVE_DIM`;
     playhead = `PLAYHEAD`. `WAVE_DIM`/`PLAYHEAD` are module vars read from
     the computed `--wave-dim` / `--tx` tokens by `readThemeColors()`, called
     on every theme switch. This fixes the bug where unplayed bars
     (`rgba(255,255,255,0.16)`) and the playhead (`#fff`) vanish on light.
   - **NP:** played = `--np-ac` (album); unplayed = `rgba(255,255,255,0.2)`,
     playhead = `#fff` — hardcoded light, correct because NP is always dark.

   Reference `readThemeColors()`:
   `const cs=getComputedStyle(document.body); WAVE_DIM=cs.getPropertyValue("--wave-dim").trim()||WAVE_DIM; PLAYHEAD=cs.getPropertyValue("--tx").trim()||PLAYHEAD;`
   Note the reference computes the dock played color from `pal.ac` (album);
   **we override it to fixed teal** for the dock (the accent decoupling).

### Acceptance

- Changing tracks never changes any chrome color.
- Every accent (playing row, active room/nav/tab, sliders, toggles, links,
  focus rings, glows, EQ marks, badges, Get button, dock play button) is
  `#5EEAD4`, identical in light and dark.
- Fullscreen NP looks pixel-identical to today (album-driven).
- Gradient album thumbnails still render as artwork; they no longer feed any
  global color.

---

## Part 2 — Token-driven light / dark

### Today

`A.theme` (`auto`/`dark`/`light`), `effectiveTheme()`, and `applyTheme()`
already exist (in-flight v1.1); `applyTheme()` sets `data-theme` on
`<html>`. But light mode is produced **only** by `adaptForTheme()` mutating
the album palette in JS — there is no token-based light stylesheet. Many
neutrals are hardcoded (`body{background:rgb(8 8 11)}`, slider track
`rgba(255,255,255,.14)`, play-button ink `#08080f`/`#0a0a0f`, waveform
whites, scrims).

### Change

1. **Add the reference's neutral/surface tokens to `:root`** (dark values =
   today's look), alongside the existing `--tx --dim --dim2 --line --line2
   --panel --panel2`. Use the reference names/values verbatim:
   ```
   --page:#08080b; --elev:rgb(11 11 14 / .68); --elev2:rgb(15 15 19 / .97);
   --scrim:rgb(8 8 11 / .55); --wave-dim:rgba(255,255,255,.16);
   --track-empty:rgba(255,255,255,.14); --play-ink:#08080b;
   ```
   Note: `--page` (base bg, not `--bg`); **two** elevations `--elev`/`--elev2`
   (dock/popover vs solid menus); `--track-empty` (slider groove) is
   **separate** from `--wave-dim` (waveform bars); `--play-ink` = ink on teal
   buttons.

2. **One `body.light` (equivalently `[data-theme="light"]`) block** overrides
   *only* those tokens — no second stylesheet. Reference light values verbatim:
   ```
   --tx:#20201d; --dim:#6b6b64; --dim2:#a2a299; --line:rgba(0,0,0,.09);
   --line2:rgba(0,0,0,.16); --panel:rgba(0,0,0,.035); --panel2:rgba(0,0,0,.07);
   --page:#f2f0ea; --elev:rgb(249 247 242 / .82); --elev2:rgb(251 249 245 / .98);
   --scrim:rgb(242 240 234 / .5); --wave-dim:rgba(0,0,0,.17);
   --track-empty:rgba(0,0,0,.14); --play-ink:#f2f0ea;
   ```
   `--page` is warm off-white `#f2f0ea`. `--ac`/`--ac2` are **not** in this
   set → teal identical across themes.

3. **NP stays cinematically dark in light theme via token re-declaration.**
   Scope the dark neutrals onto `.np` (reference):
   `.np{--tx:#f4f4f7;--dim:#9a9aa6;--dim2:#63636f;--line:rgba(255,255,255,.075);--line2:rgba(255,255,255,.15);--play-ink:#08080b}`.
   NP thus reads dark neutrals regardless of body theme; its accent comes
   from `--np-ac` (album, Part 1).

4. **Replace hardcoded neutrals** throughout the CSS with tokens: body
   background → `var(--page)` (add `transition:background .45s ease,color .45s`),
   slider groove → `--track-empty`, dock/popover/toast surfaces → `--elev`/
   `--elev2`, scrims → `--scrim`, play-button ink → `--play-ink`. Grep
   `255,255,255`, `rgb(8`, `#0` to find them. (Thumbnail number/eqmark scrim
   stays a dark overlay in both themes — it sits on colorful art; reference
   hardcodes `rgb(9 9 12 / .42)` / `rgb(8 8 11 / .55)`.)

5. **Ambient washes (`#bgwrap`) stay visible in light at low opacity**
   (reference, resolving prior judgment call #1):
   `body.light #bgwrap b{opacity:.24}` and
   `body.light #bgwrap .g1{background:radial-gradient(circle,rgb(var(--ac)/.2),transparent 68%)}`.
   Because the orbs are now fixed teal (not album-driven), this supersedes the
   old CLAUDE.md "hide `#bgwrap` in light" note. A soft teal wash, not muddy.

6. **Theme toggle:** the half-disc contrast icon (distinct from the sleep
   crescent), rotating to signal state, initialized from the OS. Reference:
   `THEME_IC='<svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3v18a9 9 0 0 0 0-18z" fill="currentColor" stroke="none"/></svg>'`
   with `transform:rotate(180deg)` on light, tooltip "Switch to dark/light".
   **We keep the current tri-state `A.theme` (auto/dark/light) + `/api/prefs`
   persistence** (CLAUDE.md lists theme as a persisted pref) rather than the
   reference's binary/no-persist toggle; `auto` follows `prefers-color-scheme`,
   satisfying "initialize from OS". Restyle the existing toggle with the
   reference icon/rotation; keep persistence and the auto state.

### Acceptance

- Toggling theme swaps neutrals via tokens only; `#5EEAD4` never moves.
- Dock waveform (unplayed bars + playhead) is visible in both themes and
  refreshes immediately on toggle.
- Light base is warm `#F2F0EA`; dark unchanged from today.
- Theme disc rotates to signal state; initializes from OS preference.

---

## Part 3 — Songs & Queue row redesign

Applies to `trackRow()` (so it flows through the `none`/`folder`/`artist`
grouped views and select mode) and the queue row template. Same visual
treatment in all.

### Row layout (reference-exact)

Three-column grid — album **and** action share the right cell (`.rright`):
```
.row{display:grid;grid-template-columns:46px minmax(0,1fr) auto;
  align-items:center;gap:14px;padding:7px 12px 7px 8px;border-radius:11px;
  transition:background .13s;cursor:pointer;position:relative;
  border-bottom:1px solid var(--line)}
.row:hover{background:var(--panel);border-bottom-color:transparent}
.row.playing{background:linear-gradient(90deg,rgb(var(--ac)/.15),rgb(var(--ac)/.02) 62%);
  box-shadow:inset 3px 0 0 rgb(var(--ac));border-bottom-color:transparent}
```
`trackRow()` markup: `.rthumb` (with inline `background:${gradFor(...)}`,
holding the `<img loading="lazy" onerror="this.remove()">` + a `.idx` number
or, when playing, `.np > .eqmark`), then `.tt` (title `<b>` + artist
`<small>`), then `.rright` (`.alb` + `.acts`).

- **Thumbnail (46px):** `.rthumb{width:46px;height:46px;border-radius:9px;
  overflow:hidden;position:relative;flex:none;box-shadow:inset 0 0 0 1px
  rgba(255,255,255,.07)}`. Number overlay
  `.rthumb .idx{position:absolute;inset:0;display:grid;place-items:center;
  font-size:12.5px;color:rgba(255,255,255,.82);background:rgb(9 9 12 /.42);
  transition:opacity .14s}` fades on hover (`.row:hover .rthumb .idx{opacity:0}`);
  playing shows `.rthumb .np` (eqmark) instead. Real art via
  `<img src="/api/art?id=<id>" onerror="this.remove()">` over the gradient;
  `has_art===false` skips the img.
- **Middle `.tt`:** title `<b>` (turns teal on `.row.playing`) over artist
  `<small>` in `--dim`.
- **Album `.alb`:** right-aligned, **`--dim2`**, `max-width:300px`, ellipsis.
- **Action `.acts`:** `width:40px;justify-content:flex-end;opacity:0;
  pointer-events:none;transition:.14s` → `.row:hover .acts{opacity:1;
  pointer-events:auto}`. Reserved width = **no layout shift**. Single ghost
  **add-to-queue** button (`data-enqueue`). **Play next is preserved** as
  **⌥/Alt-click** on it (tooltip "Add to queue · ⌥ Play next") — the
  reference dropped per-row Play next; we keep it (no functionality removed).
- **Hairline divider** `var(--line)`, hidden on hover and on playing.
- **Playing row:** accent gradient + left bar as in the `.row.playing` rule
  above (replaces today's flat `rgb(var(--ac)/.1)`).
- **Select mode:** the `.selball` checkbox takes the `.rthumb` leading slot;
  existing selection action bar unchanged. (Reference has no select mode —
  this integrates the new row with the current file's multi-select.)
- **Groups (folder/artist) + sort:** the new `trackRow` is used inside the
  existing group headers and sorted lists unchanged. **Queue rows** get the
  same `.rthumb`/`.tt`/`.rright` treatment but keep their intrinsic controls
  — drag handle (`.qgrab`) + remove — in `.acts` (reference dropped the drag
  handle; we keep drag-reorder).

### Width

`.listwrap{max-width:1160px;margin:0 auto}` wraps the **Songs and Queue**
lists (reference value confirmed). The **album wall stays full-bleed**. At
`max-width:820px` the grid tightens to `44px minmax(0,1fr) auto` and `.acts`
is always visible.

### Acceptance

- Rows have a leading thumbnail with number → art-on-hover → eqmark-when-
  playing, right-aligned dim album, one reserved hover action, hairline
  rhythm, and the accent-bar+gradient playing treatment — in flat, folder,
  and artist views and in select mode.
- No layout shift on hover.
- Play next and Add to queue both still reachable per row; drag-reorder and
  remove still work in the queue.
- List capped at 1160px centered; album wall full-bleed.

---

## Risks / watch-items

- **Do NOT inherit the reference's gaps.** The reference mockup lacks
  grouping, sort, multi-select, playlists, drag-reorder, group play-all,
  reveal, and per-row Play next. We port its *visuals* into the current file
  that HAS these — every one must still work after the row rework. `trackRow`
  and the queue renderer must keep their `selMode`/`selball`, group headers,
  `data-drag`/`qgrab`, and `data-next` (⌥) hooks.
- **Accent decoupling is ours, not the reference's.** The reference keeps the
  album palette driving the global `--ac`; we fix `--ac` and route the album
  palette to `--np-*`. Don't accidentally copy the reference's live-accent
  `applyPal`/`drawWave` (which write/read the global accent).
- **Class collisions:** terse class names with prior collisions (`.spin`/
  `.disc` vs `.vspin`/`.vdisc`). Grep before adding any class. New names from
  the reference: `.rthumb`, `.rright`, `.idx`, `.listwrap` — confirm none
  clash with the current file.
- **NP regressions:** verify `#np.open` still colors from the album after
  the `--np-*` rename — miss a selector and it goes teal.
- **Canvas theme refresh:** `readThemeColors()` must run on every theme
  toggle or the dock waveform stays stale (the original bug). Confirm live.
- **Demo + real engine:** verify against `localhost:8765` (real speakers,
  live file reload) and against `DEMO` mode (fake data path in `Demo.*`).

## Verification

- `python3 tests/test_draai.py` (engine unchanged, must stay green).
- Load `localhost:8765`, reload after edits; check: track change never
  recolors chrome; theme toggle flips neutrals + waveform; rows match the
  reference in both themes and in folder/artist/select views; fullscreen NP
  unchanged; queue drag/remove intact.
- Compare side-by-side with the three reference screenshots.
