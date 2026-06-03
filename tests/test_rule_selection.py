"""Tests for keyword-based rule selection (selector vs. extraction).

A rule's keywords decide WHICH message format it applies to; its regex extracts
fields. Selection is first-applicable-match-wins, and a page that matches no rule
is kept with its raw message (no fields).

Run:  python tests/test_rule_selection.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_cfg = Path(tempfile.mkdtemp(prefix="pager_sel_")) / "config"
shutil.copytree(ROOT / "config", _cfg)
os.environ["PAGER_CONFIG"] = str(_cfg)

from app.parser import diagnose_rules, rule_applies, select_rule

CFSRES = (r"INC:(\S+)\s+(\d{1,3}/\d{1,2}/\d{2,4})\s+(\d{1,2}:\d{2})\s+RESPOND\s+"
          r"(.+?)\s+P(\d+)\s*:\s*(.+?)\s+MAP:,?(.+?)\s*,==\s*(.+?)\s*:\s*(.+?)\s*:\s*$")

RULES = [
    {"name": "CFSRES Paging", "pattern": CFSRES, "match_keywords": ["CFSRES", "INC:"],
     "groups": {"1": "incident", "2": "date", "3": "time", "4": "jobtype",
                "5": "priority", "6": "details", "7": "mapref",
                "8": "crossstreet", "9": "units"}},
    {"name": "SAAS MG", "pattern": r"MG\d+ PR:\s*(\d+)\s*-\s*(.+?)\s+Disp:\s*(\d{1,2}:\d{2})\s+(.+)$",
     "match_keywords": ["PR:", "Disp:"],
     "groups": {"1": "priority", "2": "location", "3": "time", "4": "jobtype"}},
]

MSG_CFSRES = ("MFS: *CFSRES INC:S0101 01/6/26 19:22 RESPOND TREE DOWN P1 : APPROX "
              "500M EAST OF JUBILEE HWY ROUNDABOUT MOUNT GAMBIER MAP:,MGB 122D 2210 "
              ",== TREE PARTIALLY OVER ROAD, WEST BOUND SIDE :MTG20_12:")
MSG_SAAS = "MG71 PR: 5 - MOUNT GAMBIER MGB 4 D 7 D00834 Disp: 14:14 Psychiatri"


def test_routes_to_correct_rule_by_keyword():
    f1, n1, _ = select_rule(MSG_CFSRES, RULES)
    assert n1 == "CFSRES Paging"
    assert f1["incident"] == "S0101" and f1["mapref"] == "MGB 122D 2210"

    f2, n2, _ = select_rule(MSG_SAAS, RULES)
    assert n2 == "SAAS MG"
    assert f2["priority"] == "5" and f2["time"] == "14:14"
    print("ok: each format routes to its own rule by keyword")


def test_selector_excludes_other_rule():
    # On the CFSRES message, the SAAS rule must be skipped (its keywords absent),
    # even though it should never have matched anyway.
    applies, _ = rule_applies(MSG_CFSRES, RULES[1])
    assert applies is False
    diag = diagnose_rules(MSG_CFSRES, RULES)
    saas = next(r for r in diag["rules"] if r["name"] == "SAAS MG")
    assert saas["status"] == "skipped"
    assert diag["matched_rule"] == "CFSRES Paging"
    print("ok: selector excludes the non-applicable rule")


def test_unmatched_kept_raw():
    f, n, reason = select_rule("RANDOM PAGE 123 NO FORMAT", RULES)
    assert n is None and f == {} and "no rule" in reason.lower()
    print("ok: unmatched page kept with raw message only")


def test_keyword_hit_but_regex_fails():
    bad = "CFSRES INC:S0101 01/6/26 19:22 RESPOND TREE DOWN P1 : no map section here"
    f, n, reason = select_rule(bad, RULES)
    assert n is None and "keyword" in reason.lower()
    print("ok: keyword-hit-but-malformed is reported distinctly")


def test_no_keywords_applies_to_all():
    # A rule without keywords keeps legacy behaviour: considered for every message.
    rules = [{"name": "catch-all", "pattern": r"(\w+)", "groups": {"1": "first"}}]
    applies, why = rule_applies("anything at all", rules[0])
    assert applies and "applies to all" in why
    _f, n, _r = select_rule("anything at all", rules)
    assert n == "catch-all"
    print("ok: keyword-less rule applies to all (back-compat)")


if __name__ == "__main__":
    test_routes_to_correct_rule_by_keyword()
    test_selector_excludes_other_rule()
    test_unmatched_kept_raw()
    test_keyword_hit_but_regex_fails()
    test_no_keywords_applies_to_all()
    print("\nAll rule-selection tests passed.")
