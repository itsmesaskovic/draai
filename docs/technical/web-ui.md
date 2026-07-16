# Web UI (player_ui.html)

> Single self-contained HTML file that is the "HALCYON"-level DRAAI interface: inline CSS/JS, no build step, no CDNs, no accounts, works offline against the engine's HTTP API.

## Purpose

`player_ui.html` is the full-featured player interface: library browsing (albums/songs/queue/playlists), room control and grouping, EQ, sleep timer, YouTube import status, drag-reorder queue, multi-select, folder/artist grouping, a fullscreen "now playing" view with an album-driven color wash and an optional vinyl-deck animation, and OS media-key integration. It talks to the engine only through `fetch()` calls to `/api/*` (see `draai/server.py`). It has zero build tooling — it is edited and shipped as one 1,462-line HTML file (`wc -l` at time of writing; ~104 KB).

## Where it lives

- Source of truth: `/Users/sasa/Dev/draai/player_ui.html` (repo root).
- Loaded by the engine in this precedence order (`draai/server.py:27-42`, `_load_ui()`):
  1. `player_ui.html` next to the current working directory (`os.getcwd()`) — lets you edit-and-refresh against a running engine.
  2. The copy embedded in the `draai` package via `importlib.resources` (`draai/server.py:38-40`) — this is how it survives being shipped inside `draai.pyz`.
  3. The built-in `PAGE` string fallback (`draai/server.py:755`, ~528 lines) if neither file is found.
- The built-in `PAGE` fallback is intentionally minimal — it has no grouping, no album-palette pipeline, no vinyl deck, no media-key integration (verified: zero occurrences of `vinylStage`/`GROUPKEYS`/`paletteFromImage`/`mediaSession` in `PAGE`). Per `CLAUDE.md`, new HALCYON-level features belong only in `player_ui.html`, never backported to `PAGE`.
- Design history: `docs/superpowers/specs/2026-07-15-draai-fixed-accent-theming-rows-design.md` — the spec that produced the current fixed-accent + token theming + redesigned rows. It also documents *why* certain CLAUDE.md notes are now stale (see Gotchas).

## Key concepts

### Single-file rules (enforced by inspection, not tooling)

- All CSS is in one `<style>` block (`player_ui.html:6-...`), all JS in one `<script>` block starting after the markup (script tag opens before `player_ui.html:600`).
- No external resources: no `<link>` to fonts/CDNs, no `cdn.`/`googleapis`/`fonts.`/`jsdelivr`/`unpkg` references anywhere in the file (grepped, zero hits). Font stack is `system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif` (`player_ui.html:18`).
- No `localStorage`/`sessionStorage` anywhere in the file (grepped, zero hits). All UI preferences persist through the engine:
  - `GET /api/prefs` returns `config["ui"]` verbatim (`draai/server.py:470-471`).
  - `POST /api/prefs` merges the posted keys into `config["ui"]` (a `null` value deletes the key) and calls `save_config()` (`draai/server.py:603-611`).
  - Client side: `loadPrefs()`/`savePrefs()` (`player_ui.html:1411-1425`) read/write `theme`, `group`, `sort`, `dir`, `collapsed` (the set of collapsed group keys) with a 400ms debounce on save.

### Token-driven theming: fixed chrome accent vs. per-track NP accent

Two separate accent systems exist, and they are **not** the same variable:

- `--ac` / `--ac2` (`player_ui.html:9`) — the **fixed** chrome accent, `rgb(94 234 212)` = `#5EEAD4`, with `--ac2: rgb(45 212 191)` as a fixed secondary. Comment on that line spells it out: "fixed accent (#5EEAD4) + fixed secondary — NOT album-driven". These are declared once in `:root` and JS never writes them again (confirmed: no `setProperty("--ac"` anywhere in the file). Identical in light and dark themes — `--ac`/`--ac2` are absent from the light-theme override block (`player_ui.html:231-237`). Everything in the app chrome — playing-row highlight, active room/nav, sliders, toggles, links, focus glow, EQ marks, badges, the dock play button — reads `rgb(var(--ac))`.
- `--np-ac` / `--np-ac2` / `--np-bg1` / `--np-bg2` (`player_ui.html:10`) — the **album-driven** accent, used only inside the fullscreen now-playing view (`.np …` selectors, e.g. `player_ui.html:222-227,282,287,300,307`). These are the only variables the palette pipeline below ever touches.

### The album-palette pipeline (fullscreen NP only)

Flow: `getArt()` (`player_ui.html:679-687`) loads the track art image, then:

