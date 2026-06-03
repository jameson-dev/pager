"""Tests for the built-in blank default template.

A fresh install should always have a usable, non-deletable blank white template
so a form can be built from scratch in the editor without uploading a PDF.

Run:  python tests/test_default_template.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_cfg = Path(tempfile.mkdtemp(prefix="pager_dtpl_")) / "config"
shutil.copytree(ROOT / "config", _cfg)
os.environ["PAGER_CONFIG"] = str(_cfg)

from app import config as cfg
from app import pdfgen, processor


def test_blank_pdf_is_valid():
    out = str(Path(tempfile.mkdtemp()) / "blank.pdf")
    pdfgen.make_blank_pdf(out)
    data = Path(out).read_bytes()
    assert data[:5] == b"%PDF-" and len(data) > 200
    print("ok: make_blank_pdf produces a real PDF")


def test_seed_from_empty_config():
    conf = cfg.load_config()
    conf.pop("templates", None)
    conf.pop("active_template", None)
    cfg.save_config(conf)

    cfg.ensure_default_template()
    conf = cfg.load_config()
    dflt = conf["templates"][0]
    assert dflt["name"] == cfg.DEFAULT_TEMPLATE_NAME
    assert dflt["protected"] is True
    assert Path(dflt["path"]).exists()
    assert conf["active_template"] == cfg.DEFAULT_TEMPLATE_NAME
    assert processor.active_template(conf) == dflt["path"]
    print("ok: default seeded, protected, active, resolvable")


def test_idempotent_and_preserves_active():
    # Pre-set a user's active template; seeding must NOT override a valid one.
    conf = cfg.load_config()
    conf["templates"] = [{"name": "Mine", "path": "/tmp/mine.pdf"}]
    conf["active_template"] = "Mine"
    cfg.save_config(conf)

    cfg.ensure_default_template()
    cfg.ensure_default_template()  # twice — must not duplicate
    conf = cfg.load_config()
    names = [t["name"] for t in conf["templates"]]
    assert names.count(cfg.DEFAULT_TEMPLATE_NAME) == 1
    assert "Mine" in names
    assert conf["active_template"] == "Mine"  # user's choice preserved
    print("ok: idempotent; preserves a valid user active template")


if __name__ == "__main__":
    test_blank_pdf_is_valid()
    test_seed_from_empty_config()
    test_idempotent_and_preserves_active()
    print("\nAll default-template tests passed.")
