"""In-process event bus + runtime health state + print retry queue.

Kept dependency-free (stdlib only) so it imports anywhere. The FastAPI layer
subscribes to the bus for Server-Sent Events; the watcher/processor publish to it.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime
from typing import Any

# ----------------------------------------------------------------------------- bus
_subscribers: set[queue.Queue] = set()
_sub_lock = threading.Lock()


def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=100)
    with _sub_lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _sub_lock:
        _subscribers.discard(q)


def publish(event: str, data: dict[str, Any]) -> None:
    """Fan out an event to all SSE subscribers. Never blocks/raises."""
    payload = {"event": event, "data": data}
    with _sub_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.discard(q)


# ----------------------------------------------------------------------------- health
class Health:
    """Tracks feed liveness. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.last_line_at: float | None = None   # any line from the log
        self.last_page_at: float | None = None    # a parsed, monitored page
        self.total_lines = 0
        self.total_pages = 0

    def mark_line(self) -> None:
        with self._lock:
            self.last_line_at = time.time()
            self.total_lines += 1

    def mark_page(self) -> None:
        with self._lock:
            self.last_page_at = time.time()
            self.total_pages += 1

    def snapshot(self, stale_after_seconds: int) -> dict:
        with self._lock:
            now = time.time()
            last_line_age = (now - self.last_line_at) if self.last_line_at else None
            last_page_age = (now - self.last_page_at) if self.last_page_at else None
            # "stale" = no decoder lines at all for longer than the threshold.
            # (Pages are sparse by nature; raw lines should be steady-ish.)
            stale = last_line_age is None or last_line_age > stale_after_seconds
            return {
                "uptime_seconds": int(now - self.started_at),
                "last_line_age_seconds": int(last_line_age) if last_line_age is not None else None,
                "last_page_age_seconds": int(last_page_age) if last_page_age is not None else None,
                "last_line_at": datetime.fromtimestamp(self.last_line_at).isoformat(timespec="seconds") if self.last_line_at else None,
                "last_page_at": datetime.fromtimestamp(self.last_page_at).isoformat(timespec="seconds") if self.last_page_at else None,
                "total_lines": self.total_lines,
                "total_pages": self.total_pages,
                "stale": stale,
                "stale_after_seconds": stale_after_seconds,
            }


health = Health()


def sse_format(payload: dict) -> str:
    """Encode a bus payload as an SSE frame."""
    return f"event: {payload['event']}\ndata: {json.dumps(payload['data'])}\n\n"
