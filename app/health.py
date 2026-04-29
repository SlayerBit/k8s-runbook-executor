"""
health.py — Minimal HTTP health check server.

Exposes two endpoints on HEALTH_PORT (default: 8080):
  GET /healthz   → liveness probe  (always 200 while the process is up)
  GET /readyz    → readiness probe (200 once the worker has connected to Redis)

Runs in a daemon thread so it never blocks the main worker loop.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


from app.logging_config import get_logger

logger = get_logger(__name__)

# This flag is set to True by the worker after the first successful Redis ping
_redis_ready = threading.Event()


def signal_ready() -> None:
    """Call this once Redis is reachable."""
    _redis_ready.set()


def _make_handler() -> type:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._respond(200, {"status": "ok"})
            elif self.path == "/readyz":
                if _redis_ready.is_set():
                    self._respond(200, {"status": "ready"})
                else:
                    self._respond(503, {"status": "not_ready", "reason": "redis_not_connected"})
            else:
                self._respond(404, {"status": "not_found"})

        def _respond(self, code: int, body: dict) -> None:
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args) -> None:  # type: ignore[override]
            # Suppress default access log spam; health probes fire every few seconds
            pass

    return _Handler


def start_health_server(port: int) -> threading.Thread:
    """
    Start the health server in a background daemon thread.

    Returns the thread (already started).
    """
    server = HTTPServer(("0.0.0.0", port), _make_handler())

    def _serve() -> None:
        logger.info("Health server listening", extra={"port": port})
        server.serve_forever()

    thread = threading.Thread(target=_serve, name="health-server", daemon=True)
    thread.start()
    return thread
