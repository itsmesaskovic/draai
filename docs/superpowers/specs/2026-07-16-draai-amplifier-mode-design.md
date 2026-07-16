# DRAAI Amplifier full-screen mode — design

**Date:** 2026-07-16
**Status:** Approved for planning
**Topic:** A vintage Onkyo-style twin-VU "Amplifier" full-screen mode in `player_ui.html`, plus direct switching between the existing "Now Playing" and the new "Amplifier" full-screen modes.

## Purpose

Add a second full-screen showpiece to the desktop player: a brushed-metal stereo-amplifier faceplate with two analog VU meters whose needles are driven by the track's analysis energy through the existing playback clock. Users can jump straight between the two full-screen modes without returning to the main view.

## Goals

- A faithful port of the twin-VU amplifier from the reference (`HALCYON Player (2).html`), reconciled to DRAAI's current `player_ui.html` conventions (fixed teal accent `#5EEAD4` for chrome, `data-theme` theming).
- The amp face is **always amber/dark, independent of the light/dark theme** — a lit VU meter only reads on black.
- Needles driven only by `/api/analysis` energy via the shared playback clock — **never a real audio signal** (audio plays on the speaker, not the browser) and **never random**.
- Real chassis controls wired to real behavior (volume, source/room, meter lamp, transport, seek).
- Robust on short / split-screen viewports; **desktop only**.
- **No new backend endpoints.**

## Non-goals

- Mobile/phone support (amp is desktop-only; `@media (max-width:820px)` hides it, and the slim `/remote` is unaffected).
- Real per-channel audio metering (impossible — the browser never sees the audio).
- Changing the built-in `PAGE` fallback UI (this is a `player_ui.html`-only, HALCYON-level feature).
- New EQ/sleep/grouping behavior.

## Architecture

All in `player_ui.html` (the single UI file). Three additions:

1. **Markup** — a new `<section class="amp" id="amp">` sibling of `#np`, containing: `.amphead` (brandplate + label + a **to-Now-Playing** toggle + close), a `.chassis` scroll container, the `.faceplate` (screws, `.meterglass` with two `.metercase` blocks each holding a `<canvas>` + PEAK lamp, `.glare`), a `.controls` grid (SOURCE knob · center `.vumeta` with title/artist + waveform seek + transport · VOLUME knob), and a `.switchrow` (METER LAMP toggle · grille · power label). A new **Amp button** in the dock beside `#expandBtn`, and a **to-Amplifier** toggle in the `.nphead`.
2. **CSS** — an "amplifier mode" block ported from the reference (faceplate, screws, meterglass, metercase, peaklamp, knobs, switchrow, `@media` rules). Amber palette hardcoded; chrome uses DRAAI tokens.
3. **Script** — an amp module: `drawAmp(dt,t)` (called from the existing `tick`), `drawVU(o,val)`, canvas helpers for the VU pair, knob helpers (`setKnob`/`wireKnob`), open/close + mode-switch functions, and event wiring.

### DRAAI integration surface (verified 2026-07-16)

The reference shares DRAAI's engine of animation, so the port binds to existing symbols:

| Need | DRAAI symbol (file: `player_ui.html`) |
|---|---|
| render loop | `function tick(t)` at line 707 (advances `posSec`, computes `energyAt`, smooths into `sm`, draws waves, `requestAnimationFrame(tick)`) |
| smoothed energy | `sm = {amp,low,mid,high}` (line 706), updated each tick |
| playback clock | `posSec`, `durSec`, `playing` (line 705), `parseTime`, `fmt`, `energyAt(sec)` (700) |
| per-track peaks + waveform | `PEAKS`, `drawWave(o,prog)` (730), `mkCanvas(sel)` (726), `sizeCanvas(o)` (728), `dockCv`/`npCv` (727) |
| transport / volume command | `async function cmd(action,value)` (801) → `POST /api/cmd {speaker:A.speaker.uuid, action, value}` then `poll()` |
| room selection | `async function selectRoom(uuid)` (835) |
| state | `A.speakers` / `A.speaker` / `A.status` (with `.volume`, `.title`, `.state`, `.track_no`) (605) |
| slider sync | `setSlider(el,v)` (784); volume sliders `#volSlider` (497) + `#npVol` (546); existing `onVol` handler (1045) |
| fullscreen NP | `#np` / `.np.open` (219), opened by `#expandBtn` (483/1052), closed by `#npClose` (516/1053), `.nphead` (513) |
| keyboard | global `keydown` handler (lines 1080–1083): `Escape` closes `#np` + pops, `f`/`F` → `#expandBtn`, Shift+arrows transport |
| demo mode | `DEMO` + `Demo.route` — lets the amp be exercised with no engine/speaker (analysis + status are synthesized) |

