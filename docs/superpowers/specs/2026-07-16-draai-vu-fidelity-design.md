# DRAAI VU-meter fidelity (pass A+B+C+D+E) — design

**Date:** 2026-07-16
**Status:** Approved for planning
**Topic:** Make the Amplifier mode's twin VU needles track the music more tightly, read more like a real VU meter, and move as an authentic stereo pair — three client-side fixes plus a finer, now-stereo analysis.

## Purpose

The amp needles today feel loosely coupled to the music for four reasons the analysis identified: they consume an over-smoothed signal (A), they map energy to deflection linearly so they sit low and occasionally peg (B), they're timed off a playback clock that only hard-corrects past 1.6 s of drift (C), and the underlying analysis is only 100 ms-resolution so per-beat transients are averaged away before the needle ever sees them (D). A–C are fixed in `player_ui.html`; D is a small resolution bump in `draai/analysis.py` (100 ms → 30 ms) that re-analyzes the library lazily and compounds with A (the de-smoothed meter now has genuinely fine material to move on). Bundled into D's single re-analysis is **E — real per-channel stereo**: the amp pass decodes both channels, so the twin needles finally diverge authentically instead of showing the same mono signal split by a cosmetic band-weighting hack.

## Goals

- **A — Decouple the meter from the ambient smoothing.** Drive the needles from the raw per-frame analysis energy plus the VU ballistics only, instead of reusing the 90 ms-smoothed `sm` signal (which is tuned for the background glow + waveform).
- **B — Perceptual (dB-window) mapping.** Curve the energy→deflection mapping so the needles dance across the dial where the eye expects, instead of hugging the floor and pegging.
- **C — Tighter clock alignment + latency knob.** Continuously nudge the local clock toward the polled speaker position (instead of only snapping past 1.6 s), and add a single tunable latency offset applied to the meter's energy sampling so the visual can be aligned to what's heard in the room.
- **D — Finer analysis resolution.** Drop the analysis frame from 100 ms to **30 ms** in `draai/analysis.py` so the envelope captures per-beat transients (hi-hats, kicks) the 10 Hz frame currently averages away. Bump `ANALYSIS_VERSION` so caches recompute lazily on next play. The UI adapts automatically (it already reads `analysis.step`).
- **E — Real stereo.** Make the amp pass a stereo decode (`-ac 2`) and emit per-channel envelopes `ampL`/`ampR`; drive the two needles from their own channels (dropping the L→low / R→high band-weight hack and the sine wobble). Rides D's single re-analysis (same `ANALYSIS_VERSION` bump). Tracks whose stereo image is truly centered will still look near-together (correctly); wide/panned material now diverges for real.

## Non-goals

- No new backend endpoints; `/api/status` still polls every 2 s and `/api/analysis` is unchanged in shape (only the arrays get denser).
- No change to the analysis **sample rate** (8 kHz) or the band split (250 / 250–2000 / 2000 Hz) — D changes temporal resolution, E adds channels; SR is a separate future knob.
- No bulk re-analysis job — caches recompute lazily per track on next play (existing prefetch behavior), driven by the single `ANALYSIS_VERSION` bump that covers D **and** E.
- The mono bands `low/mid/high` are still computed (the background glow and the dock EQ-mark read `--e-low/mid/high`); E adds `ampL/ampR` alongside them, it doesn't replace them.
- The Now-Playing waveform, background glow, and seek bar behavior are unchanged — only the amp needles' data path (A–C, E) and the analysis density (D) change.

## Background — the current meter data path

```
energyAt(posSec)            // raw interpolated 0..1 energy {amp,low,mid,high} from the 100ms frames  [player_ui.html:824]
  → sm[k] += (e[k]-sm[k])*(1-e^(-dt/0.09))   // 90ms smoothing, SHARED with background+waveform     [:838]
  → drawAmp reads sm.amp/low/high            // meters inherit the 90ms smoothing                    [:881]
  → tL=clamp(amp*0.9+low*0.28,0,1.12), tR=... ; VU ballistics attack 50ms / decay 240ms
Clock: posSec += dt each frame; hard-resync only when |polled - posSec| > 1.6s or paused             [:936]
```
`energyAt` returns 0..1 floats; `sm` and `e` share that range, so swapping the source is range-consistent.

