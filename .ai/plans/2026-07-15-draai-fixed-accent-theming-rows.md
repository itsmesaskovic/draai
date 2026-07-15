# DRAAI Fixed-Accent / Token-Theming / Row-Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace DRAAI's per-track album coloring with one fixed accent (`#5EEAD4`), make light/dark fully token-driven, and adopt the HALCYON row design — without removing any existing functionality.

**Architecture:** All changes live in the single file `player_ui.html`. The album-palette pipeline is kept but re-pointed at NP-only CSS variables (`--np-*`) so the fullscreen Now Playing view stays album-colored while the chrome uses a constant accent. Neutrals/surfaces become CSS tokens with a `[data-theme="light"]` override; canvas (waveform) colors are read from those tokens and refreshed on theme switch. Rows are rebuilt to the reference's 3-column layout, integrated into the existing grouped/sorted/multi-select/queue renderers.

**Tech Stack:** Plain HTML/CSS/JS in one file, no build, no deps. Engine (`sonos_player.py`) is **not** touched. Design reference decoded at `/private/tmp/claude-501/-Users-sasa-Dev-draai/837cc28c-e5de-4ae3-8946-b9ac3fa68edb/scratchpad/halcyon_decoded.html` (re-derivable from `~/Downloads/HALCYON Player update.html`). Spec: `docs/superpowers/specs/2026-07-15-draai-fixed-accent-theming-rows-design.md`.

## Global Constraints

- `player_ui.html` stays **ONE file**; all CSS/JS inline; no CDNs, no web fonts, no localStorage/sessionStorage. Must work offline. (CLAUDE.md hard rule 2.)
- No engine changes, no new API endpoints, no new dependencies.
- Accent is the exact constant `--ac: 94 234 212` (= `#5EEAD4` = `rgb(94 234 212)`), identical in light and dark, never written by JS.
- Fullscreen NP (`#np.open`) must render **visually identical** to today (album-driven, always dark).
- Keep ALL functionality: folder grouping, sort, multi-select, playlists, drag-reorder, group play-all, Finder reveal, per-row Play next, EQ, rooms, sleep, YouTube import, resume, media keys.
- User-facing copy stays short/warm/English (CLAUDE.md rule 6). Errors are human sentences (rule 5).
- **Grep before adding any CSS class** — terse names with prior collisions (`.spin`/`.disc` vs `.vspin`/`.vdisc`). New names introduced: `.rthumb`, `.rright`, `.idx`, `.listwrap` — confirm none pre-exist.
- **Commits are the maintainer's** (per CLAUDE.md). Each task ends at a *verified checkpoint*; do not commit — leave the working tree staged/clean for the maintainer to commit.
- **Verification is visual + regression**, not unit tests (no JS test harness; engine untouched). Every task verifies by reloading `http://localhost:8765/` (a live DRAAI engine serving this file against real speakers, re-reads the file each request) in the relevant theme(s)/view(s), plus keeping `python3 tests/test_draai.py` green.

---

### Task 1: Fix the accent and decouple the album palette to NP-only variables

**Files:**
- Modify: `player_ui.html` — `:root` (lines ~7–15), `applyPal()` (~634–640), `adaptForTheme()` (~628–633), all `.np`-scoped CSS rules that read `--ac`/`--ac2`/`--bg1`/`--bg2`.

**Interfaces:**
- Produces: global `--ac`/`--ac2` are now fixed; NP reads `--np-ac`/`--np-ac2`/`--np-bg1`/`--np-bg2`. The JS `pal` object still holds album RGB (used by `drawWave` NP branch in Task 3).

- [ ] **Step 1: Fix the accent in `:root`.** In `player_ui.html` line ~9, replace:

```
  --ac:120 210 255; --ac2:180 150 255;  /* accent + secondary */
```
with:
```
  --ac:94 234 212; --ac2:45 212 191;  /* fixed accent (#5EEAD4) + fixed secondary — NOT album-driven */
```
(Leave `--bg1`/`--bg2` on line 8 as-is; they become static and are no longer written by JS.)

- [ ] **Step 2: Re-point `applyPal()` to the NP variables.** Replace the body of `applyPal()` (lines ~634–640) with:

