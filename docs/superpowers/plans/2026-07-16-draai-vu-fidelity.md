# DRAAI VU-meter fidelity (A+B+C+D+E) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Amplifier VU needles track the music tighter, read like a real VU meter, and move as an authentic stereo pair — by de-smoothing + perceptually mapping + time-aligning the meter (A/B/C, UI), and by making the analysis finer and stereo (D/E, backend, one re-analysis).

**Architecture:** Two isolated changes. **Backend (D+E):** `draai/analysis.py` drops the frame to 30 ms, makes the amp pass a stereo decode emitting `ampL`/`ampR`, and bumps `ANALYSIS_VERSION` (one lazy re-analysis wave). **Frontend (A+B+C+E):** `player_ui.html`'s `drawAmp` reads its own raw energy at `posSec+VU_LATENCY` (not the shared 90 ms `sm`), maps it through a dB window, and drives each needle from its real channel; `applyStatus` gains a gentle per-poll clock nudge. The UI is otherwise untouched.

**Tech Stack:** Python 3 stdlib + ffmpeg (already a runtime-detected optional tool) for analysis; inline vanilla JS / Canvas for the UI. No new dependencies, no new endpoints.

## Global Constraints

- Pure Python **standard library** for the engine; ffmpeg stays a *detected* optional tool (`find_tool`), never required. `player_ui.html` stays ONE self-contained file (inline, no CDN/fonts/storage, offline).
- **No new backend endpoints.** `/api/analysis` keeps its shape; only its arrays get denser + gain `ampL`/`ampR`.
- **One `ANALYSIS_VERSION` bump** covers D and E; caches recompute lazily per track on next play (no bulk job). Tracks with no analysis stay graceful (needles rest).
- Meter energy comes only from `/api/analysis` via the playback clock — never real audio, never `Math.random`. The mono bands `low/mid/high` (background glow + dock EQ-mark) and the 240-bucket waveform are unchanged.
- `prefers-reduced-motion` (`REDUCE`) still eases gently. Amp stays desktop-only, amber, one-full-screen-at-a-time — untouched here.
- `python3 tests/test_draai.py` (25) stays green. **No real-device playback in testing** — analysis is an offline file decode (safe); UI is verified in DEMO mode. Never start audio on hardware.
- Commit after each task, SHORT title, NO trailers.

## File structure

- **Modify `draai/analysis.py`** — add `_stream_envelope_stereo`, rework the amp step of `_analyze` to stereo + `ampL`/`ampR`, bump `ANALYSIS_STEP` + `ANALYSIS_VERSION`.
- **Modify `tests/test_draai.py`** — add an ffmpeg-guarded stereo-analysis test.
- **Modify `player_ui.html`** — amp constants block, `vuMap`, `energyAt` `ampL/ampR`, `applyStatus` soft nudge, `drawAmp` rewrite, and demo stereo for verification.

Reference (verified 2026-07-16): `analysis.py` — `_stream_envelope` 28-66, `_analyze` 68-100, constants `ANALYSIS_SR=8000`/`ANALYSIS_STEP=0.1`/`ANALYSIS_VERSION=2`/`PEAK_BUCKETS=240` lines 13-17. `player_ui.html` — `energyAt` 824-826, `tick` 831-848 (`sm` 830, `ek` 838, amp call 848), `drawAmp` 880-892, `applyStatus` resync 936.

---

### Task 1: Backend — finer + stereo analysis (D+E)

**Files:**
- Modify: `draai/analysis.py`
- Test: `tests/test_draai.py`

**Interfaces:**
- Produces: `_stream_envelope_stereo(ffmpeg, path) -> (list, list)`; `_analyze` output JSON gains `"ampL"`, `"ampR"` (0..100 ints, same length as `amp`), `"step": 0.03`, `"v": 3`.

- [ ] **Step 1: Bump the analysis constants** (`draai/analysis.py:14,16`):

```python
ANALYSIS_STEP = 0.03         # seconds per frame (was 0.1 — finer transients)
```
```python
ANALYSIS_VERSION = 3         # bump to invalidate older caches (finer + stereo)
```

- [ ] **Step 2: Add the stereo envelope function** right after `_stream_envelope` (after line 66):

