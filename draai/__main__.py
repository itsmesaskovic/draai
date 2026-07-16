"""DRAAI entry point:  python3 -m draai   (and the packaged draai.pyz)."""
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer

from draai import __version__, state
from draai.constants import APP_NAME, PREFERRED_PORT
from draai.config import load_config
from draai.library import scan_all
from draai.backends import load_positions, refresh_speakers
from draai.server import Handler


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


def main():
    if "--version" in sys.argv:
        print("%s %s" % (APP_NAME, __version__))
        return
    if "--install-autostart" in sys.argv:
        install_autostart()
        return
    if "--uninstall-autostart" in sys.argv:
        uninstall_autostart()
        return
    load_config()
    load_positions()

    httpd = None
    for port in range(PREFERRED_PORT, PREFERRED_PORT + 20):
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
            state.server_port = port
            break
        except OSError:
            continue
    if httpd is None:
        print("Could not find a free port to run on. Is the app already running?")
        sys.exit(1)

    url = "http://localhost:%d" % state.server_port
    print()
    print("  %s %s" % (APP_NAME, __version__))
    print("  " + "-" * len(APP_NAME))
    print("  Control panel:  %s" % url)
    print()
    print("  Keep this window open while music is playing — the speakers")
    print("  stream the files from your Mac through this app.")
    print()
    print("  If macOS asks whether Python may accept incoming network")
    print("  connections, click Allow (the speakers need it to fetch music).")
    print()
    print("  Press Ctrl+C to quit.")
    print()

    # Warm up in the background so the page has data quickly.
    def warmup():
        scan_all()
        # discovery can be slow right after boot — retry a few times
        for _ in range(4):
            found, _err = refresh_speakers()
            if found:
                break
            time.sleep(4)
    threading.Thread(target=warmup, daemon=True).start()

    if "--headless" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")


if __name__ == "__main__":
    main()
