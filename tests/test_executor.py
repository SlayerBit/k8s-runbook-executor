"""
test_executor.py — Unit tests for app.executor

All Kubernetes calls are mocked so no real cluster is needed.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.executor import Executor, StepStatus
from app.models import ActionType, ExecutionPlan, RestartDeploymentAction, ScaleDeploymentAction
from app.utils import CooldownTracker


def _make_executor(dry_run: bool = False, stop_on_failure: bool = True) -> Executor:
    cooldown = CooldownTracker(cooldown_seconds=0)  # no cooldown in tests
    return Executor(
        cooldown=cooldown,
        target_namespace="food-app",
        dry_run=dry_run,
        stop_on_failure=stop_on_failure,
    )


def _scale_action(deployment: str = "backend", replicas: int = 3) -> ScaleDeploymentAction:
    return ScaleDeploymentAction(
        action=ActionType.SCALE_DEPLOYMENT,
        deployment=deployment,
        replicas=replicas,
    )


def _restart_action(deployment: str = "api") -> RestartDeploymentAction:
    return RestartDeploymentAction(
        action=ActionType.RESTART_DEPLOYMENT,
        deployment=deployment,
    )


class TestExecutorDryRun:
    def test_dry_run_does_not_call_k8s(self):
        executor = _make_executor(dry_run=True)
        plan = ExecutionPlan(
            runbook_id="rb-dry-001",
            actions=[_scale_action(), _restart_action()],
        )

        with patch("app.executor.k8s.scale_deployment") as mock_scale, \
             patch("app.executor.k8s.restart_deployment") as mock_restart:
            report = executor.run(plan)

        mock_scale.assert_not_called()
        mock_restart.assert_not_called()
        assert all(s.status == StepStatus.DRY_RUN for s in report.steps)
        assert report.dry_run_count == 2

    def test_dry_run_records_cooldown(self):
        # Use a long cooldown so is_allowed() returns False right after recording
        cooldown = CooldownTracker(cooldown_seconds=9999)
        executor = Executor(
            cooldown=cooldown,
            target_namespace="food-app",
            dry_run=True,
        )
        plan = ExecutionPlan(runbook_id="rb-dry-002", actions=[_scale_action()])

        with patch("app.executor.k8s.scale_deployment"):
            executor.run(plan)

        # After dry-run the cooldown entry should be recorded — action is now blocked
        assert not executor._cooldown.is_allowed("scale_deployment", "backend")


class TestExecutorLiveMode:
    def test_successful_scale(self):
        executor = _make_executor(dry_run=False)
        plan = ExecutionPlan(runbook_id="rb-live-001", actions=[_scale_action(replicas=5)])

        with patch("app.executor.k8s.scale_deployment") as mock_scale:
            report = executor.run(plan)

        mock_scale.assert_called_once_with(
            deployment="backend", replicas=5, namespace="food-app"
        )
        assert report.steps[0].status == StepStatus.SUCCESS
        assert report.success_count == 1

    def test_successful_restart(self):
        executor = _make_executor(dry_run=False)
        plan = ExecutionPlan(runbook_id="rb-live-002", actions=[_restart_action()])

        with patch("app.executor.k8s.restart_deployment") as mock_restart:
            report = executor.run(plan)

        mock_restart.assert_called_once_with(deployment="api", namespace="food-app")
        assert report.steps[0].status == StepStatus.SUCCESS

    def test_kubernetes_api_error_marks_step_failed(self):
        from kubernetes.client.exceptions import ApiException

        executor = _make_executor(dry_run=False)
        plan = ExecutionPlan(runbook_id="rb-live-003", actions=[_scale_action()])

        with patch("app.executor.k8s.scale_deployment", side_effect=ApiException(status=404, reason="Not Found")):
            report = executor.run(plan)

        assert report.steps[0].status == StepStatus.FAILED
        assert report.failure_count == 1

    def test_stop_on_failure_aborts_remaining_steps(self):
        from kubernetes.client.exceptions import ApiException

        executor = _make_executor(dry_run=False, stop_on_failure=True)
        plan = ExecutionPlan(
            runbook_id="rb-live-004",
            actions=[_scale_action(), _restart_action()],
        )

        with patch("app.executor.k8s.scale_deployment", side_effect=ApiException(status=500, reason="Error")), \
             patch("app.executor.k8s.restart_deployment") as mock_restart:
            report = executor.run(plan)

        # Should stop after first failure
        assert report.aborted_early is True
        assert len(report.steps) == 1
        mock_restart.assert_not_called()

    def test_continue_on_failure_executes_all_steps(self):
        from kubernetes.client.exceptions import ApiException

        executor = _make_executor(dry_run=False, stop_on_failure=False)
        plan = ExecutionPlan(
            runbook_id="rb-live-005",
            actions=[_scale_action(), _restart_action()],
        )

        with patch("app.executor.k8s.scale_deployment", side_effect=ApiException(status=500, reason="Error")), \
             patch("app.executor.k8s.restart_deployment"):
            report = executor.run(plan)

        assert report.aborted_early is False
        assert len(report.steps) == 2

    def test_empty_plan_returns_empty_report(self):
        executor = _make_executor()
        plan = ExecutionPlan(runbook_id="rb-live-006", actions=[])
        report = executor.run(plan)
        assert len(report.steps) == 0
        assert not report.aborted_early

    def test_namespace_from_action_overrides_default(self):
        executor = _make_executor(dry_run=False)
        action = ScaleDeploymentAction(
            action=ActionType.SCALE_DEPLOYMENT,
            deployment="db",
            replicas=1,
            namespace="staging",
        )
        plan = ExecutionPlan(runbook_id="rb-live-007", actions=[action])

        with patch("app.executor.k8s.scale_deployment") as mock_scale:
            executor.run(plan)

        mock_scale.assert_called_once_with(deployment="db", replicas=1, namespace="staging")

    def test_cooldown_recorded_after_success(self):
        # Use a long cooldown so is_allowed() returns False right after recording
        cooldown = CooldownTracker(cooldown_seconds=9999)
        executor = Executor(
            cooldown=cooldown,
            target_namespace="food-app",
            dry_run=False,
        )
        plan = ExecutionPlan(runbook_id="rb-live-008", actions=[_restart_action()])

        with patch("app.executor.k8s.restart_deployment"):
            executor.run(plan)

        # Cooldown must be recorded — action is now blocked
        assert not executor._cooldown.is_allowed("restart_deployment", "api")
