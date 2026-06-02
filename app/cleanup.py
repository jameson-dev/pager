"""Retention cleanup: delete old job PDFs and DB rows past the retention window.

Runs once on startup and then daily. Controlled by config:
    retention_days: 90        # 0 disables cleanup
    retention_delete_pdf: true
    retention_delete_rows: true   # also remove the DB history rows
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config as cfg
from .database import JobStore

log = logging.getLogger("pager.cleanup")

RUN_INTERVAL_SECONDS = 24 * 3600


class CleanupWorker(threading.Thread):
    def __init__(self, store: JobStore):
        super().__init__(daemon=True, name="CleanupWorker")
        self.store = store
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                log.exception("Cleanup failed: %s", exc)
            self._stop.wait(RUN_INTERVAL_SECONDS)

    def run_once(self) -> dict:
        """Returns a summary dict (also used by the manual 'run now' endpoint)."""
        conf = cfg.load_config()
        days = int(conf.get("retention_days", 0) or 0)
        if days <= 0:
            return {"enabled": False, "deleted_pdfs": 0, "deleted_rows": 0}

        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        old = self.store.list_old_jobs(cutoff)
        deleted_pdfs = 0
        if conf.get("retention_delete_pdf", True):
            for job in old:
                p = job.get("pdf_path")
                if p and Path(p).exists():
                    try:
                        Path(p).unlink()
                        deleted_pdfs += 1
                    except OSError as exc:
                        log.warning("Could not delete %s: %s", p, exc)

        deleted_rows = 0
        if conf.get("retention_delete_rows", True):
            deleted_rows = self.store.delete_jobs([j["id"] for j in old])

        log.info("Retention cleanup: %s pdfs, %s rows older than %s days",
                 deleted_pdfs, deleted_rows, days)
        return {
            "enabled": True,
            "cutoff": cutoff,
            "candidates": len(old),
            "deleted_pdfs": deleted_pdfs,
            "deleted_rows": deleted_rows,
        }
