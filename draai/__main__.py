"""DRAAI entry point:  python3 -m draai   (and the packaged draai.pyz)."""
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
from draai.install import install_autostart, uninstall_autostart
from draai.listening import load_listening, load_trackinfo, start_watcher


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
    load_listening()
    load_trackinfo()

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
    start_watcher()   # credits time listened per track, browser or not

    if "--headless" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")


if __name__ == "__main__":
    main()
