"""How DRAAI is installed, and starting it at login.

Leaf module (stdlib + constants only). A Homebrew install lives under
<prefix>/Cellar/draai/<version>/; there brew owns autostart
(`brew services`) and updates (`brew upgrade`), so we hand off to it
instead of writing our own LaunchAgent.
"""
import json
import os
import subprocess
import sys
import urllib.request

from draai.constants import PREFERRED_PORT

_PKG_FILE = os.path.abspath(__file__)


def install_kind(path=None):
    """'homebrew', 'pyz' (a downloaded draai.pyz) or 'source' (python -m draai)."""
    p = os.path.realpath(path or _PKG_FILE).replace(os.sep, "/")
    if "/Cellar/draai/" in p:
        return "homebrew"
    if ".pyz/" in p:
        return "pyz"
    return "source"


def running_engine(ports=None, timeout=0.5):
    """URL of a DRAAI engine already running on this Mac, or None.
    Only counts a server whose /api/state looks like DRAAI's."""
    for port in ports or range(PREFERRED_PORT, PREFERRED_PORT + 20):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/state" % port, timeout=timeout) as r:
                d = json.load(r)
        except Exception:
            continue
        if isinstance(d, dict) and "version" in d and "speakers" in d:
            return "http://localhost:%d" % port
    return None


def _launch_args():
    """How to re-launch DRAAI at login: the .pyz if we're running from one,
    else `python -m draai` from the folder that contains the package."""
    python = sys.executable or "/usr/bin/python3"
    argv0 = os.path.abspath(sys.argv[0])
    if argv0.endswith(".pyz"):
        args = [python, argv0, "--headless"]
        workdir = os.path.dirname(argv0)
    else:
        args = [python, "-m", "draai", "--headless"]
        workdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return args, workdir


def install_autostart():
    if install_kind() == "homebrew":
        print("DRAAI was installed with Homebrew, so Homebrew runs it at login:")
        print("  brew services start draai")
        print("Updates:  brew upgrade draai")
        return
    plist_dir = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")
    plist_path = os.path.join(plist_dir, "com.draai.player.plist")
    log_path = os.path.join(os.path.expanduser("~"), "Library", "Logs", "draai.log")
    args, workdir = _launch_args()
    prog = "".join("<string>%s</string>" % a for a in args)
    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.draai.player</string>
  <key>ProgramArguments</key>
  <array>%s</array>
  <key>WorkingDirectory</key><string>%s</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>%s</string>
  <key>StandardErrorPath</key><string>%s</string>
</dict></plist>
""" % (prog, workdir, log_path, log_path)
    os.makedirs(plist_dir, exist_ok=True)
    with open(plist_path, "w") as f:
        f.write(plist)
    try:
        subprocess.run(["launchctl", "unload", plist_path], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", plist_path], capture_output=True)
    except Exception:
        pass
    print("Installed. DRAAI now starts automatically when you log in,")
    print("running quietly in the background — no Terminal window needed.")
    print("Control panel:  http://localhost:%d" % PREFERRED_PORT)
    print("To undo:        python3 -m draai --uninstall-autostart")


def uninstall_autostart():
    plist_path = os.path.join(os.path.expanduser("~"), "Library",
                              "LaunchAgents", "com.draai.player.plist")
    try:
        subprocess.run(["launchctl", "unload", plist_path], capture_output=True)
    except Exception:
        pass
    if os.path.exists(plist_path):
        os.remove(plist_path)
        print("Removed. DRAAI no longer starts at login.")
    else:
        print("Nothing to remove — autostart was not installed.")
