"""Media URL helpers + external-tool discovery (ffmpeg / yt-dlp)."""
import os
import shutil
import socket
import urllib.parse

from draai import state


def local_ip_facing(speaker_ip):
    """The IP of the interface this Mac uses to reach the speaker."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((speaker_ip, 1400))
        return s.getsockname()[0]
    finally:
        s.close()


def media_url(track, speaker_ip):
    host = local_ip_facing(speaker_ip)
    name = urllib.parse.quote(track["title"] + track["ext"])
    return "http://%s:%d/media/%s/%s" % (host, state.server_port, track["id"], name)


def find_tool(name):
    p = shutil.which(name)
    if p:
        return p
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        c = os.path.join(d, name)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None
