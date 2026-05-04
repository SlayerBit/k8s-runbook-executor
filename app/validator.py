"""
validator.py — Pre-execution validation layer.

Checks an ExecutionPlan before any Kubernetes call is made:
  1. Action type is in the configured allowlist
  2. Target namespace is permitted
  3. Action-specific safety checks (replicas cap, resource format/limits)
  4. Cooldown window has elapsed for (action, target)

Returns a ValidationResult so the caller can log rejections clearly.
"""

from __future__ import annotations

import re
from typing import List, Tuple

from app.config import settings
from app.logging_config import get_logger
from app.models import (
    ActionType,
    DeleteNetworkPolicyAction,
    DeletePodAction,
    ExecutionAction,
    ExecutionPlan,
    ScaleDeploymentAction,
    UpdateResourcesAction,
)
from app.utils import CooldownTracker

logger = get_logger(__name__)

# ── Safety constants ───────────────────────────────────────────────────────────

# Absolute replica cap (regardless of what the runbook requests)
MAX_SAFE_REPLICAS = 20

# Resource limit caps
MAX_CPU_MILLICORES = 2000   # 2 vCPUs
MAX_MEMORY_MIB = 4096       # 4 GiB

# CPU format:  "500m"  (millicores)  or  "1.5"  (fractional cores)
_CPU_RE = re.compile(r"^\d+m$|^\d+(\.\d+)?$")
# Memory format: "512Mi", "1Gi", "2048Ki", etc.
_MEMORY_RE = re.compile(r"^\d+(Ki|Mi|Gi|Ti|Pi|K|M|G|T|P)$", re.IGNORECASE)


# ── Result container ───────────────────────────────────────────────────────────


class ValidationResult:
    """Holds the outcome of validating a full ExecutionPlan."""

    def __init__(self) -> None:
        self.approved: List[ExecutionAction] = []
        self.rejected: List[Tuple[ExecutionAction, str]] = []

    @property
    def has_approved(self) -> bool:
        return len(self.approved) > 0

    @property
    def has_rejected(self) -> bool:
        return len(self.rejected) > 0


# ── Public API ────────────────────────────────────────────────────────────────


def validate_plan(
    plan: ExecutionPlan,
    cooldown: CooldownTracker,
    target_namespace: str,
) -> ValidationResult:
    """
    Validate every action in *plan* against the configured safety rules.

    Parameters
    ----------
    plan            : ExecutionPlan to validate
    cooldown        : CooldownTracker instance (shared with executor)
    target_namespace: Fallback namespace when an action omits one

    Returns
    -------
    ValidationResult with .approved and .rejected lists.
    """
    result = ValidationResult()

    for action in plan.actions:
        rejection = _check_action(action, cooldown, target_namespace)
        if rejection:
            logger.warning(
                "Action rejected during validation",
                extra={
                    "runbook_id": plan.runbook_id,
                    "action": action.action,
                    "target": getattr(action, "deployment", "?"),
                    "reason": rejection,
                },
            )
            result.rejected.append((action, rejection))
        else:
            result.approved.append(action)

    return result


# ── Internal checks ───────────────────────────────────────────────────────────


def _check_action(
    action: ExecutionAction,
    cooldown: CooldownTracker,
    target_namespace: str,
) -> str:
    """
    Return a non-empty rejection reason string, or '' if the action is approved.
    """

    # ── 1. Allowlist check ────────────────────────────────────────────────────
    if action.action.value not in settings.ALLOWED_ACTIONS:
        return (
            f"Action '{action.action.value}' is not in ALLOWED_ACTIONS: "
            f"{settings.ALLOWED_ACTIONS}"
        )

    # ── 2. Namespace guard ────────────────────────────────────────────────────
    ns = action.namespace or target_namespace
    if ns not in settings.ALLOWED_NAMESPACES:
        return (
            f"Namespace '{ns}' is not in ALLOWED_NAMESPACES: "
            f"{settings.ALLOWED_NAMESPACES}"
        )

    # ── 3. Action-specific safety checks ──────────────────────────────────────

    if action.action == ActionType.SCALE_DEPLOYMENT:
        assert isinstance(action, ScaleDeploymentAction)
        if action.replicas > MAX_SAFE_REPLICAS:
            return (
                f"Requested replicas ({action.replicas}) exceeds "
                f"MAX_SAFE_REPLICAS ({MAX_SAFE_REPLICAS})"
            )

    elif action.action == ActionType.UPDATE_RESOURCES:
        assert isinstance(action, UpdateResourcesAction)
        err = _validate_cpu(action.cpu) or _validate_memory(action.memory)
        if err:
            return err

    elif action.action == ActionType.DELETE_POD:
        assert isinstance(action, DeletePodAction)
        if not action.pod:
            return "delete_pod requires a non-empty pod name"

    elif action.action == ActionType.DELETE_NETWORK_POLICY:
        assert isinstance(action, DeleteNetworkPolicyAction)
        if not action.name:
            return "delete_network_policy requires a non-empty policy name"

    # rollback_deployment: deployment field is already validated by the model

    # ── 4. Cooldown guard ─────────────────────────────────────────────────────
    cooldown_target = getattr(action, "deployment", "unknown")
    if not cooldown.is_allowed(action.action.value, cooldown_target):
        remaining = cooldown.seconds_remaining(action.action.value, cooldown_target)
        return (
            f"Cooldown active for ({action.action.value}, {cooldown_target}) — "
            f"{remaining:.0f}s remaining"
        )

    return ""  # approved


# ── Resource format helpers ────────────────────────────────────────────────────


def _validate_cpu(cpu: str) -> str:
    """Return a rejection reason string, or '' if the cpu value is acceptable."""
    if not _CPU_RE.match(cpu):
        return (
            f"CPU value '{cpu}' is invalid — must be millicores (e.g. '500m') "
            "or fractional cores (e.g. '1.5')"
        )
    # Convert to millicores for the cap check
    if cpu.endswith("m"):
        millicores = int(cpu[:-1])
    else:
        millicores = int(float(cpu) * 1000)
    if millicores > MAX_CPU_MILLICORES:
        return (
            f"CPU '{cpu}' ({millicores}m) exceeds MAX_CPU_MILLICORES "
            f"({MAX_CPU_MILLICORES}m)"
        )
    return ""


def _validate_memory(memory: str) -> str:
    """Return a rejection reason string, or '' if the memory value is acceptable."""
    if not _MEMORY_RE.match(memory):
        return (
            f"Memory value '{memory}' is invalid — must be a quantity with a "
            "binary suffix (e.g. '512Mi', '1Gi', '2048Ki')"
        )
    # Convert to MiB for the cap check (handle Mi/Gi/Ki/Ti only; M/G/K treated as base-10)
    upper = memory.upper()
    if upper.endswith("GI"):
        mib = int(memory[:-2]) * 1024
    elif upper.endswith("MI"):
        mib = int(memory[:-2])
    elif upper.endswith("KI"):
        mib = int(memory[:-2]) // 1024
    else:
        # Rough approximation for base-10 suffixes
        mib = int(re.sub(r"[^0-9]", "", memory))
    if mib > MAX_MEMORY_MIB:
        return (
            f"Memory '{memory}' (~{mib} MiB) exceeds MAX_MEMORY_MIB "
            f"({MAX_MEMORY_MIB} MiB)"
        )
    return ""
