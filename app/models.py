"""
models.py — Pydantic data models for runbooks and execution actions.

All incoming data is validated through these models before any logic runs.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ActionType(str, Enum):
    SCALE_DEPLOYMENT = "scale_deployment"
    RESTART_DEPLOYMENT = "restart_deployment"


class SeverityLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ScaleDeploymentAction(BaseModel):
    """Structured action to scale a Kubernetes Deployment."""

    action: ActionType = ActionType.SCALE_DEPLOYMENT
    deployment: str = Field(..., min_length=1, max_length=253)
    replicas: int = Field(..., ge=0, le=50)
    namespace: Optional[str] = None  # filled in from config if omitted

    @field_validator("deployment")
    @classmethod
    def deployment_name_safe(cls, v: str) -> str:
        import re

        if not re.match(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$|^[a-z0-9]$", v):
            raise ValueError(
                f"Deployment name '{v}' is not a valid Kubernetes resource name."
            )
        return v


class RestartDeploymentAction(BaseModel):
    """Structured action to rolling-restart a Kubernetes Deployment."""

    action: ActionType = ActionType.RESTART_DEPLOYMENT
    deployment: str = Field(..., min_length=1, max_length=253)
    namespace: Optional[str] = None

    @field_validator("deployment")
    @classmethod
    def deployment_name_safe(cls, v: str) -> str:
        import re

        if not re.match(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$|^[a-z0-9]$", v):
            raise ValueError(
                f"Deployment name '{v}' is not a valid Kubernetes resource name."
            )
        return v


# Union type used throughout the executor pipeline
ExecutionAction = Union[ScaleDeploymentAction, RestartDeploymentAction]


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
    steps: Optional[List[str]] = None
    remediation_commands: Optional[List[str]] = None
    execution_plan: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="allow")  # tolerate unknown fields from Agent 1
