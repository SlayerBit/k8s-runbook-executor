"""
parser.py — Converts raw remediation_commands strings into structured ExecutionActions.

Supported kubectl input formats:
  kubectl scale deployment <name> --replicas=<N>
  kubectl rollout restart deployment <name>
  kubectl rollout undo deployment <name>
  kubectl delete pod <name>
  kubectl delete networkpolicy <name>

Supported structured execution_plan entries:
  {"action": "scale_deployment",      "deployment": "...", "replicas": N}
  {"action": "restart_deployment",    "deployment": "..."}
  {"action": "rollback_deployment",   "deployment": "..."}
  {"action": "delete_pod",            "pod": "..."}
  {"action": "update_resources",      "deployment": "...", "cpu": "...", "memory": "..."}
  {"action": "delete_network_policy", "name": "..."}

Safety design: positive allowlisted patterns are matched FIRST so that targeted
'kubectl delete pod/networkpolicy' commands are captured before the general
unsafe-verb guard fires. Everything else is dropped — no exceptions are raised,
so a single bad command never blocks the entire runbook.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.logging_config import get_logger
from app.models import (
    ActionType,
    DeleteNetworkPolicyAction,
    DeletePodAction,
    ExecutionAction,
    RestartDeploymentAction,
    RollbackDeploymentAction,
    ScaleDeploymentAction,
    UpdateResourcesAction,
)

logger = get_logger(__name__)

# ── Regex patterns ─────────────────────────────────────────────────────────────

# Reusable name segment (valid K8s lowercase name)
_NAME = r"(?P<name>[a-z0-9][a-z0-9\-]*[a-z0-9]|[a-z0-9])"

# kubectl scale deployment <name> --replicas=<N>
_SCALE_RE = re.compile(
    r"kubectl\s+scale\s+deployment[s]?\s+" + _NAME + r".*?--replicas[=\s]+(?P<replicas>\d+)",
    re.IGNORECASE,
)

# kubectl rollout restart deployment <name>
_RESTART_RE = re.compile(
    r"kubectl\s+rollout\s+restart\s+deployment[s]?\s+" + _NAME,
    re.IGNORECASE,
)

# kubectl rollout undo deployment <name>
_ROLLBACK_RE = re.compile(
    r"kubectl\s+rollout\s+undo\s+deployment[s]?\s+" + _NAME,
    re.IGNORECASE,
)

# kubectl delete pod[s] <name>  — allowlisted BEFORE the unsafe guard
_DELETE_POD_RE = re.compile(
    r"kubectl\s+delete\s+pods?\s+" + _NAME,
    re.IGNORECASE,
)

# kubectl delete networkpolicy[ies] <name>  — allowlisted BEFORE the unsafe guard
_DELETE_NETPOL_RE = re.compile(
    r"kubectl\s+delete\s+networkpolic(?:y|ies)\s+" + _NAME,
    re.IGNORECASE,
)

# Explicitly dangerous / unsupported verb prefixes (keep 'delete' here so that
# any delete target NOT handled above is still dropped)
_UNSAFE_PATTERNS = re.compile(
    r"kubectl\s+(exec|delete|run|apply|patch|replace|create|label|annotate"
    r"|cordon|uncordon|drain|taint|cp|port-forward|proxy|auth|certificate"
    r"|attach|debug)\b",
    re.IGNORECASE,
)

# Read-only verbs — informational, not executed
_READONLY_PATTERNS = re.compile(
    r"kubectl\s+(get|describe|logs|top|explain|diff|api-resources|api-versions|version)\b",
    re.IGNORECASE,
)


# ── Internal helper ────────────────────────────────────────────────────────────

def _try_build(action_cls, kwargs: Dict[str, Any], command: str, label: str) -> Optional[ExecutionAction]:
    """Instantiate *action_cls* from *kwargs*, returning None on any error."""
    try:
        action = action_cls(**kwargs)
        logger.debug(
            "Parsed %s action", label,
            extra={"command": command, "action": action.model_dump()},
        )
        return action
    except Exception as exc:
        logger.warning(
            "Failed to build %s", label,
            extra={"command": command, "error": str(exc)},
        )
        return None


# ── Public API ────────────────────────────────────────────────────────────────


def parse_command(command: str) -> Optional[ExecutionAction]:
    """
    Parse a single raw command string into a structured ExecutionAction.

    Positive patterns are evaluated first so that allowlisted 'delete'
    sub-commands (pod, networkpolicy) are captured before the unsafe-verb
    guard fires.

    Returns None if the command is unsupported, unsafe, or unrecognisable.
    """
    command = command.strip()
    if not command:
        return None

    # ── 1. Scale ───────────────────────────────────────────────────────────────
    m = _SCALE_RE.search(command)
    if m:
        return _try_build(
            ScaleDeploymentAction,
            {
                "action": ActionType.SCALE_DEPLOYMENT,
                "deployment": m.group("name").lower(),
                "replicas": int(m.group("replicas")),
            },
            command, "scale_deployment",
        )

    # ── 2. Rolling restart ─────────────────────────────────────────────────────
    m = _RESTART_RE.search(command)
    if m:
        return _try_build(
            RestartDeploymentAction,
            {"action": ActionType.RESTART_DEPLOYMENT, "deployment": m.group("name").lower()},
            command, "restart_deployment",
        )

    # ── 3. Rollback (rollout undo) ─────────────────────────────────────────────
    m = _ROLLBACK_RE.search(command)
    if m:
        return _try_build(
            RollbackDeploymentAction,
            {"action": ActionType.ROLLBACK_DEPLOYMENT, "deployment": m.group("name").lower()},
            command, "rollback_deployment",
        )

    # ── 4. Delete pod (allowlisted before unsafe guard) ───────────────────────
    m = _DELETE_POD_RE.search(command)
    if m:
        return _try_build(
            DeletePodAction,
            {"action": ActionType.DELETE_POD, "pod": m.group("name").lower()},
            command, "delete_pod",
        )

    # ── 5. Delete networkpolicy (allowlisted before unsafe guard) ─────────────
    m = _DELETE_NETPOL_RE.search(command)
    if m:
        return _try_build(
            DeleteNetworkPolicyAction,
            {"action": ActionType.DELETE_NETWORK_POLICY, "name": m.group("name").lower()},
            command, "delete_network_policy",
        )

    # ── 6. Reject remaining unsafe verbs ──────────────────────────────────────
    if _UNSAFE_PATTERNS.search(command):
        logger.warning(
            "Dropping unsafe command",
            extra={"command": command, "reason": "matches_unsafe_pattern"},
        )
        return None

    # ── 7. Skip read-only commands ─────────────────────────────────────────────
    if _READONLY_PATTERNS.search(command):
        logger.info(
            "Skipping read-only command (not executed)",
            extra={"command": command},
        )
        return None

    logger.info("Command not recognised — skipping", extra={"command": command})
    return None


def parse_execution_plan_entry(entry: Dict[str, Any]) -> Optional[ExecutionAction]:
    """
    Convert a pre-structured dict from the runbook's `execution_plan` field
    into an ExecutionAction.

    Supported action strings:
      scale_deployment, restart_deployment, rollback_deployment,
      delete_pod, update_resources, delete_network_policy
    """
    action_str = entry.get("action", "")

    _ACTION_MAP = {
        ActionType.SCALE_DEPLOYMENT:      ScaleDeploymentAction,
        ActionType.RESTART_DEPLOYMENT:    RestartDeploymentAction,
        ActionType.ROLLBACK_DEPLOYMENT:   RollbackDeploymentAction,
        ActionType.DELETE_POD:            DeletePodAction,
        ActionType.UPDATE_RESOURCES:      UpdateResourcesAction,
        ActionType.DELETE_NETWORK_POLICY: DeleteNetworkPolicyAction,
    }

    for action_type, cls in _ACTION_MAP.items():
        if action_str == action_type:
            try:
                return cls(**entry)
            except Exception as exc:
                logger.warning(
                    "Invalid %s entry in execution_plan", action_str,
                    extra={"entry": entry, "error": str(exc)},
                )
                return None

    logger.info(
        "Unsupported action in execution_plan — skipping",
        extra={"action": action_str, "entry": entry},
    )
    return None


def build_execution_plan(
    runbook_id: str,
    remediation_commands: Optional[List[str]] = None,
    execution_plan: Optional[List[Dict[str, Any]]] = None,
) -> List[ExecutionAction]:
    """
    Build an ordered list of ExecutionActions from a runbook.

    Priority:
      1. execution_plan  (pre-structured, preferred)
      2. remediation_commands (raw kubectl strings, parsed)

    Duplicate actions within the same runbook are deduplicated by their
    canonical JSON representation.
    """
    actions: List[ExecutionAction] = []
    seen_keys: set = set()

    def _unique_key(act: ExecutionAction) -> str:
        return act.model_dump_json()

    # ── Path 1: pre-structured execution_plan ─────────────────────────────────
    if execution_plan:
        logger.info(
            "Building plan from execution_plan field",
            extra={"runbook_id": runbook_id, "entries": len(execution_plan)},
        )
        for entry in execution_plan:
            action = parse_execution_plan_entry(entry)
            if action is not None:
                key = _unique_key(action)
                if key not in seen_keys:
                    actions.append(action)
                    seen_keys.add(key)
                else:
                    logger.debug("Duplicate action within runbook — skipping", extra={"key": key})
        return actions

    # ── Path 2: raw remediation_commands ──────────────────────────────────────
    if remediation_commands:
        logger.info(
            "Building plan from remediation_commands",
            extra={"runbook_id": runbook_id, "commands": len(remediation_commands)},
        )
        for cmd in remediation_commands:
            action = parse_command(cmd)
            if action is not None:
                key = _unique_key(action)
                if key not in seen_keys:
                    actions.append(action)
                    seen_keys.add(key)
                else:
                    logger.debug("Duplicate action within runbook — skipping", extra={"key": key})
        return actions

    logger.info(
        "Runbook has no parseable commands or execution_plan",
        extra={"runbook_id": runbook_id},
    )
    return []
