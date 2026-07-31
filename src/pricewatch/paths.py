"""XDG-conformant path resolution.

Hand-rolled rather than pulling in `platformdirs`: the deployment target is
Linux + systemd, where the XDG spec is the whole answer.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "pricewatch"


def _xdg_dir(env_var: str, fallback: str) -> Path:
    raw = os.environ.get(env_var)
    if raw:
        return Path(raw)
    return Path.home() / fallback


def xdg_config_home() -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", ".config")


def xdg_data_home() -> Path:
    return _xdg_dir("XDG_DATA_HOME", ".local/share")


def default_config_path() -> Path:
    return xdg_config_home() / APP_NAME / "config.toml"


def default_state_dir() -> Path:
    return xdg_data_home() / APP_NAME
