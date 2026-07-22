# DRIVE and its cars

> The 80s arcade driving mode (`D`), and how to author a car for it. The second
> half is a **workflow**, written down because getting it wrong is expensive:
> the first NSX took thirteen hand-built versions and still lost to a one-pass
> trace.

## Where it lives

- Scene + sprite rendering: `ui/modes/drive/drive.js` (with `drive.html`, `drive.css`)
- Car loading and validation: `draai/cars.py`, served at `GET /api/cars`
- User cars: `<config>/cars/*.json` — drop-in, no rebuild
- Authoring scripts: `.ai/cars/` (`build.py`, `render.py`, `shadow.py`, `glass.py`)

## The scene

A 320x180 backing buffer, blitted nearest-neighbour onto a display canvas sized
to an exact integer multiple of it. House rule: **never a fake signal** — every
motion traces to real audio.

| Element | Driven by |
| --- | --- |
| Sun core, road edges, speed lines, car indicators | `drivePulse` — detected beats, scaled by how hard the low band actually hits |
| Road + palm scroll | `WORLD_RATE`, one shared constant so parallax can never disagree |
| Driving speed | Smoothed loudness x tempo, with ~1.1s throttle inertia |
| Palm planting | One per detected beat, bounded by `PALM_GAP_MIN`/`PALM_FILL_S` |
| Which side a palm grows on | Real stereo balance (`balSmooth`) |

## Car sprite format

```json
{
  "name": "Honda NSX",
  "w": 36, "h": 43,
  "flare": "9",
  "pal": { "0": "#0a0c10", "9": "#f6e8c8", "S": "rgba(0,0,0,0.55)" },
  "rows": ["....0111111…", "…one string per row, h of them…"]
}
```

- **`rows` is the LEFT HALF only.** It is mirrored at draw time, so col 0 is the
  outer edge and the last column is the centreline. `"."` is transparent.
- **`pal`** maps single characters to colours. `rgba(...)` is supported and is
  how glass and ground shadows let the road show through.
- **`flare`** names the one key that pulses on the beat. `drawCarSprite` bakes
  everything else into a canvas once and redraws only these pixels per frame.

Validated on load (`draai/cars.py`): dimensions, row count, row length, and every
character present in the palette. A malformed file is skipped, never rendered
half-broken. Cars are **data, never code** — the UI plots them as rectangles.

## Authoring a car: what actually works

**Ask for pixel art, not a photograph.** This is the whole lesson. Ranked by how
well it worked:

| Input | Result |
| --- | --- |
| **Pixel art, transparent background** | Auto-traced in one pass. Best sprite by a wide margin. |
| Colour photograph | Good for *measuring* — bbox, aspect, band boundaries, real colours. Still needs hand-assembly. |
| ASCII luminance map | Surprisingly useful. Caught two proportion errors instantly. |
| Line art / sticker art | Structure only; its glass is white and its lamps black, the inverse of reality. |
| Describing a photo in conversation | Thirteen versions, worst result. Do not. |

### The pipeline that works

1. **Trace** — downsample the source onto the sprite grid, median-cut the palette,
   map nearest. The source is already pixel art, so this is a resample, not an
   interpretation. No hand-placed regions, no eyeballed proportions.
2. **Recolour** (optional) — derive other liveries by mapping body tones onto a
   target hue *while preserving luminance*, so the panel shading survives.
3. **Shadow** — traced art has a transparent background, so the car floats. Add a
   translucent ground shadow following the per-column footprint.
4. **Glass** — traced art is fully opaque; convert the screen to `rgba` so the
   road reads through.

### Traps, all of which bit during the first build

- **Anything centred must reach the last column.** Otherwise the mirror leaves a
  seam. This caught the Honda badge (it rendered as *two* squares), the diffuser
  strip, and a glass reflection — three times, same bug.
- **Never auto-pick `flare` as "the brightest colour".** On a white car that is
  the bodywork: 16.4% of the sprite, and the whole car strobes on every beat.
  Pick the lamps explicitly and keep it to ~1% of the pixels.
- **Mask recolouring by region *and* hue, never by palette key.** The same tone is
  reused for parts that must not change together — the C-pillar shares a colour
  with the glass, the plate surround with the exhausts. Recolouring by key turned
  the pillars see-through and the plate red.
- **A photo's glass is not the car's glass.** A studio shot measures ~`#5f5f61`
  because a white wall is reflecting in it. Faithful, and wrong for a night
  scene — it renders as concrete. Keep the measured hue, drop the value.
- **Check it at true size.** The car is ~72px wide on the road. Louvres, badges
  and exhaust bores all vanish there; only the silhouette and the light band
  survive. Tuning at 10x optimises for a view nobody sees.
- **A translucent shadow looks broken in an isolated preview** — 55% black on a
  dark backdrop is nothing. It only reads against the road. Check there first.
