# DRAAI UI modularization + Spectrum mode — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `player_ui.html` into `ui/` source partials assembled into one self-contained served document (byte-identical to today), then add a Spectrum analyzer full-screen mode as the first new triplet.

**Architecture:** A pure-stdlib assembler (`draai/ui.py`) concatenates an ordered manifest of `ui/` partials into the single `player_ui.html` document. `_load_ui` assembles from `ui/` at serve time (clone→run and edit→reload need no build); `build.py` bakes the assembled file into the `.pyz`. Full-screen modes become `ui/modes/<name>/` triplets (css+html+js) reusing the shared engine.

**Tech Stack:** Python 3 standard library (assembler + engine + build). Inline HTML/CSS/JS partials. No bundler, no npm, no new deps.

## Global Constraints

- **Pure Python standard library**; no pip. `build.py`/assembler use only stdlib.
- **The browser still receives ONE self-contained offline document** — no external requests, no CDNs/fonts, no local/sessionStorage. Assembly happens server-side/at build; a user dropping their own `cwd/player_ui.html` still overrides.
- **Phase 1 is byte-identical:** `assemble_ui(ui/)` must equal the pre-refactor `player_ui.html` **byte-for-byte** — zero behavior/visual change. Prove it before Phase 2.
- **Newline-exact:** open partials/snapshots with `newline=""` (disable universal-newline translation) so bytes are preserved.
- `python3 tests/test_draai.py` stays green; no JS test harness exists (UI verified by the byte-identical diff + Browser-pane DEMO). **No real-device playback in testing.**
- Full-screen modes: desktop-only, always dark, teal `#5EEAD4`, `prefers-reduced-motion` respected. Commit after each task, SHORT title, NO trailers.

## File structure

- **Create `draai/ui.py`** — `assemble_ui(ui_dir) -> str`.
- **Create `ui/`** — `manifest.txt` + shell/CSS/body/JS partials + `ui/modes/np/`, `ui/modes/amp/` (Phase 1), `ui/modes/spectrum/` (Phase 2).
- **Modify `draai/server.py`** — `_load_ui` assembles from `ui/`.
- **Modify `build.py`** — assemble `ui/` → packaged `draai/player_ui.html`.
- **Delete top-level `player_ui.html`; git-ignore it.**
- **Modify `tests/test_draai.py`** — assembly marker test + `_load_ui` serve/override test.
- **Modify `CLAUDE.md`** — rule #2.

Reference (verified 2026-07-16): `_load_ui` `draai/server.py:27-42`; `build.py:22-30` (copytree + `player_ui.html`/`remote.html` copies); `.gitignore:6-8`; `player_ui.html` 1,685 lines, `<style>`→~478, markup ~478-716, `<script>`→~717, 40 `/* ---- ---- */` section headers; full-screen shared engine + `openNp`/`openAmp`/`closeAmp` at `player_ui.html:813-1002`.

---

## Phase 1 — Modularization (byte-identical)

### Task 1: Assembler + carve `ui/` (byte-identical, not yet wired)

**Files:**
- Create: `draai/ui.py`, `ui/manifest.txt`, `ui/**` partials
- Test: `tests/test_draai.py`

**Interfaces:**
- Produces: `draai.ui.assemble_ui(ui_dir: str) -> str` — reads `manifest.txt` (one partial path per line, `#` comments/blank lines skipped) and returns the concatenation of those partials, verbatim, in order.

- [ ] **Step 1: Write the assembler** `draai/ui.py`:

```python
"""Assemble the web UI from ui/ partials into one self-contained document.

Mirrors how the package ships as one draai.pyz: modular source, single
served artifact. Plain ordered concatenation — the shell fragments
(<style>, </head><body>, <script>, ...) are themselves partials — so the
output is byte-identical to a hand-written single file.
"""
import os


def assemble_ui(ui_dir):
    manifest = os.path.join(ui_dir, "manifest.txt")
    parts = []
    with open(manifest, "r", encoding="utf-8", newline="") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                parts.append(s)
    out = []
    for rel in parts:
        with open(os.path.join(ui_dir, rel), "r", encoding="utf-8", newline="") as f:
            out.append(f.read())
    return "".join(out)
```

- [ ] **Step 2: Snapshot the current file** (the byte-identical target):

```bash
cp player_ui.html /tmp/player_ui.snapshot.html
```

