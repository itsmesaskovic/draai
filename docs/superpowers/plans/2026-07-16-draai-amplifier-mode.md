# DRAAI Amplifier full-screen mode — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a vintage Onkyo-style twin-VU "Amplifier" full-screen mode to `player_ui.html`, with direct switching between the existing "Now Playing" and the new "Amplifier" modes.

**Architecture:** A self-contained `#amp` `<section>` (sibling of `#np`) plus an amp CSS block and an amp JS module, ported from the reference `HALCYON Player (2).html` and bound to DRAAI's existing render loop (`tick`), smoothed energy (`sm`), playback clock (`posSec/durSec/playing`, `energyAt`), waveform (`PEAKS`/`drawWave`/`sizeCanvas`), state (`A.*`), and commands (`cmd`, `selectRoom`, `setSlider`). Twin canvases drawn per-frame with modeled VU ballistics from analysis energy. Desktop-only; the amp interior is always amber/dark regardless of theme.

**Tech Stack:** One hand-written HTML file, inline CSS + vanilla JS, Canvas 2D. No frameworks, no CDNs, no new backend.

## Global Constraints

- **`player_ui.html` stays ONE self-contained file** — all CSS/JS inline, no CDNs, no web fonts, no `localStorage`/`sessionStorage`, works offline. Do not touch the built-in `PAGE` fallback in the engine (amp is a `player_ui.html`-only feature).
- **The amp interior is always amber/dark, independent of light/dark theme.** Chrome that belongs to DRAAI (dock Amp button, NP-header toggle) uses the fixed teal accent `--ac` (`#5EEAD4`).
- **Needles are driven only by `/api/analysis` energy via the playback clock** (`sm.*` while `playing`) — never a real audio signal, never `Math.random()`.
- **Desktop only:** `@media (max-width:820px){ .amp{display:none} }`; the dock Amp button hides there too.
- **No new backend endpoints.** `python3 tests/test_draai.py` (25 tests) must stay green (a UI-only change).
- **No real-device playback in testing** — verify in DEMO mode (open the UI with no reachable engine so status/analysis are synthesized) and via the network panel; against a real engine use a fake/idle speaker. Never start audio on hardware.
- **Respect `prefers-reduced-motion`** (the existing `REDUCE` const, line 601): gentle constant easing, no sine decorrelation.
- Commit after each task, SHORT title, NO trailers.

## File structure

- **Modify `player_ui.html` only.** Three regions:
  - **Markup:** a new `#amp` section after the `#np` `</section>`; a dock Amp button beside `#expandBtn`; a to-Amp toggle in `.nphead`.
  - **CSS:** an "amplifier mode" block after the fullscreen `.np` CSS.
  - **JS:** an amp module after `drawWave`; a `drawAmp` call inside `tick`; canvas-init additions; an `applyStatus` extension; keyboard + resize + wiring additions.

Reference (verified 2026-07-16, `player_ui.html`): `tick` 707; `sm` 706; `posSec/durSec/playing` 705; `energyAt` 700; `drawWave` 730 / `mkCanvas` 726 / `sizeCanvas` 728 / `dockCv,npCv` 727 / canvas init 1187; `cmd` 801; `selectRoom` 835; `A` 605; `setSlider` 784 + `onVol` 1045; `applyStatus` play-icon block 752–753; `#np`/`.np.open` 219, `#expandBtn` 483/1052, `.nphead` 513, `#npClose` 516/1053; keyboard handler 1080–1083; `$$(".wave")` seek 1048; `REDUCE` 601; `fmt` 626; `guard` 800; `pal` 634.

---

### Task 1: Amp scaffold — markup, CSS, open/close, mode switching, keyboard

**Files:**
- Modify: `player_ui.html` (markup after `#np`; dock button; `.nphead` toggle; amp CSS block; JS open/close + wiring + keyboard + resize + canvas init)

**Interfaces:**
- Produces (JS, script scope): globals `vuLc, vuRc, ampCv, ampLamp` (declared here, drawn in Task 2); `function mkCv(id)`, `function sizeCv(o)`, `function openAmp()`, `function closeAmp()`; the dock `#ampBtn`, header `#npToAmp`/`#ampToNp`/`#ampClose`, and `#lampToggle` are wired; keyboard `a`/`A` and `Escape` handle the amp.

- [ ] **Step 1: Add the dock Amp button** in `player_ui.html` immediately after `#expandBtn` (line 483):