```python
def _stream_envelope_stereo(ffmpeg, path):
    """Decode STEREO and reduce to per-channel max-abs envelopes, streaming.

    Same constant-memory approach as _stream_envelope, but keeps both
    channels: s16le stereo is interleaved L,R,L,R... so per window the
    even int16s are left, the odd are right.
    """
    import array
    cmd = [ffmpeg, "-v", "error", "-t", str(ANALYSIS_MAX_SEC), "-i", path,
           "-ac", "2", "-ar", str(ANALYSIS_SR), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    win_bytes = int(ANALYSIS_SR * ANALYSIS_STEP) * 2 * 2   # frames * 2ch * 2 bytes
    outL, outR, buf = [], [], b""
    try:
        while True:
            chunk = proc.stdout.read(1 << 16)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= win_bytes:
                seg, buf = buf[:win_bytes], buf[win_bytes:]
                a = array.array("h")
                a.frombytes(seg)
                if sys.byteorder == "big":
                    a.byteswap()
                left, right = a[0::2], a[1::2]
                outL.append(max(max(left), -min(left), 1))
                outR.append(max(max(right), -min(right), 1))
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()
    if not outL:
        raise RuntimeError("could not decode audio")
    return outL, outR
```

- [ ] **Step 3: Rework the amp step of `_analyze`** — replace the current mono amp lines (`raw_amp = _stream_envelope(ffmpeg, track["path"])` / `peak = ...` / `amp = _scale(raw_amp, peak)`, `draai/analysis.py:74-76`) with:

```python
        raw_L, raw_R = _stream_envelope_stereo(ffmpeg, track["path"])
        raw_amp = [l if l >= r else r for l, r in zip(raw_L, raw_R)]  # mono peak = per-window max(L,R)
        peak = max(raw_amp) if raw_amp else 1
        amp = _scale(raw_amp, peak)
        ampL = _scale(raw_L, peak)     # per-channel, scaled to the same full-band peak
        ampR = _scale(raw_R, peak)
```
Leave the three band passes (`low`, `mid`, `high`) exactly as they are — they stay mono.

- [ ] **Step 4: Emit `ampL`/`ampR`** — in the `data = {...}` dict (`draai/analysis.py:~98`), add them to the arrays line:

```python
            "amp": amp, "low": low, "mid": mid[:frames], "high": high[:frames],
            "ampL": ampL[:frames], "ampR": ampR[:frames],
```

- [ ] **Step 5: Write the failing test** in `tests/test_draai.py` (inside `DraaiTests`) — an ffmpeg-guarded test that builds a hard-panned stereo WAV (left loud, right quiet) and asserts the analysis separates the channels:

```python
    @unittest.skipUnless(sp.find_tool("ffmpeg"), "ffmpeg not installed")
    def test_stereo_analysis_channels(self):
        import wave, struct
        path = os.path.join(self.tmp, "pan.wav")
        sr, n = 8000, 8000  # 1 second
        with wave.open(path, "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
            frames = b"".join(struct.pack("<hh", 30000, 2000) for _ in range(n))  # L loud, R quiet
            w.writeframes(frames)
        d = sp._analyze({"id": "pan", "path": path})
        self.assertEqual(d["step"], 0.03)
        self.assertIn("ampL", d); self.assertIn("ampR", d)
        self.assertEqual(len(d["ampL"]), len(d["amp"]))
        self.assertEqual(len(d["ampR"]), len(d["amp"]))
        self.assertLessEqual(len(d["peaks"]), 240)
        # left channel is much louder than right
        self.assertGreater(sum(d["ampL"]), sum(d["ampR"]) * 3)
```
(`sp._analyze` and `sp.find_tool` resolve via the test's `sp` namespace, which already merges `draai.analysis`'s names.)

- [ ] **Step 6: Run the test to verify it fails, then passes**

Run: `python3 tests/test_draai.py -k test_stereo_analysis_channels -v`
Expected before Steps 1-4: FAIL (no `ampL`, or `step`≠0.03). After: PASS. If ffmpeg is absent it SKIPS (still counts as not-failed) — note that in the report.

- [ ] **Step 7: Real-track spot check (offline, no playback)** — analyze one real library file directly and eyeball the output shape:

```bash
python3 - <<'PY'
import draai.analysis as A, json
d = A._analyze({"id":"chk","path":"<pick any real audio file path>"})
print("step", d["step"], "v", d["v"], "frames", len(d["amp"]),
      "ampL", len(d.get("ampL",[])), "ampR", len(d.get("ampR",[])), "peaks", len(d["peaks"]))
PY
```
Expected: `step 0.03`, `v 3`, `ampL`/`ampR` equal length to `amp`, `peaks` ≤ 240. (This decodes a file offline — it does NOT touch any speaker.)

