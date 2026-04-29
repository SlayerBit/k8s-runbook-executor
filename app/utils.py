"""
utils.py — Shared helpers used across the application.

Contains:
  - retry_with_backoff  : decorator / context-free retry helper
  - CooldownTracker     : per-action rate limiter (in-memory)
  - RunbookDeduplicator : prevents the same runbook_id from running twice
"""

from __future__ import annotations

import functools
import logging
import time
from threading import Lock
from typing import Any, Callable, Dict, Optional, Tuple, Type

logger = logging.getLogger(__name__)


# ── Retry with exponential backoff ────────────────────────────────────────────

def retry_with_backoff(
    max_attempts: int = 3,
    backoff_factor: float = 2.0,
    retriable_exceptions: Tuple[Type[Exception], ...] = (Exception,),
    logger_name: str = __name__,
) -> Callable:
    """
    Decorator that retries the wrapped function up to *max_attempts* times
    with exponential backoff between retries.

    Usage::

        @retry_with_backoff(max_attempts=3, backoff_factor=2.0)
        def flaky_operation():
            ...
    """
    _log = logging.getLogger(logger_name)

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            delay = 1.0
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except retriable_exceptions as exc:
                    if attempt == max_attempts:
                        _log.error(
                            "Function %s failed after %d attempts: %s",
                            fn.__name__,
                            max_attempts,
                            exc,
                        )
                        raise
                    _log.warning(
                        "Attempt %d/%d for %s failed: %s — retrying in %.1fs",
                        attempt,
                        max_attempts,
                        fn.__name__,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                    delay *= backoff_factor

        return wrapper

    return decorator


# ── CooldownTracker ───────────────────────────────────────────────────────────

class CooldownTracker:
    """
    In-memory cooldown guard.

    Tracks the last execution timestamp per (action_type, target) pair and
    refuses to allow the same action sooner than *cooldown_seconds*.

    Thread-safe via a simple lock.
    """

    def __init__(self, cooldown_seconds: int = 60) -> None:
        self._cooldown = cooldown_seconds
        self._lock = Lock()
        self._last_run: Dict[Tuple[str, str], float] = {}

    def _key(self, action: str, target: str) -> Tuple[str, str]:
        return (action, target)

    def is_allowed(self, action: str, target: str) -> bool:
        """Return True if the action is outside the cooldown window."""
        key = self._key(action, target)
        with self._lock:
            last = self._last_run.get(key)
            if last is None:
                return True
            return (time.monotonic() - last) >= self._cooldown

    def record(self, action: str, target: str) -> None:
        """Record that an action was executed right now."""
        key = self._key(action, target)
        with self._lock:
            self._last_run[key] = time.monotonic()

    def seconds_remaining(self, action: str, target: str) -> float:
        """How many seconds until this action is allowed again (0 if already allowed)."""
        key = self._key(action, target)
        with self._lock:
            last = self._last_run.get(key)
            if last is None:
                return 0.0
            elapsed = time.monotonic() - last
            remaining = self._cooldown - elapsed
            return max(remaining, 0.0)


# ── RunbookDeduplicator ───────────────────────────────────────────────────────

class RunbookDeduplicator:
    """
    Prevents the same runbook_id from being processed more than once.

    Uses a time-based in-memory set with a TTL so the store doesn't grow
    unbounded in long-running deployments.
    """

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._lock = Lock()
        # Maps runbook_id → expiry monotonic timestamp
        self._seen: Dict[str, float] = {}

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, exp in self._seen.items() if now >= exp]
        for k in expired:
            del self._seen[k]

    def is_duplicate(self, runbook_id: str) -> bool:
        """Return True if this runbook_id was already processed within TTL."""
        with self._lock:
            self._evict_expired()
            exp = self._seen.get(runbook_id)
            if exp is None:
                return False
            return time.monotonic() < exp

    def mark_processed(self, runbook_id: str) -> None:
        """Record this runbook_id as processed."""
        with self._lock:
            self._seen[runbook_id] = time.monotonic() + self._ttl

    @property
    def size(self) -> int:
        with self._lock:
            self._evict_expired()
            return len(self._seen)