```html
        <button class="cbtn" id="ampBtn" title="Amplifier (A)"><svg class="ic" viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"></rect><circle cx="8" cy="12" r="2.4"></circle><circle cx="16" cy="12" r="2.4"></circle></svg></button>
```

- [ ] **Step 2: Add the to-Amp toggle in the NP header** — in `.nphead` (line 513–516), insert before `#npClose`:

```html
    <button class="iconbtn" id="npToAmp" title="Amplifier view (A)"><svg class="ic" viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"></rect><circle cx="8" cy="12" r="2.4"></circle><circle cx="16" cy="12" r="2.4"></circle></svg></button>
```

- [ ] **Step 3: Add the `#amp` section** right after the `#np` `</section>` closes (search for the end of the `#np` block). Paste this markup verbatim:

```html
<!-- amplifier mode (Onkyo-style twin VU) — desktop showpiece, always dark -->
<section class="amp" id="amp">
  <div class="amphead">
    <div class="brandplate"><span class="lampdot"></span>DRAAI<span class="mk">M-1</span></div>
    <div class="lbl">STEREO POWER AMPLIFIER</div>
    <button class="iconbtn" id="ampToNp" title="Now playing view (F)"><svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"></circle><circle cx="12" cy="12" r="2.6"></circle></svg></button>
    <button class="iconbtn" id="ampClose" title="Close (Esc)"><svg class="ic" viewBox="0 0 24 24"><path d="M6 18 18 6M6 6l12 12"></path></svg></button>
  </div>
  <div class="chassis">
    <div class="faceplate">
      <div class="screws"><i></i><i></i><i></i><i></i></div>
      <div class="meterglass">
        <div class="metercase">
          <div class="metertitle">LEFT<span>dB / VU</span></div>
          <canvas id="vuL"></canvas>
          <div class="peaklamp" id="peakL">PEAK</div>
        </div>
        <div class="metercase">
          <div class="metertitle">RIGHT<span>dB / VU</span></div>
          <canvas id="vuR"></canvas>
          <div class="peaklamp" id="peakR">PEAK</div>
        </div>
        <div class="glare"></div>
      </div>
      <div class="controls">
        <div class="knobwrap">
          <div class="knob" id="knobSrc"><span class="ind"></span></div>
          <div class="knoblab">SOURCE</div>
          <div class="knobval mono" id="srcVal">—</div>
        </div>
        <div class="vumeta">
          <div class="track"><b id="ampTitle">Nothing playing</b><small id="ampArtist"></small></div>
          <div class="ampseek"><div class="wave" id="ampWave"><canvas></canvas></div>
            <div class="nprow"><span class="t mono" id="ampPos">0:00</span><span class="t mono" id="ampDur">0:00</span></div></div>
          <div class="ampctrls">
            <button class="cbtn" data-cmd="prev"><svg class="ic fill" viewBox="0 0 24 24"><rect x="5" y="5" width="2.3" height="14" rx="1"></rect><path d="M20 6v12a1 1 0 0 1-1.6.8L9 12l9.4-6.8A1 1 0 0 1 20 6z"></path></svg></button>
            <button class="play" id="ampPlay"><svg class="ic" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"></path></svg></button>
            <button class="cbtn" data-cmd="next"><svg class="ic fill" viewBox="0 0 24 24"><path d="M4 6v12a1 1 0 0 0 1.6.8L15 12 5.6 5.2A1 1 0 0 0 4 6z"></path><rect x="16.7" y="5" width="2.3" height="14" rx="1"></rect></svg></button>
          </div>
        </div>
        <div class="knobwrap">
          <div class="knob big" id="knobVol"><span class="ind"></span></div>
          <div class="knoblab">VOLUME</div>
          <div class="knobval mono" id="volVal">30</div>
        </div>
      </div>
      <div class="switchrow">
        <button class="tglbtn on" id="lampToggle"><span class="led"></span>METER LAMP</button>
        <div class="grille"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
        <div class="powerlab">POWER · <span id="ampSpeaker">—</span></div>
      </div>
    </div>
  </div>
</section>
```

- [ ] **Step 4: Add the amp CSS block** after the fullscreen `.np` CSS (after the `.np` / `.artstage` rules, before the light-theme block). Paste verbatim:

