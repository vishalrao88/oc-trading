"""Token-bucket rate limiter with adaptive 429 widening.

Each external API gets its own RateLimiter instance. The limiter has two
behaviors:

1. **Configured floor**: enforced by spacing calls at least
   `60 / calls_per_minute` seconds apart. This is the deterministic part —
   we pre-plan to stay under known limits.

2. **Adaptive widening**: when a caller reports a 429 (rate-limit response
   from the API), the limiter doubles its spacing temporarily. Each
   subsequent OK report narrows the additional spacing back toward the
   configured floor. This handles unpublished limits (yfinance) and
   transient tightening.

Time injection: `now_fn` and `sleep_fn` can be overridden in tests so we
can verify timing without actually sleeping.
"""

from __future__ import annotations

import threading
import time as time_module
from typing import Callable

from portfolio_lib.config import RateLimitConfig


class RateLimitExceeded(Exception):
    """Raised when acquire() would block longer than max_wait_seconds."""


class RateLimiter:
    """Thread-safe rate limiter with adaptive widening on 429.

    Usage:
        limiter = RateLimiter(FINNHUB_LIMITS)
        limiter.acquire()
        response = requests.get(...)
        if response.status_code == 429:
            limiter.report_429()
            # then retry with backoff
        else:
            limiter.report_ok()
    """

    def __init__(
        self,
        config: RateLimitConfig,
        now_fn: Callable[[], float] = time_module.monotonic,
        sleep_fn: Callable[[float], None] = time_module.sleep,
    ):
        self.config = config
        self._now = now_fn
        self._sleep = sleep_fn
        self._lock = threading.Lock()

        # The base interval enforces the configured floor.
        self._base_interval = 60.0 / config.calls_per_minute

        # Multiplier on top of base interval; widens on 429, narrows on ok.
        # Floor is 1.0 (the configured rate); capped at 8x to prevent runaway.
        self._widening = 1.0

        # When the next call is allowed (monotonic timestamp).
        self._next_allowed: float = 0.0

    @property
    def current_interval(self) -> float:
        """Current effective interval between calls, in seconds."""
        return self._base_interval * self._widening

    def acquire(self) -> None:
        """Block until a call is permitted. Raise if wait exceeds max."""
        with self._lock:
            now = self._now()
            wait = self._next_allowed - now
            if wait > self.config.max_wait_seconds:
                raise RateLimitExceeded(
                    f"{self.config.name}: would wait {wait:.1f}s, "
                    f"exceeds max {self.config.max_wait_seconds}s"
                )
            sleep_for = max(0.0, wait)
            # Schedule the next call slot *before* sleeping, so concurrent
            # callers serialize correctly.
            self._next_allowed = max(now, self._next_allowed) + self.current_interval

        if sleep_for > 0:
            self._sleep(sleep_for)

    def report_429(self) -> None:
        """Widen the interval after a rate-limit response.

        Doubles the widening factor, capped at 8x the base rate.
        Also pushes the next-allowed time out further to give the
        API a chance to cool down.
        """
        with self._lock:
            self._widening = min(self._widening * 2.0, 8.0)
            now = self._now()
            self._next_allowed = max(self._next_allowed, now) + self.current_interval

    def report_ok(self) -> None:
        """Narrow the interval after a successful call.

        Multiplies the widening factor by 0.9 each time, bottoming out at
        1.0 (the configured rate). After several consecutive OKs, we're
        back at the configured floor.
        """
        with self._lock:
            self._widening = max(self._widening * 0.9, 1.0)
