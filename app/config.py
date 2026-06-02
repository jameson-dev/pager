"""Configuration loading/saving. config.yaml + rules.yaml + layout.json."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import yaml

# Allow overriding the config dir (handy for dev on a non-Linux box).
CONFIG_DIR = Path(
    os.environ.get("PAGER_CONFIG")
    or os.environ.get("PAGER2PDF_CONFIG")  # backwards-compat with the old name
    or "/opt/pager/config"
)

CONFIG_PATH = CONFIG_DIR / "config.yaml"
RULES_PATH = CONFIG_DIR / "rules.yaml"
LAYOUT_PATH = CONFIG_DIR / "layout.json"

_lock = threading.Lock()


def _read_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _write_yaml(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)
    os.replace(tmp, path)


def load_config() -> dict:
    with _lock:
        return _read_yaml(CONFIG_PATH)


def save_config(data: dict) -> None:
    with _lock:
        _write_yaml(CONFIG_PATH, data)


def load_rules() -> dict:
    with _lock:
        return _read_yaml(RULES_PATH)


def save_rules(data: dict) -> None:
    with _lock:
        _write_yaml(RULES_PATH, data)


def load_layout() -> dict:
    with _lock:
        with open(LAYOUT_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)


def save_layout(data: dict) -> None:
    with _lock:
        tmp = LAYOUT_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, LAYOUT_PATH)
