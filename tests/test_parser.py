"""
test_parser.py — Unit tests for app.parser
"""

from __future__ import annotations

import pytest

from app.models import ActionType
from app.parser import (
    build_execution_plan,
    parse_command,
    parse_execution_plan_entry,
)


# ── parse_command ──────────────────────────────────────────────────────────────

class TestParseCommand:
    def test_scale_basic(self):
        action = parse_command("kubectl scale deployment backend --replicas=5")
        assert action is not None
        assert action.action == ActionType.SCALE_DEPLOYMENT
        assert action.deployment == "backend"
        assert action.replicas == 5

    def test_scale_with_namespace_flag(self):
        """Namespace flag in the command is fine — parser ignores it (namespace from config)."""
        action = parse_command(
            "kubectl scale deployment frontend --replicas=3 -n food-app"
        )
        assert action is not None
        assert action.deployment == "frontend"
        assert action.replicas == 3

    def test_scale_zero_replicas(self):
        action = parse_command("kubectl scale deployment myapp --replicas=0")
        assert action is not None
        assert action.replicas == 0

    def test_restart_basic(self):
        action = parse_command("kubectl rollout restart deployment backend")
        assert action is not None
        assert action.action == ActionType.RESTART_DEPLOYMENT
        assert action.deployment == "backend"

    def test_restart_extra_whitespace(self):
        action = parse_command("  kubectl  rollout  restart  deployment  api-server  ")
        assert action is not None
        assert action.deployment == "api-server"

    def test_unsafe_exec_dropped(self):
        assert parse_command("kubectl exec -it mypod -- bash") is None

    def test_unsafe_delete_dropped(self):
        assert parse_command("kubectl delete pod mypod") is None

    def test_readonly_get_skipped(self):
        assert parse_command("kubectl get pods") is None

    def test_readonly_logs_skipped(self):
        assert parse_command("kubectl logs mypod") is None

    def test_empty_string(self):
        assert parse_command("") is None

    def test_unrecognised_command(self):
        assert parse_command("helm upgrade my-release ./chart") is None

    def test_invalid_deployment_name(self):
        """Names with uppercase chars are invalid K8s resource names."""
        action = parse_command("kubectl scale deployment BadName --replicas=2")
        # Parser lowercases the name from the regex; pydantic validation may reject
        # truly invalid names — either None or with lowercase name is acceptable.
        if action is not None:
            assert action.deployment == "badname"


# ── parse_execution_plan_entry ─────────────────────────────────────────────────

class TestParseExecutionPlanEntry:
    def test_scale_entry(self):
        entry = {"action": "scale_deployment", "deployment": "backend", "replicas": 3}
        action = parse_execution_plan_entry(entry)
        assert action is not None
        assert action.action == ActionType.SCALE_DEPLOYMENT
        assert action.replicas == 3

    def test_restart_entry(self):
        entry = {"action": "restart_deployment", "deployment": "worker"}
        action = parse_execution_plan_entry(entry)
        assert action is not None
        assert action.action == ActionType.RESTART_DEPLOYMENT

    def test_unsupported_action(self):
        entry = {"action": "delete_pod", "deployment": "backend"}
        assert parse_execution_plan_entry(entry) is None

    def test_missing_replicas_for_scale(self):
        entry = {"action": "scale_deployment", "deployment": "backend"}
        assert parse_execution_plan_entry(entry) is None

    def test_negative_replicas_rejected(self):
        entry = {"action": "scale_deployment", "deployment": "backend", "replicas": -1}
        assert parse_execution_plan_entry(entry) is None


# ── build_execution_plan ───────────────────────────────────────────────────────

class TestBuildExecutionPlan:
    def test_prefers_execution_plan_over_commands(self):
        execution_plan = [
            {"action": "restart_deployment", "deployment": "api"},
        ]
        remediation_commands = [
            "kubectl scale deployment backend --replicas=5",
        ]
        actions = build_execution_plan(
            runbook_id="rb-001",
            remediation_commands=remediation_commands,
            execution_plan=execution_plan,
        )
        # execution_plan takes priority
        assert len(actions) == 1
        assert actions[0].action == ActionType.RESTART_DEPLOYMENT
        assert actions[0].deployment == "api"

    def test_falls_back_to_commands(self):
        actions = build_execution_plan(
            runbook_id="rb-002",
            remediation_commands=["kubectl scale deployment svc --replicas=2"],
            execution_plan=None,
        )
        assert len(actions) == 1
        assert actions[0].replicas == 2

    def test_deduplication_within_plan(self):
        cmds = [
            "kubectl scale deployment backend --replicas=5",
            "kubectl scale deployment backend --replicas=5",  # duplicate
        ]
        actions = build_execution_plan("rb-003", remediation_commands=cmds)
        assert len(actions) == 1

    def test_mixed_valid_invalid_commands(self):
        cmds = [
            "kubectl rollout restart deployment api",
            "kubectl exec -it pod -- bash",   # unsafe → dropped
            "kubectl get pods",               # read-only → skipped
            "kubectl scale deployment db --replicas=1",
        ]
        actions = build_execution_plan("rb-004", remediation_commands=cmds)
        assert len(actions) == 2

    def test_no_commands_returns_empty(self):
        actions = build_execution_plan("rb-005")
        assert actions == []
