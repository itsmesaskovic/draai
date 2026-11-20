"""Config load/save (config.json under the app support dir)."""
import json
import os
import tempfile
import threading
import time

from draai.constants import CONFIG_PATH, CONFIG_DIR
from draai.state import config


def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            saved = json.load(f)
    except Exception:
        return
    if isinstance(saved.get("folders"), list) and saved["folders"]:
        config["folders"] = saved["folders"]
    elif saved.get("folder"):               # migrate old single-folder config
        config["folders"] = [saved["folder"]]
    for k in ("manual_ips", "last_speaker", "ui", "local_volume"):
        if k in saved:
            config[k] = saved[k]


# Re-entrant and public: code that mutates `config` while saves may run takes
# it too (so json.dumps never sees a dict change size mid-iteration).
config_lock = threading.RLock()
_save_lock = config_lock                # old name


def _snapshot():
    for attempt in range(3):
        try:
            return json.dumps(config, indent=2)
        except RuntimeError:            # an unlocked mutator resized a dict
            if attempt == 2:
                raise
            time.sleep(0.005)


def save_config():
    """Atomic and serialised: concurrent callers can't truncate config.json.
    Returns True when the file was written."""
    with config_lock:
        tmp = None
        try:
            data = _snapshot()
            os.makedirs(CONFIG_DIR, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CONFIG_PATH) or ".",
                                       prefix=".config-", suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.replace(tmp, CONFIG_PATH)
            tmp = None
            return True
        except Exception:
            return False
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