```css
/* ---------------- amplifier mode ---------------- */
.amp{position:fixed;inset:0;z-index:41;transform:translateY(101%);transition:transform .6s cubic-bezier(.16,1,.3,1);overflow:hidden;
  display:flex;flex-direction:column;color:#e9e2d3;
  background:radial-gradient(120% 90% at 50% -10%,#242018,#0c0b09 62%)}
.amp.open{transform:none}
.amp .amphead{display:flex;align-items:center;gap:20px;padding:22px 34px}
.brandplate{display:flex;align-items:center;gap:11px;font-size:17px;font-weight:700;letter-spacing:.24em;color:#f3ecdd}
.brandplate .mk{font-size:10px;letter-spacing:.14em;color:#0c0b09;background:#c9a24b;padding:3px 6px;border-radius:4px;font-weight:800}
.brandplate .lampdot{width:9px;height:9px;border-radius:50%;background:#ffb347;box-shadow:0 0 12px #ffb347,0 0 3px #fff inset;animation:pwr 4s ease-in-out infinite}
@keyframes pwr{0%,100%{opacity:.75}50%{opacity:1}}
.amp .amphead .lbl{font-size:11px;letter-spacing:.26em;color:#8a8069;text-transform:uppercase;margin-left:auto}
.amp .iconbtn{color:#8a8069}.amp .iconbtn:hover{color:#f3ecdd;background:rgba(255,255,255,.06);border-color:rgba(255,255,255,.1)}
.chassis{flex:1;display:grid;place-items:center;padding:0 34px 44px;min-height:0;overflow-y:auto}
.faceplate{width:min(1180px,100%);border-radius:20px;padding:30px;position:relative;
  background:linear-gradient(180deg,#3a352b,#211d16 55%,#2a251c);
  box-shadow:0 40px 100px rgba(0,0,0,.7),inset 0 1px 0 rgba(255,255,255,.12),inset 0 -2px 8px rgba(0,0,0,.6),0 0 0 1px rgba(0,0,0,.5);
  background-image:linear-gradient(180deg,#3a352b,#211d16 55%,#2a251c),repeating-linear-gradient(90deg,rgba(255,255,255,.018) 0 1px,transparent 1px 3px)}
.screws{position:absolute;inset:0;pointer-events:none}
.screws i{position:absolute;width:13px;height:13px;border-radius:50%;background:radial-gradient(circle at 35% 30%,#6a6252,#211d16);box-shadow:inset 0 0 0 1px rgba(0,0,0,.5),0 1px 1px rgba(255,255,255,.1)}
.screws i:nth-child(1){top:13px;left:13px}.screws i:nth-child(2){top:13px;right:13px}
.screws i:nth-child(3){bottom:13px;left:13px}.screws i:nth-child(4){bottom:13px;right:13px}
.screws i::after{content:"";position:absolute;inset:4px 2px;border-top:1px solid rgba(0,0,0,.55)}
.meterglass{position:relative;display:grid;grid-template-columns:1fr 1fr;gap:22px;padding:22px;border-radius:14px;
  background:linear-gradient(180deg,#141712,#0a0c08);box-shadow:inset 0 3px 14px rgba(0,0,0,.85),inset 0 0 0 1px rgba(0,0,0,.6),0 1px 0 rgba(255,255,255,.06)}
.metercase{position:relative;border-radius:9px;padding:14px 14px 10px;overflow:hidden;
  background:linear-gradient(180deg,#1f2419,#141810);box-shadow:inset 0 0 40px rgba(0,0,0,.7),inset 0 0 0 1px rgba(255,180,80,.06)}
.amp.lampon .metercase{background:linear-gradient(180deg,#2e2f16,#1c1c0d);box-shadow:inset 0 0 46px rgba(255,176,70,.22),inset 0 0 0 1px rgba(255,190,90,.14)}
.metertitle{font-size:10px;letter-spacing:.2em;color:#b9a879;display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;font-weight:600}
.metertitle span{font-size:8.5px;letter-spacing:.12em;color:#7d7150}
.metercase canvas{width:100%;height:150px;display:block}
.peaklamp{position:absolute;top:12px;right:14px;font-size:8px;letter-spacing:.14em;font-weight:800;color:#5a1f16;
  background:#2a120d;padding:2px 5px;border-radius:3px;box-shadow:inset 0 0 0 1px rgba(255,80,60,.15);transition:.08s}
.peaklamp.hot{color:#08080b;background:#ff5436;box-shadow:0 0 14px #ff5436,0 0 3px #fff inset}
.glare{position:absolute;inset:0;border-radius:14px;pointer-events:none;
  background:linear-gradient(105deg,rgba(255,255,255,.09) 0%,transparent 30%,transparent 70%,rgba(255,255,255,.04) 100%)}
.controls{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:30px;margin-top:26px}
.knobwrap{display:flex;flex-direction:column;align-items:center;gap:9px}
.knob{width:74px;height:74px;border-radius:50%;position:relative;cursor:pointer;touch-action:none;
  background:radial-gradient(circle at 38% 32%,#4a463c,#1a170f 78%);
  box-shadow:0 6px 16px rgba(0,0,0,.6),inset 0 2px 3px rgba(255,255,255,.15),inset 0 -4px 8px rgba(0,0,0,.6),0 0 0 1px rgba(0,0,0,.5)}
.knob::after{content:"";position:absolute;inset:12px;border-radius:50%;background:repeating-conic-gradient(from 0deg,#2a271e 0 4deg,#221f17 4deg 8deg);box-shadow:inset 0 0 0 1px rgba(0,0,0,.4)}
.knob.big{width:104px;height:104px}
.knob .ind{position:absolute;left:50%;top:7px;width:3px;height:20px;border-radius:2px;background:#ffb347;box-shadow:0 0 8px #ffb347;transform-origin:50% 30px;transform:translateX(-50%) rotate(var(--rot,-140deg));z-index:2}
.knob.big .ind{height:26px;transform-origin:50% 45px;transform:translateX(-50%) rotate(var(--rot,-140deg))}
.knoblab{font-size:9.5px;letter-spacing:.2em;color:#8a8069;font-weight:600}
.knobval{font-size:12px;color:#d8cdb0}
.vumeta{text-align:center;min-width:0;padding:0 6px}
.vumeta .track b{font-size:16px;font-weight:560;color:#f3ecdd;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vumeta .track small{font-size:12.5px;color:#9a8f72;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.ampseek{margin:14px 0 12px}
.ampseek .wave{height:34px}
.ampseek .nprow{display:flex;justify-content:space-between;margin-top:2px}
.ampseek .t{font-size:11px;color:#7d7150}
.ampctrls{display:flex;align-items:center;justify-content:center;gap:14px}
.amp .cbtn{color:#b9a879}.amp .cbtn:hover{color:#f3ecdd}
.amp .play{background:#e9e2d3;color:#1a170f;box-shadow:0 4px 16px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.6)}
.switchrow{display:flex;align-items:center;gap:20px;margin-top:24px;padding-top:20px;border-top:1px solid rgba(0,0,0,.4);box-shadow:0 -1px 0 rgba(255,255,255,.05)}
.tglbtn{display:flex;align-items:center;gap:9px;font-size:10px;letter-spacing:.16em;font-weight:700;color:#8a8069;
  padding:9px 14px;border-radius:8px;background:linear-gradient(180deg,#2b271e,#1c1912);box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 2px 5px rgba(0,0,0,.5)}
.tglbtn .led{width:8px;height:8px;border-radius:50%;background:#3a3323;box-shadow:inset 0 0 0 1px rgba(0,0,0,.5)}
.tglbtn.on{color:#f3ecdd}.tglbtn.on .led{background:#ffb347;box-shadow:0 0 10px #ffb347,0 0 2px #fff inset}
.grille{flex:1;display:flex;gap:4px;align-items:center;justify-content:center;opacity:.5}
.grille i{width:3px;height:22px;border-radius:2px;background:linear-gradient(180deg,#3a352b,#15120c);box-shadow:inset 0 0 0 1px rgba(0,0,0,.4)}
.powerlab{font-size:10px;letter-spacing:.14em;color:#8a8069;font-weight:600}
@media(max-width:820px){.amp{display:none}}
@media(max-height:720px){.metercase canvas{height:112px}.knob{width:60px;height:60px}.knob.big{width:82px;height:82px}.faceplate{padding:22px}.meterglass{padding:16px}.controls{margin-top:18px}.switchrow{margin-top:16px;padding-top:14px}}
```

