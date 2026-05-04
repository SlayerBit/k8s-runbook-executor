"""
redis_worker.py — Blocking Redis consumer loop.

Behaviour:
  - BRPOP from the configured queue (right-to-left = FIFO when Agent 1 uses LPUSH)
  - Parse JSON payload into a Runbook model
  - Deduplicate by runbook_id
  - Build an ExecutionPlan via parser
  - Validate the plan via validator
  - Execute via executor
  - Reconnect gracefully on Redis disconnect
  - Honour a threading.Event for clean shutdown (SIGTERM/SIGINT)
"""

from __future__ import annotations

import json
import threading
import time
from typing import Optional

import redis
from pydantic import ValidationError

from app.config import settings
from app.execution_logs import ExecutionLogEntry, get_execution_log_store, utc_now_iso
from app.execution_logs import init_execution_log_store
from app.executor import Executor
from app.logging_config import get_logger
from app.models import ExecutionPlan, Runbook
from app.parser import build_execution_plan
from app.utils import CooldownTracker, RunbookDeduplicator
from app.validator import validate_plan

logger = get_logger(__name__)


def _build_redis_client() -> redis.Redis:
    """Create and return a Redis client (not yet connected)."""
    kwargs = {
        "host": settings.REDIS_HOST,
        "port": settings.REDIS_PORT,
        "db": settings.REDIS_DB,
        "decode_responses": True,
        "socket_connect_timeout": 5,
        "socket_timeout": settings.REDIS_BRPOP_TIMEOUT + 2,
        "retry_on_timeout": True,
    }
    if settings.REDIS_PASSWORD:
        kwargs["password"] = settings.REDIS_PASSWORD
    return redis.Redis(**kwargs)


