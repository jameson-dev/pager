"""Turn a parsed page into a stored, rendered, (optionally) printed job."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from . import config as cfg
from . import events, pdfgen, printing
from .database import JobStore
from .parser import RawPage, build_field_context, select_rule

log = logging.getLogger("pager.processor")

# Set by main.py at startup so the processor can nudge the retry worker.
retry_worker = None


def monitored_capcodes(conf: dict) -> dict[str, dict]:
    return {str(c["code"]): c for c in conf.get("capcodes", [])}


def active_template(conf: dict) -> str:
    """
    Resolve the template PDF path. Supports a `templates` list of
    {name, path} plus `active_template` (name). Falls back to the legacy
    single `template_pdf` key.
    """
    templates = conf.get("templates") or []
    active = conf.get("active_template")
    if templates and active:
        for t in templates:
            if t.get("name") == active:
                return t.get("path", "")
    if templates:
        return templates[0].get("path", "")
    return conf.get("template_pdf", "")


def should_print(conf: dict, capcode: str, jobtype: str | None) -> bool:
    """global AND per-capcode AND per-jobtype must all allow printing."""
    if not conf.get("global_print_enabled", False):
        return False

    cap = monitored_capcodes(conf).get(capcode)
    if cap is not None and not cap.get("print_enabled", True):
        return False

    if jobtype:
        jt = conf.get("jobtypes", {}).get(jobtype)
        if jt is not None and not jt.get("print_enabled", True):
            return False

    return True


def _copies_for(conf: dict, capcode: str) -> int:
    cap = monitored_capcodes(conf).get(capcode, {})
    return max(1, int(cap.get("copies", 1) or 1))


def _do_print(conf: dict, pdf_path: str, capcode: str, title: str) -> tuple[bool, str | None]:
    """Print N copies; success if all copies succeed."""
    copies = _copies_for(conf, capcode)
    printer = conf.get("printer_name", "")
    last_err = None
    for _ in range(copies):
        ok, err = printing.print_pdf(printer, pdf_path, title=title)
        if not ok:
            last_err = err
            return False, last_err
    return True, None


def process_page(page: RawPage, store: JobStore, *, is_test: bool = False,
                 alias_override: str | None = None) -> dict | None:
    """
    Full pipeline for one page. Returns the stored job dict, or None if the
    capcode is not monitored.

    `alias_override` lets an ingest source supply its own label (e.g. the
    PagerMon DB already resolves the capcode alias); the configured label is
    used as a fallback when it's not given.
    """
    conf = cfg.load_config()
    monitored = monitored_capcodes(conf)
    if page.capcode not in monitored:
        log.debug("Ignoring unmonitored capcode %s", page.capcode)
        return None

    rules = cfg.load_rules().get("rules", [])
    layout = cfg.load_layout()

    extracted, matched_rule, match_reason = select_rule(page.message, rules)
    alias = alias_override or (monitored.get(page.capcode) or {}).get("label") or None
    context = build_field_context(page, extracted, alias=alias, tz_name=conf.get("timezone"))
    # Record how the rule was chosen (kept in the fields JSON — no schema change)
    # so the Jobs list/detail can show why a page was routed the way it was.
    context["_match_reason"] = match_reason
    jobtype = context.get("jobtype")

    # Render PDF.
    ts = page.received_at.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(conf.get("output_dir", "data/jobs"))
    prefix = "test" if is_test else "job"
    out_path = str(out_dir / f"{prefix}_{ts}_{page.capcode}.pdf")
    try:
        pdfgen.render_job_pdf(active_template(conf), layout, context, out_path)
    except Exception as exc:  # noqa: BLE001
        log.exception("PDF render failed: %s", exc)
        out_path = None

    # Decide + do printing.
    printed = False
    print_error = None
    attempted = False
    if out_path and should_print(conf, page.capcode, jobtype):
        attempted = True
        printed, print_error = _do_print(conf, out_path, page.capcode, f"Page {page.capcode}")
        if print_error:
            log.warning("Print failed for job %s: %s", page.capcode, print_error)

    job_id = store.add_job(
        received_at=page.received_at,
        capcode=page.capcode,
        jobtype=jobtype,
        message=page.message,
        fields=context,
        pdf_path=out_path,
        printed=printed,
        print_error=print_error,
        matched_rule=matched_rule,
        attempted_print=attempted,
        is_test=is_test,
    )
    log.info("Stored job %s (capcode=%s type=%s printed=%s)", job_id, page.capcode, jobtype, printed)

    job = store.get_job(job_id)
    # Push to UI for live alert.
    events.publish("new_job", {"job": job, "is_test": is_test})
    if attempted and not printed:
        events.publish("print_status", {"failed": store.count_failed_unresolved(5)})
        if retry_worker:
            retry_worker.nudge()
    return job


def reprint_job(job_id: int, store: JobStore) -> tuple[bool, str | None]:
    """Force-reprint an existing job, bypassing the gating toggles."""
    conf = cfg.load_config()
    job = store.get_job(job_id)
    if not job:
        return False, "Job not found"
    if not job.get("pdf_path") or not Path(job["pdf_path"]).exists():
        return False, "PDF for this job no longer exists"
    ok, err = _do_print(conf, job["pdf_path"], job["capcode"], f"Reprint {job['capcode']}")
    store.mark_printed(job_id, ok, err)
    events.publish("print_status", {"failed": store.count_failed_unresolved(5)})
    return ok, err


def inject_test_page(store: JobStore, capcode: str, message: str) -> dict | None:
    """Synthesize a page (as if received) for layout/printer testing."""
    page = RawPage(capcode=capcode, function="0", message=message, proto="TEST")
    return process_page(page, store, is_test=True)
