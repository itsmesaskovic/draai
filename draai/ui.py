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