class RedisWorker:
    """
    Long-running worker that consumes runbooks from Redis and executes them.

    Parameters
    ----------
    stop_event : threading.Event that signals the worker to shut down cleanly.
    """

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event
        self._cooldown = CooldownTracker(cooldown_seconds=settings.COOLDOWN_SECONDS)
        self._dedup = RunbookDeduplicator(ttl_seconds=settings.RUNBOOK_DEDUP_TTL)
        self._executor = Executor(
            cooldown=self._cooldown,
            target_namespace=settings.TARGET_NAMESPACE,
            dry_run=settings.DRY_RUN,
        )
        self._redis: Optional[redis.Redis] = None

    def run(self) -> None:
        """Main loop — runs until stop_event is set."""
        logger.info(
            "RedisWorker starting",
            extra={
                "queue": settings.REDIS_QUEUE_NAME,
                "redis_host": settings.REDIS_HOST,
                "redis_port": settings.REDIS_PORT,
                "dry_run": settings.DRY_RUN,
                "enable_execution": settings.ENABLE_EXECUTION,
            },
        )

        while not self._stop.is_set():
            try:
                self._ensure_connected()
                self._poll_once()
            except redis.ConnectionError as exc:
                logger.warning(
                    "Redis connection lost — will reconnect",
                    extra={"error": str(exc), "delay": settings.RECONNECT_DELAY_SECONDS},
                )
                self._redis = None
                self._interruptible_sleep(settings.RECONNECT_DELAY_SECONDS)
            except redis.TimeoutError:
                # BRPOP timed out with no message — normal, loop again
                pass
            except Exception as exc:
                logger.exception(
                    "Unexpected error in worker loop",
                    extra={"error": str(exc)},
                )
                self._interruptible_sleep(settings.RECONNECT_DELAY_SECONDS)

        logger.info("RedisWorker stopped cleanly")

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _ensure_connected(self) -> None:
        if self._redis is None:
            self._redis = _build_redis_client()
            self._redis.ping()
            logger.info(
                "Connected to Redis",
                extra={"host": settings.REDIS_HOST, "port": settings.REDIS_PORT},
            )
            # Wire the execution log store to Redis once we know Redis is reachable.
            init_execution_log_store(redis_client=self._redis)

    def _poll_once(self) -> None:
        """
        Block for up to REDIS_BRPOP_TIMEOUT seconds waiting for a message.
        Returns immediately if the queue is empty or stop is requested.
        """
        assert self._redis is not None

        result = self._redis.brpop(
            settings.REDIS_QUEUE_NAME,
            timeout=settings.REDIS_BRPOP_TIMEOUT,
        )

        if result is None:
            # Timeout — no message in the window
            return

        _queue_name, raw_payload = result
        self._handle_message(raw_payload)

    def _handle_message(self, raw_payload: str) -> None:
        """Parse, validate, and execute one runbook message."""
        store = get_execution_log_store()

        # ── 1. JSON parse ──────────────────────────────────────────────────────
        try:
            data = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            logger.error(
                "Failed to decode JSON payload — discarding",
                extra={"error": str(exc), "raw": raw_payload[:200]},
            )
            return

        # ── 2. Runbook model validation ────────────────────────────────────────
        try:
            runbook = Runbook(**data)
        except ValidationError as exc:
            logger.error(
                "Invalid runbook schema — discarding",
                extra={"errors": exc.errors(), "raw": str(data)[:200]},
            )
            return

        incident_type = str(runbook.incident_type or "Unknown")

        store.append(
            ExecutionLogEntry(
                timestamp=utc_now_iso(),
                event="runbook_received",
                runbook_id=runbook.runbook_id,
                incident_type=incident_type,
                action=None,
                command="runbook_received",
                status="success",
                error=None,
            )
        )

        logger.info(
            "Received runbook",
            extra={
                "runbook_id": runbook.runbook_id,
                "incident_type": runbook.incident_type,
                "severity": runbook.severity,
            },
        )

        # ── 3. Deduplication ───────────────────────────────────────────────────
        if self._dedup.is_duplicate(runbook.runbook_id):
            logger.warning(
                "Duplicate runbook_id — skipping",
                extra={"runbook_id": runbook.runbook_id},
            )
            return

        # ── 4. Build execution plan ────────────────────────────────────────────
        raw_actions = build_execution_plan(
            runbook_id=runbook.runbook_id,
            remediation_commands=runbook.remediation_commands,
            execution_plan=runbook.execution_plan,
        )

        store.append(
            ExecutionLogEntry(
                timestamp=utc_now_iso(),
                event="runbook_parsed",
                runbook_id=runbook.runbook_id,
                incident_type=incident_type,
                action=None,
                command=f"parsed_actions={len(raw_actions)}",
                status="success",
                error=None,
            )
        )

        if not raw_actions:
            logger.info(
                "No executable actions found in runbook",
                extra={"runbook_id": runbook.runbook_id},
            )
            store.append(
                ExecutionLogEntry(
                    timestamp=utc_now_iso(),
                    event="no_actions_found",
                    runbook_id=runbook.runbook_id,
                    incident_type=incident_type,
                    action=None,
                    command="No executable actions found",
                    status="skipped",
                    error=None,
                )
            )
            self._dedup.mark_processed(runbook.runbook_id)
            return

        plan = ExecutionPlan(runbook_id=runbook.runbook_id, actions=raw_actions)

        # ── 5. Validate ────────────────────────────────────────────────────────
        validation = validate_plan(
            plan=plan,
            cooldown=self._cooldown,
            target_namespace=settings.TARGET_NAMESPACE,
        )

        if validation.has_rejected:
            logger.warning(
                "Some actions were rejected by validation",
                extra={
                    "runbook_id": runbook.runbook_id,
                    "rejected": [
                        {"action": a.action.value, "reason": r}
                        for a, r in validation.rejected
                    ],
                },
            )

        if not validation.has_approved:
            logger.warning(
                "All actions rejected — nothing to execute",
                extra={"runbook_id": runbook.runbook_id},
            )
            self._dedup.mark_processed(runbook.runbook_id)
            return

        approved_plan = ExecutionPlan(
            runbook_id=plan.runbook_id,
            actions=validation.approved,
        )

        # ── 6. Execute ─────────────────────────────────────────────────────────
        report = self._executor.run(approved_plan, incident_type=incident_type)

        # Mark as processed regardless of success/failure to prevent replay loops
        self._dedup.mark_processed(runbook.runbook_id)

        logger.info(
            "Runbook processing complete",
            extra={
                "runbook_id": runbook.runbook_id,
                "summary": report.summary(),
            },
        )

    def _interruptible_sleep(self, seconds: int) -> None:
        """Sleep in small increments so SIGTERM wakes us up promptly."""
        deadline = time.monotonic() + seconds
        while not self._stop.is_set() and time.monotonic() < deadline:
            time.sleep(0.25)