1. `paletteFromImage(img)` (`player_ui.html:654-673`) samples a 26×26 downscale of the artwork into color buckets, scores by saturation × frequency, and derives `{ac, ac2, bg1, bg2}`. Falls back to `paletteFromHash(id)` (`player_ui.html:674-675`, a deterministic HSL hash) on load error or if no usable colors are found.
2. `setPaletteTarget(p)` (`player_ui.html:644-646`) runs `readableAccent()` (`player_ui.html:636-643`) on `ac`/`ac2`: it lifts each color toward white until its relative luminance reaches a **floor of 0.55**, preserving hue, so album colors never end up too dark to read on the dark NP background. The result becomes `palT` (the lerp target).
3. Every animation frame, `tick(t)` (`player_ui.html:707-725`) exponentially lerps the live `pal` object toward `palT` (~0.42s time constant, "cinematic cross-fade") and calls `applyPal()`.
4. `applyPal()` (`player_ui.html:648-653`) writes the four `--np-*` CSS custom properties on `document.documentElement`. Nothing else is written — in particular `--ac`/`--ac2` are never touched here.
5. `adaptForTheme(c, isAccent)` (`player_ui.html:647`) is a **pass-through**: `return c;`. It used to re-tone the (then-global) album accent for light mode; since the chrome accent is now fixed and the NP accent is NP-only, there is nothing left to adapt. See Gotchas for why this contradicts an older note in `CLAUDE.md`.
6. The fullscreen `#np` element has a hardcoded dark background (`background:rgb(7 7 10)`, `player_ui.html:218`) regardless of `data-theme`, and re-declares neutral tokens (`--tx`, `--dim`, `--line`, `--play-ink`, etc.) scoped to `.np` in the light-theme block (`player_ui.html:241`) so its text/panels stay legible against that dark backdrop even when the rest of the app is light. Its accent still comes from `--np-ac`.

### Light / dark theming

- Theme state: `A.theme` is `"auto" | "dark" | "light"` (`player_ui.html:608`). `effectiveTheme()` resolves `"auto"` via `matchMedia("(prefers-color-scheme: dark)")` (`player_ui.html:1393-1394`).
- `applyTheme()` (`player_ui.html:1395-1404`) sets `data-theme` on `<html>` — **not** a `body.light` class. (The design spec at `docs/superpowers/specs/2026-07-15-draai-fixed-accent-theming-rows-design.md` describes the mechanism as `body.light` in prose, but the shipped selector is `html[data-theme=light]`, e.g. `player_ui.html:231,238,239,241`. Functionally equivalent, just a different selector than the spec text.)
- The light override block (`player_ui.html:231-241`) re-points neutral tokens only (`--tx --dim --dim2 --line --line2 --panel --panel2 --page --elev --elev2 --scrim --wave-dim --track-empty --play-ink`) — never `--ac`/`--ac2`/`--np-*`.
- `#bgwrap` (the blurred ambient orbs, `player_ui.html:36-42`) is **not** hidden in light theme — it stays visible at low opacity: `html[data-theme=light] #bgwrap b{opacity:.24}` and the second orb's gradient switches to use the fixed `--ac` at low alpha (`player_ui.html:238-239`). See Gotchas — this supersedes the older "hide `#bgwrap` in light" guidance.
- `readThemeColors()` (`player_ui.html:1390-1392`) re-reads `--wave-dim`/`--tx` into module vars `WAVE_DIM`/`PLAYHEAD` on every theme change so the dock waveform's unplayed bars and playhead stay visible in both themes.

### Class-collision hazards — grep before adding a class

- `.spin` (loading spinner, `player_ui.html:382`) and `.disc` were already taken by the base design, so the vinyl-deck record uses `.vdisc`/`.vspin` instead (`player_ui.html:278-284,1438`). `.disc` as an id (`#disc`) is reused for the vinyl element itself (`player_ui.html:521`) — id and class namespaces don't collide, but it's easy to confuse with the unrelated `.spin` class.
- `.np` is the **fullscreen** now-playing view (`<section class="np" id="np">`, `player_ui.html:511`; toggled via `#np.open`). `.rnp` is a completely different thing: the small "now playing" overlay drawn inside a list row's thumbnail (`.rthumb .rnp`, `player_ui.html:149`, an equalizer-bars icon shown instead of the track index when that row is the currently playing track, e.g. `player_ui.html:905`). Do not confuse `.np`/`#np` (fullscreen) with `.rnp` (row overlay) — the names look related but are unrelated pieces of UI.

### Fullscreen now-playing view