- [ ] **Step 3: Carve `player_ui.html` into `ui/` partials + `manifest.txt`.** Split the file into contiguous slices at section boundaries so the ordered concatenation reproduces the snapshot exactly. Use a deterministic slicing script (adjust the boundary line list until the diff in Step 4 is empty). Target layout — a shell + coarse `core` + one triplet per existing full-screen mode:

```
ui/manifest.txt
ui/00-head.html            # <!doctype>…<head>…  through the opening <style>
ui/css/10-base.css         # tokens/reset/scrollbars/.ic …
ui/css/20-chrome.css       # sidebar + main + dock
ui/css/30-fullscreen.css   # shared fullscreen chrome (.np/.artstage/kinetic) + vinyl
ui/modes/np/np.css         # (if NP-specific CSS is separable; else keep in 30-fullscreen)
ui/modes/amp/amp.css       # amplifier-mode CSS block
ui/css/40-theme.css        # light theme + selection + group + folder chips + popovers
ui/css/49-responsive.css   # responsive/phone-view media queries
ui/40-body.html            # </style></head><body> + sidebar/main/dock markup
ui/modes/np/np.html        # #np section markup
ui/modes/amp/amp.html      # #amp section markup
ui/59-popovers.html        # popovers + toasts markup
ui/70-script.html          # <script> "use strict" … through shared engine + main-UI JS (core)
ui/modes/amp/amp.js        # amplifier VU JS (drawAmp/drawVU/openAmp/knobs/tuning)
ui/90-boot.html            # remaining core JS (events/boot) + </script></body></html>
```
Notes:
- Granularity is guided by "isolate the modes, keep core cohesive" — exact cut lines are chosen so concatenation is byte-identical; if a mode's CSS/JS is interleaved with core in a way that would break the invariant, keep that slice in a `core`/`chrome` partial rather than forcing a split (the invariant wins over tidiness for v1).
- The `manifest.txt` lists every partial in assembly order, e.g.:
```
00-head.html
css/10-base.css
css/20-chrome.css
css/30-fullscreen.css
modes/amp/amp.css
css/40-theme.css
css/49-responsive.css
40-body.html
modes/np/np.html
modes/amp/amp.html
59-popovers.html
70-script.html
modes/amp/amp.js
90-boot.html
```
- Write every partial with `newline=""` (exact bytes).

- [ ] **Step 4: Prove byte-identical**

```bash
python3 - <<'PY'
from draai.ui import assemble_ui
got = assemble_ui("ui")
snap = open("/tmp/player_ui.snapshot.html","r",encoding="utf-8",newline="").read()
print("IDENTICAL" if got == snap else "DIFFERS len %d vs %d" % (len(got), len(snap)))
if got != snap:
    for i,(a,b) in enumerate(zip(got,snap)):
        if a!=b: print("first diff at char", i, repr(got[max(0,i-40):i+10])); break
PY
```
Expected: `IDENTICAL`. If not, adjust the slice boundaries and repeat until identical.

- [ ] **Step 5: Add the durable assembly test** in `tests/test_draai.py`:

```python
    def test_ui_assembles(self):
        html = sp.assemble_ui(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(sp.__file__ if hasattr(sp,"__file__") else "."))), "ui")) \
            if False else __import__("draai.ui", fromlist=["assemble_ui"]).assemble_ui(
                os.path.join(REPO_ROOT, "ui"))
        self.assertTrue(html.lstrip().lower().startswith("<!doctype html"))
        self.assertIn("</html>", html)
        for marker in ('id="np"', 'id="amp"', 'function tick', 'function boot', '--ac:'):
            self.assertIn(marker, html)
```
Define `REPO_ROOT` near the top of the test module: `REPO_ROOT = os.path.dirname(os.path.abspath(__file__)) + "/.."` (the tests live in `tests/`, so its parent is the repo root). Simplify the call to `from draai.ui import assemble_ui; html = assemble_ui(os.path.join(REPO_ROOT, "ui"))`.

- [ ] **Step 6: Run the suite**

Run: `python3 tests/test_draai.py`
Expected: OK (26 tests). (`player_ui.html` is still committed and still served the old way — nothing wired yet.)

- [ ] **Step 7: Commit**

```bash
git add draai/ui.py ui tests/test_draai.py
git commit -m "add ui assembler + carve player_ui.html into ui/ partials"
```

---