- [ ] **Step 8: Run the full suite + commit**

Run: `python3 tests/test_draai.py`  → OK (26 tests; the new one runs where ffmpeg exists).
```bash
git add draai/analysis.py tests/test_draai.py
git commit -m "analysis: finer 30ms frames + real stereo ampL/ampR"
```

---

### Task 2: Frontend — de-smooth, dB map, clock nudge, real stereo (A+B+C+E)

**Files:**
- Modify: `player_ui.html`

**Interfaces:**
- Consumes: `energyAt`, `posSec`, `playing`, `clamp`, `REDUCE`, `vuL/vuR/peakLT/peakRT`, `drawVU`, `ampCv`, `drawWave`, `fmt`, `A.status` (Task-1 analysis now carries `ampL/ampR`).
- Produces: an amp-tuning constants block; `vuMap(v)`; `energyAt` returning `ampL/ampR`; a soft clock nudge in `applyStatus`; a rewritten `drawAmp`.

- [ ] **Step 1: Add the amp-tuning constants + `vuMap`** — right above `function drawAmp` (search for it; ~line 880). This is the single labeled knob block:

```javascript
/* ---- amp meter tuning (A/B/C) ---- */
const VU_LATENCY = 0.0;    // s — meter energy read offset vs the clock (C2); room-align knob, 0 = off
const SYNC_NUDGE = 0.25;   // fraction of sub-threshold drift corrected per poll (C1)
const VU_DB_FLOOR = -38;   // dB — bottom of the perceptual window (B)
const VU_EPS = 1e-3;       // log floor (B)
function vuMap(v){ const db = 20*Math.log10(Math.max(v, VU_EPS));
  return clamp((db - VU_DB_FLOOR) / (0 - VU_DB_FLOOR), 0, 1.10); }   // 0..1.1, >1 keeps red zone / PEAK alive
```

- [ ] **Step 2: Return `ampL`/`ampR` from `energyAt`** (`player_ui.html:826`) — replace the `return {...}` line:

```javascript
  return {amp:g(analysis.amp),low:g(analysis.low),mid:g(analysis.mid),high:g(analysis.high),
          ampL:analysis.ampL?g(analysis.ampL):g(analysis.amp),
          ampR:analysis.ampR?g(analysis.ampR):g(analysis.amp)}; }
```
(Fallback to mono `amp` for tracks not yet re-analyzed so both needles still move.)

- [ ] **Step 3: Add the soft clock nudge (C1)** — in `applyStatus` (`player_ui.html:936`), replace:

```javascript
  if(Math.abs(np-posSec)>1.6 || !playing) posSec=np; // resync when drift or paused
```
with:

```javascript
  if(Math.abs(np-posSec)>1.6 || !playing) posSec=np;              // hard snap on seek/track-change/paused
  else posSec += (np-posSec)*SYNC_NUDGE;                          // else ease out sub-threshold drift (C1)
```

- [ ] **Step 4: Rewrite `drawAmp` (A+B+E)** — replace the whole function (`player_ui.html:880-892`) with:

```javascript
function drawAmp(dt,t,prog){
  // A: own raw read at the clock (+C offset), NOT the 90ms `sm`. B: perceptual map. E: real per-channel.
  const em = playing ? energyAt(posSec + VU_LATENCY) : null;
  let tL = em ? clamp(vuMap(em.ampL), 0, 1.12) : 0;
  let tR = em ? clamp(vuMap(em.ampR), 0, 1.12) : 0;
  const atk=REDUCE?0.2:1-Math.exp(-dt/0.05), dec=1-Math.exp(-dt/0.24);   // ~50ms attack, ~240ms decay
  vuL+= tL>vuL?(tL-vuL)*atk:(tL-vuL)*dec;
  vuR+= tR>vuR?(tR-vuR)*atk:(tR-vuR)*dec;
  drawVU(vuLc,vuL); drawVU(vuRc,vuR);
  peakLT=vuL>1.0?0.9:Math.max(0,peakLT-dt); peakRT=vuR>1.0?0.9:Math.max(0,peakRT-dt);
  $("#peakL").classList.toggle("hot",peakLT>0); $("#peakR").classList.toggle("hot",peakRT>0);
  if(ampCv){ drawWave(ampCv,prog); $("#ampPos").textContent=fmt(posSec); $("#ampDur").textContent=fmt(durSec); }
}
```
(The `t` param is now unused — the sine decorrelation and the `amp*0.9+band*0.28` split are gone, replaced by real stereo. Keep the signature so the `tick` call site is untouched.)

