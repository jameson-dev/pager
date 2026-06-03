"""Poll a PagerMon SQLite database for new pages and feed the pipeline.

The DB-backed counterpart to `LogWatcher`. Runs in a daemon thread, re-probes
the schema when the configured path changes, and processes rows with id greater
than the last one seen so each page is handled exactly once. Mirrors the log
watcher's health-marking so the feed-stale watchdog works identically.
"""
from __future__ import annotations

import logging
import threading
import time

from . import config as cfg
from . import events, pagermon_db
from .database import JobStore
from .processor import process_page

log = logging.getLogger("pager.pagermon_watcher")


class PagerMonDbWatcher(threading.Thread):
    def __init__(self, db_path: str, store: JobStore, poll_interval: float = 2.0):
        super().__init__(daemon=True, name="PagerMonDbWatcher")
        self.db_path = db_path
        self.store = store
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._mapping: pagermon_db.DbMapping | None = None
        self._last_id = 0
        self._probed_path: str | None = None

    def stop(self) -> None:
        self._stop.set()

    def set_path(self, new_path: str) -> None:
        """Switch the polled DB at runtime (Settings change). Re-probes next tick."""
        if new_path and new_path != self.db_path:
            log.info("Switching PagerMon DB: %s -> %s", self.db_path, new_path)
            self.db_path = new_path

    def mapping(self) -> pagermon_db.DbMapping | None:
        return self._mapping

    def _ensure_probed(self) -> bool:
        """(Re)probe when the path changed. Returns True if usable."""
        if self._probed_path == self.db_path and self._mapping and self._mapping.detected:
            return True
        override = cfg.load_config().get("pagermon_db_mapping") or {}
        self._mapping = pagermon_db.probe_schema(self.db_path, override)
        self._probed_path = self.db_path
        if self._mapping.detected:
            # Start at the current high-water mark: only ingest pages from now on.
            self._last_id = pagermon_db.latest_id(self.db_path, self._mapping)
            log.info("PagerMon DB ready (%s); starting after id %d",
                     self._mapping.note, self._last_id)
            return True
        log.warning("PagerMon DB not usable: %s", self._mapping.note)
        return False

    def run(self) -> None:
        log.info("Polling PagerMon DB %s", self.db_path)
        while not self._stop.is_set():
            try:
                if self._ensure_probed():
                    self._poll_once()
            except Exception as exc:  # noqa: BLE001
                log.exception("PagerMon DB watcher error, retrying: %s", exc)
            self._stop.wait(self.poll_interval)

    def _poll_once(self) -> None:
        assert self._mapping is not None
        rows, high = pagermon_db.fetch_new(self.db_path, self._mapping, self._last_id)
        # Any successful read of the DB counts as feed liveness — even zero new
        # rows means PagerMon's DB is reachable (distinct from a dead decoder).
        events.health.mark_line()
        for rid, page, alias in rows:
            events.health.mark_page()
            try:
                process_page(page, self.store, alias_override=alias)
            except Exception as exc:  # noqa: BLE001
                log.exception("Failed to process PagerMon row %s: %s", rid, exc)
        self._last_id = high