- [ ] **Step 5: Hide the dock Amp button on narrow viewports** — add to the existing responsive CSS (or beside the amp block):

```css
@media(max-width:820px){#ampBtn{display:none}}
```

- [ ] **Step 6: Declare amp canvas globals + helpers + open/close** — add a JS block right after `function drawWave(...)` ends (~line 744):

```javascript
/* ---------------- amplifier VU ---------------- */
let vuLc, vuRc, ampCv, vuL=0, vuR=0, peakLT=0, peakRT=0, ampLamp=true;
function mkCv(id){ const cv=document.getElementById(id); return {cv,ctx:cv.getContext("2d")}; }
function sizeCv(o){ if(!o||!o.cv) return; const r=o.cv.getBoundingClientRect(), dpr=Math.min(2,devicePixelRatio||1);
  o.cv.width=Math.max(1,r.width*dpr); o.cv.height=Math.max(1,r.height*dpr); o.ctx.setTransform(dpr,0,0,dpr,0,0); o.W=r.width; o.H=r.height; }
function openAmp(){ $("#np").classList.remove("open"); $("#amp").classList.add("open"); $("#amp").classList.toggle("lampon",ampLamp);
  requestAnimationFrame(()=>{ sizeCv(vuLc); sizeCv(vuRc); if(ampCv) sizeCanvas(ampCv); }); }
function closeAmp(){ $("#amp").classList.remove("open"); }
```

