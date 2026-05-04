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


# ── parse_command — existing actions ──────────────────────────────────────────

class TestParseCommandScale:
    def test_scale_basic(self):
        action = parse_command("kubectl scale deployment backend --replicas=5")
        assert action is not None
        assert action.action == ActionType.SCALE_DEPLOYMENT
        assert action.deployment == "backend"
        assert action.replicas == 5

    def test_scale_with_namespace_flag(self):
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


class TestParseCommandRestart:
    def test_restart_basic(self):
        action = parse_command("kubectl rollout restart deployment backend")
        assert action is not None
        assert action.action == ActionType.RESTART_DEPLOYMENT
        assert action.deployment == "backend"

    def test_restart_extra_whitespace(self):
        action = parse_command("  kubectl  rollout  restart  deployment  api-server  ")
        assert action is not None
        assert action.deployment == "api-server"


# ── parse_command — NEW actions ───────────────────────────────────────────────

class TestParseCommandRollback:
    def test_rollback_basic(self):
        action = parse_command("kubectl rollout undo deployment backend")
        assert action is not None
        assert action.action == ActionType.ROLLBACK_DEPLOYMENT
        assert action.deployment == "backend"

    def test_rollback_with_namespace_flag(self):
        action = parse_command("kubectl rollout undo deployment api -n food-app")
        assert action is not None
        assert action.deployment == "api"

    def test_rollback_hyphenated_name(self):
        action = parse_command("kubectl rollout undo deployment api-gateway")
        assert action is not None
        assert action.deployment == "api-gateway"


class TestParseCommandDeletePod:
    def test_delete_pod_basic(self):
        action = parse_command("kubectl delete pod backend-xyz")
        assert action is not None
        assert action.action == ActionType.DELETE_POD
        assert action.pod == "backend-xyz"

    def test_delete_pods_plural(self):
        """Plural 'pods' variant is also accepted."""
        action = parse_command("kubectl delete pods myservice-abc")
        assert action is not None
        assert action.action == ActionType.DELETE_POD

    def test_delete_pod_lowercase_name(self):
        action = parse_command("kubectl delete pod Worker-Pod-001")
        # Parser lowercases
        assert action is not None
        assert action.pod == "worker-pod-001"

    def test_delete_pod_deployment_proxy(self):
        """action.deployment must proxy to pod name for cooldown/logging compat."""
        action = parse_command("kubectl delete pod backend-xyz")
        assert action is not None
        assert action.deployment == action.pod


class TestParseCommandDeleteNetworkPolicy:
    def test_delete_netpol_basic(self):
        action = parse_command("kubectl delete networkpolicy block-ingress")
        assert action is not None
        assert action.action == ActionType.DELETE_NETWORK_POLICY
        assert action.name == "block-ingress"

    def test_delete_networkpolicies_plural(self):
        action = parse_command("kubectl delete networkpolicies deny-all")
        assert action is not None
        assert action.action == ActionType.DELETE_NETWORK_POLICY
        assert action.name == "deny-all"

    def test_delete_netpol_deployment_proxy(self):
        action = parse_command("kubectl delete networkpolicy deny-egress")
        assert action is not None
        assert action.deployment == action.name


# ── parse_command — safety / edge cases ──────────────────────────────────────

