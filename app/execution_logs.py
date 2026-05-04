"""
execution_logs.py — Lightweight execution observability for Agent 2.

Stores structured JSON execution events in Redis (preferred) with an in-memory
ring-buffer fallback so Simulator can query recent activity via HTTP.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

import redis

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _safe_json_dumps(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))


@dataclass(frozen=True)
class ExecutionLogEntry:
    timestamp: str
    event: str
    runbook_id: str
    incident_type: str
    action: Optional[str]
    command: str
    status: str  # success | failed | skipped
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event": self.event,
            "runbook_id": self.runbook_id,
            "incident_type": self.incident_type,
            "action": self.action,
            "command": self.command,
            "status": self.status,
            "error": self.error,
        }


class ExecutionLogStore:
    """
    Append-only store with newest-first reads.

    Redis schema:
      - Key: settings.EXECUTION_LOG_REDIS_KEY
      - Type: LIST
      - Value: JSON-serialized ExecutionLogEntry dict
      - Push: LPUSH (newest at head)
      - Trim: LTRIM 0..MAX-1
    """

    def __init__(self, redis_client: Optional[redis.Redis], max_entries: int) -> None:
        self._redis = redis_client
        self._max = max(1, int(max_entries))
        self._mem: Deque[Dict[str, Any]] = deque(maxlen=self._max)  # oldest→newest

    def append(self, entry: ExecutionLogEntry) -> None:
        payload = entry.as_dict()

        # Always write to memory so the endpoint remains useful even if Redis is down.
        self._mem.append(payload)

        if not self._redis:
            return

        try:
            key = settings.EXECUTION_LOG_REDIS_KEY
            self._redis.lpush(key, _safe_json_dumps(payload))
            self._redis.ltrim(key, 0, self._max - 1)
        except Exception as exc:
            # Degrade gracefully — never break execution on observability failure.
            logger.warning("Execution log Redis write failed — falling back to memory", extra={"error": str(exc)})
            self._redis = None

    def latest(self, limit: int) -> List[Dict[str, Any]]:
        n = max(0, int(limit))
        if n == 0:
            return []

        # Prefer Redis because it's shared across pods/restarts.
        if self._redis:
            try:
                key = settings.EXECUTION_LOG_REDIS_KEY
                rows = self._redis.lrange(key, 0, n - 1)  # newest first
                out: List[Dict[str, Any]] = []
                for r in rows:
                    try:
                        out.append(json.loads(r))
                    except Exception:
                        # Ignore malformed entries instead of failing the endpoint.
                        continue
                return out
            except Exception as exc:
                logger.warning("Execution log Redis read failed — falling back to memory", extra={"error": str(exc)})
                self._redis = None

        # Memory fallback: deque is oldest→newest; API expects newest first.
        mem_list = list(self._mem)
        mem_list.reverse()
        return mem_list[:n]


_store: Optional[ExecutionLogStore] = None


def init_execution_log_store(redis_client: Optional[redis.Redis] = None) -> ExecutionLogStore:
    global _store
    if _store is not None:
        return _store

    _store = ExecutionLogStore(redis_client=redis_client, max_entries=settings.EXECUTION_LOG_MAX_ENTRIES)
    return _store


def get_execution_log_store() -> ExecutionLogStore:
    global _store
    if _store is None:
        _store = ExecutionLogStore(redis_client=None, max_entries=settings.EXECUTION_LOG_MAX_ENTRIES)
    return _store

