"""
main.py — Application entrypoint.

Responsibilities:
  1. Configure logging
  2. Load Kubernetes client config
  3. Start the health-check HTTP server (daemon thread)
  4. Install signal handlers for graceful shutdown
  5. Start the RedisWorker loop on the main thread
"""

from __future__ import annotations

import signal
import threading

from app.config import settings
from app.health import signal_ready, start_health_server
from app.kubernetes_client import load_kube_config
from app.logging_config import get_logger, setup_logging
from app.redis_worker import RedisWorker

# NOTE: setup_logging() is called inside main() — NOT here at module level.
# Calling it here would fire during pytest import and corrupt log capture.
logger = get_logger(__name__)


def main() -> None:
    # Logging MUST be configured first so every subsequent log call is structured.
    setup_logging(level=settings.LOG_LEVEL, fmt=settings.LOG_FORMAT)

    logger.info(
        "Agent 2 starting up",
        extra={
            "version": "1.0.0",
            "dry_run": settings.DRY_RUN,
            "enable_execution": settings.ENABLE_EXECUTION,
            "target_namespace": settings.TARGET_NAMESPACE,
            "allowed_actions": settings.ALLOWED_ACTIONS,
            "allowed_namespaces": settings.ALLOWED_NAMESPACES,
            "redis_queue": settings.REDIS_QUEUE_NAME,
        },
    )

    # ── Kubernetes client initialisation ──────────────────────────────────────
    try:
        load_kube_config()
    except Exception as exc:
        logger.warning(
            "Could not load Kubernetes config — Kubernetes actions will fail at runtime",
            extra={"error": str(exc)},
        )

    # ── Health server ─────────────────────────────────────────────────────────
    if settings.ENABLE_HEALTH_SERVER:
        start_health_server(port=settings.HEALTH_PORT)

    # ── Graceful shutdown coordination ────────────────────────────────────────
    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame) -> None:  # type: ignore[misc]
        sig_name = signal.Signals(signum).name
        logger.info("Shutdown signal received", extra={"signal": sig_name})
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # ── Worker loop ───────────────────────────────────────────────────────────
    worker = RedisWorker(stop_event=stop_event)

    # Signal the health server that we are ready to serve traffic.
    # The worker will fail on first Redis connect if Redis is unavailable,
    # but we signal here so the Pod isn't killed before it has a chance to retry.
    signal_ready()

    worker.run()
    logger.info("Agent 2 shut down cleanly")


if __name__ == "__main__":
    main()
