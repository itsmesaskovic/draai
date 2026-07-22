"""Waveform / loudness analysis via ffmpeg (optional feature)."""
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading

from draai.constants import CONFIG_DIR
from draai.media import find_tool
from draai.state import tracks_by_id, state_lock

ANALYSIS_DIR = os.path.join(CONFIG_DIR, "analysis")
ANALYSIS_SR = 8000
ANALYSIS_STEP = 0.03         # seconds per frame (was 0.1 — finer transients)
ANALYSIS_MAX_SEC = 43200     # analyze up to 12 hours of audio
ANALYSIS_VERSION = 8         # bump to invalidate older caches.
                             # 8: use an external beat tracker when one is installed.
                             # 7: beat-grid fit no longer counts off-beat onsets as
                             #    beats (was inflating every tempo on dense material).
                             # 6: reverted the 250 BPM widening (it broke real tracks) and
                             #    added IOI corroboration for soft tempo peaks.
                             # 5: widened BEAT_TEMPO_MIN_S — reverted, see the constant.
                             # NOTE: changing any
                             # of the beat/section tuning constants below REQUIRES a bump —
                             # get_analysis serves the cached JSON whenever v matches, so
                             # retuned detection silently never reaches already-played
                             # tracks. Missing this made a real 200 BPM fix look like it
                             # had failed, because the stale result was served from disk.
PEAK_BUCKETS = 240

# -- beat grid tuning (see docs/technical/audio-analysis.md) --
BEAT_MIN_GAP = 0.12           # seconds, minimum inter-onset spacing
BEAT_LOCAL_WINDOW = 16         # frames, ~0.5s either side, for adaptive threshold
BEAT_LOCAL_MAX_WINDOW = 2      # frames either side for local-max onset picking
BEAT_TEMPO_MIN_S = 0.30        # 200 BPM. Do NOT widen this without testing against real
                               # files. A synthetic 60-250 BPM pulse sweep is perfectly
                               # clean at 0.24, which made widening look safe — but on
                               # real tracks it broke 4 of 10: "How Much Is the Fish"
                               # 140.6 -> 266.7, "Everytime You Need Me" 91.2 -> 266.7,
                               # and the latter still cleared the confidence gate, so it
                               # reported a confidently WRONG tempo. Clean pulse trains
                               # have no sub-beat onsets to alias against; real dance
                               # music is full of them. 200 BPM is a real ceiling, but a
                               # correct one is worth more than a wider wrong one.
BEAT_TEMPO_MAX_S = 1.00        # 60 BPM
BEAT_OCTAVE_MIN_S = 0.333      # ~180 BPM — widened from 0.40 so 150-180 BPM
                               # dance/hardstyle tempos aren't half-timed
BEAT_OCTAVE_MAX_S = 0.75
BEAT_OCTAVE_RATIO = 0.95       # raised from 0.85 — only override on a
                               # near-tie, not a clearly weaker peak
