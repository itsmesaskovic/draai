"""Config load/save (config.json under the app support dir)."""
import json
import os

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
    for k in ("manual_ips", "last_speaker", "ui"):
        if k in saved:
            config[k] = saved[k]


def save_config():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass
