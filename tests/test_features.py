"""Tests for features added beyond the original pipeline:
alias, plain-regex group assignment, available-fields, timezone formatting,
multi-page PDF, and job filtering.

Run:  python tests/test_features.py   (or python -m pytest tests/)
"""
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Point config at a throwaway copy of the seed config/ so test runs don't write a
# generated auth secret back into the tracked file.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_cfg = Path(tempfile.mkdtemp(prefix="pager_test_cfg_")) / "config"
shutil.copytree(ROOT / "config", _cfg)
os.environ["PAGER_CONFIG"] = str(_cfg)

from app import parser
from app.parser import (RawPage, build_field_context, apply_field_names,
                        scan_capturing_groups, effective_pattern, available_fields,
                        diagnose_rules)


# --------------------------------------------------------------- group assignment
def test_scan_capturing_groups_skips_noncapturing():
    pat = r"(?:foo)\(lit\)[()x](\d+)(?P<kept>\w+)"
    g = scan_capturing_groups(pat)
    # Only the plain (\d+) and the named (?P<kept>) count; escaped/char-class/(?: don't.
    assert [x["number"] for x in g] == [1, 2]
    assert g[0]["name"] is None and g[1]["name"] == "kept"
    print("scan_capturing_groups OK")


def test_apply_field_names_basic_and_dedup():
    pat = r"INC:(\S+)\s+(\d+:\d+)"
    out = apply_field_names(pat, {"1": "incident", "2": "time"})
    assert out == r"INC:(?P<incident>\S+)\s+(?P<time>\d+:\d+)"
    # Duplicate field name must not produce an invalid regex.
    import re
    dup = apply_field_names(r"(\S+):(\S+)", {"1": "units", "2": "units"})
    re.compile(dup)  # would raise "redefinition of group name" if not de-duped
    assert dup.count("?P<units>") == 1
    print("apply_field_names OK")


def test_effective_pattern_and_diagnose():
    rule = {"name": "CFS", "pattern": r"INC:(\S+)\s+(\d+:\d+)",
            "groups": {"1": "incident", "2": "time"}}
    assert effective_pattern(rule) == r"INC:(?P<incident>\S+)\s+(?P<time>\d+:\d+)"
    d = diagnose_rules("INC:S0101 19:22", [rule])
    assert d["fields"] == {"incident": "S0101", "time": "19:22"}
    caps = d["rules"][0]["capture_groups"]
    assert caps[0]["field"] == "incident" and caps[0]["value"] == "S0101"
    print("effective_pattern + diagnose OK")


# --------------------------------------------------------------- alias
def test_capcode_alias_field():
    page = RawPage(capcode="1234567", function="0", message="hi", proto="T")
    ctx = build_field_context(page, {}, alias="Station 1 Dispatch")
    assert ctx["capcode_alias"] == "Station 1 Dispatch"
    # Falls back to the raw capcode when no alias.
    ctx2 = build_field_context(page, {}, alias=None)
    assert ctx2["capcode_alias"] == "1234567"
    # capcode_alias is a built-in placeable field.
    assert "capcode_alias" in available_fields([])
    print("capcode alias OK")


# --------------------------------------------------------------- timezone
def test_timezone_formatting():
    # A fixed naive local moment, formatted in a specific zone, differs from UTC.
    page = RawPage(capcode="1", function="0", message="m", proto="T")
    ctx_local = build_field_context(page, {}, tz_name=None)
    ctx_utc = build_field_context(page, {}, tz_name="UTC")
    # Both must produce HH:MM:SS strings; the point is no crash + valid zone.
    assert len(ctx_local["time"].split(":")) == 3
    assert len(ctx_utc["time"].split(":")) == 3
    # An invalid zone falls back gracefully (no exception).
    ctx_bad = build_field_context(page, {}, tz_name="Not/AZone")
    assert "datetime" in ctx_bad
    print("timezone formatting OK:", ctx_utc["datetime"])


# --------------------------------------------------------------- available fields
def test_available_fields_from_rules():
    rules = [{"name": "r", "pattern": r"(\S+) (\S+)", "groups": {"1": "incident", "2": "units"}}]
    fields = available_fields(rules)
    # Built-ins first, then rule fields, de-duplicated.
    assert fields[:5] == ["date", "time", "datetime", "capcode", "capcode_alias"]
    assert "incident" in fields and "units" in fields
    print("available_fields OK:", fields)


# --------------------------------------------------------------- multi-page PDF
def test_multipage_template_preserved():
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas
    from app import pdfgen

    # Build a 2-page template.
    tpl = Path(tempfile.gettempdir()) / "pager_tpl_2page.pdf"
    c = canvas.Canvas(str(tpl))
    c.drawString(72, 700, "PAGE ONE"); c.showPage()
    c.drawString(72, 700, "PAGE TWO"); c.showPage()
    c.save()

    page = RawPage(capcode="1234567", function="0", message="m", proto="T")
    ctx = build_field_context(page, {"jobtype": "F2"})
    layout = {"page_width": 595, "page_height": 842,
              "fields": [{"name": "jobtype", "x": 90, "y": 740, "size": 14}]}
    out = Path(tempfile.gettempdir()) / "pager_2page_out.pdf"
    pdfgen.render_job_pdf(str(tpl), layout, ctx, str(out))
    assert len(PdfReader(str(out)).pages) == 2  # both template pages preserved
    print("multipage template OK")


# --------------------------------------------------------------- job filtering
def test_job_query_filters():
    from app.database import JobStore
    db_path = Path(tempfile.gettempdir()) / "pager_filter_jobs.db"
    db_path.unlink(missing_ok=True)  # fresh DB each run for deterministic counts
    db = JobStore(str(db_path))
    # Seed a couple of jobs.
    db.add_job(received_at=datetime(2026, 1, 1, 10, 0), capcode="1234567",
               jobtype="FIRE", message="house fire main st", fields={},
               pdf_path=None, printed=True, print_error=None, matched_rule=None,
               attempted_print=True)
    db.add_job(received_at=datetime(2026, 6, 1, 10, 0), capcode="9999999",
               jobtype="TREE", message="tree down oak rd", fields={},
               pdf_path=None, printed=False, print_error=None, matched_rule=None,
               attempted_print=False)
    assert db.count_jobs(capcode="1234567") == 1
    assert db.count_jobs(jobtype="TREE") == 1
    assert db.count_jobs(q="fire") == 1
    assert db.count_jobs(printed="yes") == 1
    assert db.count_jobs(date_from="2026-05-01") == 1  # only the June job
    rows = db.query_jobs(q="oak")
    assert len(rows) == 1 and rows[0]["capcode"] == "9999999"
    print("job query filters OK")


# --------------------------------------------------------------- auth
def test_password_hash_and_session():
    from app import auth
    h = auth.hash_password("hunter2")
    assert h.startswith("pbkdf2$")
    assert auth.verify_password("hunter2", h) is True
    assert auth.verify_password("wrong", h) is False
    # Session token is deterministic for a given hash+secret, and rejects junk.
    tok = auth.make_session_token()
    assert auth.valid_session(tok) is True
    assert auth.valid_session("deadbeef") is False
    assert auth.valid_session(None) is False
    print("auth hash + session OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [PASS] {name}")
    print("\nALL FEATURE TESTS PASSED")
