"""Tail the multimon-ng log file and feed new lines to the processor.

Uses a simple `tail -F`-style poll loop: robust against log rotation and
truncation, and has no inotify quirks across filesystems. Runs in a daemon
thread started by the FastAPI app.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from . import events
from .database import JobStore
from .parser import parse_line
from .processor import process_page

log = logging.getLogger("pager.watcher")


class LogWatcher(threading.Thread):
    def __init__(self, log_path: str, store: JobStore, poll_interval: float = 0.5):
        super().__init__(daemon=True, name="LogWatcher")
        self.log_path = log_path
        self.store = store
        self.poll_interval = poll_interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def set_path(self, new_path: str) -> None:
        """Switch the watched file at runtime (e.g. log_file changed in Settings).

        The follow loop notices on its next poll and reopens the new path.
        """
        if new_path and new_path != self.log_path:
            log.info("Switching watched log: %s -> %s", self.log_path, new_path)
            self.log_path = new_path

    def run(self) -> None:
        log.info("Watching %s", self.log_path)
        while not self._stop.is_set():
            try:
                self._follow()
            except FileNotFoundError:
                # Log not created yet; wait for reader.sh to start producing.
                time.sleep(2.0)
            except Exception as exc:  # noqa: BLE001
                log.exception("Watcher error, retrying: %s", exc)
                time.sleep(2.0)

    def _follow(self) -> None:
        opened_path = self.log_path
        path = Path(opened_path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            # Start at end of file: only process pages that arrive from now on.
            fh.seek(0, 2)
            inode = path.stat().st_ino
            while not self._stop.is_set():
                # Path switched at runtime (Settings change) — reopen the new one.
                if self.log_path != opened_path:
                    return
                line = fh.readline()
                if line:
                    self._handle(line)
                    continue
                time.sleep(self.poll_interval)
                # Detect rotation/truncation.
                try:
                    if path.stat().st_ino != inode or path.stat().st_size < fh.tell():
                        log.info("Log rotated/truncated, reopening")
                        return
                except FileNotFoundError:
                    return

    def _handle(self, line: str) -> None:
        # Any non-blank decoder line counts toward feed liveness, even if it's
        # not an alpha page we keep — that's how we detect a dead SDR/decoder.
        if line.strip():
            events.health.mark_line()
        page = parse_line(line)
        if not page:
            return
        events.health.mark_page()
        try:
            process_page(page, self.store)
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to process page: %s", exc)