```js
function applyPal(){ const rs=document.documentElement.style;
  rs.setProperty("--np-ac",`${pal.ac[0]|0} ${pal.ac[1]|0} ${pal.ac[2]|0}`);
  rs.setProperty("--np-ac2",`${pal.ac2[0]|0} ${pal.ac2[1]|0} ${pal.ac2[2]|0}`);
  rs.setProperty("--np-bg1",`${pal.bg1[0]|0} ${pal.bg1[1]|0} ${pal.bg1[2]|0}`);
  rs.setProperty("--np-bg2",`${pal.bg2[0]|0} ${pal.bg2[1]|0} ${pal.bg2[2]|0}`);
}
```
(Removes the `adaptForTheme` calls and stops writing the global `--ac`/`--ac2`/`--bg1`/`--bg2`.)

- [ ] **Step 3: Collapse `adaptForTheme()` to a passthrough.** Replace the whole function (lines ~628–633) with:

```js
function adaptForTheme(c,isAccent){ return c; }
```
(Kept as a no-op so any stray caller still works; safe to inline-remove later.)

- [ ] **Step 4: Add NP variable defaults to `:root`.** So NP has sane values before the first track loads, add to the `:root` block (after the `--ac` line):

```
  --np-ac:94 234 212; --np-ac2:45 212 191; --np-bg1:16 16 22; --np-bg2:22 18 30;
```

- [ ] **Step 5: Re-point NP CSS to `--np-*`.** Find every CSS rule whose selector begins with `.np` (the fullscreen view — NOT `.rthumb .np`, NOT `#bgwrap`) that references `var(--ac)`, `var(--ac2)`, `var(--bg1)`, or `var(--bg2)`, and swap to the `--np-` prefixed name. Locate them:

Run: `grep -nE '^\.np|^\.nphead|^\.artstage|^\.npmeta|^\.fadetrack|^\.npbg' player_ui.html`
Known sites to convert (verify each line's content first): the `.np .npbg .a` orbs (~207), `.nphead .lbl b` (~212), `.artstage` / `.artstage .halo` (~263, ~281), `.npmeta .k` (~288), `.fadetrack .rail` (~322). Example — line ~207:
```
.np .npbg .a{...background:radial-gradient(circle,rgb(var(--ac) / .5),transparent 66%);...}
```
becomes `rgb(var(--np-ac) / .5)`. Do the same for every `--ac`/`--ac2`/`--bg1`/`--bg2` inside `.np`-scoped rules.

- [ ] **Step 6: Verify — accent is fixed, NP still album-colored.**

Run: `python3 tests/test_draai.py` — Expected: `Ran 15 tests ... OK` (engine unaffected).
Reload `http://localhost:8765/`. Confirm:
- Rooms/nav/sliders/playing-row are teal `#5EEAD4`.
- Play a track, then skip to a **different album** → the chrome accent does **not** change.
- Open fullscreen NP (expand the dock) → the art, halo, "NOW PLAYING" label, and NP waveform are still colored by the album (not forced teal). Skipping albums still re-tints NP.

- [ ] **Step 7: Checkpoint.** Leave changes staged for the maintainer to commit. Do not commit.

---

### Task 2: Token-drive all neutrals/surfaces and the light theme

**Files:**
- Modify: `player_ui.html` — `:root` (~7–15), `body` (~18), `#bgwrap::after` (~38), `.btn.acc` (~92), `.cov .ply` (~126), `.dock` (~167), `.play` (~185), slider (~197), `.picker` (~246) and `.pop`, `.chipbtn.on` (~339), `.gopt.on .chk` (~349), the `html[data-theme=light]` block (~216–221).

**Interfaces:**
- Produces: tokens `--page --elev --elev2 --scrim --wave-dim --track-empty --play-ink` in both themes. Consumed by Task 3 (`--wave-dim`, `--tx` for canvas).

- [ ] **Step 1: Add surface tokens to `:root`.** Append to the `:root` block (dark values, reference-exact):

```
  --page:#08080b; --elev:rgb(11 11 14 / .68); --elev2:rgb(15 15 19 / .97);
  --scrim:rgb(8 8 11 / .55); --wave-dim:rgba(255,255,255,.16);
  --track-empty:rgba(255,255,255,.14); --play-ink:#08080b;
```

- [ ] **Step 2: Point the base page background at `--page` with a theme transition.** Replace line ~18:
```
body{background:rgb(8 8 11);color:var(--tx);overflow:hidden;...}
```
so `background:rgb(8 8 11)` becomes `background:var(--page)` and add `transition:background .45s ease,color .45s ease` to the same rule.

- [ ] **Step 3: Tokenize chrome surfaces.**
  - `.dock` (~167): `background:rgb(12 12 16 / .72)` → `background:var(--elev)`.
  - `.picker` (~246): `background:rgb(16 16 21 / .97)` → `background:var(--elev2)`.
  - `.pop` rule (find with `grep -nE '^\.pop\b|^\.pop\{' player_ui.html`): its background → `var(--elev2)`.
  - `#bgwrap::after` (~38): `background:rgb(8 8 11 / .55)` → `background:var(--scrim)`.
  - Slider track (~197): `rgba(255,255,255,.14)` → `var(--track-empty)`.

- [ ] **Step 4: Tokenize accent ink.** Replace the ink color on accent/play surfaces with `var(--play-ink)`:
  - `.btn.acc` (~92) `color:#08080f` → `color:var(--play-ink)`.
  - `.cov .ply` (~126) `color:#08080f` → `color:var(--play-ink)`.
  - The `.row.playing` / badge site at ~163 (`#08080f`) → `var(--play-ink)`.
  - `.play` (~185) `color:#08080b` → `color:var(--play-ink)` (its bg is `var(--tx)`, so ink MUST flip with theme — this is the key one).
  - `.chipbtn.on` (~339) `color:#08080f` → `var(--play-ink)`.
  - `.gopt.on .chk` (~349) `color:#08080f` → `var(--play-ink)`.
  - The brand badge `#0a0a0f` (~54) → `var(--play-ink)`.
  - **Do NOT** change the QR path fill `#08080f` (~960) — QR modules stay dark in both themes.

- [ ] **Step 5: Rewrite the `html[data-theme=light]` block** (~216–221) to reference-exact tokens + surfaces, and show the ambient wash instead of hiding it:

```css
html[data-theme=light]{
  --tx:#20201d; --dim:#6b6b64; --dim2:#a2a299; --line:rgba(0,0,0,.09); --line2:rgba(0,0,0,.16);
  --panel:rgba(0,0,0,.035); --panel2:rgba(0,0,0,.07);
  --page:#f2f0ea; --elev:rgb(249 247 242 / .82); --elev2:rgb(251 249 245 / .98);
  --scrim:rgb(242 240 234 / .5); --wave-dim:rgba(0,0,0,.17);
  --track-empty:rgba(0,0,0,.14); --play-ink:#f2f0ea;
}
html[data-theme=light] #bgwrap b{opacity:.24}
html[data-theme=light] #bgwrap .g1{background:radial-gradient(circle,rgb(var(--ac) / .2),transparent 68%)}
html[data-theme=light] ::-webkit-scrollbar-thumb{background:rgba(0,0,0,.12);background-clip:content-box}
html[data-theme=light] .np{--tx:#f4f4f7;--dim:#9a9aa6;--dim2:#63636f;--line:rgba(255,255,255,.075);--line2:rgba(255,255,255,.15);--panel:rgba(255,255,255,.03);--panel2:rgba(255,255,255,.06);--play-ink:#08080b}
```
This removes the old `body{background:linear-gradient(...)}`, `.dock{...}`, `.pop,.picker{...}` overrides (now handled by tokens) and the `#bgwrap{display:none}` (now a soft teal wash — Decision D).

- [ ] **Step 6: Verify — neutrals flip, teal constant, NP dark, ambient wash faint.**

Reload `http://localhost:8765/`. Cycle the theme button through auto → dark → light. Confirm:
- Light base is warm off-white `#f2f0ea`; dark unchanged.
- Teal accent identical in both themes.
- Dock, popovers, and toasts are readable in light (elev tokens).
- The ambient background orbs are a **faint teal wash** in light (not hidden, not muddy).
- Open NP in light theme → NP stays dark.
- `python3 tests/test_draai.py` still `OK`.

- [ ] **Step 7: Checkpoint.** Leave staged for the maintainer.

---

### Task 3: Restyle the theme toggle and make the waveform theme-aware

**Files:**
- Modify: `player_ui.html` — near the theme JS (`applyTheme` ~1346, `#themeBtn` handler), `drawWave()` (~717–731), and add module-level constants/vars.

**Interfaces:**
- Consumes: `--wave-dim`, `--tx` tokens (Task 2); fixed `--ac` (Task 1); album `pal.ac` (Task 1).
- Produces: `WAVE_DIM`, `PLAYHEAD` module vars; `readThemeColors()`; `THEME_IC`. `applyTheme()` now also refreshes canvas colors + toggle icon.

- [ ] **Step 1: Add the icon constant + canvas color vars + refresh fn.** Just above the theme JS (before `const darkMq=...`), insert:

```js
const THEME_IC='<svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3v18a9 9 0 0 0 0-18z" fill="currentColor" stroke="none"/></svg>';
let WAVE_DIM="rgba(255,255,255,.16)", PLAYHEAD="#f4f4f7";
function readThemeColors(){ const cs=getComputedStyle(document.body);
  WAVE_DIM=(cs.getPropertyValue("--wave-dim")||"").trim()||WAVE_DIM;
  PLAYHEAD=(cs.getPropertyValue("--tx")||"").trim()||PLAYHEAD; }
```

- [ ] **Step 2: Update `applyTheme()`** (~1346–1350) to set the disc icon, rotate by resolved theme, and refresh canvas colors. Replace it with:

```js
function applyTheme(){
  const eff=effectiveTheme();
  document.documentElement.setAttribute("data-theme",eff);
  const b=$("#themeBtn");
  b.innerHTML=THEME_IC;
  b.style.transform=eff==="light"?"rotate(180deg)":"none";
  b.title="Theme: "+A.theme;
  b.classList.toggle("on",A.theme!=="auto");
  readThemeColors();
}
```
(`.cbtn.on` already colors the button teal; keeps the tri-state signal. `readThemeColors()` runs on every theme change and at boot via `loadPrefs()`→`applyTheme()`.)

- [ ] **Step 3: Make `drawWave()` theme-aware** — dock played = fixed teal, NP played = album; dock unplayed/playhead read tokens, NP stays light. Replace lines ~719, ~724, ~730:

Line ~719 — replace `const acc=...pal.ac...` with a per-canvas played color:
```js
  const acc=(o===npCv)?`rgb(${pal.ac[0]|0} ${pal.ac[1]|0} ${pal.ac[2]|0})`:"rgb(94 234 212)";
```
Line ~724 — unplayed bar:
```js
    const x=i*bw; ctx.fillStyle=played?acc:(o===npCv?"rgba(255,255,255,0.2)":WAVE_DIM);
```
Line ~730 — playhead:
```js
  const px=prog*W; ctx.fillStyle=(o===npCv)?"#fff":PLAYHEAD; ctx.fillRect(px-1,0,2,H);
```

- [ ] **Step 4: Verify — waveform visible in both themes, disc rotates.**

Reload `http://localhost:8765/`. Confirm:
- Dock waveform: played bars are teal; unplayed bars and playhead are **visible in light** (dark) and in dark (light) — and update the instant you toggle theme (no reload).
- Fullscreen NP waveform: played bars still album-colored, playhead white, on dark.
- Theme disc rotates 180° in light, upright in dark; teal-tinted when not on auto.
- `python3 tests/test_draai.py` still `OK`.

- [ ] **Step 5: Checkpoint.** Leave staged for the maintainer.

---

### Task 4: Rebuild the Songs rows (thumbnail + 3-col layout + ⌥ Play-next + select mode)

**Files:**
- Modify: `player_ui.html` — row CSS (`.rows` ~134, `.row` ~135–146, `.row.playing` ~137, `.row.sel` ~223–225, mobile `.row` ~377), `trackRow()` (~), the song list container render, and the click handler (`[data-enqueue]` at ~977). Add `.rthumb`/`.idx`/`.rright`/`.alb`/`.listwrap` CSS.

**Interfaces:**
- Consumes: `gradFor(key)` (existing gradient helper), `/api/art?id=`, `A.sel`/`A.selMode`, `A.curId`.
- Produces: new `trackRow(t,idx)` markup with `.rthumb`/`.tt`/`.rright`. Same signature — grouped/sorted renderers keep calling it unchanged.

- [ ] **Step 1: Replace the row CSS block.** Replace the current `.rows`/`.row`/`.row .num`/`.row .tt`/`.row .alb`/`.row .acts`/`.row.playing` rules (~134–146) and the `.row.sel` rules (~223–225) with:

```css
.listwrap{max-width:1160px;margin:0 auto}
.rows{display:flex;flex-direction:column}
.row{display:grid;grid-template-columns:46px minmax(0,1fr) auto;align-items:center;gap:14px;padding:7px 12px 7px 8px;border-radius:11px;transition:background .13s;cursor:pointer;position:relative;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row:hover{background:var(--panel);border-bottom-color:transparent}
.row.playing{background:linear-gradient(90deg,rgb(var(--ac) / .15),rgb(var(--ac) / .02) 62%);box-shadow:inset 3px 0 0 rgb(var(--ac));border-bottom-color:transparent}
.rthumb{width:46px;height:46px;border-radius:9px;overflow:hidden;position:relative;flex:none;box-shadow:inset 0 0 0 1px rgba(255,255,255,.07)}
.rthumb img{width:100%;height:100%;object-fit:cover;display:block}
.rthumb .idx{position:absolute;inset:0;display:grid;place-items:center;font-size:12.5px;color:rgba(255,255,255,.82);background:rgb(9 9 12 / .42);transition:opacity .14s}
.row:hover .rthumb .idx{opacity:0}
.rthumb .np{position:absolute;inset:0;display:grid;place-items:center;background:rgb(8 8 11 / .55)}
.row .tt{min-width:0}
.row .tt b{font-size:14px;font-weight:520;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row.playing .tt b{color:rgb(var(--ac))}
.row .tt small{font-size:12px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
.rright{display:flex;align-items:center;gap:12px;justify-content:flex-end}
.rright .alb{font-size:12.5px;color:var(--dim2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:300px;text-align:right}
.row .acts{display:flex;gap:4px;width:40px;justify-content:flex-end;opacity:0;transition:.14s;pointer-events:none}
.row:hover .acts{opacity:1;pointer-events:auto}
.row.sel{background:rgb(var(--ac) / .10);box-shadow:inset 3px 0 0 rgb(var(--ac));border-bottom-color:transparent}
.rthumb .selball{position:absolute;inset:0;margin:auto;width:20px;height:20px;border-radius:50%;border:2px solid var(--line2);background:rgb(8 8 11 / .45)}
.row.sel .rthumb .selball{background:rgb(var(--ac));border-color:rgb(var(--ac))}
```

- [ ] **Step 2: Update the mobile override** (~377). Replace the `.row{grid-template-columns:...}` inside `@media(max-width:820px)` with:
```css
  .row{grid-template-columns:44px minmax(0,1fr) auto;gap:12px}
  .row .acts{opacity:1;pointer-events:auto}
```

- [ ] **Step 3: Rewrite `trackRow()`.** Replace the whole function with (keeps select mode + adds ⌥ Play-next hint; single visible ghost = add-to-queue):

```js
function trackRow(t,idx){ const playing=String(t.id)===String(A.curId);
  const sel=A.sel.has(String(t.id));
  const g=gradFor((t.album||"")+"§"+(t.artist||t.title));
  const url=DEMO?Demo.art(t.id):"/api/art?id="+enc(t.id);
  const overlay = (A.selMode||sel)
    ? `<span class="selball"></span>`
    : (playing ? `<div class="np"><span class="eqmark"><i></i><i></i><i></i></span></div>` : `<div class="idx">${idx}</div>`);
  const art = (t.has_art===false) ? "" : `<img loading="lazy" src="${url}" onerror="this.remove()" alt="">`;
  return `<div class="row ${playing?"playing":""}${sel?" sel":""}" data-play="${t.id}">
    <div class="rthumb" style="background:${g}">${art}${overlay}</div>
    <div class="tt"><b>${esc(t.title)}</b>${t.artist?`<small>${esc(t.artist)}</small>`:""}</div>
    <div class="rright">${t.album?`<span class="alb">${esc(t.album)}</span>`:""}
      <div class="acts"><button class="iconbtn" data-enqueue="${t.id}" title="Add to queue · ⌥ Play next"><svg class="ic" viewBox="0 0 24 24"><path d="M3 6h13M3 12h9M3 18h9M17 12v6M14 15h6"/></svg></button></div>
    </div></div>`;
}
```

- [ ] **Step 4: Preserve per-row Play next via ⌥-click.** In the click handler where `data-enqueue` is dispatched (~977, `const enq=t("[data-enqueue]")`), gate on the modifier. Find the block that calls the enqueue action for `enq` and change it so an Alt/Option click plays next instead. Example — replace the enqueue dispatch:
```js
    if(enq){ const id=enq.getAttribute("data-enqueue");
      if(e.altKey){ enqueue([id],true); toast("Playing next",true); }
      else { enqueue([id]); toast("Added to queue",true); }
      return; }
```
Use the **existing** enqueue call the current handler already uses (match its function name/signature — e.g. if the current code calls `api("/api/enqueue",{ids:[id],play_next:true})`, use that form for the ⌥ branch and the plain form otherwise). Do not invent a new endpoint; the `/api/enqueue` `play_next` flag already exists.

- [ ] **Step 5: Wrap the Songs list in `.listwrap`.** In the songs render path (the branch that emits `renderSongRows(view)` into the content container), wrap its output so the list is capped/centered. In the function that builds the songs view HTML, change the container that holds the `.listhead` + rows to include `class="listwrap"` (e.g. wrap the returned markup in `<div class="listwrap">…</div>`). The **album wall** render path must NOT get `.listwrap`.

- [ ] **Step 6: Verify — rows in every songs view + select mode.**

Reload `http://localhost:8765/`. Confirm across **flat, folder-grouped, and artist-grouped** Songs views and in **dark and light**:
- 46px gradient thumbnail with the index number; on hover the number fades to the art; the playing row shows the eq glyph.
- Title/artist stacked; album right-aligned and dim.
- Hover reveals one ghost add-to-queue button with **no layout shift**; hairline divider hides on hover and on the playing row.
- Playing row shows the teal left bar + soft gradient (not a flat block); title teal.
- Click a row plays; **⌥/Option-click** the ghost button plays next (toast "Playing next"); plain click adds to queue.
- Select mode: checkbox sits in the thumbnail slot; selection bar still works; multi-select play/next/add unaffected.
- List is centered, capped ~1160px; album wall still full-bleed.
- `python3 tests/test_draai.py` still `OK`.

- [ ] **Step 7: Checkpoint.** Leave staged for the maintainer.

---

### Task 5: Rebuild the Queue rows (same treatment, keep drag + remove) and cap width

**Files:**
- Modify: `player_ui.html` — `renderQueue()` row template (~904–907), and wrap the queue list in `.listwrap`.

**Interfaces:**
- Consumes: the `.rthumb`/`.tt`/`.rright`/`.acts` CSS from Task 4; `A.queue` items (`{no,title,artist,album,id,has_art}`), `A.status.track_no`, `gradFor`, `enc`, `DEMO`.
- Produces: queue rows visually matching songs while keeping `data-jump`, `.qgrab` drag (`data-drag`), and `data-remove`.

- [ ] **Step 1: Rewrite the queue row template.** In `renderQueue()` (~904–907), replace the per-item `A.queue.map(...)` row markup with:

```js
    ${A.queue.length?`<div class="rows listwrap" id="qRows">${A.queue.map(it=>{ const cur=it.no===A.status.track_no;
      const g=gradFor((it.album||"")+"§"+(it.artist||it.title));
      const url=DEMO?Demo.art(it.id):"/api/art?id="+enc(it.id);
      const art=(it.has_art===false)?"":`<img loading="lazy" src="${url}" onerror="this.remove()" alt="">`;
      const overlay=cur?`<div class="np"><span class="eqmark"><i></i><i></i><i></i></span></div>`:`<div class="idx">${it.no}</div>`;
      return `<div class="row ${cur?"playing":""}" data-jump="${it.no}">
        <div class="rthumb" style="background:${g}">${art}${overlay}</div>
        <div class="tt"><b>${esc(it.title||"")}</b>${it.artist?`<small>${esc(it.artist)}</small>`:""}</div>
        <div class="rright">${it.album?`<span class="alb">${esc(it.album)}</span>`:""}
          <div class="acts"><span class="qgrab" data-drag="${it.no}" title="Drag to reorder">≡</span><button class="iconbtn" data-remove="${it.no}" title="Remove"><svg class="ic" viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg></button></div>
        </div></div>`; }).join("")}</div>`
```
Note: the `.acts` reserved width is 40px; the queue's two controls (grab + remove) fit — if they feel cramped, widen `.acts` to `width:56px` **only within the queue** via a scoped rule `#qRows .row .acts{width:56px}` (add if needed during verify).

- [ ] **Step 2: Verify — queue rows styled, drag + remove intact.**

Reload `http://localhost:8765/`, open the Queue tab. Confirm in both themes:
- Rows match the Songs design (thumbnail, title/artist, right-aligned album).
- The currently-playing queue item shows the teal bar + eq glyph.
- Drag handle reorders tracks (test moving a track **down** and **up** — the `ReorderTracksInQueue` semantics are engine-side and unchanged; this only restyles the row). Remove button removes. Clicking a row jumps to it.
- Queue list centered/capped ~1160px.
- `python3 tests/test_draai.py` still `OK`.

> Real-hardware note: queue reorder/remove should be confirmed against the live speakers at `localhost:8765` (the maintainer's Eetkamer/Woonkamer), per CLAUDE.md ("real-hardware checks matter for queue reorder").

- [ ] **Step 3: Checkpoint.** Leave staged for the maintainer.

---

### Task 6: Full regression + reference parity pass

**Files:** none (verification only).

- [ ] **Step 1: Engine tests.** Run: `python3 tests/test_draai.py` — Expected: `Ran 15 tests ... OK`.

- [ ] **Step 2: Functionality sweep** at `http://localhost:8765/` (real engine). Confirm none regressed: folder/artist grouping, sort (title/artist/date, direction), multi-select (Cmd/Shift-click + Select button) with the selection bar, playlists save/load/delete, queue drag-reorder + remove + jump, group Play-all / Add-all, Finder reveal (folder groups), EQ, room grouping/volume, sleep timer, YouTube import (badge/flow), resume, media keys.

- [ ] **Step 3: Theme + NP sweep.** Toggle auto/dark/light: neutrals flip, teal constant, dock waveform visible + live-refreshes, ambient wash faint teal in light. Fullscreen NP unchanged (album-colored, dark) in both themes; skipping albums re-tints NP only.

- [ ] **Step 4: Demo-mode check.** Stop the engine or open the file where `/api/state` fails so `DEMO=true`; confirm the redesigned rows, theming, and NP all render on fake data (`Demo.*`) with the demo badge.

- [ ] **Step 5: Reference parity.** Side-by-side with the three HALCYON screenshots (light songs, dark songs, dark NP) and `halcyon_decoded.html`: rows, spacing, playing-row treatment, thumbnail behavior, and 1160 cap match (allowing for the intentional divergences: fixed teal vs album accent, preserved v1.1 controls).

- [ ] **Step 6: Final checkpoint.** Summarize what was verified; leave the tree staged for the maintainer to commit and (per release process) tag if desired.

---

## Self-Review

**Spec coverage:**
- Part 1 (fixed accent, decouple NP, waveform) → Tasks 1 (accent/NP), 3 (waveform). ✓
- Part 2 (tokens, light block, `.np` scoping, ambient wash, theme toggle) → Task 2 + Task 3 (toggle). ✓
- Part 3 (rows, thumbnail, playing treatment, hairline, width cap, select mode, queue) → Tasks 4 (songs) + 5 (queue). ✓
- Preserve-functionality guardrail → Task 6 sweep. ✓
- Decisions A–E: A (`--ac2` fixed shade) Task 1 S1; B (dock teal / NP album / theme-aware) Task 3; C (keep tri-state + persistence) preserved — Task 2/3 only restyle; D (`#bgwrap` visible in light) Task 2 S5; E (⌥ Play-next) Task 4 S3–S4. ✓

**Placeholder scan:** No TBD/TODO; every code step shows exact code; the two "find the current block" steps (`.pop` background, the enqueue dispatch) give exact grep anchors and the exact replacement, matching existing signatures rather than inventing them. ✓

**Type/name consistency:** `--np-ac/--np-ac2/--np-bg1/--np-bg2` (Task 1) reused in Task 3's NP branch via `pal.ac`; `WAVE_DIM`/`PLAYHEAD`/`readThemeColors`/`THEME_IC` defined and used in Task 3; `.rthumb`/`.rright`/`.idx`/`.listwrap`/`.selball` defined in Task 4 CSS and reused in Task 5 markup; `trackRow(t,idx)` signature unchanged so grouped/sorted callers are untouched. ✓
