"""Config load/save (config.json under the app support dir)."""
import json
import os
import tempfile
import threading

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


_save_lock = threading.Lock()


def save_config():
    """Atomic and serialised: concurrent callers can't truncate config.json."""
    with _save_lock:
        tmp = None
        try:
            data = json.dumps(config, indent=2)     # snapshot while no one else saves
            os.makedirs(CONFIG_DIR, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CONFIG_PATH) or ".",
                                       prefix=".config-", suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.replace(tmp, CONFIG_PATH)
            tmp = None
        except Exception:
            pass
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
