"""Desktop config/history file locations (XDG on Linux, App Support on macOS)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def config_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".config"
    return base / "gcgo"


CONFIG_DIR = config_dir()
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = CONFIG_DIR / "history"
CONFIG_FILE = CONFIG_DIR / "config.json"