Knob helpers (`setKnob`/`wireKnob`) do **not** exist in DRAAI yet (it uses sliders) — they are ported from the reference.

## The VU meters

Two `<canvas>` elements `#vuL` / `#vuR`, DPR-sized by a `sizeCv(o)` helper (mirrors `sizeCanvas` but for a bare canvas), re-sized on amp open and window resize.

**`drawAmp(dt,t)`** — called from `tick` only while `#amp` is open:

- Energy source while `playing`: `eAmp=sm.amp, eLow=sm.low, eHigh=sm.high`; else `0`.
- Stereo targets (both track overall level, gently decorrelated):
  - `tL = clamp(eAmp*0.9 + eLow*0.28, 0, 1.12)`
  - `tR = clamp(eAmp*0.9 + eHigh*0.28, 0, 1.12)`
  - When `playing && !REDUCE`: `tL *= 1 + 0.05*sin(t/230)`, `tR *= 1 + 0.05*cos(t/205)`.
- **Ballistics** (fast attack, slow decay): `atk = REDUCE ? 0.2 : 1-exp(-dt/0.05)` (~50 ms), `dec = 1-exp(-dt/0.24)` (~240 ms). Integrate `vuL`/`vuR`: rising uses `atk`, falling uses `dec`.
- **PEAK lamps**: a hold timer per channel set to `0.9` when `vu > 1.0`, decaying by `dt`; toggle `.hot` on `#peakL`/`#peakR` while > 0.
- At rest (paused / no analysis) targets are 0 → needles ease to the `−20` end. `REDUCE` gives a single gentle easing constant and no sine decorrelation.

**`drawVU(o,val)`** — per meter:

- Face gradient: lamp on → cream (`#f6e8c6`→`#e7cc88`); lamp off → dark (`#403c2d`→`#29271d`).
- Ink/red colors switch with the lamp. Arc geometry `cx=W/2, cy=H*1.34, R=H*1.16, a0=-π/2-0.62, a1=-π/2+0.62`.
- Printed scale via `VU_MARKS = [[0,"20"],[0.26,"10"],[0.42,"7"],[0.54,"5"],[0.67,"3"],[0.82,"0"],[1,"+3"]]`; the segment past `0.82` (0 dB) is a **red zone**; major + minor ticks; a "VU" label.
- Black needle from pivot to `a0+(a1-a0)*clamp(val,0,1.06)`, plus a hub dot.

## Chassis controls (wired to real behavior)

- **VOLUME knob** (`#knobVol`, big) — `wireKnob` drag/scroll → `v=clamp(round,0,100)` → `setKnob` + update `#volVal` + `setSlider(#volSlider)` + `setSlider(#npVol)` + `cmd("volume", v)`. Reflect incoming `A.status.volume` on poll when not dragging.
- **SOURCE knob** (`#knobSrc`) — click/step cycles `A.speakers` → `selectRoom(next.uuid)`; if fewer than 2 speakers, no-op. Show current room name in `#srcVal`.
- **METER LAMP** (`#lampToggle`) — toggles `ampLamp`, the `.on` class, and `.amp.lampon` (which lights the meter cases); `drawVU` reads `ampLamp` for the face palette.
- **Center column** — `#ampTitle`/`#ampArtist` from status/track; a waveform seek bar `#ampWave` (a `mkCanvas`-style wrapper drawn by `drawWave`, `#ampPos`/`#ampDur` labels, click-to-seek like the NP wave); transport `prev` / `#ampPlay` / `next` via `cmd`.
- Play/pause icon on `#ampPlay` is updated by the existing `applyStatus` (add `#ampPlay` to the set it already updates for `#playBtn`/`#npPlay`).

## Robustness (short / split-screen)