- [ ] **Step 7: Initialise the amp canvases** — extend the canvas-init line (~1187, `dockCv=mkCanvas("#dockWave"); npCv=mkCanvas("#npWave"); ...`) by appending:

```javascript
  ampCv=mkCanvas("#ampWave"); vuLc=mkCv("vuL"); vuRc=mkCv("vuR");
```

- [ ] **Step 8: Wire the entry/exit controls** — in the DOM-ready wiring section (near the `#expandBtn`/`#npClose` handlers, ~1052):

```javascript
  $("#ampBtn").addEventListener("click",openAmp);
  $("#npToAmp").addEventListener("click",openAmp);
  $("#ampToNp").addEventListener("click",()=>{ closeAmp(); $("#np").classList.add("open"); requestAnimationFrame(()=>sizeCanvas(npCv)); });
  $("#ampClose").addEventListener("click",closeAmp);
  $("#lampToggle").addEventListener("click",()=>{ ampLamp=!ampLamp; $("#lampToggle").classList.toggle("on",ampLamp); $("#amp").classList.toggle("lampon",ampLamp); });
```

- [ ] **Step 9: Extend the keyboard handler** (~1082) so `f`/`F` = NP, `a`/`A` = amp, `Escape` closes whichever is open. Replace the existing `Escape` and `f` branches with:

```javascript
    else if(e.key==="Escape"){ $("#np").classList.remove("open"); closeAmp(); $$(".pop").forEach(p=>p.classList.remove("open")); }
    else if(e.key==="f"||e.key==="F"){ if($("#amp").classList.contains("open")){ closeAmp(); $("#np").classList.add("open"); requestAnimationFrame(()=>sizeCanvas(npCv)); } else $("#expandBtn").click(); }
    else if(e.key==="a"||e.key==="A"){ if($("#amp").classList.contains("open")) closeAmp(); else openAmp(); }
```

- [ ] **Step 10: Re-size amp canvases on window resize** — extend the existing `resize` handler (~1084) to also handle the amp:

```javascript
  addEventListener("resize",()=>{ sizeCanvas(dockCv); if($("#np").classList.contains("open"))sizeCanvas(npCv);
    if($("#amp").classList.contains("open")){ sizeCv(vuLc); sizeCv(vuRc); sizeCanvas(ampCv); } });
```
(Replace the old resize handler; keep its dock/np behaviour.)

- [ ] **Step 11: Verify in the Browser pane (desktop 1280×800, DEMO mode)** — serve `player_ui.html` with no reachable engine (DEMO). Confirm: the dock Amp button opens the amp (slide-up); the faceplate, screws, twin (blank) meters, knobs, and METER LAMP render; the NP-header toggle swaps NP→Amp and the amp-header disc toggle swaps Amp→NP; `F`/`A`/`Esc` behave (one full-screen at a time); at `max-height:720px` (resize the pane short) the chassis scrolls and METER LAMP is reachable; at `max-width:820px` the amp and its dock button are hidden. No console errors (meters are blank until Task 2). **No real playback.**

- [ ] **Step 12: Commit**

```bash
git add player_ui.html
git commit -m "amp: scaffold, mode switching, keyboard"
```

---

### Task 2: VU meters — drawVU + drawAmp ballistics

**Files:**
- Modify: `player_ui.html` (add `VU_MARKS`, `drawVU`, `drawAmp`; call `drawAmp` from `tick`)

**Interfaces:**
- Consumes: `vuLc,vuRc,ampCv,vuL,vuR,peakLT,peakRT,ampLamp` (Task 1); `sm`, `playing`, `posSec`, `durSec`, `REDUCE`, `clamp`, `fmt`, `drawWave`, `sizeCv`.
- Produces: `drawVU(o,val)`, `drawAmp(dt,t,prog)`; needles animate while `#amp` is open.

