from __future__ import annotations

import time
from threading import Lock

from samsung_auto_trader.config import config


_request_lock = Lock()
_last_request_monotonic: float | None = None


def throttle_kis_request() -> None:
    global _last_request_monotonic

    interval = config.kis_min_request_interval_seconds
    if interval <= 0:
        return

    with _request_lock:
        now = time.monotonic()
        if _last_request_monotonic is not None:
            wait_seconds = interval - (now - _last_request_monotonic)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
                now = time.monotonic()
        _last_request_monotonic = now


def reset_kis_rate_limiter() -> None:
    global _last_request_monotonic

    with _request_lock:
        _last_request_monotonic = None
