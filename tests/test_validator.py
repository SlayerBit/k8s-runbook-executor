"""
test_validator.py — Unit tests for app.validator
"""

from __future__ import annotations

import time

import pytest

from app.models import (
    ActionType,
    ExecutionPlan,
    RestartDeploymentAction,
    ScaleDeploymentAction,
)
from app.utils import CooldownTracker
from app.validator import MAX_SAFE_REPLICAS, validate_plan


def _make_scale(deployment: str, replicas: int, namespace: str = "food-app") -> ScaleDeploymentAction:
    return ScaleDeploymentAction(
        action=ActionType.SCALE_DEPLOYMENT,
        deployment=deployment,
        replicas=replicas,
        namespace=namespace,
    )


def _make_restart(deployment: str, namespace: str = "food-app") -> RestartDeploymentAction:
    return RestartDeploymentAction(
        action=ActionType.RESTART_DEPLOYMENT,
        deployment=deployment,
        namespace=namespace,
    )


def _fresh_cooldown(seconds: int = 60) -> CooldownTracker:
    return CooldownTracker(cooldown_seconds=seconds)


class TestValidatePlan:
    # ── Allowlist ──────────────────────────────────────────────────────────────

    def test_allowed_action_passes(self, monkeypatch):
        monkeypatch.setattr("app.validator.settings.ALLOWED_ACTIONS", ["scale_deployment", "restart_deployment"])
        monkeypatch.setattr("app.validator.settings.ALLOWED_NAMESPACES", ["food-app"])

        action = _make_scale("backend", 3)
        plan = ExecutionPlan(runbook_id="rb-001", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")

        assert len(result.approved) == 1
        assert not result.has_rejected

    def test_disallowed_action_rejected(self, monkeypatch):
        monkeypatch.setattr("app.validator.settings.ALLOWED_ACTIONS", ["restart_deployment"])
        monkeypatch.setattr("app.validator.settings.ALLOWED_NAMESPACES", ["food-app"])

        action = _make_scale("backend", 3)
        plan = ExecutionPlan(runbook_id="rb-002", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")

        assert len(result.rejected) == 1
        assert not result.has_approved

    # ── Namespace guard ────────────────────────────────────────────────────────

    def test_forbidden_namespace_rejected(self, monkeypatch):
        monkeypatch.setattr("app.validator.settings.ALLOWED_ACTIONS", ["scale_deployment"])
        monkeypatch.setattr("app.validator.settings.ALLOWED_NAMESPACES", ["food-app"])

        action = _make_scale("backend", 3, namespace="kube-system")
        plan = ExecutionPlan(runbook_id="rb-003", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")

        assert len(result.rejected) == 1
        _, reason = result.rejected[0]
        assert "kube-system" in reason

    # ── Replica cap ───────────────────────────────────────────────────────────

    def test_replica_cap_enforced(self, monkeypatch):
        monkeypatch.setattr("app.validator.settings.ALLOWED_ACTIONS", ["scale_deployment"])
        monkeypatch.setattr("app.validator.settings.ALLOWED_NAMESPACES", ["food-app"])

        action = _make_scale("backend", MAX_SAFE_REPLICAS + 1)
        plan = ExecutionPlan(runbook_id="rb-004", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")

        assert len(result.rejected) == 1
        _, reason = result.rejected[0]
        assert "MAX_SAFE_REPLICAS" in reason

    def test_at_replica_cap_passes(self, monkeypatch):
        monkeypatch.setattr("app.validator.settings.ALLOWED_ACTIONS", ["scale_deployment"])
        monkeypatch.setattr("app.validator.settings.ALLOWED_NAMESPACES", ["food-app"])

        action = _make_scale("backend", MAX_SAFE_REPLICAS)
        plan = ExecutionPlan(runbook_id="rb-005", actions=[action])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")

        assert result.has_approved

    # ── Cooldown ──────────────────────────────────────────────────────────────

    def test_cooldown_blocks_repeated_action(self, monkeypatch):
        monkeypatch.setattr("app.validator.settings.ALLOWED_ACTIONS", ["scale_deployment"])
        monkeypatch.setattr("app.validator.settings.ALLOWED_NAMESPACES", ["food-app"])

        cooldown = CooldownTracker(cooldown_seconds=9999)
        cooldown.record("scale_deployment", "backend")

        action = _make_scale("backend", 3)
        plan = ExecutionPlan(runbook_id="rb-006", actions=[action])
        result = validate_plan(plan, cooldown, "food-app")

        assert len(result.rejected) == 1
        _, reason = result.rejected[0]
        assert "Cooldown" in reason

    def test_cooldown_allows_after_expiry(self, monkeypatch):
        monkeypatch.setattr("app.validator.settings.ALLOWED_ACTIONS", ["scale_deployment"])
        monkeypatch.setattr("app.validator.settings.ALLOWED_NAMESPACES", ["food-app"])

        cooldown = CooldownTracker(cooldown_seconds=0)
        cooldown.record("scale_deployment", "backend")
        # With 0-second cooldown it should immediately be allowed again
        time.sleep(0.01)

        action = _make_scale("backend", 3)
        plan = ExecutionPlan(runbook_id="rb-007", actions=[action])
        result = validate_plan(plan, cooldown, "food-app")

        assert result.has_approved

    # ── Mixed approved + rejected ─────────────────────────────────────────────

    def test_partial_approval(self, monkeypatch):
        monkeypatch.setattr("app.validator.settings.ALLOWED_ACTIONS", ["scale_deployment", "restart_deployment"])
        monkeypatch.setattr("app.validator.settings.ALLOWED_NAMESPACES", ["food-app"])

        cooldown = CooldownTracker(cooldown_seconds=9999)
        cooldown.record("restart_deployment", "api")

        plan = ExecutionPlan(
            runbook_id="rb-008",
            actions=[
                _make_scale("backend", 2),   # OK
                _make_restart("api"),         # blocked by cooldown
            ],
        )
        result = validate_plan(plan, cooldown, "food-app")

        assert len(result.approved) == 1
        assert len(result.rejected) == 1

    # ── Empty plan ────────────────────────────────────────────────────────────

    def test_empty_plan(self, monkeypatch):
        monkeypatch.setattr("app.validator.settings.ALLOWED_ACTIONS", ["scale_deployment"])
        monkeypatch.setattr("app.validator.settings.ALLOWED_NAMESPACES", ["food-app"])

        plan = ExecutionPlan(runbook_id="rb-009", actions=[])
        result = validate_plan(plan, _fresh_cooldown(), "food-app")

        assert not result.has_approved
        assert not result.has_rejected