class TestParseCommandSafety:
    def test_unsafe_exec_dropped(self):
        assert parse_command("kubectl exec -it mypod -- bash") is None

    def test_unsafe_delete_deployment_dropped(self):
        """delete deployment is NOT allowlisted — must still be rejected."""
        assert parse_command("kubectl delete deployment backend") is None

    def test_unsafe_delete_secret_dropped(self):
        assert parse_command("kubectl delete secret mysecret") is None

    def test_unsafe_apply_dropped(self):
        assert parse_command("kubectl apply -f manifest.yaml") is None

    def test_readonly_get_skipped(self):
        assert parse_command("kubectl get pods") is None

    def test_readonly_logs_skipped(self):
        assert parse_command("kubectl logs mypod") is None

    def test_empty_string(self):
        assert parse_command("") is None

    def test_unrecognised_command(self):
        assert parse_command("helm upgrade my-release ./chart") is None

    def test_invalid_deployment_name_scale(self):
        """Names with uppercase chars are invalid K8s resource names."""
        action = parse_command("kubectl scale deployment BadName --replicas=2")
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

    def test_rollback_entry(self):
        entry = {"action": "rollback_deployment", "deployment": "backend"}
        action = parse_execution_plan_entry(entry)
        assert action is not None
        assert action.action == ActionType.ROLLBACK_DEPLOYMENT
        assert action.deployment == "backend"

    def test_delete_pod_entry(self):
        entry = {"action": "delete_pod", "pod": "backend-abc"}
        action = parse_execution_plan_entry(entry)
        assert action is not None
        assert action.action == ActionType.DELETE_POD
        assert action.pod == "backend-abc"

    def test_update_resources_entry(self):
        entry = {
            "action": "update_resources",
            "deployment": "backend",
            "cpu": "500m",
            "memory": "512Mi",
        }
        action = parse_execution_plan_entry(entry)
        assert action is not None
        assert action.action == ActionType.UPDATE_RESOURCES
        assert action.cpu == "500m"
        assert action.memory == "512Mi"

    def test_delete_network_policy_entry(self):
        entry = {"action": "delete_network_policy", "name": "block-ingress"}
        action = parse_execution_plan_entry(entry)
        assert action is not None
        assert action.action == ActionType.DELETE_NETWORK_POLICY
        assert action.name == "block-ingress"

    def test_unsupported_action_returns_none(self):
        entry = {"action": "nuke_cluster", "deployment": "backend"}
        assert parse_execution_plan_entry(entry) is None

    def test_missing_replicas_for_scale(self):
        entry = {"action": "scale_deployment", "deployment": "backend"}
        assert parse_execution_plan_entry(entry) is None

    def test_negative_replicas_rejected(self):
        entry = {"action": "scale_deployment", "deployment": "backend", "replicas": -1}
        assert parse_execution_plan_entry(entry) is None

    def test_update_resources_missing_cpu(self):
        entry = {"action": "update_resources", "deployment": "backend", "memory": "512Mi"}
        assert parse_execution_plan_entry(entry) is None

    def test_delete_pod_missing_pod_name(self):
        entry = {"action": "delete_pod"}
        assert parse_execution_plan_entry(entry) is None

    def test_delete_network_policy_missing_name(self):
        entry = {"action": "delete_network_policy"}
        assert parse_execution_plan_entry(entry) is None


# ── build_execution_plan ───────────────────────────────────────────────────────

class TestBuildExecutionPlan:
    def test_prefers_execution_plan_over_commands(self):
        execution_plan = [{"action": "restart_deployment", "deployment": "api"}]
        remediation_commands = ["kubectl scale deployment backend --replicas=5"]
        actions = build_execution_plan(
            runbook_id="rb-001",
            remediation_commands=remediation_commands,
            execution_plan=execution_plan,
        )
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
            "kubectl exec -it pod -- bash",        # unsafe → dropped
            "kubectl get pods",                    # read-only → skipped
            "kubectl scale deployment db --replicas=1",
            "kubectl rollout undo deployment frontend",   # rollback
            "kubectl delete pod cache-abc",              # delete pod
        ]
        actions = build_execution_plan("rb-004", remediation_commands=cmds)
        assert len(actions) == 4

    def test_no_commands_returns_empty(self):
        actions = build_execution_plan("rb-005")
        assert actions == []

    def test_rollback_via_execution_plan(self):
        plan = [{"action": "rollback_deployment", "deployment": "backend"}]
        actions = build_execution_plan("rb-006", execution_plan=plan)
        assert len(actions) == 1
        assert actions[0].action == ActionType.ROLLBACK_DEPLOYMENT
