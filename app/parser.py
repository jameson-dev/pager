"""Parse multimon-ng output lines and apply field-extraction rules."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

# multimon-ng POCSAG/FLEX alpha line, e.g.:
#   POCSAG1200: Address:  408872  Function: 0  Alpha: STRUCTURE FIRE 12 MAIN ST<NUL>
# FLEX lines vary slightly; we capture the address (capcode) and the text after
# the first "Alpha:" / "Message:" marker.
_LINE_RE = re.compile(
    r"""(?P<proto>POCSAG\d+|FLEX[\w\-/]*)
        .*?
        Address:\s*(?P<capcode>\d+)
        (?:\s+Function:\s*(?P<function>\d+))?
        .*?
        (?:Alpha|Message):\s*(?P<text>.*)$
    """,
    re.VERBOSE,
)


@dataclass
class RawPage:
    capcode: str
    function: str | None
    message: str
    proto: str
    received_at: datetime = field(default_factory=datetime.now)


def parse_line(line: str) -> RawPage | None:
    """Parse a single multimon-ng line into a RawPage, or None if not an alpha page."""
    line = line.rstrip("\r\n")
    m = _LINE_RE.search(line)
    if not m:
        return None
    text = m.group("text")
    # Strip multimon's <NUL> markers and trailing control noise.
    text = text.replace("<NUL>", "").replace("<EOT>", "").strip()
    if not text:
        return None
    return RawPage(
        capcode=m.group("capcode"),
        function=m.group("function"),
        message=text,
        proto=m.group("proto"),
    )


def scan_capturing_groups(pattern: str) -> list[dict]:
    """
    Walk a regex and locate each *capturing* group's opening paren.

    Returns a list (in source order, i.e. by group number starting at 1) of
    {"number", "pos", "name"} where `pos` is the index of the "(" and `name` is
    the existing (?P<name>...) name if it has one, else None.

    Skips non-capturing/look-around groups `(?:`, `(?=`, `(?!`, `(?<=`, `(?<!`,
    `(?#...)`, flag groups `(?i)`, escaped parens `\\(`, and parens inside a
    character class `[...]`. This lets the editor treat plain `( )` groups as
    assignable fields while leaving everything else alone.
    """
    groups: list[dict] = []
    i, n = 0, len(pattern)
    in_class = False
    number = 0
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2  # skip escaped char
            continue
        if in_class:
            if c == "]":
                in_class = False
            i += 1
            continue
        if c == "[":
            in_class = True
            i += 1
            continue
        if c == "(":
            # Determine the group kind from what follows the "(".
            if pattern[i + 1 : i + 2] == "?":
                tok = pattern[i + 2 : i + 3]
                if tok == "P" and pattern[i + 3 : i + 4] == "<":
                    # Named capturing group: (?P<name>...) — counts toward numbering.
                    end = pattern.find(">", i + 4)
                    name = pattern[i + 4 : end] if end != -1 else None
                    number += 1
                    groups.append({"number": number, "pos": i, "name": name})
                # else (?:, (?=, (?!, (?<=, (?<!, (?#, (?i) etc. — not capturing.
            else:
                # Plain capturing group.
                number += 1
                groups.append({"number": number, "pos": i, "name": None})
        i += 1
    return groups


def apply_field_names(pattern: str, assignments: dict) -> str:
    """
    Produce a named-group pattern from a plain pattern + {group_number: field}.

    `assignments` maps a 1-based capturing-group number (as str or int) to a
    field name. Each targeted plain `(` becomes `(?P<field>`. Groups that are
    already named, or have no assignment, are left untouched. This is what lets
    the editor work in plain regex while the stored/executed pattern keeps the
    (?P<name>...) form the parser expects.
    """
    norm = {str(k): v for k, v in (assignments or {}).items() if v}
    if not norm:
        return pattern

    scanned = scan_capturing_groups(pattern)
    # A field name may only name ONE group, else re.compile raises
    # "redefinition of group name". If duplicates slip in (e.g. a hand-edited
    # config), keep the first (lowest group number) and drop the rest — and
    # don't clash with names already present via (?P<...>) in the pattern.
    used = {g["name"] for g in scanned if g["name"]}
    keep: dict[str, str] = {}
    for g in sorted(scanned, key=lambda x: x["number"]):
        name = norm.get(str(g["number"]))
        if not name or g["name"] is not None or name in used:
            continue
        used.add(name)
        keep[str(g["number"])] = name

    out = pattern
    # Inject right-to-left so earlier positions stay valid as we mutate.
    for g in sorted(scanned, key=lambda x: x["pos"], reverse=True):
        field_name = keep.get(str(g["number"]))
        if not field_name:
            continue
        pos = g["pos"]
        out = out[: pos + 1] + f"?P<{field_name}>" + out[pos + 1 :]
    return out


def effective_pattern(rule: dict) -> str:
    """The regex actually executed for a rule.

    If the rule carries a plain `pattern` plus a `groups` field-assignment map,
    expand it to a named-group pattern. Rules authored directly with
    (?P<name>...) (and no `groups` map) are returned unchanged.
    """
    pattern = rule.get("pattern") or ""
    return apply_field_names(pattern, rule.get("groups") or {})


# Built-in fields always available for PDF placement, regardless of rules.
BUILTIN_FIELDS = ["date", "time", "datetime", "capcode", "capcode_alias", "message"]


def rule_field_names(rule: dict) -> list[str]:
    """Field names a single rule produces (its named/assigned capture groups)."""
    try:
        names = list(re.compile(effective_pattern(rule)).groupindex.keys())
    except re.error:
        names = []
    return names


def available_fields(rules: list[dict]) -> list[str]:
    """All placeable field names: built-ins + every field the rules produce.

    De-duplicated, built-ins first, then rule fields in first-seen order.
    """
    seen = set(BUILTIN_FIELDS)
    out = list(BUILTIN_FIELDS)
    for rule in rules or []:
        for name in rule_field_names(rule):
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def rule_keywords(rule: dict) -> list[str]:
    """A rule's selector keywords (case-insensitive 'message contains' triggers).

    Stored as `match_keywords`: a list of strings, or a single comma/newline-
    separated string (tolerated for hand-edited configs). Empty = no selector,
    meaning the rule is considered for every message (legacy behaviour)."""
    kw = rule.get("match_keywords")
    if kw is None:
        return []
    if isinstance(kw, str):
        kw = re.split(r"[,\n]", kw)
    return [k.strip() for k in kw if k and k.strip()]


def keyword_hit(message: str, keywords: list[str]) -> str | None:
    """The first keyword found in `message` (case-insensitive), else None."""
    low = message.lower()
    for k in keywords:
        if k.lower() in low:
            return k
    return None


def rule_applies(message: str, rule: dict) -> tuple[bool, str | None]:
    """Does this rule's SELECTOR admit `message`? Returns (applies, reason).

    A rule with keywords applies only when one is present; a rule with no
    keywords applies to everything (so existing rules keep working). `reason`
    explains the decision for display in the tester / job detail."""
    keywords = rule_keywords(rule)
    if not keywords:
        return True, "no keywords (applies to all)"
    hit = keyword_hit(message, keywords)
    if hit is not None:
        return True, f"matched keyword “{hit}”"
    return False, None


def apply_rules(message: str, rules: list[dict]) -> tuple[dict, str | None]:
    """
    Choose a rule and extract its fields. A rule is chosen only if its SELECTOR
    (keyword 'contains' test) admits the message AND its extraction regex matches;
    the first such rule in order wins. Selection (keywords) is kept separate from
    extraction (regex) so a page is routed by a cheap, reliable signal even when
    the detailed extraction pattern is strict.

    Returns (fields, matched_rule_name). If no rule is chosen, returns ({}, None)
    and the caller stores the page with built-in fields only (raw message kept).
    """
    fields, name, _ = select_rule(message, rules)
    return fields, name


def select_rule(message: str, rules: list[dict]) -> tuple[dict, str | None, str | None]:
    """Like apply_rules but also returns a human `reason` for how the rule was
    chosen (or why nothing matched), for surfacing in the UI."""
    saw_keyword_no_regex: str | None = None
    for rule in rules:
        applies, _why = rule_applies(message, rule)
        if not applies:
            continue
        pattern = effective_pattern(rule)
        if not pattern:
            continue
        try:
            rx = re.compile(pattern)
        except re.error:
            continue
        m = rx.search(message)
        if m:
            fields = {k: (v.strip() if v else "") for k, v in m.groupdict().items()}
            if not fields.get("jobtype") and rule.get("jobtype_default"):
                fields["jobtype"] = rule["jobtype_default"]
            kws = rule_keywords(rule)
            hit = keyword_hit(message, kws) if kws else None
            reason = f"matched keyword “{hit}”" if hit else "matched (no keywords set)"
            return fields, rule.get("name"), reason
        # The keyword said "this is the right rule" but the regex didn't extract —
        # remember it so we can explain a likely malformed page of a known format.
        if saw_keyword_no_regex is None and rule_keywords(rule):
            hit = keyword_hit(message, rule_keywords(rule))
            if hit:
                saw_keyword_no_regex = f"{rule.get('name') or 'a rule'} (keyword “{hit}”)"
    if saw_keyword_no_regex:
        return {}, None, (f"no rule extracted fields — {saw_keyword_no_regex} matched by "
                          "keyword but its pattern didn't fit this message")
    return {}, None, "no rule matched — stored with raw message only"


def diagnose_rules(message: str, rules: list[dict]) -> dict:
    """
    Detailed, authoring-oriented view of how `rules` apply to `message`.

    Mirrors apply_rules' first-match-wins semantics but, instead of just the
    winning fields, reports for *every* rule:
      - status: "match" | "nomatch" | "error" | "empty"
      - error:  the regex compile error message (when status == "error")
      - span:   [start, end] of the whole match in the message (when matched)
      - groups: [{name, value, start, end}] for each named capture that
                participated (start/end are null if the group didn't match)
    Plus top-level `matched_index`/`matched_rule`/`fields` for the winning rule
    (the one the real pipeline would use).
    """
    results: list[dict] = []
    matched_index: int | None = None
    winning_fields: dict = {}
    winning_name: str | None = None

    for i, rule in enumerate(rules):
        raw_pattern = rule.get("pattern") or ""
        assignments = {str(k): v for k, v in (rule.get("groups") or {}).items()}
        pattern = effective_pattern(rule)
        # Map each capturing group NUMBER -> assigned field name (for the editor's
        # "Group N -> field" dropdowns). Pre-named groups keep their own name.
        scanned = scan_capturing_groups(raw_pattern)
        keywords = rule_keywords(rule)
        applies, _why = rule_applies(message, rule)
        kw_hit = keyword_hit(message, keywords) if keywords else None
        entry: dict = {
            "index": i,
            "name": rule.get("name") or f"rule {i + 1}",
            "status": "empty",
            "error": None,
            "span": None,
            "groups": [],          # named-group spans (winning-match highlight)
            "capture_groups": [],  # numbered plain groups + assigned field (editor)
            "jobtype_default": rule.get("jobtype_default") or "",
            # Selector (keyword) diagnostics, so the tester shows WHY a rule was
            # (not) chosen, separate from whether its regex extracts.
            "keywords": keywords,
            "selector_applies": applies,
            "keyword_hit": kw_hit,
        }
        if not pattern:
            results.append(entry)
            continue
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            entry["status"] = "error"
            entry["error"] = str(exc)
            results.append(entry)
            continue

        m = rx.search(message)
        if not m:
            # Distinguish "selector excluded this rule" from "regex didn't match":
            # a known-format page that's malformed shows up as keyword-hit but
            # nomatch, which is the useful signal.
            entry["status"] = "skipped" if not applies else "nomatch"
            # Even on no-match, expose the group numbers so dropdowns can render.
            for g in scanned:
                entry["capture_groups"].append(
                    {"number": g["number"], "field": assignments.get(str(g["number"])) or g["name"],
                     "value": None, "start": None, "end": None}
                )
            results.append(entry)
            continue

        # The regex matches; but if the selector excludes this rule it's NOT the
        # chosen one (it's reported as skipped, with its would-be captures shown).
        entry["status"] = "skipped" if not applies else "match"
        entry["span"] = [m.start(), m.end()]
        # Per named-group spans (so the UI can highlight each capture).
        for name in rx.groupindex:
            gtext = m.group(name)
            if gtext is None:
                entry["groups"].append({"name": name, "value": None, "start": None, "end": None})
            else:
                entry["groups"].append(
                    {"name": name, "value": gtext, "start": m.start(name), "end": m.end(name)}
                )
        # Numbered capturing groups with the text they matched, for the editor.
        for g in scanned:
            num = g["number"]
            gtext = m.group(num)
            entry["capture_groups"].append({
                "number": num,
                "field": assignments.get(str(num)) or g["name"],
                "value": gtext,
                "start": m.start(num) if gtext is not None else None,
                "end": m.end(num) if gtext is not None else None,
            })
        results.append(entry)

        # First APPLICABLE match wins — record the winner but keep diagnosing
        # later rules so the author can still see what they would have matched.
        if matched_index is None and applies:
            matched_index = i
            winning_fields = {k: (v.strip() if v else "") for k, v in m.groupdict().items()}
            if not winning_fields.get("jobtype") and rule.get("jobtype_default"):
                winning_fields["jobtype"] = rule["jobtype_default"]
            winning_name = rule.get("name")

    # Overall, human-readable account of how the rule was chosen (or why not),
    # matching what the live pipeline (select_rule) would decide.
    _f, _n, selection_reason = select_rule(message, rules)
    return {
        "message": message,
        "matched_index": matched_index,
        "matched_rule": winning_name,
        "fields": winning_fields,
        "selection_reason": selection_reason,
        "rules": results,
    }


def resolve_timezone(tz_name: str | None):
    """Return a tzinfo for `tz_name`, or None to use the server's local time.

    Unknown/empty names fall back to local time (never raises).
    """
    if not tz_name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001  (bad name, missing tzdata, etc.)
        return None


def build_field_context(page: RawPage, extracted: dict, alias: str | None = None,
                        tz_name: str | None = None) -> dict:
    """Combine built-in fields (date/time/capcode/message/alias) with extracted ones.

    `alias` is the friendly name configured for the capcode (its label); it's
    exposed as the `capcode_alias` field. `tz_name` (e.g. "Australia/Adelaide")
    formats the date/time/datetime fields in that zone; falls back to local time.
    """
    when = page.received_at
    tz = resolve_timezone(tz_name)
    if tz is not None:
        # received_at is naive local time; attach local zone then convert.
        from datetime import timezone as _dt_timezone
        if when.tzinfo is None:
            when = when.astimezone()  # assume local
        when = when.astimezone(tz)
    ctx = {
        "capcode": page.capcode,
        "capcode_alias": alias or page.capcode,
        "message": page.message,
        "date": when.strftime("%d/%m/%Y"),
        "time": when.strftime("%H:%M:%S"),
        "datetime": when.strftime("%d/%m/%Y %H:%M:%S"),
    }
    ctx.update(extracted)
    return ctx
