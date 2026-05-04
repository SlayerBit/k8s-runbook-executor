"""
kubernetes_client.py — Thin wrapper around the official kubernetes-python client.

Responsibilities:
  - Load in-cluster config (with kubeconfig fallback for local dev)
  - scale_deployment()
  - restart_deployment()       (rolling restart via patch annotation)
  - rollback_deployment()      (find previous ReplicaSet, patch Deployment template)
  - delete_pod()               (CoreV1Api, with idempotency check)
  - update_deployment_resources()  (patch resource limits on the primary container)
  - delete_network_policy()    (NetworkingV1Api, with idempotency check)
  - get_deployment_replicas()  (read-only, used for idempotency)

All functions raise kubernetes.client.exceptions.ApiException on Kubernetes
errors so callers can handle them uniformly.

Retries: transient 5xx errors are retried with exponential back-off via the
retry_with_backoff decorator from app.utils.
"""

from __future__ import annotations

import datetime
import logging

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from app.utils import retry_with_backoff

logger = logging.getLogger(__name__)

# Retry on transient server-side errors only (not 4xx client errors)
_RETRY_EXCEPTIONS = (ApiException,)


def _is_transient(exc: ApiException) -> bool:
    """True for 5xx and 429 (rate-limited) responses."""
    return exc.status is not None and (exc.status >= 500 or exc.status == 429)


