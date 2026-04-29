"""
validator.py — Pre-execution validation layer.

Checks an ExecutionPlan before any Kubernetes call is made:
  1. Action type is in the configured allowlist
  2. Target namespace is permitted
  3. Replica count is within safe bounds
  4. Cooldown window has elapsed for (action, deployment)

Returns a list of (action, reason) rejects so the caller can log them clearly.
"""

from __future__ import annotations

from typing import List, Tuple

from app.config import settings
from app.logging_config import get_logger
from app.models import ActionType, ExecutionAction, ExecutionPlan, ScaleDeploymentAction
from app.utils import CooldownTracker

logger = get_logger(__name__)

# Absolute replica safety cap (regardless of what the runbook requests)
MAX_SAFE_REPLICAS = 20


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
    target_namespace: The namespace that will be used if the action omits one

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
                    "deployment": getattr(action, "deployment", "?"),
                    "reason": rejection,
                },
            )
            result.rejected.append((action, rejection))
        else:
            result.approved.append(action)

    return result


def _check_action(
    action: ExecutionAction,
    cooldown: CooldownTracker,
    target_namespace: str,
) -> str:
    """
    Return an error string if the action should be rejected, empty string otherwise.
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

    # ── 3. Replica safety cap ─────────────────────────────────────────────────
    if action.action == ActionType.SCALE_DEPLOYMENT:
        assert isinstance(action, ScaleDeploymentAction)
        if action.replicas > MAX_SAFE_REPLICAS:
            return (
                f"Requested replicas ({action.replicas}) exceeds "
                f"MAX_SAFE_REPLICAS ({MAX_SAFE_REPLICAS})"
            )

    # ── 4. Cooldown guard ─────────────────────────────────────────────────────
    if not cooldown.is_allowed(action.action.value, action.deployment):
        remaining = cooldown.seconds_remaining(action.action.value, action.deployment)
        return (
            f"Cooldown active for ({action.action.value}, {action.deployment}) — "
            f"{remaining:.0f}s remaining"
        )

    return ""  # approved
