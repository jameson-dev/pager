"""Smoke tests for the parse -> rules -> render -> gating pipeline.

Run from the repo root:  python -m pytest tests/  (or just python tests/test_pipeline.py)
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Make `app` importable. Point config at a throwaway copy of the repo's seed
# config/ so test runs never write a generated auth secret back into the tracked
# file (the app persists one on first use).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_cfg = Path(tempfile.mkdtemp(prefix="pager_test_cfg_")) / "config"
shutil.copytree(ROOT / "config", _cfg)
os.environ["PAGER_CONFIG"] = str(_cfg)

from app.parser import parse_line, apply_rules, build_field_context
from app import config as cfg
from app import pdfgen
from app.processor import should_print

POCSAG_LINE = "POCSAG1200: Address:  1234567  Function: 0  Alpha: F2 STRUCTURE FIRE @ 12 MAIN ST //CROSS OAK ST [MAP G245] UNITS: P1 P2<NUL>"
NON_ALPHA   = "POCSAG1200: Address:  1234567  Function: 3  Numeric: 12345"
FLEX_LINE   = "FLEX: 2009-01-01 12:00:00 1600/2/K/A 12.345 [1234568] ALN STRUCTURE FIRE 5 OAK ST"

# Self-contained rule for the example message above, so these tests don't depend
# on whatever rules happen to be in the live config/rules.yaml.
EXAMPLE_RULES = [{
    "name": "Standard dispatch",
    "pattern": (r"^(?P<jobtype>[A-Z]\d+)\s+(?P<details>.+?)\s+@\s+(?P<address>.+?)"
                r"\s+//(?P<crossstreet>.+?)\s+\[MAP\s+(?P<mapref>[^\]]+)\]"
                r"\s+UNITS:\s+(?P<units>.+)$"),
}]


def test_parse_pocsag():
    page = parse_line(POCSAG_LINE)
    assert page is not None
    assert page.capcode == "1234567"
    assert page.message.startswith("F2 STRUCTURE FIRE")
    assert "<NUL>" not in page.message
    print("parse_pocsag OK:", page.capcode, "|", page.message)


def test_parse_non_alpha_ignored():
    assert parse_line(NON_ALPHA) is None
    print("non-alpha ignored OK")


def test_rules_extraction():
    page = parse_line(POCSAG_LINE)
    rules = EXAMPLE_RULES
    fields, matched = apply_rules(page.message, rules)
    print("matched rule:", matched)
    print("fields:", fields)
    assert fields["jobtype"] == "F2"
    assert fields["address"] == "12 MAIN ST"
    assert fields["crossstreet"] == "CROSS OAK ST"
    assert fields["mapref"] == "G245"
    assert fields["units"] == "P1 P2"


def test_context_builtins():
    page = parse_line(POCSAG_LINE)
    rules = EXAMPLE_RULES
    fields, _ = apply_rules(page.message, rules)
    ctx = build_field_context(page, fields)
    assert ctx["capcode"] == "1234567"
    assert "date" in ctx and "time" in ctx
    print("context OK:", {k: ctx[k] for k in ("capcode", "date", "jobtype", "address")})


def test_pdf_render():
    page = parse_line(POCSAG_LINE)
    rules = EXAMPLE_RULES
    fields, _ = apply_rules(page.message, rules)
    ctx = build_field_context(page, fields)
    layout = cfg.load_layout()
    out = Path(tempfile.gettempdir()) / "pager_test.pdf"
    # No template -> blank A4 fallback, still produces a valid PDF.
    pdfgen.render_job_pdf("", layout, ctx, str(out))
    assert out.exists() and out.stat().st_size > 500
    from pypdf import PdfReader
    assert len(PdfReader(str(out)).pages) == 1
    print("pdf render OK:", out, out.stat().st_size, "bytes")


def test_print_gating():
    base = {
        "global_print_enabled": True,
        "capcodes": [
            {"code": "1234567", "print_enabled": True},
            {"code": "1234568", "print_enabled": False},
        ],
        "jobtypes": {"TEST": {"print_enabled": False}},
    }
    assert should_print(base, "1234567", "F2") is True          # all allow
    assert should_print(base, "1234568", "F2") is False         # capcode muted
    assert should_print(base, "1234567", "TEST") is False       # jobtype muted
    base2 = dict(base, global_print_enabled=False)
    assert should_print(base2, "1234567", "F2") is False        # master off
    print("print gating OK")


def test_db_failed_and_retry_tracking():
    from app.database import JobStore
    db = JobStore(str(Path(tempfile.gettempdir()) / "pager_test_jobs.db"))
    # Job that was attempted but failed.
    from datetime import datetime
    jid = db.add_job(
        received_at=datetime.now(), capcode="1234567", jobtype="F2",
        message="m", fields={}, pdf_path=str(Path(tempfile.gettempdir()) / "pager_test.pdf"),
        printed=False, print_error="printer offline", matched_rule=None,
        attempted_print=True,
    )
    assert db.count_failed_unresolved(5) >= 1
    failed = db.list_failed_unresolved(5)
    assert any(j["id"] == jid for j in failed)
    # Simulate a successful retry -> resolves the failure.
    db.update_print_result(jid, True, None, attempts=2)
    job = db.get_job(jid)
    assert job["printed"] is True and job["print_failed"] == 0 and job["print_attempts"] == 2

    # A gated-off job (never attempted) must NOT count as failed.
    jid2 = db.add_job(
        received_at=datetime.now(), capcode="1234568", jobtype="ADMIN",
        message="m2", fields={}, pdf_path=None, printed=False,
        print_error=None, matched_rule=None, attempted_print=False,
    )
    j2 = db.get_job(jid2)
    assert j2["print_failed"] == 0
    print("db failed/retry tracking OK")


def test_active_template_resolution():
    from app.processor import active_template
    # templates list + active wins
    conf = {"templates": [{"name": "A", "path": "/a.pdf"}, {"name": "B", "path": "/b.pdf"}],
            "active_template": "B", "template_pdf": "/legacy.pdf"}
    assert active_template(conf) == "/b.pdf"
    # falls back to first template if active missing
    assert active_template({"templates": [{"name": "A", "path": "/a.pdf"}]}) == "/a.pdf"
    # legacy single key when no list
    assert active_template({"template_pdf": "/legacy.pdf"}) == "/legacy.pdf"
    print("active_template resolution OK")


def test_health_snapshot():
    from app.events import Health
    h = Health()
    assert h.snapshot(3600)["stale"] is True  # nothing seen yet -> stale
    h.mark_line()
    h.mark_page()
    snap = h.snapshot(3600)
    assert snap["stale"] is False
    assert snap["total_lines"] == 1 and snap["total_pages"] == 1
    print("health snapshot OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [PASS] {name}")
    print("\nALL TESTS PASSED")