- [ ] **Step 1: Add `drawAmp` + `drawVU`** right after the amp globals/helpers from Task 1 (after `closeAmp`):

```javascript
function drawAmp(dt,t,prog){
  const eAmp=playing?sm.amp:0, eLow=playing?sm.low:0, eHigh=playing?sm.high:0;
  // classic L/R stereo pair: both track overall level, gently decorrelated for life
  let tL=clamp(eAmp*0.9+eLow*0.28,0,1.12), tR=clamp(eAmp*0.9+eHigh*0.28,0,1.12);
  if(playing&&!REDUCE){ tL*=1+0.05*Math.sin(t/230); tR*=1+0.05*Math.cos(t/205); }
  const atk=REDUCE?0.2:1-Math.exp(-dt/0.05), dec=1-Math.exp(-dt/0.24);   // ~50ms attack, ~240ms decay
  vuL+= tL>vuL?(tL-vuL)*atk:(tL-vuL)*dec;
  vuR+= tR>vuR?(tR-vuR)*atk:(tR-vuR)*dec;
  drawVU(vuLc,vuL); drawVU(vuRc,vuR);
  peakLT=vuL>1.0?0.9:Math.max(0,peakLT-dt); peakRT=vuR>1.0?0.9:Math.max(0,peakRT-dt);
  $("#peakL").classList.toggle("hot",peakLT>0); $("#peakR").classList.toggle("hot",peakRT>0);
  if(ampCv){ drawWave(ampCv,prog); $("#ampPos").textContent=fmt(posSec); $("#ampDur").textContent=fmt(durSec); }
}
const VU_MARKS=[[0,"20"],[0.26,"10"],[0.42,"7"],[0.54,"5"],[0.67,"3"],[0.82,"0"],[1,"+3"]];
function drawVU(o,val){ if(!o||!o.W){ if(o) sizeCv(o); if(!o||!o.W) return; } const {ctx,W,H}=o; ctx.clearRect(0,0,W,H);
  const face=ctx.createLinearGradient(0,0,0,H);
  if(ampLamp){ face.addColorStop(0,"#f6e8c6"); face.addColorStop(1,"#e7cc88"); } else { face.addColorStop(0,"#403c2d"); face.addColorStop(1,"#29271d"); }
  ctx.fillStyle=face; ctx.fillRect(0,0,W,H);
  const ink=ampLamp?"#2a2114":"#6b6250", red=ampLamp?"#c0341c":"#7a3226";
  const cx=W/2, cy=H*1.34, R=H*1.16, a0=-Math.PI/2-0.62, a1=-Math.PI/2+0.62;
  // red zone arc (past 0 dB)
  ctx.strokeStyle=red; ctx.lineWidth=3.5; ctx.beginPath(); ctx.arc(cx,cy,R-3,a0+(a1-a0)*0.82,a1); ctx.stroke();
  ctx.strokeStyle=ink; ctx.lineWidth=1.5; ctx.beginPath(); ctx.arc(cx,cy,R-3,a0,a0+(a1-a0)*0.82); ctx.stroke();
  // ticks + labels
  ctx.textAlign="center"; ctx.font="600 10px ui-monospace,Menlo,monospace";
  for(const [tk,lab] of VU_MARKS){ const ang=a0+(a1-a0)*tk;
    const x1=cx+Math.cos(ang)*(R-8), y1=cy+Math.sin(ang)*(R-8), x2=cx+Math.cos(ang)*(R-16), y2=cy+Math.sin(ang)*(R-16);
    ctx.strokeStyle=tk>=0.82?red:ink; ctx.lineWidth=1.6; ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();
    const lx=cx+Math.cos(ang)*(R-27), ly=cy+Math.sin(ang)*(R-27)+3.5; ctx.fillStyle=tk>=0.82?red:ink; ctx.fillText(lab,lx,ly); }
  // minor ticks
  ctx.lineWidth=1;
  for(let k=0;k<=40;k++){ const tk=k/40; if(VU_MARKS.some(m=>Math.abs(m[0]-tk)<0.02))continue; const ang=a0+(a1-a0)*tk;
    ctx.strokeStyle=tk>=0.82?red:ink; ctx.globalAlpha=0.5; ctx.beginPath();
    ctx.moveTo(cx+Math.cos(ang)*(R-8),cy+Math.sin(ang)*(R-8)); ctx.lineTo(cx+Math.cos(ang)*(R-13),cy+Math.sin(ang)*(R-13)); ctx.stroke(); ctx.globalAlpha=1; }
  ctx.fillStyle=ink; ctx.font="700 11px system-ui"; ctx.fillText("VU",cx,H-14);
  // needle
  const ang=a0+(a1-a0)*clamp(val,0,1.06);
  ctx.strokeStyle="#1a1206"; ctx.lineWidth=2; ctx.lineCap="round"; ctx.beginPath();
  ctx.moveTo(cx,cy); ctx.lineTo(cx+Math.cos(ang)*(R+4),cy+Math.sin(ang)*(R+4)); ctx.stroke();
  ctx.fillStyle="#1a1206"; ctx.beginPath(); ctx.arc(cx,cy,5,0,7); ctx.fill();
}
```

