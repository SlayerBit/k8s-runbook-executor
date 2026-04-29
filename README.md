# Agent 2 — Closed-Loop Self-Healing Executor

Agent 2 is the **execution arm** of the self-healing pipeline. It consumes
runbooks produced by Agent 1 from a Redis queue and executes only safe,
pre-approved remediation actions inside a Kubernetes cluster using the
official Python client — no `kubectl` subprocess calls, no shell scripts.

---

## Architecture

```
┌─────────────┐   LPUSH    ┌─────────┐   BRPOP   ┌───────────────────────────┐
│   Agent 1   │ ─────────► │  Redis  │ ─────────► │         Agent 2           │
│  (Runbook   │            │ "runbooks│            │  Parser → Validator →     │
│  Generator) │            │  queue" │            │  Executor → Kubernetes API │
└─────────────┘            └─────────┘            └───────────────────────────┘
```

### Module map

| Module | Responsibility |
|---|---|
| `main.py` | Entrypoint — wires logging, k8s config, health server, signal handlers |
| `config.py` | All configuration from environment variables with safe defaults |
| `models.py` | Pydantic models: `Runbook`, `ExecutionPlan`, `ScaleDeploymentAction`, `RestartDeploymentAction` |
| `redis_worker.py` | Blocking BRPOP consumer loop with reconnect and graceful shutdown |
| `parser.py` | Converts raw `kubectl` command strings and structured dicts into typed actions |
| `validator.py` | Pre-execution safety checks — allowlist, namespace guard, replica cap, cooldown |
| `executor.py` | Ordered step-by-step execution with dry-run, stop-on-failure, and reporting |
| `kubernetes_client.py` | Thin wrapper around the official `kubernetes` Python client |
| `utils.py` | `CooldownTracker`, `RunbookDeduplicator`, `retry_with_backoff` |
| `health.py` | Lightweight HTTP server for `/healthz` and `/readyz` probes |

---

## Supported Actions

| Action | Input command example |
|---|---|
| `scale_deployment` | `kubectl scale deployment backend --replicas=5` |
| `restart_deployment` | `kubectl rollout restart deployment backend` |

All other commands (`exec`, `delete`, `get`, `logs`, …) are **logged and dropped**.

---

## Safety Features

- **Strict allowlist** — only configured action types execute
- **Namespace restriction** — agent refuses to act outside `ALLOWED_NAMESPACES`
- **Replica cap** — hard ceiling of 20 replicas regardless of runbook content
- **Deduplication** — same `runbook_id` is never processed twice within the TTL window
- **Per-action cooldown** — prevents hammering the same deployment repeatedly
- **Dry-run mode** — logs what *would* happen without touching Kubernetes
- **`ENABLE_EXECUTION=false`** — observe-only mode for safe rollout
- **No hardcoded secrets** — everything through env vars / Kubernetes Secrets
- **Non-root container** — `runAsUser: 1000`, read-only root filesystem
- **Minimal RBAC** — Role scoped to `food-app` namespace only

---

## Runbook Schema

Agent 1 should push JSON to the `runbooks` Redis key:

```json
{
  "runbook_id": "rb-20240101-001",
  "incident_type": "high_memory_usage",
  "severity": "high",
  "remediation_commands": [
    "kubectl rollout restart deployment backend",
    "kubectl scale deployment worker --replicas=3"
  ],
  "steps": [
    "Restart the backend deployment to clear memory leak",
    "Scale worker pods to absorb load"
  ]
}
```

Or with a pre-structured `execution_plan` (takes priority over `remediation_commands`):

