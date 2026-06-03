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


# The built-in, non-deletable default template: a blank white page so a fresh
# install can build a form from scratch in the editor without uploading a PDF.
DEFAULT_TEMPLATE_NAME = "Blank (default)"
DEFAULT_TEMPLATE_FILENAME = "blank_default.pdf"


def ensure_default_template() -> None:
    """Guarantee a blank built-in template exists, is registered, and is active
    when nothing else is. Idempotent — safe to call on every startup.

    The template file is (re)generated if absent; the config gains a protected
    `templates` entry pointing at it. `protected: true` marks it as the one entry
    the UI/API refuse to delete.
    """
    from . import pdfgen  # local import avoids a heavy import at module load

    with _lock:
        conf = _read_yaml(CONFIG_PATH)
        blank_path = CONFIG_DIR / DEFAULT_TEMPLATE_FILENAME
        if not blank_path.exists():
            try:
                pdfgen.make_blank_pdf(str(blank_path))
            except Exception:  # noqa: BLE001  (don't block startup on this)
                return

        templates = list(conf.get("templates") or [])
        existing = next((t for t in templates if t.get("name") == DEFAULT_TEMPLATE_NAME), None)
        if existing is None:
            # Put the default first so it's the obvious baseline in the picker.
            templates.insert(0, {
                "name": DEFAULT_TEMPLATE_NAME,
                "path": str(blank_path),
                "protected": True,
            })
        else:
            existing["path"] = str(blank_path)
            existing["protected"] = True

        conf["templates"] = templates
        # Adopt the default as active only when no (valid) active template is set.
        active = conf.get("active_template")
        names = {t.get("name") for t in templates}
        if not active or active not in names:
            conf["active_template"] = DEFAULT_TEMPLATE_NAME

        _write_yaml(CONFIG_PATH, conf)


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