- [ ] **Step 2: Call `drawAmp` from the render loop** — in `tick` (line 723, right after the `drawWave(dockCv,prog)` / NP line), add:

```javascript
  if($("#amp").classList.contains("open")) drawAmp(dt,t,prog);
```
Confirm `tick` has `dt`, `t`, and `prog` in scope at that point (it does: `dt` line 707, `t` is the rAF arg, `prog` line 720).

- [ ] **Step 3: Verify in the Browser pane (DEMO mode, desktop)** — open the amp during synthesized playback. Confirm: both needles sweep with energy and read as fast-attack / slow-decay (not jittery, not random — pausing settles them to −20); the printed −20→+3 scale with a red zone past 0 renders; PEAK lamps light when a needle nears/passes the top and hold briefly; the amp waveform seek bar animates and shows position/duration. Toggle METER LAMP off → faces go dark, needles still legible. Enable `prefers-reduced-motion` (DevTools rendering emulation) → needles ease gently, no sine wobble. No console errors.

- [ ] **Step 4: Commit**

```bash
git add player_ui.html
git commit -m "amp: twin VU meters with modeled ballistics"
```

---

### Task 3: Chassis controls wired to real behavior

**Files:**
- Modify: `player_ui.html` (knob helpers; VOLUME + SOURCE knobs; extend `applyStatus`)

**Interfaces:**
- Consumes: `A`, `A.status`, `A.speakers`, `A.speaker`, `cmd`, `selectRoom`, `setSlider`, `clamp`, `#volSlider`/`#npVol`, `applyStatus` (Task 1/2 amp elements).
- Produces: `setKnob(el,v,min,max)`, `wireKnob(el,get,set)`, a `knobDrag` flag; VOLUME/SOURCE knobs live; `applyStatus` drives `#ampTitle/#ampArtist/#ampSpeaker/#srcVal/#volVal/#knobVol/#ampPlay`.

- [ ] **Step 1: Add knob helpers** near the amp JS (after `drawVU`):

```javascript
let knobDrag=false;
function setKnob(el,v,min,max){ const f=(v-min)/(max-min||1); el.style.setProperty("--rot",(-140+f*280)+"deg"); }
function wireKnob(el,get,set){
  const range=280, sens=0.7;
  const start=e=>{ knobDrag=true; const y0=(e.touches?e.touches[0]:e).clientY, v0=get();
    const move=ev=>{ const y=(ev.touches?ev.touches[0]:ev).clientY; set(v0+(y0-y)*sens); ev.preventDefault(); };
    const up=()=>{ knobDrag=false; removeEventListener("pointermove",move); removeEventListener("pointerup",up); };
    addEventListener("pointermove",move); addEventListener("pointerup",up); };
  el.addEventListener("pointerdown",start);
  el.addEventListener("wheel",e=>{ set(get()+(e.deltaY<0?2:-2)); e.preventDefault(); },{passive:false});
}
```

- [ ] **Step 2: Wire VOLUME + SOURCE knobs + reflect status** — in the DOM-ready section (beside the Task 1 amp wiring):

