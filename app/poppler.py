"""Locate the poppler binaries used by pdf2image for template previews.

On Linux the install script apt-installs `poppler-utils`, so `pdftoppm` is on
PATH and nothing else is needed. On Windows poppler is typically unpacked to a
folder that isn't on PATH, so we also look in a configurable `poppler_path` and
a few common locations, and pass it explicitly to pdf2image.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import config as cfg

# The binary pdf2image actually shells out to.
_BIN = "pdftoppm.exe" if os.name == "nt" else "pdftoppm"


def _candidates() -> list[str]:
    paths: list[str] = []
    # 1. Explicit override from config (a dir containing pdftoppm).
    cfgd = (cfg.load_config().get("poppler_path") or "").strip()
    if cfgd:
        paths.append(cfgd)
    # 2. Common Windows install locations (and a vendored copy beside the app).
    if os.name == "nt":
        here = Path(__file__).resolve().parent.parent
        paths += [str(here / "poppler" / "bin")]
        for base in (r"C:\Program Files\poppler", r"C:\Program Files (x86)\poppler",
                     r"C:\poppler"):
            b = Path(base)
            if b.exists():
                # poppler-xx/Library/bin or poppler-xx/bin
                for sub in b.glob("**/bin"):
                    paths.append(str(sub))
    return paths


def poppler_dir() -> str | None:
    """Return a directory containing pdftoppm, or None if only on PATH/absent.

    None with poppler on PATH is fine — pdf2image finds it itself. None with no
    PATH entry means poppler is unavailable.
    """
    for d in _candidates():
        if d and (Path(d) / _BIN).exists():
            return d
    return None


def is_available() -> bool:
    """True if pdf2image will be able to render (poppler on PATH or located)."""
    return shutil.which(_BIN) is not None or poppler_dir() is not None