BEAT_SUBHARMONIC_RATIO = 0.80  # see _estimate_tempo's sub-harmonic check.
# 0.85 left 129/138/148/174 BPM detected at half tempo (the true-half candidate
# scored 0.837-0.846 of the winner — just under the bar). A full 60-200 BPM sweep
# is clean at 0.82 and below with no doubling introduced; 0.80 sits inside that
# band with margin. Kept deliberately high: synthetic pulse trains cannot show
# the false-positive direction (real music can have a strong half-lag peak), so
# the faster reading must still score nearly as well as the winner to be chosen.
BEAT_CONFIDENCE_MIN = 1.3
# Second chance for a tempo the autocorrelation is not sure about. Measured on a real
# 200 BPM frenchcore track the period came out at 0.3005s — 199.7 BPM, right — and was
# thrown away for scoring 1.204 against the 1.3 gate. A third of its onsets land on the
# off-beat (IOI histogram: 255 at 1.0x the median, 110 at 0.5x), which is musically
# correct for the genre and flattens the autocorrelation peak.
# Simply lowering the gate is not the answer: at 1.15 it admits tempos whose period
# disagrees with the onsets entirely. Instead, require CORROBORATION — the median
# inter-onset interval is an estimator independent of the autocorrelation, so when the
# two agree the period is trustworthy even with a soft peak. Two weak agreeing
# estimates beat one confident one.
BEAT_CONFIDENCE_FLOOR = 1.05   # below this, not even corroboration rescues it
BEAT_IOI_AGREE = 0.06          # period must match the median IOI within 6%
# -- beat-grid fit (see _fit_beat_grid) --
BEAT_FIT_SUBBEAT = 0.75        # onsets closer than this fraction of a beat are subdivisions
BEAT_FIT_TOL = 0.25            # a gap this far from a whole beat count is ambiguous: don't regress on it
BEAT_FIT_MIN_POINTS = 8        # too few survivors -> keep the seed period untouched
BEAT_FIT_MAX_DRIFT = 0.02      # the fit polishes quantization (±2%), never re-decides tempo

# -- section tuning --
SECTION_WINDOW_S = 8           # seconds either side for novelty comparison
SECTION_MIN_GAP_S = 12         # minimum spacing between section boundaries
SECTION_STD_MULT = 0.5

analysis_state = {}          # id -> "pending" | "error:<msg>"
analysis_lock = threading.Lock()


def _scale(values, peak):
    """Scale raw envelope values to 0..100 against a shared peak."""
    return [min(100, round(100 * v / peak)) for v in values]