```javascript
  wireKnob($("#knobVol"),()=>A.status.volume,v=>{ v=clamp(Math.round(v),0,100); A.status.volume=v;
    setKnob($("#knobVol"),v,0,100); $("#volVal").textContent=v;
    setSlider($("#volSlider"),v); setSlider($("#npVol"),v); cmd("volume",v); });
  $("#knobSrc").addEventListener("click",()=>{ if(A.speakers.length<2)return;
    const i=A.speakers.findIndex(s=>A.speaker&&s.uuid===A.speaker.uuid);
    const nx=A.speakers[(i+1)%A.speakers.length]; selectRoom(nx.uuid); });
```
(The `data-cmd="prev"`/`"next"` amp buttons and the `.wave` seek are already handled by DRAAI's existing delegated `[data-cmd]` and `$$(".wave")` click handlers — confirm the amp markup is present before those handlers run; both run at DOM-ready over the full document, so the amp elements are covered. `#ampPlay` gets its click from the existing play-toggle wiring in the next step.)

- [ ] **Step 3: Wire `#ampPlay` to play/pause** — add beside the existing `$("#playBtn")...$("#npPlay")` play-toggle wiring (~1043):

```javascript
  $("#ampPlay").addEventListener("click",togglePlay);
```

- [ ] **Step 4: Extend `applyStatus`** to drive the amp readouts — after the `#npPlay` play-icon line (~753) add:

```javascript
  $("#ampPlay").querySelector(".ic").innerHTML=pp; $("#ampPlay").querySelector(".ic").classList.toggle("fill",playing);
  $("#ampSpeaker").textContent=spk; $("#srcVal").textContent=A.speaker?A.speaker.name:"—";
  $("#ampTitle").textContent=A.status.title||"Nothing playing";
  if(!knobDrag){ setKnob($("#knobVol"),A.status.volume,0,100); $("#volVal").textContent=A.status.volume; }
```
(`pp` and `spk` are already defined earlier in `applyStatus`; `knobDrag` from Step 1.)

- [ ] **Step 5: Populate `#ampArtist` on track change** — in `onTrackChange` (where the NP artist/meta is set), set `$("#ampArtist").textContent = <artist of the resolved track>` using the same source the NP view uses (the resolved track's `artist`; fall back to empty string). Match the existing NP-artist assignment exactly so the two stay consistent.

- [ ] **Step 6: Verify in the Browser pane (DEMO mode + network panel)** — with the amp open:
  - Drag the VOLUME knob up/down and scroll it: `#volVal` + indicator move, both sliders (`#volSlider`, `#npVol`) stay in sync, and a `POST /api/cmd {action:"volume",value}` fires (network panel). Against a real engine use a fake/idle speaker — **no playback**.
  - Click SOURCE: the active room cycles (`#srcVal` updates) when ≥2 speakers exist.
  - Transport prev/play/next issue `POST /api/cmd`; `#ampPlay` shows play vs pause per state.
  - Title/artist/speaker populate and update on track change; the seek bar seeks (click) and shows position/duration.
  - METER LAMP still toggles faces.

- [ ] **Step 7: Run the engine suite (unchanged) + commit**

```bash
python3 tests/test_draai.py   # expect OK (25) — UI-only change
git add player_ui.html
git commit -m "amp: wire volume/source knobs, transport, readouts"
```

---

## Self-review

**Spec coverage:** Amber faceplate/screws/glass + twin canvas VU meters → Task 1 (chassis) + Task 2 (drawVU). Lit cream face, printed −20→+3 scale with red zone, black needle, PEAK lamp → Task 2. Ballistics (50ms/240ms, L→low/R→high, decorrelation, rest at −20, reduced-motion) → Task 2 (`drawAmp`). Analysis-energy-driven, never audio/random → Task 2 (`sm` while `playing`). VOLUME/SOURCE/METER-LAMP + title/artist/seek/transport → Task 1 (markup/lamp) + Task 3 (knobs/transport/readouts). Short-viewport scroll + `max-height:720px` + desktop-only `max-width:820px` → Task 1 CSS. Mode switching (dock button, both header toggles, F/A/Esc, resize-on-entry, one-at-a-time) → Task 1. Token-driven teal chrome / amber face independent of theme → Task 1 CSS + global constraints. No new endpoints; tests green → Task 3 Step 7.

**Placeholder scan:** All CSS/markup/JS is complete and bound to verified DRAAI symbols with line anchors. The only prose-described step is Task 3 Step 5 (set `#ampArtist` to match the NP artist source) — it references the existing NP assignment rather than inventing a value; the implementer copies that source. No TBDs.

**Type/name consistency:** `vuLc/vuRc/ampCv/vuL/vuR/peakLT/peakRT/ampLamp` declared in Task 1, consumed in Task 2. `mkCv/sizeCv/openAmp/closeAmp` (Task 1) used by Tasks 2–3 and the wiring. `setKnob/wireKnob/knobDrag` defined in Task 3 Step 1, used in Steps 2/4. `drawAmp(dt,t,prog)` signature matches the `tick` call (Task 2 Step 2). `cmd("volume",v)`, `selectRoom(uuid)`, `setSlider`, `A.status.volume` match DRAAI's actual signatures.