def _retryable(fn):
    """Decorator: retry up to 3 times with 2× back-off for transient ApiExceptions."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        delay = 1.0
        last_exc = None
        for attempt in range(1, 4):
            try:
                return fn(*args, **kwargs)
            except ApiException as exc:
                if not _is_transient(exc) or attempt == 3:
                    raise
                last_exc = exc
                logger.warning(
                    "%s: transient error (status=%s) — retry %d/3 in %.1fs",
                    fn.__name__, exc.status, attempt, delay,
                )
                import time
                time.sleep(delay)
                delay *= 2.0
        raise last_exc  # pragma: no cover

    return wrapper


# ── Config ─────────────────────────────────────────────────────────────────────


def load_kube_config() -> None:
    """
    Load the Kubernetes client configuration.

    Tries in-cluster config first (works when running as a Pod).
    Falls back to the local kubeconfig for development.
    """
    try:
        config.load_incluster_config()
        logger.info("Kubernetes: using in-cluster config")
    except config.ConfigException:
        logger.info("Kubernetes: in-cluster config unavailable — falling back to kubeconfig")
        config.load_kube_config()


# ── Read helpers ───────────────────────────────────────────────────────────────


def get_deployment_replicas(deployment: str, namespace: str) -> int:
    """
    Return the current *desired* replica count for a Deployment.

    Raises ApiException if the Deployment does not exist.
    """
    apps_v1 = client.AppsV1Api()
    dep = apps_v1.read_namespaced_deployment(name=deployment, namespace=namespace)
    return dep.spec.replicas or 0


# ── Write helpers ──────────────────────────────────────────────────────────────


@_retryable
def scale_deployment(deployment: str, replicas: int, namespace: str) -> None:
    """
    Scale a Kubernetes Deployment to *replicas*.

    Idempotency: if the current desired count already matches, skip the call.
    """
    apps_v1 = client.AppsV1Api()

    try:
        current = get_deployment_replicas(deployment, namespace)
    except ApiException as exc:
        logger.error(
            "Cannot read current replicas before scaling",
            extra={
                "deployment": deployment,
                "namespace": namespace,
                "status": exc.status,
                "reason": exc.reason,
            },
        )
        raise

    if current == replicas:
        logger.info(
            "Scaling skipped — already at desired replicas",
            extra={"deployment": deployment, "namespace": namespace, "replicas": replicas},
        )
        return

    body = {"spec": {"replicas": replicas}}
    apps_v1.patch_namespaced_deployment_scale(
        name=deployment, namespace=namespace, body=body,
    )
    logger.info(
        "Scaled deployment",
        extra={
            "deployment": deployment,
            "namespace": namespace,
            "from_replicas": current,
            "to_replicas": replicas,
        },
    )


@_retryable
def restart_deployment(deployment: str, namespace: str) -> None:
    """
    Trigger a rolling restart of a Kubernetes Deployment.

    Achieved by patching the Pod template annotation
    `kubectl.kubernetes.io/restartedAt` — identical to what
    *kubectl rollout restart* does under the hood.
    """
    apps_v1 = client.AppsV1Api()
    now = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()

    body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": now,
                    }
                }
            }
        }
    }

    apps_v1.patch_namespaced_deployment(
        name=deployment, namespace=namespace, body=body,
    )
    logger.info(
        "Rolling restart triggered",
        extra={"deployment": deployment, "namespace": namespace, "restartedAt": now},
    )


@_retryable
def rollback_deployment(deployment: str, namespace: str) -> None:
    """
    Roll back a Deployment to its previous revision.

    Strategy (equivalent to `kubectl rollout undo`):
      1. Read current Deployment and its revision annotation.
      2. List all owned ReplicaSets and locate the one at revision (current - 1).
      3. Patch the Deployment's pod template with the previous RS template.

    If no previous ReplicaSet is found (e.g. first-ever deploy), a rolling
    restart is performed as a safe fallback.
    """
    apps_v1 = client.AppsV1Api()

    dep = apps_v1.read_namespaced_deployment(name=deployment, namespace=namespace)
    annotations = dep.metadata.annotations or {}
    current_rev = int(annotations.get("deployment.kubernetes.io/revision", "0"))

    if current_rev <= 1:
        logger.warning(
            "No previous revision found — falling back to rolling restart",
            extra={"deployment": deployment, "namespace": namespace, "current_revision": current_rev},
        )
        restart_deployment(deployment, namespace)
        return

    target_rev = current_rev - 1

    # List ReplicaSets owned by this Deployment via label selector
    match_labels = dep.spec.selector.match_labels or {}
    label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())

    rs_list = apps_v1.list_namespaced_replica_set(
        namespace=namespace, label_selector=label_selector
    )

    prev_rs = None
    for rs in rs_list.items:
        rs_annotations = rs.metadata.annotations or {}
        rs_rev = int(rs_annotations.get("deployment.kubernetes.io/revision", "0"))
        if rs_rev == target_rev:
            prev_rs = rs
            break

    if prev_rs is None:
        logger.error(
            "Previous ReplicaSet not found — cannot roll back; falling back to restart",
            extra={
                "deployment": deployment,
                "namespace": namespace,
                "target_revision": target_rev,
            },
        )
        restart_deployment(deployment, namespace)
        return

    # Build a patch from the previous RS template; stamp a new restartedAt
    now = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
    template = prev_rs.spec.template.to_dict()
    template.setdefault("metadata", {}).setdefault("annotations", {})[
        "kubectl.kubernetes.io/restartedAt"
    ] = now

    body = {"spec": {"template": template}}
    apps_v1.patch_namespaced_deployment(name=deployment, namespace=namespace, body=body)
    logger.info(
        "Rolled back deployment",
        extra={
            "deployment": deployment,
            "namespace": namespace,
            "from_revision": current_rev,
            "to_revision": target_rev,
        },
    )


@_retryable
def delete_pod(pod: str, namespace: str) -> None:
    """
    Delete a Kubernetes Pod.

    Idempotency: if the Pod no longer exists (404), log and return silently.
    """
    core_v1 = client.CoreV1Api()

    # Idempotency check
    try:
        core_v1.read_namespaced_pod(name=pod, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            logger.info(
                "Pod not found — already deleted (idempotent)",
                extra={"pod": pod, "namespace": namespace},
            )
            return
        raise

    core_v1.delete_namespaced_pod(name=pod, namespace=namespace)
    logger.info(
        "Deleted pod",
        extra={"pod": pod, "namespace": namespace},
    )


@_retryable
def update_deployment_resources(
    deployment: str,
    namespace: str,
    cpu: str,
    memory: str,
) -> None:
    """
    Patch the first container's resource limits (and requests) for a Deployment.

    Idempotency: if existing limits already match cpu/memory, skip the patch.

    The patch is surgical — only the `resources` field of the named container
    is updated; all other spec fields are preserved.
    """
    apps_v1 = client.AppsV1Api()
    dep = apps_v1.read_namespaced_deployment(name=deployment, namespace=namespace)

    containers = dep.spec.template.spec.containers
    if not containers:
        raise ValueError(f"Deployment '{deployment}' has no containers to patch")

    primary = containers[0]

    # Idempotency: skip if limits are already correct
    if primary.resources and primary.resources.limits:
        existing_cpu = primary.resources.limits.get("cpu")
        existing_mem = primary.resources.limits.get("memory")
        if existing_cpu == cpu and existing_mem == memory:
            logger.info(
                "Resource limits already match — skipping update",
                extra={
                    "deployment": deployment,
                    "namespace": namespace,
                    "cpu": cpu,
                    "memory": memory,
                },
            )
            return

    resource_patch = {"limits": {"cpu": cpu, "memory": memory},
                      "requests": {"cpu": cpu, "memory": memory}}

    body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": primary.name, "resources": resource_patch}
                    ]
                }
            }
        }
    }
    apps_v1.patch_namespaced_deployment(name=deployment, namespace=namespace, body=body)
    logger.info(
        "Updated deployment resource limits",
        extra={
            "deployment": deployment,
            "namespace": namespace,
            "cpu": cpu,
            "memory": memory,
        },
    )


@_retryable
def delete_network_policy(name: str, namespace: str) -> None:
    """
    Delete a Kubernetes NetworkPolicy.

    Idempotency: if the policy no longer exists (404), log and return silently.
    """
    networking_v1 = client.NetworkingV1Api()

    # Idempotency check
    try:
        networking_v1.read_namespaced_network_policy(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            logger.info(
                "NetworkPolicy not found — already deleted (idempotent)",
                extra={"name": name, "namespace": namespace},
            )
            return
        raise

    networking_v1.delete_namespaced_network_policy(name=name, namespace=namespace)
    logger.info(
        "Deleted NetworkPolicy",
        extra={"name": name, "namespace": namespace},
    )