- `#np` starts off-screen (`transform:translateY(101%)`) and slides in on `.open` (`player_ui.html:217-219`). Opened via the expand button (`$("#expandBtn")`, `player_ui.html:1051`) which adds `.open`, sizes its canvas, arms an idle timer, and loads the QR code for remote-queueing; closed via `#npClose` or Escape (`player_ui.html:1052,1081`).
- Layout: `.nphead` (top bar: "PLAYING IN <room>", vinyl-toggle button, close button, `player_ui.html:513-517`) → `.npbody` (two-column grid: `.artstage` with album art or vinyl deck, and `.npmeta` with title/artist/seek/transport/volume, `player_ui.html:518-528`) → `.npfoot` (QR "scan to be the DJ" box + clock, `player_ui.html:551-557`).
- **Vinyl deck**: `#vinylBtn` (a small icon button in `.nphead`, `player_ui.html:515`) toggles `VINYL` and calls `applyVinyl()` (`player_ui.html:1429-1433`), which shows `#vinylStage` and hides `#npArtWrap` (or vice versa) inside the same `.artstage` panel — it replaces the album-art image, it is not a separate icon "at the top" of the view. A `setInterval` (`player_ui.html:1434-1439`) rotates the tonearm proportional to playback progress and toggles `.vspin` on `#disc` while playing (skipped under `prefers-reduced-motion`, `player_ui.html:299`).

### Media keys

- A silent, looping, near-zero-volume `<audio>` element (`SILENT_WAV`, a data-URI, `player_ui.html:1442`) is created by `armMediaKeys()` (`player_ui.html:1444-1456`) and kept playing so macOS/the browser treats the page as an active media session, which unlocks `navigator.mediaSession` action handlers (play/pause/prev/next/seekto → `cmd(...)`, `player_ui.html:1450-1454`).
- `armMediaKeys` is wired to fire once on the first `pointerdown` anywhere in the document (`player_ui.html:1457`, `{once:true}`) — browsers require a user gesture before they'll let a page autoplay audio, so it can't run at page load.

### Songs & queue rows

- `trackRow(t, idx)` (`player_ui.html:899-913`) renders one row: a 46×46 `.rthumb` (album-gradient background + lazy-loaded art image + an overlay that is either the track index, a selection ball, or the `.rnp` playing-indicator), a `.tt` title/artist block, and `.rright` with a right-aligned dim `.alb` album name plus `.acts` (a single icon button, "add to queue", with an Option-click hint for "play next"). The add-to-queue button is invisible until hover: `.acts{opacity:0;pointer-events:none}` → `.row:hover .acts{opacity:1;pointer-events:auto}` (`player_ui.html:156-157`; queue rows themselves keep `.acts` always visible, `player_ui.html:158`, since they show drag/remove controls instead).
- The currently-playing row gets `.row.playing`: a left inset bar plus a horizontal gradient wash, both keyed to the fixed `--ac` (`player_ui.html:144`, `box-shadow:inset 3px 0 0 rgb(var(--ac))` + `linear-gradient(90deg, rgb(var(--ac)/.15), rgb(var(--ac)/.02) 62%)`), and its title turns accent-colored (`player_ui.html:152`).
- Rows have hairline dividers (`border-bottom:1px solid var(--line)`, `player_ui.html:141`), and the whole list is centered with a max width via `.listwrap{max-width:1160px;margin:0 auto}` (`player_ui.html:139`).
- The queue view (`renderQueue`, `player_ui.html:928-943`) reuses the same row shell but swaps the actions for a drag handle (`.qgrab`, drag-to-reorder wired at `player_ui.html:1370-1382` via `pointermove`/`pointerup`, calling `/api/queue_move`) and a remove button.

### Collapsible folder/artist groups

- `renderSongRows(view)` (`player_ui.html:1248-1275`) only groups when `A.group !== "none"`; it buckets tracks by folder or by artist, builds a `.ghead` header per group (name, track count, play-all/queue-all/reveal-in-Finder buttons, a caret that rotates via `.ghead.collapsed .gcaret{transform:rotate(-90deg)}`, `player_ui.html:255`), and renders that group's `.rows` only if it is not collapsed.
- Collapse state is keyed by `"<group-mode>:<group-name>"` and stored in the `A.collapsed` `Set` (`player_ui.html:1263,1266`), persisted through `/api/prefs` on every toggle (`gc` click handler, `player_ui.html:1307`, calls `savePrefs()`). A "collapse all / expand all" button (`#collapseAllBtn`, `player_ui.html:1312`) operates over `window.GROUPKEYS`, the list of keys present in the current render.

## Gotchas

