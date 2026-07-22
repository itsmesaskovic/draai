"""User-supplied DRIVE car sprites, loaded from <config>/cars/*.json.

The DRIVE visualizer ships one built-in car. Anything dropped in this folder is
offered alongside it, so a new car needs no code change and no rebuild — the same
"drop a file, it appears" idea as the Playlists folder.

A car file is plain JSON, one character per pixel:

    {
      "name": "Honda NSX",
      "w": 36, "h": 42,
      "flare": "9",
      "pal": {"0": "#0c0e12", "9": "#e8a020", ...},
      "rows": ["..................0222222", ...]
    }

`rows` is the LEFT HALF only, mirrored at draw time, so the sprite is symmetric by
construction. "." is transparent. `flare` names the palette key that pulses on the
beat (the indicators); it is redrawn every frame while everything else is baked once.

Everything here is data, never code — the UI plots these as coloured rectangles.
Files are validated on load and a malformed one is skipped rather than breaking
the mode, because these are hand-edited by definition.
"""
import json
import os

from draai.constants import CONFIG_DIR

CARS_DIR = os.path.join(CONFIG_DIR, "cars")
# Oncoming traffic is a separate folder on purpose: those sprites are FRONT views,
# and offering them in the player-car picker would let you drive a car that is
# facing you. Same file format, same validation.
TRAFFIC_DIR = os.path.join(CONFIG_DIR, "traffic")

MAX_DIM = 128        # a sprite far larger than the 320x180 buffer is a mistake, not art
MAX_FILES = 64
MAX_BYTES = 512 * 1024


def _valid(car):
    """Return a cleaned car dict, or None. Deliberately strict: a bad file should
    disappear quietly rather than render as garbage or throw mid-frame."""
    try:
        w, h = int(car["w"]), int(car["h"])
        rows, pal = car["rows"], car["pal"]
        if not (0 < w <= MAX_DIM and 0 < h <= MAX_DIM):
            return None
        if not isinstance(rows, list) or len(rows) != h:
            return None
        if not isinstance(pal, dict) or not pal:
            return None
        for k, v in pal.items():
            if not isinstance(k, str) or len(k) != 1 or not isinstance(v, str):
                return None
        for r in rows:
            if not isinstance(r, str) or len(r) != w:
                return None
            for ch in r:
                if ch != "." and ch not in pal:
                    return None          # an unknown key would plot as nothing, silently
        flare = car.get("flare", "9")
        if not isinstance(flare, str) or len(flare) != 1:
            flare = "9"
        out = {"name": str(car.get("name") or "Car")[:40],
               "w": w, "h": h, "flare": flare, "pal": pal, "rows": rows}
        # optional: a key whose pixels sweep left-to-right instead of pulsing
        # together (KITT's grille bar). Renderer derives the order from x.
        scanner = car.get("scanner")
        if isinstance(scanner, str) and len(scanner) == 1 and scanner in pal:
            out["scanner"] = scanner
        return out
    except Exception:
        return None


def _list_dir(d):
    """Every valid sprite in a directory, name order. Never raises."""
    out = []
    try:
        names = sorted(os.listdir(d))
    except Exception:
        return out
    for fn in names[:MAX_FILES]:
        if not fn.lower().endswith(".json"):
            continue
        p = os.path.join(d, fn)
        try:
            if os.path.getsize(p) > MAX_BYTES:
                continue
            with open(p) as f:
                car = _valid(json.load(f))
        except Exception:
            car = None
        if car:
            car["id"] = os.path.splitext(fn)[0][:40]
            out.append(car)
    return out


def list_cars():
    """Player cars (rear view) from <config>/cars."""
    return _list_dir(CARS_DIR)


def list_traffic():
    """Oncoming vehicles (front view) from <config>/traffic."""
    return _list_dir(TRAFFIC_DIR)
