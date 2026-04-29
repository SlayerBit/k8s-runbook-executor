"""
parser.py — Converts raw remediation_commands strings into structured ExecutionActions.

Supported input formats (kubectl-style):
  kubectl scale deployment <name> --replicas=<N>
  kubectl rollout restart deployment <name>

Everything else is logged as unsupported and dropped — no exceptions are raised
so a single bad command never blocks the entire runbook.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.logging_config import get_logger
from app.models import (
    ActionType,
    ExecutionAction,
    RestartDeploymentAction,
    ScaleDeploymentAction,
)

logger = get_logger(__name__)

# ── Regex patterns ─────────────────────────────────────────────────────────────

# kubectl scale deployment <name> --replicas=<N>
_SCALE_RE = re.compile(
    r"kubectl\s+scale\s+deployment[s]?\s+"
    r"(?P<name>[a-z0-9][a-z0-9\-]*[a-z0-9]|[a-z0-9])"
    r".*?--replicas[=\s]+(?P<replicas>\d+)",
    re.IGNORECASE,
)

# kubectl rollout restart deployment <name>
_RESTART_RE = re.compile(
    r"kubectl\s+rollout\s+restart\s+deployment[s]?\s+"
    r"(?P<name>[a-z0-9][a-z0-9\-]*[a-z0-9]|[a-z0-9])",
    re.IGNORECASE,
)

# Explicitly dangerous / unsupported verb prefixes — logged and dropped early
_UNSAFE_PATTERNS = re.compile(
    r"kubectl\s+(exec|delete|run|apply|patch|replace|create|label|annotate"
    r"|cordon|uncordon|drain|taint|cp|port-forward|proxy|auth|certificate"
    r"|attach|debug)\b",
    re.IGNORECASE,
)

# Read-only verbs — safe to log but not executed
_READONLY_PATTERNS = re.compile(
    r"kubectl\s+(get|describe|logs|top|explain|diff|api-resources|api-versions|version)\b",
    re.IGNORECASE,
)


# ── Public API ────────────────────────────────────────────────────────────────

def parse_command(command: str) -> Optional[ExecutionAction]:
    """
    Parse a single raw command string into a structured ExecutionAction.

    Returns None if the command is unsupported, unsafe, or unrecognisable.
    """
    command = command.strip()

    if not command:
        return None

    # Reject explicitly unsafe commands first
    if _UNSAFE_PATTERNS.search(command):
        logger.warning(
            "Dropping unsafe command",
            extra={"command": command, "reason": "matches_unsafe_pattern"},
        )
        return None

    # Skip read-only commands — informational, not executed
    if _READONLY_PATTERNS.search(command):
        logger.info(
            "Skipping read-only command (not executed)",
            extra={"command": command},
        )
        return None

    # Try scale
    m = _SCALE_RE.search(command)
    if m:
        try:
            action = ScaleDeploymentAction(
                action=ActionType.SCALE_DEPLOYMENT,
                deployment=m.group("name").lower(),
                replicas=int(m.group("replicas")),
            )
            logger.debug(
                "Parsed scale action",
                extra={"command": command, "action": action.model_dump()},
            )
            return action
        except Exception as exc:
            logger.warning(
                "Failed to build ScaleDeploymentAction",
                extra={"command": command, "error": str(exc)},
            )
            return None

    # Try restart
    m = _RESTART_RE.search(command)
    if m:
        try:
            action = RestartDeploymentAction(
                action=ActionType.RESTART_DEPLOYMENT,
                deployment=m.group("name").lower(),
            )
            logger.debug(
                "Parsed restart action",
                extra={"command": command, "action": action.model_dump()},
            )
            return action
        except Exception as exc:
            logger.warning(
                "Failed to build RestartDeploymentAction",
                extra={"command": command, "error": str(exc)},
            )
            return None

    logger.info(
        "Command not recognised — skipping",
        extra={"command": command},
    )
    return None


def parse_execution_plan_entry(entry: Dict[str, Any]) -> Optional[ExecutionAction]:
    """
    Convert a pre-structured dict from the runbook's `execution_plan` field
    into an ExecutionAction.

    Expected dict shapes:
      {"action": "scale_deployment",   "deployment": "...", "replicas": N}
      {"action": "restart_deployment", "deployment": "..."}
    """
    action_str = entry.get("action", "")

    if action_str == ActionType.SCALE_DEPLOYMENT:
        try:
            return ScaleDeploymentAction(**entry)
        except Exception as exc:
            logger.warning(
                "Invalid scale_deployment entry in execution_plan",
                extra={"entry": entry, "error": str(exc)},
            )
            return None

    if action_str == ActionType.RESTART_DEPLOYMENT:
        try:
            return RestartDeploymentAction(**entry)
        except Exception as exc:
            logger.warning(
                "Invalid restart_deployment entry in execution_plan",
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
    canonical string representation.
    """
    actions: List[ExecutionAction] = []
    seen_keys: set = set()

    def _unique_key(act: ExecutionAction) -> str:
        return act.model_dump_json()

    # ── Path 1: pre-structured execution_plan ──────────────────────────────
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

    # ── Path 2: raw remediation_commands ──────────────────────────────────
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