- `.chassis{ flex:1; overflow-y:auto; ... }` so the faceplate scrolls when the viewport is short — **METER LAMP must always be reachable**.
- `@media (max-height:720px)` shrinks: `.metercase canvas{height:112px}`, `.knob{60px}`, `.knob.big{82px}`, `.faceplate{padding:22px}`, `.meterglass{padding:16px}`, tightened `.controls`/`.switchrow`.
- `@media (max-width:820px){ .amp{display:none} }` — desktop only; the dock Amp button is likewise hidden at that width.

## Full-screen mode switching

- **Dock**: new `#ampBtn` beside `#expandBtn` → `openAmp()`.
- **NP header**: `#npToAmp` → close `#np`, `openAmp()`.
- **Amp header**: `#ampToNp` → close `#amp`, open `#np` (+ `sizeCanvas(npCv)`); `#ampClose` → close `#amp`.
- **`openAmp()`**: add `.open` to `#amp`, apply `.lampon` per state, then `requestAnimationFrame(()=>{ sizeCv(vuLc); sizeCv(vuRc); sizeCanvas(ampCv); })` so canvases size to their laid-out boxes.
- **Keyboard** (extend the existing handler): `f`/`F` → Now Playing, `a`/`A` → Amplifier, `Escape` → close whichever full-screen is open. Only one full-screen open at a time (opening one closes the other).
- `#amp` is a fixed `z-index:41` slide-up layer (`transform:translateY(101%)` → `none`), always dark, mirroring `#np`'s transition.
- `window resize` handler also re-sizes the VU + amp-wave canvases when `#amp` is open.

## Theming

- Chrome that belongs to DRAAI (the dock Amp button, the NP-header toggle) uses the existing `--ac` teal and matches sibling controls.
- The amp interior is a self-contained amber palette (hardcoded hex in the amp CSS block) and does not consume `--ac`; it renders identically in light and dark theme. The reference's `body.light` overrides are dropped; DRAAI's amp simply never themes.

## Reference reconciliation notes

- Accent `120 210 255` (blue) → not used inside the amp; DRAAI chrome uses `--ac` teal.
- `body.light` → DRAAI uses `html[data-theme=light]`; amp is theme-independent so no light rules are ported.
- Reference symbols already match DRAAI's (`sm`, `PEAKS`, `drawWave`, `sizeCanvas`, `cmd`, `A.status`, `posSec/durSec/playing`, `energyAt`, `selectRoom`, `setSlider`) — bind to DRAAI's, do not duplicate.
- `applyStatus` must also drive `#ampTitle`/`#ampArtist`/`#ampSpeaker`/`#ampPlay`/`#srcVal`/`#volVal`/`#knobVol` (extend the block that already updates `#np*`).

## Testing

- **Engine**: no new endpoints; `python3 tests/test_draai.py` (25 tests) must stay green (untouched by a UI-only change).
- **UI (Browser pane, desktop viewport ~1280×800)** — run in **DEMO mode** where possible (open the UI with no reachable engine, or the built-in demo) so analysis/status are synthesized and **no real speaker is touched**:
  - VU needles rise/fall with energy; ballistics read as fast-attack/slow-decay; PEAK lamps light near the top; at rest needles sit at −20.
  - METER LAMP toggles amber↔dark faces.
  - VOLUME knob drag/scroll changes the value and issues `POST /api/cmd {action:"volume"}` (assert via the network panel; against a real engine use a fake/idle speaker — **never trigger playback on hardware**); sliders stay in sync. SOURCE knob cycles rooms. Transport buttons issue `prev`/`resume`/`pause`/`next`.
  - Mode switch: dock Amp button, both header toggles, and `F`/`A`/`Esc` move between NP ⇄ Amp ⇄ closed with correct canvas sizing; only one full-screen at a time.
  - Short viewport (`max-height:720px` and a split-screen height): the chassis scrolls and METER LAMP is reachable; `max-width:820px` hides the amp.
  - `prefers-reduced-motion`: needles ease gently, no sine decorrelation, no jitter.

## Risks

- `player_ui.html` is already large; this adds a self-contained block. Keep the amp CSS/JS grouped and clearly sectioned; do not refactor unrelated code.
- Canvas sizing before layout yields 0×0 — always size on open via `requestAnimationFrame` and guard `drawVU` when `!o.W`.
- The reference's `selectRoom`/`cmd("volume")`/`applyStatus` names must be bound to DRAAI's actual functions, not copied verbatim from the reference's own (they are the same names here, but confirm at implementation time before wiring).
