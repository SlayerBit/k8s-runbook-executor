"""
config.py — Centralised configuration loaded entirely from environment variables.

Every tunable knob lives here. No secrets or hostnames are hardcoded.
"""

from __future__ import annotations

import os
from typing import List


def _env_list(key: str, default: str) -> List[str]:
    """Parse a comma-separated environment variable into a list of stripped strings."""
    raw = os.environ.get(key, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    return default


class Config:
    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_HOST: str = os.environ.get("REDIS_HOST", "localhost")
    REDIS_PORT: int = int(os.environ.get("REDIS_PORT", "6379"))
    REDIS_DB: int = int(os.environ.get("REDIS_DB", "0"))
    REDIS_PASSWORD: str = os.environ.get("REDIS_PASSWORD", "")
    REDIS_QUEUE_NAME: str = os.environ.get("REDIS_QUEUE_NAME", "runbooks")
    REDIS_BRPOP_TIMEOUT: int = int(os.environ.get("REDIS_BRPOP_TIMEOUT", "5"))

    # ── Kubernetes ────────────────────────────────────────────────────────────
    # Namespace(s) the agent is allowed to act in (comma-separated)
    TARGET_NAMESPACE: str = os.environ.get("TARGET_NAMESPACE", "default")
    ALLOWED_NAMESPACES: List[str] = _env_list(
        "ALLOWED_NAMESPACES",
        os.environ.get("TARGET_NAMESPACE", "default"),
    )

    # ── Safety ────────────────────────────────────────────────────────────────
    # Master switch: set to "false" to put the agent in observe-only mode
    ENABLE_EXECUTION: bool = _env_bool("ENABLE_EXECUTION", True)
    # DRY_RUN overrides ENABLE_EXECUTION and logs what *would* happen
    DRY_RUN: bool = _env_bool("DRY_RUN", False)

    # Allowlist of action types the agent may execute
    ALLOWED_ACTIONS: List[str] = _env_list(
        "ALLOWED_ACTIONS",
        "scale_deployment,restart_deployment,rollback_deployment,"
        "delete_pod,update_resources,delete_network_policy",
    )

    # ── Rate Limiting / Idempotency ───────────────────────────────────────────
    # Seconds to wait before the same action can be re-applied to the same target
    COOLDOWN_SECONDS: int = int(os.environ.get("COOLDOWN_SECONDS", "60"))
    # TTL for the processed-runbook-id deduplication set (seconds)
    RUNBOOK_DEDUP_TTL: int = int(os.environ.get("RUNBOOK_DEDUP_TTL", "3600"))

    # ── Reliability ───────────────────────────────────────────────────────────
    RECONNECT_DELAY_SECONDS: int = int(
        os.environ.get("RECONNECT_DELAY_SECONDS", "5")
    )
    MAX_RETRY_ATTEMPTS: int = int(os.environ.get("MAX_RETRY_ATTEMPTS", "3"))
    RETRY_BACKOFF_FACTOR: float = float(
        os.environ.get("RETRY_BACKOFF_FACTOR", "2.0")
    )

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()
    LOG_FORMAT: str = os.environ.get("LOG_FORMAT", "json")  # "json" | "text"

    # ── Health Check ─────────────────────────────────────────────────────────
    HEALTH_PORT: int = int(os.environ.get("HEALTH_PORT", "8080"))
    ENABLE_HEALTH_SERVER: bool = _env_bool("ENABLE_HEALTH_SERVER", True)


# Singleton instance — import this everywhere
settings = Config()
