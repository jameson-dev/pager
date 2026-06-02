"""Off-screen alerting via an outgoing webhook.

If `alert_webhook_url` is set in config, important reliability events (a print
that won't succeed, or the feed going stale) POST a small JSON payload so they
can be noticed without a browser tab open — wire it to Slack/Discord/ntfy/etc.
Best-effort and non-blocking: failures are logged, never raised.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.request

from . import config as cfg
from . import events

log = logging.getLogger("pager.notify")


def _post(url: str, payload: dict) -> None:
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:  # noqa: BLE001
        log.warning("Webhook POST failed: %s", exc)


def send(event: str, text: str, **extra) -> None:
    """Fire a webhook for `event` with a human `text` line, off the caller's thread."""
    try:
        url = cfg.load_config().get("alert_webhook_url") or ""
    except Exception:  # noqa: BLE001
        url = ""
    if not url:
        return
    payload = {"event": event, "text": text, "app": "pager", **extra}
    threading.Thread(target=_post, args=(url, payload), daemon=True).start()


class WatchdogNotifier(threading.Thread):
    """Periodically check feed liveness and webhook on the OK->stale transition
    (and again when it recovers), so a silently-dead decoder is noticed."""

    def __init__(self, check_interval: float = 60.0):
        super().__init__(daemon=True, name="WatchdogNotifier")
        self.check_interval = check_interval
        self._stop = threading.Event()
        self._was_stale = False

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                conf = cfg.load_config()
                stale_after = int(conf.get("watchdog_stale_seconds", 3600))
                snap = events.health.snapshot(stale_after)
                # Only alert once we've actually seen the feed (avoid firing at
                # cold start before any line has ever arrived).
                if snap["last_line_at"] is not None:
                    if snap["stale"] and not self._was_stale:
                        self._was_stale = True
                        send("feed_stale",
                             f"Feed STALE: no decoder output for over {stale_after}s "
                             "— check the SDR / multimon-ng / reader.sh.")
                    elif not snap["stale"] and self._was_stale:
                        self._was_stale = False
                        send("feed_recovered", "Feed recovered: decoder output resumed.")
            except Exception as exc:  # noqa: BLE001
                log.warning("Watchdog notifier error: %s", exc)
            self._stop.wait(self.check_interval)