```json
{
  "runbook_id": "rb-20240101-002",
  "execution_plan": [
    {"action": "scale_deployment", "deployment": "backend", "replicas": 5},
    {"action": "restart_deployment", "deployment": "worker"}
  ]
}
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `REDIS_HOST` | `localhost` | Redis hostname |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_DB` | `0` | Redis database index |
| `REDIS_PASSWORD` | `""` | Redis password (use Secret) |
| `REDIS_QUEUE_NAME` | `runbooks` | Redis list key to consume from |
| `REDIS_BRPOP_TIMEOUT` | `5` | Seconds to block on each BRPOP call |
| `TARGET_NAMESPACE` | `default` | Primary namespace for actions |
| `ALLOWED_NAMESPACES` | `default` | Comma-separated list of permitted namespaces |
| `ENABLE_EXECUTION` | `true` | Set `false` to disable all Kubernetes calls |
| `DRY_RUN` | `false` | Set `true` to log intent only |
| `ALLOWED_ACTIONS` | `scale_deployment,restart_deployment` | Comma-separated action allowlist |
| `COOLDOWN_SECONDS` | `60` | Minimum seconds between identical actions |
| `RUNBOOK_DEDUP_TTL` | `3600` | Seconds to remember processed runbook IDs |
| `RECONNECT_DELAY_SECONDS` | `5` | Seconds to wait before Redis reconnect |
| `MAX_RETRY_ATTEMPTS` | `3` | Retry attempts for transient failures |
| `RETRY_BACKOFF_FACTOR` | `2.0` | Exponential backoff multiplier |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FORMAT` | `json` | `json` (structured) or `text` (human-readable) |
| `HEALTH_PORT` | `8080` | Port for liveness/readiness HTTP server |
| `ENABLE_HEALTH_SERVER` | `true` | Set `false` to disable the health server |

---

## Local Development

```bash
# 1. Create and activate a virtual environment
make install
source .venv/bin/activate

# 2. Start a local Redis instance
docker run -d -p 6379:6379 redis:7-alpine

# 3. Run in dry-run mode (no real Kubernetes calls)
make run-local

# 4. Push a test runbook
redis-cli LPUSH runbooks '{
  "runbook_id": "test-001",
  "remediation_commands": ["kubectl rollout restart deployment backend"]
}'
```

---

## Running Tests

```bash
make test
# or directly:
pytest tests/ -v --cov=app --cov-report=term-missing
```

Test coverage:
- `tests/test_parser.py` — command parsing, plan building, deduplication
- `tests/test_validator.py` — allowlist, namespace guard, replica cap, cooldown
- `tests/test_executor.py` — dry-run, live dispatch (mocked k8s), stop-on-failure, cooldown recording

---

## Docker

```bash
# Build
make docker-build

# Build and push
REGISTRY=gcr.io/my-project make docker-push
```

---

## Kubernetes Deployment

```bash
# 1. Create the Secret (copy and fill in the example)
cp k8s/secret.example.yaml k8s/secret.yaml
# edit k8s/secret.yaml with your real base64-encoded values
kubectl apply -f k8s/secret.yaml

# 2. Update the image in k8s/deployment.yaml to your registry

# 3. Apply everything
make k8s-apply

# 4. Verify
make k8s-status
make k8s-logs
```

### RBAC Summary

The `agent2` Role grants **only** what is needed, scoped to `food-app` namespace:

| API Group | Resource | Verbs |
|---|---|---|
| `apps` | `deployments` | `get`, `list`, `patch`, `update` |
| `apps` | `deployments/scale` | `get`, `patch`, `update` |
| `""` | `pods` | `get`, `list` |
| `""` | `pods/log` | `get` |

No cluster-wide permissions. No access to Secrets, ConfigMaps, or other namespaces.

---

## Health Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness — always `200` while the process is running |
| `GET /readyz` | Readiness — `200` after first successful Redis connection, `503` before |

---

## Project Structure

```
Agent-2/
├── app/
│   ├── __init__.py
│   ├── main.py              # Entrypoint
│   ├── config.py            # Environment-driven configuration
│   ├── logging_config.py    # Structured JSON / text logging
│   ├── models.py            # Pydantic data models
│   ├── redis_worker.py      # BRPOP consumer loop
│   ├── parser.py            # kubectl string → typed action converter
│   ├── validator.py         # Pre-execution safety checks
│   ├── executor.py          # Step-by-step Kubernetes executor
│   ├── kubernetes_client.py # k8s Python client wrapper
│   ├── health.py            # HTTP liveness/readiness server
│   └── utils.py             # CooldownTracker, Deduplicator, retry helper
├── tests/
│   ├── __init__.py
│   ├── test_parser.py
│   ├── test_validator.py
│   └── test_executor.py
├── k8s/
│   ├── namespace.yaml
│   ├── serviceaccount.yaml
│   ├── role.yaml
│   ├── rolebinding.yaml
│   ├── configmap.yaml
│   ├── deployment.yaml
│   └── secret.example.yaml
├── Dockerfile
├── .dockerignore
├── .gitignore
├── Makefile
├── requirements.txt
└── README.md
```
