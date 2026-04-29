# syntax=docker/dockerfile:1

# ── Stage 1: base ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

# Ensures Python output is sent straight to the container log
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /agent2

# ── Stage 2: dependencies ────────────────────────────────────────────────────
FROM base AS deps

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 3: final runtime image ─────────────────────────────────────────────
FROM base AS final

# Copy installed packages from deps stage
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Copy application source
COPY app/ ./app/

# Run as a non-root user for security
RUN addgroup --system agent2 && adduser --system --ingroup agent2 agent2
USER agent2

# Health check — Kubernetes liveness probe
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

EXPOSE 8080

ENTRYPOINT ["python", "-m", "app.main"]
