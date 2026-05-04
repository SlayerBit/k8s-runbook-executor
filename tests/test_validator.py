"""
test_validator.py — Unit tests for app.validator
"""

from __future__ import annotations

import time

import pytest

from app.models import (
    ActionType,
    DeleteNetworkPolicyAction,
    DeletePodAction,
    ExecutionPlan,
    RestartDeploymentAction,
    RollbackDeploymentAction,
    ScaleDeploymentAction,
    UpdateResourcesAction,
)
from app.utils import CooldownTracker
from app.validator import (
    MAX_CPU_MILLICORES,
    MAX_MEMORY_MIB,
    MAX_SAFE_REPLICAS,
    validate_plan,
)


# ── Factories ─────────────────────────────────────────────────────────────────

def _make_scale(deployment: str, replicas: int, namespace: str = "food-app") -> ScaleDeploymentAction:
    return ScaleDeploymentAction(
        action=ActionType.SCALE_DEPLOYMENT, deployment=deployment,
        replicas=replicas, namespace=namespace,
    )


def _make_restart(deployment: str, namespace: str = "food-app") -> RestartDeploymentAction:
    return RestartDeploymentAction(
        action=ActionType.RESTART_DEPLOYMENT, deployment=deployment, namespace=namespace,
    )


def _make_rollback(deployment: str, namespace: str = "food-app") -> RollbackDeploymentAction:
    return RollbackDeploymentAction(
        action=ActionType.ROLLBACK_DEPLOYMENT, deployment=deployment, namespace=namespace,
    )


def _make_delete_pod(pod: str, namespace: str = "food-app") -> DeletePodAction:
    return DeletePodAction(
        action=ActionType.DELETE_POD, pod=pod, namespace=namespace,
    )


def _make_update_resources(
    deployment: str, cpu: str, memory: str, namespace: str = "food-app"
) -> UpdateResourcesAction:
    return UpdateResourcesAction(
        action=ActionType.UPDATE_RESOURCES, deployment=deployment,
        cpu=cpu, memory=memory, namespace=namespace,
    )


def _make_delete_netpol(name: str, namespace: str = "food-app") -> DeleteNetworkPolicyAction:
    return DeleteNetworkPolicyAction(
        action=ActionType.DELETE_NETWORK_POLICY, name=name, namespace=namespace,
    )


def _fresh_cooldown(seconds: int = 60) -> CooldownTracker:
    return CooldownTracker(cooldown_seconds=seconds)


def _all_actions_allowed(monkeypatch):
    monkeypatch.setattr(
        "app.validator.settings.ALLOWED_ACTIONS",
        [
            "scale_deployment", "restart_deployment", "rollback_deployment",
            "delete_pod", "update_resources", "delete_network_policy",
        ],
    )
    monkeypatch.setattr("app.validator.settings.ALLOWED_NAMESPACES", ["food-app"])


# ── Allowlist ──────────────────────────────────────────────────────────────────

