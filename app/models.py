"""
models.py — Pydantic data models for runbooks and execution actions.

All incoming data is validated through these models before any logic runs.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── Shared validator ───────────────────────────────────────────────────────────

_K8S_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$|^[a-z0-9]$")


def _validate_k8s_name(v: str, label: str = "Resource name") -> str:
    if not _K8S_NAME_RE.match(v):
        raise ValueError(
            f"{label} '{v}' is not a valid Kubernetes resource name "
            "(must be lowercase alphanumeric + hyphens, no leading/trailing hyphens)."
        )
    return v


# ── Enums ─────────────────────────────────────────────────────────────────────


class ActionType(str, Enum):
    SCALE_DEPLOYMENT = "scale_deployment"
    RESTART_DEPLOYMENT = "restart_deployment"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    DELETE_POD = "delete_pod"
    UPDATE_RESOURCES = "update_resources"
    DELETE_NETWORK_POLICY = "delete_network_policy"


class SeverityLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ── Action models ─────────────────────────────────────────────────────────────


class ScaleDeploymentAction(BaseModel):
    """Structured action to scale a Kubernetes Deployment."""

    action: ActionType = ActionType.SCALE_DEPLOYMENT
    deployment: str = Field(..., min_length=1, max_length=253)
    replicas: int = Field(..., ge=0, le=50)
    namespace: Optional[str] = None  # filled in from config if omitted

    @field_validator("deployment")
    @classmethod
    def deployment_name_safe(cls, v: str) -> str:
        return _validate_k8s_name(v, "Deployment name")


class RestartDeploymentAction(BaseModel):
    """Structured action to rolling-restart a Kubernetes Deployment."""

    action: ActionType = ActionType.RESTART_DEPLOYMENT
    deployment: str = Field(..., min_length=1, max_length=253)
    namespace: Optional[str] = None

    @field_validator("deployment")
    @classmethod
    def deployment_name_safe(cls, v: str) -> str:
        return _validate_k8s_name(v, "Deployment name")


class RollbackDeploymentAction(BaseModel):
    """Structured action to roll back a Deployment to its previous revision."""

    action: ActionType = ActionType.ROLLBACK_DEPLOYMENT
    deployment: str = Field(..., min_length=1, max_length=253)
    namespace: Optional[str] = None

    @field_validator("deployment")
    @classmethod
    def deployment_name_safe(cls, v: str) -> str:
        return _validate_k8s_name(v, "Deployment name")


class DeletePodAction(BaseModel):
    """Structured action to delete a specific Kubernetes Pod."""

    action: ActionType = ActionType.DELETE_POD
    pod: str = Field(..., min_length=1, max_length=253)
    namespace: Optional[str] = None

    @field_validator("pod")
    @classmethod
    def pod_name_safe(cls, v: str) -> str:
        return _validate_k8s_name(v, "Pod name")

    @property
    def deployment(self) -> str:
        """Proxy 'deployment' as pod name — used by cooldown/logging layers."""
        return self.pod


class UpdateResourcesAction(BaseModel):
    """Structured action to update CPU/memory resource limits for a Deployment."""

    action: ActionType = ActionType.UPDATE_RESOURCES
    deployment: str = Field(..., min_length=1, max_length=253)
    cpu: str = Field(..., description="CPU limit, e.g. '500m' or '1.5'")
    memory: str = Field(..., description="Memory limit, e.g. '512Mi' or '1Gi'")
    namespace: Optional[str] = None

    @field_validator("deployment")
    @classmethod
    def deployment_name_safe(cls, v: str) -> str:
        return _validate_k8s_name(v, "Deployment name")


class DeleteNetworkPolicyAction(BaseModel):
    """Structured action to delete a Kubernetes NetworkPolicy."""

    action: ActionType = ActionType.DELETE_NETWORK_POLICY
    name: str = Field(..., min_length=1, max_length=253)
    namespace: Optional[str] = None

    @field_validator("name")
    @classmethod
    def policy_name_safe(cls, v: str) -> str:
        return _validate_k8s_name(v, "NetworkPolicy name")

    @property
    def deployment(self) -> str:
        """Proxy 'deployment' as policy name — used by cooldown/logging layers."""
        return self.name


# Union type used throughout the executor pipeline
ExecutionAction = Union[
    ScaleDeploymentAction,
    RestartDeploymentAction,
    RollbackDeploymentAction,
    DeletePodAction,
    UpdateResourcesAction,
    DeleteNetworkPolicyAction,
]


# ── Plan & Runbook envelopes ───────────────────────────────────────────────────


class ExecutionPlan(BaseModel):
    """Ordered list of validated actions derived from a runbook."""

    runbook_id: str
    actions: List[ExecutionAction] = Field(default_factory=list)


class Runbook(BaseModel):
    """
    Incoming runbook envelope produced by Agent 1 and pushed into Redis.

    Fields are intentionally permissive so the system degrades gracefully
    when only partial information is available.
    """

    runbook_id: str = Field(..., min_length=1)
    incident_type: Optional[str] = None
    severity: Optional[SeverityLevel] = None
    # Agent 1 may emit either plain strings or small structured step objects; normalize later.
    steps: Optional[List[Union[str, Dict[str, Any]]]] = None
    remediation_commands: Optional[List[str]] = None
    execution_plan: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="allow")  # tolerate unknown fields from Agent 1