- **`CLAUDE.md`'s theming notes are partly stale relative to the shipped code** (both superseded intentionally per the design spec, not accidental drift):
  - "light mode hides `#bgwrap`" is no longer true — `#bgwrap` stays visible at reduced opacity in light theme (`player_ui.html:238-239`); see `docs/superpowers/specs/2026-07-15-draai-fixed-accent-theming-rows-design.md:155-160`, which explicitly says this "supersedes the old CLAUDE.md 'hide `#bgwrap` in light' note."
  - "in light theme accents get a luminance CAP of 0.45 ... via `adaptForTheme`" no longer applies — `adaptForTheme` is now a no-op passthrough (`player_ui.html:647`) because the chrome accent is fixed and the NP accent is NP-only and NP is always dark; only the 0.55 luminance **floor** in `readableAccent()` (`player_ui.html:636-643`) remains, and it applies in both themes (it's about NP legibility, not light/dark adaptation).
  - Recommend updating `CLAUDE.md`'s "Interface gotchas" section to match this file rather than the old design intent.
- The theming mechanism is `html[data-theme="light"]`, not `body.light` — the design spec's prose uses `body.light` as shorthand, but grepping the shipped file shows zero `body.light` occurrences; only `data-theme` is set (`player_ui.html:1397`) and selected on (`player_ui.html:231` etc).
- `.np`/`#np` (fullscreen) and `.rnp` (row now-playing icon) are easy to confuse by name; grep for the exact selector, not just "np", before touching either.
- `.vdisc`/`.vspin` exist specifically because `.spin`/`.disc` were already claimed by unrelated design elements — grep for a class name before introducing it anywhere in this file, collisions are a recurring failure mode here.
- The vinyl deck is a toggle inside the album-art panel (`.artstage`), not a persistent or separate view; `#vinylBtn` lives in the top bar (`.nphead`) but the effect is scoped to `.artstage`.
- Media keys cannot be armed before a user gesture — don't try to call `armMediaKeys()` at boot; it is intentionally deferred to the first `pointerdown`.
- Preference persistence is debounced 400ms client-side (`prefsTimer`, `player_ui.html:1411-1414`) and silently swallows API errors (`.catch(()=>{})`) — a failed prefs save is invisible to the user by design (non-critical data).
- `PAGE` (the engine's built-in fallback, `draai/server.py:755`) is a separate, much smaller markup/JS blob maintained independently — changes to `player_ui.html` do not propagate to it, and it should stay basic per `CLAUDE.md`.

## References

All line numbers are into `/Users/sasa/Dev/draai/player_ui.html` unless a different file is named.

- Fixed chrome accent tokens: `:9-10`
- `readableAccent` (0.55 luminance floor): `:636-643`
- `setPaletteTarget`: `:644-646`
- `adaptForTheme` (no-op passthrough): `:647`
- `applyPal`: `:648-653`
- `paletteFromImage`: `:654-673`
- `paletteFromHash`: `:674-675`
- `getArt` (art load → palette trigger): `:679-687`
- `tick` (per-frame palette lerp + energy + waveform): `:707-725`
- Light theme token overrides + `#bgwrap` light behavior + `.np` light re-declaration: `:231-241`
- `#np` / fullscreen showpiece CSS: `:216-229`
- `#np` markup: `:511-558`
- `applyTheme` / `effectiveTheme` / theme toggle click handler: `:1387-1410`
- `loadPrefs` / `savePrefs`: `:1411-1425`
- Vinyl deck CSS: `:276-299`; markup: `:520-526`; toggle logic: `:1427-1439`
- Media keys (`SILENT_WAV`, `armMediaKeys`, pointerdown arm): `:1442-1457`
- `.row` / `.rthumb` / `.acts` hover / `.row.playing` CSS: `:139-161`
- `trackRow`: `:899-913`
- `renderQueue` (queue row markup + drag handle + remove): `:928-943`
- Queue drag-reorder pointer handlers: `:1370-1382`
- `renderSongRows` (folder/artist grouping + collapse): `:1247-1275`
- Group collapse/expand click handlers: `:1307,1312`
- `.ghead` / caret rotation CSS: `:248-255`
- Class-collision note (`.vdisc`/`.vspin` vs `.spin`/`.disc`): `:276-284`, `:382`
- `.rnp` row now-playing overlay: `:149`, usage `:905`, `:937`

Engine-side references (`/Users/sasa/Dev/draai/draai/server.py`):

- `_load_ui()` (external file → package resource → `PAGE` fallback precedence): `:27-42`
- `GET /api/prefs`: `:470-471`
- `POST /api/prefs`: `:603-611`
- `PAGE` fallback UI: `:755` (~528 lines)

Design history: `/Users/sasa/Dev/draai/docs/superpowers/specs/2026-07-15-draai-fixed-accent-theming-rows-design.md` (Part 1: fixed accent + NP-only palette; Part 2: token-driven light/dark, including the `#bgwrap`-in-light and `adaptForTheme` decisions that superseded older `CLAUDE.md` notes).
