"""Background print-retry worker.

When a print fails, the job id is queued here. The worker periodically retries
failed jobs (up to a max attempt count) and publishes status so the UI can show
a failed-jobs banner.
"""
from __future__ import annotations

import logging
import threading
import time

from . import config as cfg
from . import events
from . import notify
from . import printing
from .database import JobStore

log = logging.getLogger("pager.retry")

# Defaults; overridable per-deployment via config (print_max_attempts /
# print_retry_interval_seconds), editable from Settings. MAX_ATTEMPTS stays a
# module constant for the many call sites that pass it to the JobStore queries;
# it's refreshed from config at the start of each retry pass.
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_INTERVAL_SECONDS = 60
MAX_ATTEMPTS = DEFAULT_MAX_ATTEMPTS


def configured_max_attempts(conf: dict | None = None) -> int:
    conf = conf if conf is not None else cfg.load_config()
    try:
        return max(1, int(conf.get("print_max_attempts", DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ATTEMPTS


def configured_retry_interval(conf: dict | None = None) -> int:
    conf = conf if conf is not None else cfg.load_config()
    try:
        return max(5, int(conf.get("print_retry_interval_seconds", DEFAULT_RETRY_INTERVAL_SECONDS)
                          or DEFAULT_RETRY_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_INTERVAL_SECONDS


class RetryWorker(threading.Thread):
    def __init__(self, store: JobStore):
        super().__init__(daemon=True, name="RetryWorker")
        self.store = store
        self._stop = threading.Event()
        self._wake = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def nudge(self) -> None:
        """Ask the worker to attempt a pass now (e.g. after a new failure)."""
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._pass()
            except Exception as exc:  # noqa: BLE001
                log.exception("Retry pass failed: %s", exc)
            # Sleep until the (configurable) interval elapses or someone nudges us.
            self._wake.wait(configured_retry_interval())
            self._wake.clear()

    def _pass(self) -> None:
        global MAX_ATTEMPTS
        conf = cfg.load_config()
        # Keep the module-level constant in step with config so the API endpoints
        # that import MAX_ATTEMPTS (failed-count, retry-all) agree with the worker.
        MAX_ATTEMPTS = configured_max_attempts(conf)
        max_attempts = MAX_ATTEMPTS
        pending = self.store.list_failed_unresolved(max_attempts=max_attempts)
        if not pending:
            return
        printer = conf.get("printer_name", "")
        for job in pending:
            ok, err = printing.print_pdf(printer, job["pdf_path"], title=f"Retry {job['capcode']}")
            attempts = (job.get("print_attempts") or 0) + 1
            self.store.update_print_result(job["id"], ok, err, attempts)
            log.info("Retry job %s attempt %s ok=%s", job["id"], attempts, ok)
            if ok:
                events.publish("print_recovered", {"job_id": job["id"]})
            elif attempts >= max_attempts:
                # Exhausted retries — alert off-screen so a dead printer is noticed.
                notify.send("print_failed",
                            f"Print FAILED after {attempts} attempts for job {job['id']} "
                            f"(capcode {job['capcode']}): {err or 'unknown error'}",
                            job_id=job["id"], capcode=job["capcode"], error=err)
        events.publish("print_status", {"failed": self.store.count_failed_unresolved(max_attempts)})