## Design

### A — Dedicated raw meter source

- In the render loop, keep `sm` exactly as-is for the background/waveform. Give the meters their **own** energy read: `drawAmp` computes `const em = playing ? energyAt(posSec + VU_LATENCY) : null;` (the `+ VU_LATENCY` is pass C) and uses `em.amp/low/high` — **not** `sm` — as the ballistics target.
- Only the VU ballistics (attack `1-e^(-dt/0.05)`, decay `1-e^(-dt/0.24)`) smooth the needle; the 90 ms `sm` low-pass is removed from the meter path, restoring transient punch.
- `energyAt` linearly interpolates the 100 ms frames, so `em` is already free of stair-steps; no extra pre-smoothing is added (if the raw signal reads too twitchy in testing, a ≤20 ms micro-smooth is the fallback — noted as a tuning knob, not a default).
- Pass `em` (or the raw `e` already computed in `tick`, offset-adjusted) into `drawAmp` rather than recomputing twice per frame where practical.

### B — Perceptual dB-window mapping

- Map each channel's linear 0..1 energy through a dB window before the stereo split + ballistics, so deflection is perceptual:
  ```
  vuMap(v) = clamp( (20*log10(max(v, EPS)) - DB_FLOOR) / (0 - DB_FLOOR), 0, 1.10 )
  ```
  with `EPS = 1e-3`, `DB_FLOOR = -38` (dB). This maps roughly −38 dB…0 dB → 0…1, the classic VU spread; values can reach ~1.1 so the red zone + PEAK-lamp semantics (needle pins past 1.0) still fire on real peaks.
- Apply `vuMap` to each needle's channel energy. With E in scope the inputs are the real per-channel `ampL`/`ampR`: `tL = clamp(vuMap(em.ampL), 0, 1.12)`, `tR = clamp(vuMap(em.ampR), 0, 1.12)`. `DB_FLOOR` is tunable during build. (Without E the same `vuMap` would wrap the old `amp*0.9 + low/high*0.28` split — but E replaces that.)
- Net effect: quiet passages lift off the floor, loud passages stop pegging constantly, and the mid-range where music lives gets the most needle travel.

### C — Continuous clock alignment + latency offset

Two independent knobs on the clock, both small:

1. **Soft drift correction.** Keep the existing hard-snap for large jumps (seeks, track changes, paused: `|polled − posSec| > 1.6 s` → snap), but on every `/api/status` poll ALSO nudge the clock toward the freshly polled position for sub-threshold drift: `posSec += (polled − posSec) * SYNC_NUDGE` with `SYNC_NUDGE ≈ 0.25`. This converges accumulated drift over a couple of polls **without** the visible jump that lowering the hard-snap threshold would cause (Sonos `RelTime` is 1 s-granular and jittery, which is why the hard threshold must stay generous). The seek bar and clock stay smooth.
2. **Meter latency offset.** The needles sample energy at `posSec + VU_LATENCY` (pass A's `em`). `VU_LATENCY` (seconds, default **0.0**, tunable) shifts only the meter's energy read — never the displayed position/seek — so the visual can be nudged to line up with what's heard once calibrated against real playback. Default 0 introduces no offset until deliberately tuned.

### D — Finer analysis resolution (backend)

- In `draai/analysis.py`: `ANALYSIS_STEP = 0.1 → 0.03` (30 ms / ~33 Hz frames) and `ANALYSIS_VERSION = 2 → 3` (invalidates all caches → each track recomputes on next play via the existing `prefetch_analysis`/`get_analysis` path).
- Everything else in the pipeline is unchanged: still 8 kHz mono decode, still a max-abs **peak** envelope per window (right for punch), still the four band passes, still scaled 0..100 against the full-band peak. `win_bytes = int(ANALYSIS_SR * ANALYSIS_STEP) * 2` recomputes to 240 samples/window automatically.
- **Waveform stays coarse:** keep `PEAK_BUCKETS = 240` — the Now-Playing/dock waveform doesn't need finer, and the `frames // PEAK_BUCKETS` bucketing keeps that payload identical. Only the `amp/low/mid/high` envelope arrays get denser.
- **Payload:** a 4-min track's envelope JSON grows from ~28 KB to ~94 KB (3.3×), fetched once per track over the LAN and cached on disk. CPU is barely affected — the ffmpeg decode dominates and is unchanged; only the windowing loop runs more iterations.
- **UI needs no change for D:** `energyAt` already divides by `analysis.step`, so the finer frames are consumed transparently. D simply gives A's raw meter read (and B's mapping) genuinely fine material — this is where the per-beat "snap" comes from.
- `30 ms` is the tunable knob (`ANALYSIS_STEP`): finer (25/20 ms) buys marginal detail for 20–50 % more data; coarser loses transient snap. 30 ms resolves 16th-notes/hi-hats cleanly.

