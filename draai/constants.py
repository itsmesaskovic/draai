import os

APP_NAME = "DRAAI"
PREFERRED_PORT = 8765
AUDIO_EXTS = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".ogg": "audio/ogg",
}
QUEUE_CAP = 500  # max tracks sent to a speaker queue in one go

CONFIG_DIR = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "SonosMP3Player"
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

ART_CACHE_MAX = 64
POSITIONS_PATH = os.path.join(CONFIG_DIR, "positions.json")
RESUME_MIN_TRACK = 600      # only remember tracks longer than 10 min
RESUME_MIN_POS = 90         # ...and positions past 1:30
