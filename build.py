#!/usr/bin/env python3
"""Build a single, runnable draai.pyz — stdlib zipapp, no dependencies.

    python3 build.py        # -> draai.pyz
    python3 draai.pyz       # run it (or ./draai.pyz)

The archive bundles the whole draai package AND the web UI (player_ui.html),
so it is genuinely one file to distribute.
"""
import os
import shutil
import tempfile
import zipapp

HERE = os.path.dirname(os.path.abspath(__file__))


def build(target="draai.pyz"):
    stage = tempfile.mkdtemp()
    try:
        pkg = os.path.join(stage, "draai")
        shutil.copytree(os.path.join(HERE, "draai"), pkg,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # keep the embedded UI in sync with the top-level source of truth
        top_ui = os.path.join(HERE, "player_ui.html")
        if os.path.isfile(top_ui):
            shutil.copy(top_ui, os.path.join(pkg, "player_ui.html"))
        top_remote = os.path.join(HERE, "remote.html")
        if os.path.isfile(top_remote):
            shutil.copy(top_remote, os.path.join(pkg, "remote.html"))
        out = os.path.join(HERE, target)
        zipapp.create_archive(stage, target=out,
                              interpreter="/usr/bin/env python3",
                              main="draai.__main__:main")
        os.chmod(out, 0o755)
        print("built %s (%d KB)" % (target, os.path.getsize(out) // 1024))
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    build()