def _stream_envelope(ffmpeg, path, afilter=None):
    """Decode with ffmpeg and reduce to a max-abs envelope while streaming.

    Constant memory regardless of track length — a 10-hour set never
    exists in RAM as raw audio, only as its 100ms loudness envelope.
    """
    import array
    cmd = [ffmpeg, "-v", "error", "-t", str(ANALYSIS_MAX_SEC), "-i", path,
           "-ac", "1", "-ar", str(ANALYSIS_SR)]
    if afilter:
        cmd += ["-af", afilter]
    cmd += ["-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    win_bytes = int(ANALYSIS_SR * ANALYSIS_STEP) * 2
    out, buf = [], b""
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
                out.append(max(max(a), -min(a), 1))
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()
    if not out:
        raise RuntimeError("could not decode audio")
    return out


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


# ----------------------------------------------------------------------------
# beat detection — pure functions over plain lists, no ffmpeg/IO involved so
# they're directly unit-testable on synthetic envelopes.
# ----------------------------------------------------------------------------

def _onset_novelty(low, mid, high):
    """Spectral-flux-style onset novelty, smoothed with a 3-frame moving avg."""
    n = min(len(low), len(mid), len(high))
    if n == 0:
        return []
    flux = [0.0] * n
    for i in range(1, n):
        flux[i] = (max(0, low[i] - low[i - 1]) +
                   max(0, mid[i] - mid[i - 1]) +
                   max(0, high[i] - high[i - 1]))
    smoothed = [0.0] * n
    for i in range(n):
        lo, hi = max(0, i - 1), min(n, i + 2)
        window = flux[lo:hi]
        smoothed[i] = sum(window) / len(window)
    return smoothed


def _pick_onsets(flux, step, min_gap=BEAT_MIN_GAP,
                  local_window=BEAT_LOCAL_WINDOW,
                  max_window=BEAT_LOCAL_MAX_WINDOW):
    """Adaptive-threshold local-max onset picking with a minimum gap.

    Returns onset times in seconds, ascending.
    """
    n = len(flux)
    if n == 0:
        return []
    onsets = []  # list of [time, strength], mutated in place for the gap merge
    for i in range(n):
        lo, hi = max(0, i - local_window), min(n, i + local_window + 1)
        local_mean = sum(flux[lo:hi]) / (hi - lo)
        if flux[i] <= local_mean * 1.5 + 3:
            continue
        lo2, hi2 = max(0, i - max_window), min(n, i + max_window + 1)
        if flux[i] < max(flux[lo2:hi2]):
            continue
        t = i * step
        if onsets and t - onsets[-1][0] < min_gap:
            if flux[i] > onsets[-1][1]:
                onsets[-1][0] = t
                onsets[-1][1] = flux[i]
            continue
        onsets.append([t, flux[i]])
    return [t for t, _ in onsets]


def _parabolic_refine(scores, k):
    """Sub-sample-accurate peak location around integer lag k via parabolic
    interpolation of the autocorrelation scores at k-1, k, k+1.

    Falls back to the unrefined integer when a neighbour is missing or the
    three points are collinear (denom == 0, no usable curvature).
    """
    if (k - 1) not in scores or (k + 1) not in scores:
        return float(k)
    s_prev, s_here, s_next = scores[k - 1], scores[k], scores[k + 1]
    denom = s_prev - 2 * s_here + s_next
    if denom == 0:
        return float(k)
    delta = 0.5 * (s_prev - s_next) / denom
    delta = max(-0.5, min(0.5, delta))
    return k + delta


def _estimate_tempo(flux, step):
    """Autocorrelation tempo estimate with an octave guard and sub-frame
    (parabolic-interpolated) period refinement.

    Beat period is a continuous quantity in seconds; snapping it to an
    integer frame count at a coarse ANALYSIS_STEP (e.g. 0.03s, where 120 BPM
    = 16.667 frames) accumulates real drift over a track. Refining the
    autocorrelation peak to sub-frame resolution keeps the period accurate
    at the step size that actually ships.

    Returns (period_seconds, confidence). period is None when there isn't
    enough signal to even attempt an estimate (confidence is then 0.0).
    """
    n = len(flux)
    min_lag = max(1, round(BEAT_TEMPO_MIN_S / step))
    max_lag = min(n - 1, round(BEAT_TEMPO_MAX_S / step))
    if max_lag <= min_lag or n <= max_lag:
        return None, 0.0
    # pad the scored range by one lag on each side (where available) so the
    # eventually-chosen integer lag always has both neighbours for interpolation
    pad_lo = max(1, min_lag - 1)
    pad_hi = min(max_lag + 1, n - 1)
    scores = {}
    for lag in range(pad_lo, pad_hi + 1):
        s = 0.0
        for i in range(n - lag):
            s += flux[i] * flux[i + lag]
        scores[lag] = s
    official = {L: scores[L] for L in range(min_lag, max_lag + 1)}
    best_lag = max(official, key=lambda L: official[L])
    best_score = official[best_lag]
    if best_score <= 0:
        return None, 0.0
    # octave guard: prefer a lag in the "central" band if it's nearly as strong.
    # Band bounds are converted to lags with round(), not a direct L*step
    # comparison: at a coarse step (0.03s) there's no frame exactly at
    # BEAT_OCTAVE_MIN_S (e.g. 175-190 BPM's nearest lag is 11 frames =
    # 0.33s, which is genuinely < 0.333s though it's the correct fundamental
    # for that whole BPM neighbourhood) — comparing in lag space is what the
    # band boundary is meant to express and is robust to that quantization.
    band_min_lag = round(BEAT_OCTAVE_MIN_S / step)
    band_max_lag = round(BEAT_OCTAVE_MAX_S / step)
    band = [L for L in official if band_min_lag <= L <= band_max_lag]
    if band:
        band_best = max(band, key=lambda L: official[L])
        if official[band_best] >= BEAT_OCTAVE_RATIO * best_score:
            best_lag, best_score = band_best, official[band_best]
    # sub-harmonic check: the band/global-best comparison above only ever
    # promotes a lag *outside* the band in favour of one *inside* it — it
    # can't catch a winner that is already inside the band and is itself an
    # Nx-period harmonic of the true (shorter) fundamental. That happens for
    # periods whose lag falls between coarsely-represented frames near the
    # search floor (e.g. 187-195 BPM at a 0.03s step sit between the 10-
    # and 11-frame lags, both very coarse), where quantization jitter
    # suppresses the raw single-cycle autocorrelation just enough that a
    # 2x or 3x multiple — finely represented at a longer lag — outscores
    # it, even though the fundamental is still clearly present. Check every
    # divisor up to 4x; where more than one divided-down candidate clears
    # the bar, the smallest lag (fastest/shortest period) wins so a 3x
    # error resolves in a single pass rather than only correcting to 2x.
    divisor_hits = []
    for d in (2, 3, 4):
        cand_pos = best_lag / float(d)
        cands = [L for L in (int(cand_pos), int(cand_pos) + 1) if L in official]
        if not cands:
            continue
        cand_best = max(cands, key=lambda L: official[L])
        if official[cand_best] >= BEAT_SUBHARMONIC_RATIO * best_score:
            divisor_hits.append(cand_best)
    if divisor_hits:
        best_lag = min(divisor_hits)
        best_score = official[best_lag]
    mean_score = sum(official.values()) / len(official)
    confidence = (best_score / mean_score) if mean_score > 0 else 0.0
    refined_lag = _parabolic_refine(scores, best_lag)
    return refined_lag * step, confidence


def _fit_beat_grid(onsets, period0):
    """Refine phase + period against the onsets by least squares, and return
    (phase, period). The autocorrelation period seeds it and stays in charge:
    this only polishes, it cannot re-decide the tempo.

    Its job is to correct frame quantization. At ANALYSIS_STEP (0.03s) a real
    period is essentially never an exact number of frames (120 BPM = 16.667),
    and a whole-track phase search that assumes it is drifts by up to half a
    beat over a few minutes. Regressing onset time on beat index fixes phase
    and period together in one closed-form pass.

    Two rules keep the regression honest, both learned the hard way:

    NOT EVERY ONSET IS A BEAT. The previous version indexed onsets
    sequentially with `ks[-1] + max(1, round(gap / period0))`, so an off-beat
    hit — half a period after the last one — was charged a whole beat. On a
    frenchcore track with 178 such onsets the assigned span came to 868 beats
    where the music has ~809, inflating the fitted tempo by 7.3%: a correct
    199.7 BPM estimate was "refined" to 216.2. Naively allowing a delta of 0
    instead overshoots the other way (180.5), because two half-beat gaps then
    contribute nothing. So index from a FIXED anchor by float division (no
    accumulation to drift), and keep only onsets that actually land near a
    beat — the rest are subdivisions and must not vote on the tempo.

    THE PERIOD BARELY MOVES. An electronic track's tempo is stable; this step
    corrects quantization, not musical tempo, so the result is clamped to ±2%.
    The old guard allowed ±25%, which let the 7.3% error through untouched.
    """
    if not onsets:
        return None, None
    if len(onsets) < 2 or period0 is None or period0 <= 0:
        return onsets[0] % period0 if period0 else onsets[0], period0
    # 1. Drop sub-beat onsets. These are the ones the old code charged a whole beat
    #    each. Greedy: keep an onset, then skip anything arriving within three
    #    quarters of a beat of it.
    beats_only = [onsets[0]]
    for t in onsets[1:]:
        if t - beats_only[-1] >= BEAT_FIT_SUBBEAT * period0:
            beats_only.append(t)
    if len(beats_only) < BEAT_FIT_MIN_POINTS:
        return onsets[0] % period0, period0

    # 2. Index each gap INDEPENDENTLY against the seed period. This is what makes the
    #    scheme drift-proof and why indexing from a fixed anchor cannot replace it:
    #    a 0.5% seed error still rounds every one-beat gap to exactly 1, forever,
    #    whereas anchored division accumulates that error into the index itself. (An
    #    anchored version drifted ~3 beats over a 300s track and left the grid 0.12s
    #    out of phase.) Gaps whose beat count is genuinely ambiguous — anything near
    #    a half-beat, which rounds arbitrarily — still advance the index but are kept
    #    OUT of the regression so they cannot tilt the slope.
    k = 0
    pts = [(0, beats_only[0])]
    for i in range(1, len(beats_only)):
        span = (beats_only[i] - beats_only[i - 1]) / period0
        whole = round(span)
        k += max(1, whole)
        if abs(span - whole) < BEAT_FIT_TOL:
            pts.append((k, beats_only[i]))
    if len(pts) < BEAT_FIT_MIN_POINTS:
        return onsets[0] % period0, period0

    n = len(pts)
    kbar = sum(a for a, _ in pts) / n
    tbar = sum(b for _, b in pts) / n
    num = sum((a - kbar) * (b - tbar) for a, b in pts)
    den = sum((a - kbar) ** 2 for a, _ in pts)
    if den == 0:
        return onsets[0] % period0, period0
    period = num / den
    if period <= 0:
        return onsets[0] % period0, period0
    lo, hi = period0 * (1 - BEAT_FIT_MAX_DRIFT), period0 * (1 + BEAT_FIT_MAX_DRIFT)
    period = max(lo, min(hi, period))
    phase = (tbar - period * kbar) % period
    return phase, period


def _beat_grid(flux, onsets, period, step):
    """Emit the beat grid as float-second times.

    Phase and period are fit to the onset timestamps directly (see
    _fit_beat_grid) when there are enough onsets to do so; otherwise this
    falls back to the frame-resolution "sum of flux at phase + k*period"
    search over the whole track (the original approach), which is still
    reasonable when onsets are too sparse to regress on.
    """
    n = len(flux)
    duration = n * step
    phase, refined_period = _fit_beat_grid(onsets, period)
    if phase is None:
        period_frames = max(1, round(period / step))
        if period_frames >= max(n, 1):
            return [], period
        best_phi, best_sum = 0, -1.0
        for phi in range(period_frames):
            s = sum(flux[phi::period_frames])
            if s > best_sum:
                best_sum, best_phi = s, phi
        phase, refined_period = best_phi * step, period
    beats = []
    t = phase
    while t <= duration:
        beats.append(round(t, 3))
        t += refined_period
    return beats, refined_period


def _median_ioi(onsets):
    """Median gap between onsets, or None when there are too few to be meaningful."""
    if len(onsets) < 8:
        return None
    gaps = sorted(onsets[i + 1] - onsets[i] for i in range(len(onsets) - 1))
    return gaps[len(gaps) // 2] or None


def _tempo_trustworthy(period, confidence, onsets):
    """Is this tempo good enough to build a grid on?

    A sharp autocorrelation peak is enough on its own. Failing that, accept a soft
    peak only when the median inter-onset interval independently agrees with it —
    see the BEAT_CONFIDENCE_FLOOR comment for why corroboration beats a lower gate.
    """
    if confidence >= BEAT_CONFIDENCE_MIN:
        return True
    if confidence < BEAT_CONFIDENCE_FLOOR:
        return False
    med = _median_ioi(onsets)
    return med is not None and abs(period / med - 1.0) < BEAT_IOI_AGREE


def detect_beats(low, mid, high, step):
    """Detect tempo + beat grid from loudness-band envelopes.

    Returns (beats, bpm): beats is a list of times in seconds (ascending,
    possibly empty), bpm is a float or None. When tempo confidence is low
    (or the track is too short to estimate one), bpm is None and beats
    falls back to the raw onsets.
    """
    n = min(len(low), len(mid), len(high))
    if n == 0:
        return [], None
    low, mid, high = low[:n], mid[:n], high[:n]
    flux = _onset_novelty(low, mid, high)
    onsets = _pick_onsets(flux, step)
    period, confidence = _estimate_tempo(flux, step)
    if period is None or not _tempo_trustworthy(period, confidence, onsets):
        return onsets, None
    beats, refined_period = _beat_grid(flux, onsets, period, step)
    if not beats:
        return onsets, None
    return beats, 60.0 / refined_period


# ----------------------------------------------------------------------------
# section detection — same "pure function over plain lists" shape as beats.
# ----------------------------------------------------------------------------

def _second_features(amp, low, mid, high, step):
    """Mean [low, mid, high, amp] per whole second, each scaled to 0..1."""
    n = min(len(amp), len(low), len(mid), len(high))
    if n == 0:
        return []
    n_secs = int(n * step)
    feats = []
    for s in range(n_secs):
        i0 = min(n, int(round(s / step)))
        i1 = min(n, int(round((s + 1) / step)))
        if i1 <= i0:
            break
        seg_n = i1 - i0
        feats.append((
            sum(low[i0:i1]) / seg_n / 100.0,
            sum(mid[i0:i1]) / seg_n / 100.0,
            sum(high[i0:i1]) / seg_n / 100.0,
            sum(amp[i0:i1]) / seg_n / 100.0,
        ))
    return feats


def _section_novelty(feats, window=SECTION_WINDOW_S):
    """Euclidean distance between the mean feature vector of the preceding
    and following `window`-second spans, per second."""
    n = len(feats)
    novelty = [0.0] * n
    for s in range(n):
        pre = feats[max(0, s - window):s]
        post = feats[s:min(n, s + window)]
        if not pre or not post:
            continue
        pre_mean = [sum(v[k] for v in pre) / len(pre) for k in range(4)]
        post_mean = [sum(v[k] for v in post) / len(post) for k in range(4)]
        novelty[s] = math.sqrt(sum((a - b) ** 2 for a, b in zip(pre_mean, post_mean)))
    return novelty


def _pick_section_boundaries(novelty, min_gap=SECTION_MIN_GAP_S):
    """Local maxima above mean + 0.5*std, at least min_gap seconds apart."""
    n = len(novelty)
    if n == 0:
        return []
    mean_n = sum(novelty) / n
    var = sum((x - mean_n) ** 2 for x in novelty) / n
    threshold = mean_n + SECTION_STD_MULT * (var ** 0.5)
    candidates = []
    for s in range(n):
        if novelty[s] <= threshold:
            continue
        lo, hi = max(0, s - 1), min(n, s + 2)
        if novelty[s] < max(novelty[lo:hi]):
            continue
        candidates.append([s, novelty[s]])
    boundaries = []
    for s, val in candidates:
        if boundaries and s - boundaries[-1][0] < min_gap:
            if val > boundaries[-1][1]:
                boundaries[-1][0] = s
                boundaries[-1][1] = val
            continue
        boundaries.append([s, val])
    return [s for s, _ in boundaries]


def detect_sections(amp, low, mid, high, step):
    """Detect structural section boundaries from the loudness envelopes.

    Returns a list of {"t": seconds, "energy": 0..1}, always starting with
    t=0.0. Never raises, even on empty/very short input.
    """
    n = min(len(amp), len(low), len(mid), len(high))
    if n == 0:
        return [{"t": 0.0, "energy": 0.0}]
    amp = amp[:n]
    feats = _second_features(amp, low, mid, high, step)
    boundary_secs = []
    if len(feats) >= 2:
        novelty = _section_novelty(feats)
        boundary_secs = _pick_section_boundaries(novelty)
    bounds = sorted(set([0] + [b for b in boundary_secs if b > 0]))
    sections = []
    for idx, s in enumerate(bounds):
        i0 = min(n, int(round(s / step)))
        i1 = min(n, int(round(bounds[idx + 1] / step))) if idx + 1 < len(bounds) else n
        i1 = max(i1, i0)
        seg = amp[i0:i1]
        energy = (sum(seg) / len(seg) / 100.0) if seg else 0.0
        sections.append({"t": float(s), "energy": round(energy, 3)})
    return sections


# ---------------------------------------------------------------------------
# optional external beat trackers
#
# Same contract as ffmpeg and yt-dlp: DETECTED at runtime, never required. The
# built-in detector below stays the default and keeps the zero-dependency promise
# for anyone who just downloads draai.pyz — these only upgrade a machine that
# happens to have them.
#
# Why bother: measured over seven tracks from a real library, the built-in
# produced no tempo at all on four of them. Beat This! produced a grid on all
# seven and locked onto real onsets roughly twice as often (up to 64% vs 30%).
#
# Preference order is deliberate. Beat This! (MIT, ISMIR 2024, actively developed)
# beat aubio on 5 of 7. aubio is GPL-3.0 and last released in 2019, so it sits
# second and is only ever invoked as a subprocess — linking it would force DRAAI
# off its MIT licence, which running a binary does not.
#
# Everything here is best-effort: any failure, timeout or malformed output falls
# through to the built-in detector rather than breaking analysis.
# ---------------------------------------------------------------------------
BEAT_TOOL_TIMEOUT = 600        # seconds; a wedged tool must not hang analysis forever
BEAT_TOOL_MIN_BEATS = 8        # fewer than this is not a usable grid


def _beats_bpm(times):
    """(beats, bpm) from beat times, bpm taken from the median gap."""
    if len(times) < BEAT_TOOL_MIN_BEATS:
        return None
    gaps = sorted(times[i + 1] - times[i] for i in range(len(times) - 1))
    med = gaps[len(gaps) // 2]
    if not med or med <= 0:
        return None
    return [round(t, 3) for t in times], 60.0 / med


def _beats_via_beat_this(tool, path, ffmpeg):
    """Beat This! reads via torchaudio, which can no longer decode mp3 at all
    (it wants TorchCodec now), so hand it a wav that ffmpeg made. It also needs
    `soundfile` installed — that dependency is missing from its own metadata.

    A checkpoint dropped at <config>/beat_this.ckpt is used when present. Worth
    doing: without it the tool fetches weights from a university cloud share with
    no checksum, and on torch < 2.6 that is an unpickle of a remote file.
    """
    tmpdir = tempfile.mkdtemp(prefix="draai-beats-")
    try:
        wav = os.path.join(tmpdir, "a.wav")
        out = os.path.join(tmpdir, "a.beats")
        subprocess.run([ffmpeg, "-v", "error", "-t", str(ANALYSIS_MAX_SEC),
                        "-i", path, "-ac", "1", "-ar", "22050", "-y", wav],
                       check=True, timeout=BEAT_TOOL_TIMEOUT,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cmd = [tool, "--gpu", "-1", "-o", out]
        ckpt = os.path.join(CONFIG_DIR, "beat_this.ckpt")
        if os.path.isfile(ckpt):
            cmd += ["--model", ckpt]
        cmd.append(wav)
        subprocess.run(cmd, check=True, timeout=BEAT_TOOL_TIMEOUT,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        times = []
        with open(out) as f:
            for line in f:
                line = line.strip()
                if line:
                    times.append(float(line.split("\t")[0]))
        return _beats_bpm(times)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _beats_via_aubio(tool, path):
    """aubiotrack prints one beat time per line and reads mp3 itself."""
    r = subprocess.run([tool, path], capture_output=True, text=True,
                       timeout=BEAT_TOOL_TIMEOUT)
    times = []
    for line in r.stdout.split():
        try:
            times.append(float(line))
        except ValueError:
            pass
    return _beats_bpm(times)


def _external_beats(path, ffmpeg):
    """(beats, bpm, source) from the best installed tracker, or None."""
    for name, run in (("beat_this", lambda t: _beats_via_beat_this(t, path, ffmpeg)),
                      ("aubiotrack", lambda t: _beats_via_aubio(t, path))):
        tool = find_tool(name)
        if not tool:
            continue
        try:
            got = run(tool)
        except Exception:
            got = None            # missing dep, bad audio, timeout — just fall through
        if got:
            return got[0], got[1], name
    return None


def _analyze(track):
    tid = track["id"]
    try:
        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is not installed (brew install ffmpeg)")
        raw_L, raw_R = _stream_envelope_stereo(ffmpeg, track["path"])
        raw_amp = [l if l >= r else r for l, r in zip(raw_L, raw_R)]  # mono peak = per-window max(L,R)
        peak = max(raw_amp) if raw_amp else 1
        amp = _scale(raw_amp, peak)
        ampL = _scale(raw_L, peak)     # per-channel, scaled to the same full-band peak
        ampR = _scale(raw_R, peak)
        # bands share the full-band peak so relative loudness is preserved
        low = _scale(_stream_envelope(ffmpeg, track["path"],
                                      "lowpass=f=250"), peak)
        mid = _scale(_stream_envelope(ffmpeg, track["path"],
                                      "highpass=f=250,lowpass=f=2000"), peak)
        high = _scale(_stream_envelope(ffmpeg, track["path"],
                                       "highpass=f=2000"), peak)
        frames = len(amp)
        # waveform peaks: bucket the amp envelope down to PEAK_BUCKETS
        peaks = []
        if frames:
            per = max(1, frames // PEAK_BUCKETS)
            for i in range(0, frames, per):
                peaks.append(max(amp[i:i + per]))
            peaks = peaks[:PEAK_BUCKETS]
        ext = _external_beats(track["path"], ffmpeg)
        if ext:
            beats, bpm, beat_source = ext
        else:
            beats, bpm = detect_beats(low[:frames], mid[:frames], high[:frames], ANALYSIS_STEP)
            beat_source = "builtin"
        sections = detect_sections(amp, low[:frames], mid[:frames], high[:frames], ANALYSIS_STEP)
        data = {
            "status": "ready",
            "v": ANALYSIS_VERSION,
            "duration": round(frames * ANALYSIS_STEP, 1),
            "step": ANALYSIS_STEP,
            "peaks": peaks,
            "amp": amp, "low": low, "mid": mid[:frames], "high": high[:frames],
            "ampL": ampL[:frames], "ampR": ampR[:frames],
            "bpm": bpm, "beats": beats, "sections": sections,
            "beat_source": beat_source,
        }
        os.makedirs(ANALYSIS_DIR, exist_ok=True)
        with open(os.path.join(ANALYSIS_DIR, tid + ".json"), "w") as f:
            json.dump(data, f)
        with analysis_lock:
            analysis_state.pop(tid, None)
        return data
    except Exception as e:
        with analysis_lock:
            analysis_state[tid] = "error:%s" % e


def get_analysis(tid):
    """Return analysis dict, or {"status": "pending"/"error"} and kick a job."""
    path = os.path.join(ANALYSIS_DIR, tid + ".json")
    if os.path.isfile(path):
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("v") == ANALYSIS_VERSION:
                return data
            os.remove(path)   # stale cache (e.g. 25-min-capped): re-analyze
        except Exception:
            pass
    with state_lock:
        track = tracks_by_id.get(tid)
    if not track:
        return {"status": "error", "error": "unknown track"}
    with analysis_lock:
        st = analysis_state.get(tid)
        if st is None:
            analysis_state[tid] = "pending"
            threading.Thread(target=_analyze, args=(track,),
                             daemon=True).start()
            return {"status": "pending"}
        if st == "pending":
            return {"status": "pending"}
        return {"status": "error", "error": st[6:]}


def prefetch_analysis(ids, limit=3):
    for tid in ids[:limit]:
        threading.Thread(target=get_analysis, args=(tid,),
                         daemon=True).start()