- [ ] **Step 5: Give DEMO analysis distinct `ampL`/`ampR`** so stereo divergence is visible without hardware — find the demo analysis synthesizer (grep `Demo` + `amp` / where a demo `analysis` object with an `amp` array is built) and add `ampL`/`ampR` derived deterministically from `amp` (no `Math.random`):

```javascript
      // demo stereo so the twin needles diverge in DEMO (deterministic, not random)
      ampL: amp.map((v,i)=>Math.min(100, v*(0.85+0.30*Math.sin(i/7)))),
      ampR: amp.map((v,i)=>Math.min(100, v*(0.85+0.30*Math.cos(i/9)))),
```
(Match the exact variable/field names used in the demo's analysis object; if the demo builds arrays differently, mirror its style. If the demo analysis is not an easy fit, note it and rely on Step 7's DEMO check with the fallback — L≈R — plus code inspection of the real-stereo path.)

- [ ] **Step 6: Run the suite**

Run: `python3 tests/test_draai.py` → OK (26). (UI change; the suite is unaffected.)

- [ ] **Step 7: Verify in the Browser pane (DEMO mode, desktop ~1280×800, NO real playback)** — serve standalone: `python3 -m http.server 8899` from `/Users/sasa/Dev/draai`, open `http://localhost:8899/player_ui.html`, press `A`. Confirm:
  - **A:** needles are visibly more responsive / punchier than the old silky motion (react to the demo energy peaks, not a slow glide).
  - **B:** needles spread across the dial — they lift off −20 in quiet stretches and stop pegging constantly; still settle to −20 at rest (pause).
  - **E:** the LEFT and RIGHT needles now move *independently* (the demo `ampL`/`ampR` differ), not in lockstep.
  - **C1:** the clock/seek stays smooth across the ~2 s status ticks (no visible jumps).
  - Unchanged: `prefers-reduced-motion` still eases gently (verify by inspection if the harness can't emulate); PEAK lamps still fire on peaks; the background glow + Now-Playing waveform behave exactly as before.
  No console errors. Kill your server after. (Other DRAAI instances hold ports 8765–8767 — don't touch them; never trigger real playback.)

- [ ] **Step 8: Commit**

```bash
git add player_ui.html
git commit -m "amp meters: de-smooth + dB map + clock nudge + real stereo"
```

---

## Self-review

**Spec coverage:** A (own raw read, not `sm`) → Task 2 Step 4. B (dB-window `vuMap`) → Task 2 Steps 1,4. C1 (soft clock nudge) → Task 2 Step 3; C2 (`VU_LATENCY` offset) → Task 2 Steps 1,4. D (30 ms + version bump) → Task 1 Steps 1,3,4. E (stereo decode + `ampL/ampR`, needles from real channels, band-weight+sine removed) → Task 1 Steps 2-4 + Task 2 Steps 2,4. Single `ANALYSIS_VERSION` bump covers D+E → Task 1 Step 1. Lazy re-analysis / graceful no-analysis / fallback to mono → Task 2 Step 2 fallback + existing prefetch. Tests green + stereo regression test → Task 1 Steps 5-8. Mono bands + waveform + background untouched → Task 1 leaves band passes and `peaks` as-is; Task 2 touches only `drawAmp`/`energyAt`/`applyStatus`/constants.

**Placeholder scan:** All code is complete and bound to verified symbols/line anchors. The one prose-guided step is Task 2 Step 5 (match the demo analysis object's field style) — it gives exact formulas and a documented fallback, not a TBD. No "handle appropriately" anywhere.

**Type/name consistency:** `_stream_envelope_stereo` returns `(outL,outR)`, consumed in `_analyze` as `raw_L,raw_R`; `ampL/ampR` (0..100 ints) flow analysis → JSON → `energyAt` (`g()`-scaled to 0..1) → `em.ampL/ampR` → `vuMap` → `tL/tR`. `vuMap`, `VU_LATENCY`, `SYNC_NUDGE`, `VU_DB_FLOOR`, `VU_EPS` are defined once in Task 2 Step 1 and used in Steps 3-4. `clamp`, `REDUCE`, `energyAt`, `posSec`, `vuL/vuR` are pre-existing. `ANALYSIS_STEP`/`ANALYSIS_VERSION` are the same constants read by the UI via `analysis.step` and cache validation.