### Task 2: Wire assembly into `_load_ui` + `build.py`; retire the committed file

**Files:**
- Modify: `draai/server.py` (`_load_ui`), `build.py`, `.gitignore`, delete `player_ui.html`
- Test: `tests/test_draai.py`

**Interfaces:**
- Consumes: `assemble_ui` (Task 1).
- Produces: `_load_ui` serves the assembled UI from `ui/` in a source checkout; `build.py` bakes it into the `.pyz`.

- [ ] **Step 1: Update `_load_ui`** (`draai/server.py:27-42`) to insert assembly between the override and the packaged fallback:

```python
def _load_ui():
    """The web UI. An external player_ui.html in the cwd wins (drop-in
    override); else assemble it from the ui/ partials in a source checkout;
    else the copy baked into the package (.pyz); else the built-in PAGE."""
    ext = os.path.join(os.getcwd(), "player_ui.html")
    if os.path.isfile(ext):
        try:
            with open(ext, "r", encoding="utf-8", newline="") as f:
                return f.read()
        except Exception:
            pass
    ui_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
    if os.path.isdir(ui_dir):
        try:
            from draai.ui import assemble_ui
            return assemble_ui(ui_dir)
        except Exception:
            pass
    try:
        import importlib.resources as res
        return res.files("draai").joinpath("player_ui.html").read_text("utf-8")
    except Exception:
        return PAGE
```
(`os.path.dirname(os.path.dirname(abspath(__file__)))` = repo root when running from source; in a `.pyz` that dir doesn't exist → falls to the packaged file.)

- [ ] **Step 2: Update `build.py`** — replace the top-level `player_ui.html` copy (`build.py:25-27`) with an assemble step:

```python
        from draai.ui import assemble_ui
        ui_dir = os.path.join(HERE, "ui")
        if os.path.isdir(ui_dir):
            with open(os.path.join(pkg, "player_ui.html"), "w", encoding="utf-8", newline="") as f:
                f.write(assemble_ui(ui_dir))
```
(Keep the `remote.html` copy that follows it unchanged. `build.py` already imports/omits as needed — add `import sys, os` are present; `assemble_ui` import is local.)

- [ ] **Step 3: Delete the committed file + ignore it**

```bash
git rm player_ui.html
```
Append to `.gitignore` (under the existing `draai/player_ui.html` note):
```
# built UI: assembled from ui/ partials by draai/ui.py (dev) and build.py (.pyz)
/player_ui.html
```

- [ ] **Step 4: Add the `_load_ui` serve + override test** in `tests/test_draai.py` (mirror `test_api_roundtrip`'s live server):

```python
    def test_root_serves_assembled_ui(self):
        httpd = sp.ThreadingHTTPServer(("127.0.0.1", 0), sp.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5) as r:
                body = r.read().decode("utf-8")
                self.assertIn('id="amp"', body)
                self.assertIn("function tick", body)
        finally:
            httpd.shutdown()
```

- [ ] **Step 5: Verify dev serve is byte-identical to the snapshot** (clone→run path):

```bash
python3 - <<'PY'
from draai.server import _load_ui
import os
os.chdir("/tmp")  # ensure no cwd player_ui.html override; ui/ resolved package-relative
got = _load_ui()
snap = open("/tmp/player_ui.snapshot.html","r",encoding="utf-8",newline="").read()
print("SERVE IDENTICAL" if got==snap else "DIFFERS")
PY
```
Expected: `SERVE IDENTICAL`.

- [ ] **Step 6: `.pyz` smoke** (baked file served):

```bash
python3 build.py
python3 draai.pyz --headless &
sleep 3
curl -s http://localhost:8765/ | grep -o 'id="amp"' | head -1
curl -s -o /dev/null -w "root %{http_code}\n" http://localhost:8765/
kill %1
```
Expected: `id="amp"` present, `root 200`. (No playback triggered.) If ports 8765–8767 are taken by other instances, it falls back to a higher port — read the actual bound port from the banner.

- [ ] **Step 7: Full suite + Browser-pane spot check**

Run: `python3 tests/test_draai.py` → OK (27).
Then serve (`python3 -m draai --headless &` from repo root) and in the Browser pane confirm the main UI, Now Playing (⛶), and Amplifier (A) all look and behave exactly as before — the assembly is transparent. `kill %1`. No real playback.

- [ ] **Step 8: Commit**

```bash
git add draai/server.py build.py .gitignore tests/test_draai.py
git commit -m "serve UI assembled from ui/ partials; retire committed player_ui.html"
```

---

## Phase 2 — Spectrum mode (on the modular structure)

### Task 3: Shared `openMode` switching helper (behavior-identical)

**Files:**
- Modify: `ui/modes/np/np.js` or `ui/70-script.html`/`ui/90-boot.html` (wherever `openNp`/`openAmp`/`closeAmp` + the keyboard/resize handlers landed in the carve) — grep to locate.

**Interfaces:**
- Produces: `openMode(id, sizers)` — removes `.open` from all full-screen layers, adds it to `#<id>`, `requestAnimationFrame`s the given `sizers()`. `openNp`/`openAmp` delegate to it.

- [ ] **Step 1: Add `openMode` + refactor the openers.** Replace the current `openAmp`/`closeAmp`/`openNp` with:

```javascript
const FS_IDS=["np","amp"];   // spectrum appended in Task 4
function openMode(id, sizers){
  FS_IDS.forEach(x=>$("#"+x).classList.remove("open"));
  $("#"+id).classList.add("open");
  requestAnimationFrame(()=>{ (sizers||[]).forEach(s=>s()); });
}
function openNp(){ openMode("np",[()=>sizeCanvas(npCv)]); armIdle(); loadAccess(); }
function openAmp(){ $("#amp").classList.toggle("lampon",ampLamp); openMode("amp",[()=>sizeCv(vuLc),()=>sizeCv(vuRc),()=>sizeCanvas(ampCv)]); }
function closeAmp(){ $("#amp").classList.remove("open"); }
```
(Behavior identical to today: opening one closes the others; NP still `armIdle`+`loadAccess`; amp still toggles the lamp + sizes its canvases.)

- [ ] **Step 2: Verify (DEMO, Browser pane)** — serve `python3 -m draai --headless &` (or `http.server` on the assembled output), open the UI at desktop size, and confirm NP↔Amp switching, `F`/`A`/`Esc`, and one-full-screen-at-a-time all behave exactly as before. No console errors. `python3 tests/test_draai.py` → OK. No real playback.

- [ ] **Step 3: Commit**

```bash
git add ui
git commit -m "factor full-screen switching into a shared openMode helper"
```

---

### Task 4: Spectrum analyzer triplet

**Files:**
- Create: `ui/modes/spectrum/spectrum.css`, `spectrum.html`, `spectrum.js`
- Modify: `ui/manifest.txt`; the dock markup partial (add `#specBtn`); the NP + amp markup partials (add `npToSpec` / `ampToSpec`); the core/boot JS (canvas init, `tick` `drawSpec` call, `resize`, keyboard `S`, `applyStatus` spec lines, wiring)

**Interfaces:**
- Consumes: `sm`, `playing`, `posSec`, `durSec`, `clamp`, `fmt`, `drawWave`, `sizeCanvas`, `mkCanvas`, `REDUCE`, `cmd`, `openMode`, `openNp`, `openAmp`, `applyStatus`.
- Produces: `drawSpec`, `bandCurve`, `rrect`, `openSpec`; `#spec` mode wired into the mesh + `F/A/S/Esc`.

- [ ] **Step 1: `ui/modes/spectrum/spectrum.css`:**

```css
/* ---------------- spectrum analyzer mode ---------------- */
.spec{position:fixed;inset:0;z-index:41;transform:translateY(101%);transition:transform .6s cubic-bezier(.16,1,.3,1);overflow:hidden;
  display:flex;flex-direction:column;color:#e9edf0;background:radial-gradient(120% 90% at 50% 120%,#0d1a1a,#06070a 66%)}
.spec.open{transform:none}
.spec .spechead{display:flex;align-items:center;gap:20px;padding:22px 40px}
.spec .spechead .lbl{font-size:11px;letter-spacing:.24em;color:#6f7d80;text-transform:uppercase}
.spec .spechead .lbl b{color:#5EEAD4;font-weight:600}
.spec .iconbtn{color:#6f7d80}.spec .iconbtn:hover{color:#e9edf0;background:rgba(255,255,255,.06);border-color:rgba(255,255,255,.1)}
.specstage{flex:1;min-height:0;padding:6px 40px 10px}
.specstage canvas{width:100%;height:100%;display:block}
.specfoot{display:flex;align-items:center;gap:28px;padding:14px 40px 30px}
.specfoot .track{min-width:170px}
.specfoot .track b{font-size:15px;font-weight:560;color:#e9edf0;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.specfoot .track small{font-size:12.5px;color:#6f7d80;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.spec .ampctrls{margin-left:auto}
.spec .cbtn{color:#8fa0a2}.spec .cbtn:hover{color:#e9edf0}
.spec .play{background:#5EEAD4;color:#06070a;box-shadow:0 4px 18px rgba(94,234,212,.35)}
@media(max-width:820px){.spec{display:none}}
```

- [ ] **Step 2: `ui/modes/spectrum/spectrum.html`:**

```html
<!-- spectrum analyzer mode — FFT-style bars from low/mid/high bands (desktop showpiece, always dark) -->
<section class="spec" id="spec">
  <div class="spechead">
    <div class="lbl">SPECTRUM · <b id="specSpeaker">—</b></div>
    <div style="display:flex;gap:8px;margin-left:auto">
      <button class="iconbtn" id="specToNp" title="Now playing view (F)"><svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"></circle><circle cx="12" cy="12" r="2.6"></circle></svg></button>
      <button class="iconbtn" id="specToAmp" title="Amplifier view (A)"><svg class="ic" viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"></rect><circle cx="8" cy="12" r="2.4"></circle><circle cx="16" cy="12" r="2.4"></circle></svg></button>
      <button class="iconbtn" id="specClose" title="Close (Esc)"><svg class="ic" viewBox="0 0 24 24"><path d="M6 18 18 6M6 6l12 12"></path></svg></button>
    </div>
  </div>
  <div class="specstage"><canvas id="specCanvas"></canvas></div>
  <div class="specfoot">
    <div class="track"><b id="specTitle">Nothing playing</b><small id="specArtist"></small></div>
    <div class="ampseek" style="flex:1;max-width:520px"><div class="wave" id="specWave"><canvas></canvas></div>
      <div class="nprow"><span class="t mono" id="specPos">0:00</span><span class="t mono" id="specDur">0:00</span></div></div>
    <div class="ampctrls">
      <button class="cbtn" data-cmd="prev"><svg class="ic fill" viewBox="0 0 24 24"><rect x="5" y="5" width="2.3" height="14" rx="1"></rect><path d="M20 6v12a1 1 0 0 1-1.6.8L9 12l9.4-6.8A1 1 0 0 1 20 6z"></path></svg></button>
      <button class="play" id="specPlay"><svg class="ic" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"></path></svg></button>
      <button class="cbtn" data-cmd="next"><svg class="ic fill" viewBox="0 0 24 24"><path d="M4 6v12a1 1 0 0 0 1.6.8L15 12 5.6 5.2A1 1 0 0 0 4 6z"></path><rect x="16.7" y="5" width="2.3" height="14" rx="1"></rect></svg></button>
    </div>
  </div>
</section>
```

- [ ] **Step 3: `ui/modes/spectrum/spectrum.js`** (the draw + open; `rrect` is a small rounded-rect helper this needs):

```javascript
/* ---------------- spectrum analyzer ---------------- */
let specC, specW2, SPEC_N=56, specVals=new Float32Array(SPEC_N), specPk=new Float32Array(SPEC_N);
function bandCurve(f,low,mid,high){ // f: 0..1 across the spectrum
  const L=Math.exp(-Math.pow((f-0.05)/0.16,2)), M=Math.exp(-Math.pow((f-0.42)/0.26,2)), H=Math.exp(-Math.pow((f-0.9)/0.3,2));
  const roll=1-0.28*f; // gentle high-end rolloff
  return clamp((low*L+mid*M*0.95+high*H*0.9)*roll,0,1.15);
}
function rrect(ctx,x,y,w,h,r){ if(h<=0){ctx.beginPath();return;} r=Math.min(r,w/2,h/2); ctx.beginPath(); ctx.moveTo(x+r,y); ctx.arcTo(x+w,y,x+w,y+h,r); ctx.arcTo(x+w,y+h,x,y+h,r); ctx.arcTo(x,y+h,x,y,r); ctx.arcTo(x,y,x+w,y,r); ctx.closePath(); }
function drawSpec(dt,t,prog){ if(!specC) return; if(!specC.W) sizeCanvas(specC); const {ctx,W,H}=specC; ctx.clearRect(0,0,W,H);
  const on=playing&&!REDUCE, low=playing?sm.low:0, mid=playing?sm.mid:0, high=playing?sm.high:0;
  const floor=H-26, gap=Math.max(2,W/SPEC_N*0.22), bw=W/SPEC_N-gap;
  const acc=[94,234,212];
  for(let i=0;i<SPEC_N;i++){ const f=i/(SPEC_N-1);
    const jitter=on?(0.5+0.5*Math.sin(t/150+i*0.7)+0.35*Math.sin(t/70+i*1.9))/1.35:0;
    const tgt=clamp(bandCurve(f,low,mid,high)*(0.55+0.6*jitter),0,1.1);
    const atk=1-Math.exp(-dt/0.045), dec=1-Math.exp(-dt/0.16);
    specVals[i]+= tgt>specVals[i]?(tgt-specVals[i])*atk:(tgt-specVals[i])*dec;
    specPk[i]=Math.max(specVals[i], specPk[i]-dt*0.6);
    const v=clamp(specVals[i],0,1.1), h=v*(floor-10), x=i*(bw+gap)+gap/2;
    const g=ctx.createLinearGradient(0,floor,0,floor-h);
    g.addColorStop(0,`rgb(${acc[0]} ${acc[1]} ${acc[2]} / .35)`); g.addColorStop(.55,`rgb(${acc[0]} ${acc[1]} ${acc[2]})`);
    g.addColorStop(1, v>0.86?"#ff6b5a":`rgb(180 250 235)`);
    ctx.fillStyle=g; rrect(ctx,x,floor-h,bw,h,Math.min(3,bw/2)); ctx.fill();
    ctx.globalAlpha=0.12; ctx.fillStyle=`rgb(${acc[0]} ${acc[1]} ${acc[2]})`; rrect(ctx,x,floor+3,bw,Math.min(h*0.4,26),Math.min(3,bw/2)); ctx.fill(); ctx.globalAlpha=1;
    const py=floor-clamp(specPk[i],0,1.1)*(floor-10); ctx.fillStyle=specPk[i]>0.86?"#ff6b5a":"#dffaf3"; ctx.fillRect(x,py-2,bw,2);
  }
  ctx.fillStyle="rgba(255,255,255,.07)"; ctx.fillRect(0,floor+1,W,1);
  if(specW2){ drawWave(specW2,prog); $("#specPos").textContent=fmt(posSec); $("#specDur").textContent=fmt(durSec); }
}
function openSpec(){ openMode("spec",[()=>sizeCanvas(specC),()=>sizeCanvas(specW2)]); }
```

- [ ] **Step 4: Register the triplet in `ui/manifest.txt`** — add `modes/spectrum/spectrum.css` (after `modes/amp/amp.css`), `modes/spectrum/spectrum.html` (after `modes/amp/amp.html`), `modes/spectrum/spectrum.js` (after `modes/amp/amp.js`).

- [ ] **Step 5: Wire it into the core** (grep for each anchor in the carved core/boot partial):
  - `FS_IDS`: `["np","amp","spec"]`.
  - Canvas init (where `ampCv=mkCanvas("#ampWave")…`): add `specC=mkCanvas(".specstage"); specW2=mkCanvas("#specWave");`
  - `tick` (after the `drawAmp` line): `if($("#spec").classList.contains("open")) drawSpec(dt,t,prog);`
  - `resize` handler: add `if($("#spec").classList.contains("open")){ sizeCanvas(specC); sizeCanvas(specW2); }`
  - Keyboard (after the `a`/`A` branch): `else if(e.key==="s"||e.key==="S"){ if($("#spec").classList.contains("open")) $("#spec").classList.remove("open"); else openSpec(); }` and extend the `Escape` branch to also `$("#spec").classList.remove("open")`.
  - `applyStatus` (beside the amp readouts): `$("#specSpeaker").textContent=spk; $("#specTitle").textContent=A.status.title||"Nothing playing"; $("#specArtist").textContent = <same artist source as #ampArtist>; $("#specPlay").querySelector(".ic").innerHTML=pp; $("#specPlay").querySelector(".ic").classList.toggle("fill",playing);`
  - Wiring (beside the amp buttons): `$("#specBtn").addEventListener("click",openSpec); $("#npToSpec").addEventListener("click",openSpec); $("#ampToSpec").addEventListener("click",openSpec); $("#specToNp").addEventListener("click",openNp); $("#specToAmp").addEventListener("click",openAmp); $("#specClose").addEventListener("click",()=>$("#spec").classList.remove("open")); $("#specPlay").addEventListener("click",togglePlay);`

- [ ] **Step 6: Add the dock + header buttons** in the markup partials:
  - Dock (beside `#ampBtn`): `<button class="cbtn" id="specBtn" title="Spectrum (S)"><svg class="ic" viewBox="0 0 24 24"><path d="M4 20V11M9 20V4M14 20V13M19 20V7"></path></svg></button>`
  - NP header (beside `#npToAmp`): `<button class="iconbtn" id="npToSpec" title="Spectrum view (S)"><svg class="ic" viewBox="0 0 24 24"><path d="M4 20V11M9 20V4M14 20V13M19 20V7"></path></svg></button>`
  - Amp header (beside `#ampToNp`): `<button class="iconbtn" id="ampToSpec" title="Spectrum view (S)"><svg class="ic" viewBox="0 0 24 24"><path d="M4 20V11M9 20V4M14 20V13M19 20V7"></path></svg></button>`
  - Hide `#specBtn` under 820px (add to the existing `@media(max-width:820px){#ampBtn{display:none}}` rule): `#ampBtn,#specBtn{display:none}`.

- [ ] **Step 7: Verify (DEMO, Browser pane, desktop, NO real playback)** — serve and press `S` (or the dock spectrum button). Confirm: ~56 bars with an FFT-like shape (low/mid/high shaped, not three blobs) responding to demo energy; per-bar ballistics (fast rise / slow fall) + hovering peak caps; accent→white→red-hot gradient + faint reflection; bars flat when paused; mode mesh works from every header (NP↔Amp↔Spec) and `F`/`A`/`S`/`Esc`; only one full-screen open at a time; `#spec` + `#specBtn` hidden under 820px. No console errors. `python3 tests/test_draai.py` → OK.

- [ ] **Step 8: Commit**

```bash
git add ui
git commit -m "add spectrum analyzer full-screen mode"
```

---

## Self-review

**Spec coverage:** `ui/` partials + ordered-manifest assembler → Task 1. Server-side assembly in `_load_ui` (clone→run, edit→reload) + `build.py` bake + retire/ignore the committed file → Task 2. Byte-identical invariant (the refactor gate) → Task 1 Step 4 + Task 2 Step 5. Durable assembly/serve tests → Task 1 Step 5, Task 2 Step 4. `openMode` shared switching → Task 3. Spectrum triplet (bars/bandCurve/ballistics/peak-hold/gradient/reflection, furniture, mesh + `F/A/S/Esc`, dock/header, desktop-only, reduced-motion via `sm=0` + `on` gate) → Task 4. CLAUDE.md rule #2 → (fold into Task 2 or a docs step; add to Task 2's commit). Two-phase de-risking (byte-identical proven before Spectrum) → phase split.

**Placeholder scan:** Assembler, `_load_ui`, `build.py`, `openMode`, and the full spectrum triplet are complete code. The carve boundaries (Task 1 Step 3) are deliberately implementer-chosen against a hard byte-identical gate rather than pre-listed line numbers — this is correct (the file will have shifted; the gate is the spec), not a placeholder. The `applyStatus` artist source (Task 4 Step 5) references the existing `#ampArtist` assignment. Add the CLAUDE.md rule-#2 edit explicitly to Task 2.

**Type/name consistency:** `assemble_ui(ui_dir)` defined in Task 1, consumed by `_load_ui`/`build.py` in Task 2 and the tests. `openMode(id,sizers)` defined in Task 3, used by `openNp`/`openAmp` (Task 3) and `openSpec` (Task 4); `FS_IDS` grows `np,amp`→`np,amp,spec`. `specC`/`specW2`/`SPEC_N`/`specVals`/`specPk`/`bandCurve`/`rrect`/`drawSpec`/`openSpec` defined in the spectrum triplet and referenced by the core wiring (canvas init, `tick`, `resize`, keyboard, `applyStatus`, listeners). Shared symbols (`sm`, `drawWave`, `sizeCanvas`, `mkCanvas`, `cmd`, `togglePlay`, `clamp`, `fmt`, `REDUCE`, `posSec`, `durSec`, `playing`) are pre-existing.
```
