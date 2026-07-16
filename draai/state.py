"""Shared mutable runtime state. Import as `from draai import state` and use
`state.X`, OR `from draai.state import config` for the containers (which are
never rebound — mutated in place — so imported names stay valid). Only
`server_port` (a plain int) is genuinely reassigned; access it as
`state.server_port`."""
import os

from draai.constants import PREFERRED_PORT

import threading

state_lock = threading.Lock()
tracks = []            # list of dicts: {id, path, title, folder, ext}
tracks_by_id = {}      # id -> track dict
speakers = []          # list of dicts: {uuid, name, ip}
config = {"folders": [os.path.join(os.path.expanduser("~"), "Music")],
          "manual_ips": [], "last_speaker": None, "ui": {}}
server_port = PREFERRED_PORT

art_cache = {}             # track id -> (mime, bytes); simple bounded cache
enqueue_generation = [0]   # bump to cancel a background enqueue in progress
positions = {}
positions_lock = threading.Lock()
_positions_dirty = [False]
yt_jobs = {}
