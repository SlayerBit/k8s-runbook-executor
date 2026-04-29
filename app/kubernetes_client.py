"""
kubernetes_client.py — Thin wrapper around the official kubernetes-python client.

Responsibilities:
  - Load in-cluster config (with kubeconfig fallback for local dev)
  - scale_deployment()
  - restart_deployment()  (rolling restart via patch annotation)
  - get_deployment_replicas()  (read-only, used for idempotency)

All functions raise kubernetes.client.exceptions.ApiException on Kubernetes
errors so callers can handle them uniformly.
"""

from __future__ import annotations

import datetime
import logging


from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

logger = logging.getLogger(__name__)


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


def get_deployment_replicas(deployment: str, namespace: str) -> int:
    """
    Return the current *desired* replica count for a Deployment.

    Raises ApiException if the Deployment does not exist.
    """
    apps_v1 = client.AppsV1Api()
    dep = apps_v1.read_namespaced_deployment(name=deployment, namespace=namespace)
    return dep.spec.replicas or 0


def scale_deployment(deployment: str, replicas: int, namespace: str) -> None:
    """
    Scale a Kubernetes Deployment to *replicas*.

    Idempotency: if the current desired replica count already matches, skip
    the API call and log at DEBUG level.
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
            extra={
                "deployment": deployment,
                "namespace": namespace,
                "replicas": replicas,
            },
        )
        return

    body = {"spec": {"replicas": replicas}}
    apps_v1.patch_namespaced_deployment_scale(
        name=deployment,
        namespace=namespace,
        body=body,
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


def restart_deployment(deployment: str, namespace: str) -> None:
    """
    Trigger a rolling restart of a Kubernetes Deployment.

    Achieved by patching the Pod template annotation
    `kubectl.kubernetes.io/restartedAt` — identical to what *kubectl rollout
    restart* does under the hood.
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
        name=deployment,
        namespace=namespace,
        body=body,
    )
    logger.info(
        "Rolling restart triggered",
        extra={
            "deployment": deployment,
            "namespace": namespace,
            "restartedAt": now,
        },
    )