### E — Real stereo envelopes (backend + small UI)

- **Amp pass goes stereo (no extra ffmpeg pass).** Today the pipeline runs four mono passes (`amp`, `low`, `mid`, `high`). E makes the `amp` pass decode `-ac 2` and, per 30 ms window, compute a peak for the left samples and a peak for the right samples (deinterleaving the interleaved `s16le` L,R,L,R… stream). That one pass now yields **`ampL`, `ampR`, and a mono `amp`** (per-window `max(peakL, peakR)`), so the total pass count and analysis time stay ~flat. The three band passes (`low/mid/high`) remain mono, unchanged (they feed the background glow + EQ-mark).
- **Scaling:** `ampL`/`ampR` are scaled 0..100 against the same full-band `peak` (`= max(amp)`) so cross-channel relative loudness is preserved and a hotter channel can legitimately read higher (the meter's `>1.0` clamp + PEAK lamp handle overshoot).
- **Output JSON** gains `"ampL"` and `"ampR"` alongside the existing `amp/low/mid/high/peaks`.
- **UI (small):** `energyAt` returns `ampL`/`ampR` too, each falling back to mono `amp` when absent (`analysis.ampL ? g(analysis.ampL) : g(analysis.amp)`) so pre-restereo/graceful tracks still drive both needles sanely. `drawAmp`'s targets become the real channels — `tL = clamp(vuMap(em.ampL), 0, 1.12)`, `tR = clamp(vuMap(em.ampR), 0, 1.12)` — dropping the `amp*0.9 + low/high*0.28` band-weight hack and the `sin/cos` decorrelation (real stereo supplies the life). Everything downstream (ballistics, red zone, PEAK lamps, rest-at-−20, `REDUCE`) is unchanged.
- **Deinterleave detail:** stereo `s16le` window = `int(ANALYSIS_SR*ANALYSIS_STEP)` frames × 2 channels × 2 bytes; left = even int16 indices, right = odd. Compute `max(|even|)` and `max(|odd|)` per window.

### Combined meter path (after A+B+C+D+E)

```
em = playing ? energyAt(posSec + VU_LATENCY) : null        // A (own read) + C (offset); em has ampL/ampR (E)
tL = em ? clamp(vuMap(em.ampL), 0, 1.12) : 0               // E real left channel, B perceptual map
tR = em ? clamp(vuMap(em.ampR), 0, 1.12) : 0               // E real right channel
vuL/vuR += ballistics(attack 50ms / decay 240ms)           // unchanged (band-weight hack + sine decorrelation removed)
PEAK lamp when vu > 1.0 ; at rest em=null → 0 → needle eases to -20
```
`sm`, the background glow, the waveform, and the seek bar are untouched.

## Tunable constants (calibrated during build)

| Constant | Default | Role |
|---|---|---|
| `DB_FLOOR` | −38 dB | bottom of the perceptual window (B) — lower = more low-level activity |
| `EPS` | 1e-3 | log floor to avoid −∞ (B) |
| `SYNC_NUDGE` | 0.25 | fraction of sub-threshold drift corrected per poll (C1) |
| `VU_LATENCY` | 0.0 s | meter energy read offset vs the clock (C2) — the room-alignment knob |
| (micro-smooth) | off | fallback ≤20 ms pre-smooth on `em` if raw reads too twitchy (A) |
| `ANALYSIS_STEP` | 0.03 s | analysis frame size (D) — in `draai/analysis.py`; smaller = finer transients, bigger payload |
| `ANALYSIS_VERSION` | 3 | cache-invalidation version (D) — bump forces lazy re-analysis |

The A/B/C knobs live in one clearly-labeled block near the amp JS in `player_ui.html`; the D knobs are the two constants in `draai/analysis.py`. Defaults are chosen so B+D are the visible/audible-match changes; `VU_LATENCY` stays 0 until calibrated so we never introduce a wrong offset.

## Testing

- **Test suite:** `python3 tests/test_draai.py` (25) must stay green. The suite doesn't exercise the ffmpeg analysis (no ffmpeg in CI), so the `ANALYSIS_STEP`/`ANALYSIS_VERSION` change is safe; A–C are UI-only.
- **D+E — re-analysis:** with ffmpeg installed, confirm that after the version bump a freshly played track recomputes its analysis (old cache ignored), the new JSON reports `"step": 0.03`, denser `amp/low/mid/high` arrays, the waveform `peaks` array still 240 long, **and new `ampL`/`ampR` arrays of the same length**. On a hard-panned or wide-stereo track the two needles visibly diverge; on a centered/mono track they move near-together (correct). A mono source file must still analyze without error (ffmpeg `-ac 2` upmixes → `ampL≈ampR`). `energyAt`'s fallback keeps both needles sane if `ampL`/`ampR` are ever absent. Bumping the version must not break tracks that have no analysis yet (graceful, as today).
- **DEMO mode (Browser pane, desktop):** with synthesized analysis, confirm the needles are visibly more responsive/punchy than before (A), spread across the dial rather than hugging −20 or pegging (B), and that at rest they still settle to −20, `prefers-reduced-motion` still eases gently, and PEAK lamps still fire on peaks.
- **Real playback (maintainer, optional, no unprompted playback by the assistant):** the de-smoothing (A) and mapping (B) are verifiable in DEMO; `VU_LATENCY` (C2) is a room-calibration knob only the maintainer can judge by ear — it ships at 0 with guidance to nudge it if the needles feel ahead/behind the sound. `SYNC_NUDGE` (C1) is verified by watching the clock stay smooth (no jumps) across polls.
- Regression check: background glow, Now-Playing waveform, and seek bar behave exactly as before (they still read `sm` / `posSec`, untouched).

## Risks

- **Twitchiness (A):** raw energy at 100 ms interpolation could look busier than the old silky needle. The VU decay (240 ms) tames most of it; the ≤20 ms micro-smooth is the escape hatch. This is a feel tradeoff to tune, not a correctness risk.
- **Mapping tuning (B):** `DB_FLOOR` too low → needle never rests; too high → still floor-hugging. Tune against DEMO + a couple of real tracks with different dynamics.
- **Sync knobs (C):** `SYNC_NUDGE` too high reintroduces visible jumps; keep it a gentle fraction. `VU_LATENCY` is inert at its default.
- **Re-analysis + payload (D+E):** the single version bump makes every track recompute on next play (a few seconds of ffmpeg each, lazily, no bulk job). Envelope JSON grows ~3.3× from D (finer frames) plus two more arrays from E (`ampL`/`ampR`) → ~28 KB → ~140 KB per 4-min track. Fine for a LAN app, but it's the one user-visible cost (first play of each track after upgrading re-analyzes). E adds ~no analysis time (the amp pass just decodes stereo instead of mono).
- **Stereo edge cases (E):** deinterleave math must be exact (left = even int16, right = odd) or the channels swap/garble — cover with a check that `ampL`/`ampR` track the intended channels. Mono files decode fine via `-ac 2` upmix (`ampL≈ampR`).
- A–C are isolated to the amp meter path (`sm` and every other consumer untouched); D+E are contained to `analysis.py` (the amp pass + two constants) plus a small `energyAt`/`drawAmp` change, with the mono bands and every other consumer untouched — small blast radius on both sides.