class TestAllowlist:
    def test_allowed_action_passes(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        plan = ExecutionPlan(runbook_id="rb-001", actions=[_make_scale("backend", 3)])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert result.has_approved
        assert not result.has_rejected

    def test_disallowed_action_rejected(self, monkeypatch):
        monkeypatch.setattr("app.validator.settings.ALLOWED_ACTIONS", ["restart_deployment"])
        monkeypatch.setattr("app.validator.settings.ALLOWED_NAMESPACES", ["food-app"])
        plan = ExecutionPlan(runbook_id="rb-002", actions=[_make_scale("backend", 3)])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert len(result.rejected) == 1
        assert not result.has_approved


# ── Namespace guard ────────────────────────────────────────────────────────────

class TestNamespaceGuard:
    def test_forbidden_namespace_rejected(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        action = _make_scale("backend", 3, namespace="kube-system")
        plan = ExecutionPlan(runbook_id="rb-003", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert len(result.rejected) == 1
        _, reason = result.rejected[0]
        assert "kube-system" in reason

    def test_delete_pod_forbidden_namespace_rejected(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        action = _make_delete_pod("cache-abc", namespace="kube-system")
        plan = ExecutionPlan(runbook_id="rb-003b", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert len(result.rejected) == 1

    def test_delete_netpol_forbidden_namespace_rejected(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        action = _make_delete_netpol("deny-all", namespace="production")
        plan = ExecutionPlan(runbook_id="rb-003c", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert len(result.rejected) == 1


# ── Replica cap ───────────────────────────────────────────────────────────────

class TestReplicaCap:
    def test_replica_cap_enforced(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        plan = ExecutionPlan(
            runbook_id="rb-004", actions=[_make_scale("backend", MAX_SAFE_REPLICAS + 1)]
        )
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert len(result.rejected) == 1
        _, reason = result.rejected[0]
        assert "MAX_SAFE_REPLICAS" in reason

    def test_at_replica_cap_passes(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        plan = ExecutionPlan(
            runbook_id="rb-005", actions=[_make_scale("backend", MAX_SAFE_REPLICAS)]
        )
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert result.has_approved


# ── Rollback validation ────────────────────────────────────────────────────────

class TestRollbackValidation:
    def test_rollback_passes(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        plan = ExecutionPlan(runbook_id="rb-010", actions=[_make_rollback("backend")])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert result.has_approved
        assert not result.has_rejected

    def test_rollback_forbidden_namespace(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        action = _make_rollback("backend", namespace="kube-system")
        plan = ExecutionPlan(runbook_id="rb-011", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert len(result.rejected) == 1


# ── Delete pod validation ──────────────────────────────────────────────────────

class TestDeletePodValidation:
    def test_delete_pod_passes(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        plan = ExecutionPlan(runbook_id="rb-020", actions=[_make_delete_pod("backend-abc")])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert result.has_approved

    def test_delete_pod_cooldown_uses_pod_name(self, monkeypatch):
        """Cooldown tracker key is (action, pod_name) not (action, deploymentName)."""
        _all_actions_allowed(monkeypatch)
        cooldown = CooldownTracker(cooldown_seconds=9999)
        cooldown.record("delete_pod", "backend-abc")
        plan = ExecutionPlan(runbook_id="rb-021", actions=[_make_delete_pod("backend-abc")])
        result = validate_plan(plan, cooldown, "food-app")
        assert len(result.rejected) == 1
        _, reason = result.rejected[0]
        assert "Cooldown" in reason


# ── Update resources validation ────────────────────────────────────────────────

class TestUpdateResourcesValidation:
    def test_valid_resources_pass(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        action = _make_update_resources("backend", cpu="500m", memory="512Mi")
        plan = ExecutionPlan(runbook_id="rb-030", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert result.has_approved

    def test_invalid_cpu_format_rejected(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        action = _make_update_resources("backend", cpu="500cores", memory="512Mi")
        plan = ExecutionPlan(runbook_id="rb-031", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert len(result.rejected) == 1
        _, reason = result.rejected[0]
        assert "CPU" in reason

    def test_invalid_memory_format_rejected(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        action = _make_update_resources("backend", cpu="500m", memory="512megabytes")
        plan = ExecutionPlan(runbook_id="rb-032", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert len(result.rejected) == 1
        _, reason = result.rejected[0]
        assert "Memory" in reason

    def test_cpu_over_limit_rejected(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        # MAX_CPU_MILLICORES = 2000, so 2001m should be rejected
        action = _make_update_resources("backend", cpu=f"{MAX_CPU_MILLICORES + 1}m", memory="512Mi")
        plan = ExecutionPlan(runbook_id="rb-033", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert len(result.rejected) == 1
        _, reason = result.rejected[0]
        assert "MAX_CPU_MILLICORES" in reason

    def test_memory_over_limit_rejected(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        # MAX_MEMORY_MIB = 4096, so 5000Mi should be rejected
        action = _make_update_resources("backend", cpu="500m", memory="5000Mi")
        plan = ExecutionPlan(runbook_id="rb-034", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert len(result.rejected) == 1
        _, reason = result.rejected[0]
        assert "MAX_MEMORY_MIB" in reason

    def test_cpu_in_cores_format_accepted(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        action = _make_update_resources("backend", cpu="1.5", memory="512Mi")
        plan = ExecutionPlan(runbook_id="rb-035", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert result.has_approved

    def test_memory_in_gi_format_accepted(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        action = _make_update_resources("backend", cpu="500m", memory="2Gi")
        plan = ExecutionPlan(runbook_id="rb-036", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert result.has_approved


# ── Delete network policy validation ──────────────────────────────────────────

class TestDeleteNetworkPolicyValidation:
    def test_delete_netpol_passes(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        action = _make_delete_netpol("deny-all")
        plan = ExecutionPlan(runbook_id="rb-040", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert result.has_approved

    def test_delete_netpol_cooldown_uses_name(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        cooldown = CooldownTracker(cooldown_seconds=9999)
        cooldown.record("delete_network_policy", "deny-all")
        plan = ExecutionPlan(runbook_id="rb-041", actions=[_make_delete_netpol("deny-all")])
        result = validate_plan(plan, cooldown, "food-app")
        assert len(result.rejected) == 1
        _, reason = result.rejected[0]
        assert "Cooldown" in reason


# ── Cooldown ──────────────────────────────────────────────────────────────────

class TestCooldown:
    def test_cooldown_blocks_repeated_action(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        cooldown = CooldownTracker(cooldown_seconds=9999)
        cooldown.record("scale_deployment", "backend")
        plan = ExecutionPlan(runbook_id="rb-050", actions=[_make_scale("backend", 3)])
        result = validate_plan(plan, cooldown, "food-app")
        assert len(result.rejected) == 1
        _, reason = result.rejected[0]
        assert "Cooldown" in reason

    def test_cooldown_allows_after_expiry(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        cooldown = CooldownTracker(cooldown_seconds=0)
        cooldown.record("scale_deployment", "backend")
        time.sleep(0.01)
        plan = ExecutionPlan(runbook_id="rb-051", actions=[_make_scale("backend", 3)])
        result = validate_plan(plan, cooldown, "food-app")
        assert result.has_approved

    def test_cooldown_different_targets_independent(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        cooldown = CooldownTracker(cooldown_seconds=9999)
        cooldown.record("rollback_deployment", "backend")
        # Different deployment should NOT be blocked
        plan = ExecutionPlan(runbook_id="rb-052", actions=[_make_rollback("frontend")])
        result = validate_plan(plan, cooldown, "food-app")
        assert result.has_approved


# ── Mixed / empty ──────────────────────────────────────────────────────────────

class TestMixedAndEdgeCases:
    def test_partial_approval(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        cooldown = CooldownTracker(cooldown_seconds=9999)
        cooldown.record("restart_deployment", "api")
        plan = ExecutionPlan(
            runbook_id="rb-060",
            actions=[
                _make_scale("backend", 2),   # OK
                _make_restart("api"),         # blocked by cooldown
            ],
        )
        result = validate_plan(plan, cooldown, "food-app")
        assert len(result.approved) == 1
        assert len(result.rejected) == 1

    def test_empty_plan(self, monkeypatch):
        _all_actions_allowed(monkeypatch)
        plan = ExecutionPlan(runbook_id="rb-061", actions=[])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")
        assert not result.has_approved
        assert not result.has_rejected
