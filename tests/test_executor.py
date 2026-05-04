"""
test_executor.py — Unit tests for app.executor

All Kubernetes calls are mocked — no real cluster required.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.executor import Executor, StepStatus
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


# ── Factories ─────────────────────────────────────────────────────────────────

def _make_executor(dry_run: bool = False, stop_on_failure: bool = True) -> Executor:
    cooldown = CooldownTracker(cooldown_seconds=0)  # no cooldown in tests
    return Executor(
        cooldown=cooldown,
        target_namespace="food-app",
        dry_run=dry_run,
        stop_on_failure=stop_on_failure,
    )


def _scale_action(deployment: str = "backend", replicas: int = 3) -> ScaleDeploymentAction:
    return ScaleDeploymentAction(action=ActionType.SCALE_DEPLOYMENT,
                                 deployment=deployment, replicas=replicas)


def _restart_action(deployment: str = "api") -> RestartDeploymentAction:
    return RestartDeploymentAction(action=ActionType.RESTART_DEPLOYMENT, deployment=deployment)


def _rollback_action(deployment: str = "backend") -> RollbackDeploymentAction:
    return RollbackDeploymentAction(action=ActionType.ROLLBACK_DEPLOYMENT, deployment=deployment)


def _delete_pod_action(pod: str = "backend-abc") -> DeletePodAction:
    return DeletePodAction(action=ActionType.DELETE_POD, pod=pod)


def _update_resources_action(
    deployment: str = "backend", cpu: str = "500m", memory: str = "512Mi"
) -> UpdateResourcesAction:
    return UpdateResourcesAction(
        action=ActionType.UPDATE_RESOURCES, deployment=deployment, cpu=cpu, memory=memory
    )


def _delete_netpol_action(name: str = "deny-all") -> DeleteNetworkPolicyAction:
    return DeleteNetworkPolicyAction(action=ActionType.DELETE_NETWORK_POLICY, name=name)


# ── Dry-run tests ─────────────────────────────────────────────────────────────

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

    def test_dry_run_new_actions_not_called(self):
        executor = _make_executor(dry_run=True)
        plan = ExecutionPlan(
            runbook_id="rb-dry-002",
            actions=[
                _rollback_action(),
                _delete_pod_action(),
                _update_resources_action(),
                _delete_netpol_action(),
            ],
        )
        with patch("app.executor.k8s.rollback_deployment") as m1, \
             patch("app.executor.k8s.delete_pod") as m2, \
             patch("app.executor.k8s.update_deployment_resources") as m3, \
             patch("app.executor.k8s.delete_network_policy") as m4:
            report = executor.run(plan)

        m1.assert_not_called()
        m2.assert_not_called()
        m3.assert_not_called()
        m4.assert_not_called()
        assert report.dry_run_count == 4

    def test_dry_run_records_cooldown(self):
        cooldown = CooldownTracker(cooldown_seconds=9999)
        executor = Executor(cooldown=cooldown, target_namespace="food-app", dry_run=True)
        plan = ExecutionPlan(runbook_id="rb-dry-003", actions=[_scale_action()])
        with patch("app.executor.k8s.scale_deployment"):
            executor.run(plan)
        assert not executor._cooldown.is_allowed("scale_deployment", "backend")


# ── Live-mode — existing actions ──────────────────────────────────────────────

class TestExecutorLiveExisting:
    def test_successful_scale(self):
        executor = _make_executor(dry_run=False)
        plan = ExecutionPlan(runbook_id="rb-live-001", actions=[_scale_action(replicas=5)])
        with patch("app.executor.k8s.scale_deployment") as mock_scale:
            report = executor.run(plan)
        mock_scale.assert_called_once_with(deployment="backend", replicas=5, namespace="food-app")
        assert report.steps[0].status == StepStatus.SUCCESS

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
        with patch("app.executor.k8s.scale_deployment",
                   side_effect=ApiException(status=404, reason="Not Found")):
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
        with patch("app.executor.k8s.scale_deployment",
                   side_effect=ApiException(status=500, reason="Error")), \
             patch("app.executor.k8s.restart_deployment") as mock_restart:
            report = executor.run(plan)
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
        with patch("app.executor.k8s.scale_deployment",
                   side_effect=ApiException(status=500, reason="Error")), \
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
            action=ActionType.SCALE_DEPLOYMENT, deployment="db", replicas=1, namespace="staging"
        )
        plan = ExecutionPlan(runbook_id="rb-live-007", actions=[action])
        with patch("app.executor.k8s.scale_deployment") as mock_scale:
            executor.run(plan)
        mock_scale.assert_called_once_with(deployment="db", replicas=1, namespace="staging")

    def test_cooldown_recorded_after_success(self):
        cooldown = CooldownTracker(cooldown_seconds=9999)
        executor = Executor(cooldown=cooldown, target_namespace="food-app", dry_run=False)
        plan = ExecutionPlan(runbook_id="rb-live-008", actions=[_restart_action()])
        with patch("app.executor.k8s.restart_deployment"):
            executor.run(plan)
        assert not executor._cooldown.is_allowed("restart_deployment", "api")


# ── Live-mode — NEW actions ───────────────────────────────────────────────────

class TestExecutorLiveNewActions:
    def test_rollback_deployment_called(self):
        executor = _make_executor(dry_run=False)
        plan = ExecutionPlan(runbook_id="rb-new-001", actions=[_rollback_action()])
        with patch("app.executor.k8s.rollback_deployment") as mock_rb:
            report = executor.run(plan)
        mock_rb.assert_called_once_with(deployment="backend", namespace="food-app")
        assert report.steps[0].status == StepStatus.SUCCESS

    def test_delete_pod_called(self):
        executor = _make_executor(dry_run=False)
        plan = ExecutionPlan(runbook_id="rb-new-002", actions=[_delete_pod_action("backend-abc")])
        with patch("app.executor.k8s.delete_pod") as mock_dp:
            report = executor.run(plan)
        mock_dp.assert_called_once_with(pod="backend-abc", namespace="food-app")
        assert report.steps[0].status == StepStatus.SUCCESS

    def test_update_resources_called(self):
        executor = _make_executor(dry_run=False)
        plan = ExecutionPlan(
            runbook_id="rb-new-003",
            actions=[_update_resources_action(cpu="500m", memory="512Mi")],
        )
        with patch("app.executor.k8s.update_deployment_resources") as mock_ur:
            report = executor.run(plan)
        mock_ur.assert_called_once_with(
            deployment="backend", namespace="food-app", cpu="500m", memory="512Mi"
        )
        assert report.steps[0].status == StepStatus.SUCCESS

    def test_delete_network_policy_called(self):
        executor = _make_executor(dry_run=False)
        plan = ExecutionPlan(runbook_id="rb-new-004", actions=[_delete_netpol_action("deny-all")])
        with patch("app.executor.k8s.delete_network_policy") as mock_dnp:
            report = executor.run(plan)
        mock_dnp.assert_called_once_with(name="deny-all", namespace="food-app")
        assert report.steps[0].status == StepStatus.SUCCESS

    def test_rollback_api_error_marks_failed(self):
        from kubernetes.client.exceptions import ApiException
        executor = _make_executor(dry_run=False)
        plan = ExecutionPlan(runbook_id="rb-new-005", actions=[_rollback_action()])
        with patch("app.executor.k8s.rollback_deployment",
                   side_effect=ApiException(status=404, reason="Not Found")):
            report = executor.run(plan)
        assert report.steps[0].status == StepStatus.FAILED

    def test_delete_pod_step_result_shows_pod_name(self):
        """StepResult.deployment should contain the pod name (via proxy property)."""
        executor = _make_executor(dry_run=False)
        plan = ExecutionPlan(runbook_id="rb-new-006", actions=[_delete_pod_action("cache-xyz")])
        with patch("app.executor.k8s.delete_pod"):
            report = executor.run(plan)
        assert report.steps[0].deployment == "cache-xyz"

    def test_multi_step_new_actions_all_succeed(self):
        executor = _make_executor(dry_run=False)
        plan = ExecutionPlan(
            runbook_id="rb-new-007",
            actions=[
                _rollback_action("frontend"),
                _delete_pod_action("cache-abc"),
                _update_resources_action("backend", "1000m", "1Gi"),
                _delete_netpol_action("block-ingress"),
            ],
        )
        with patch("app.executor.k8s.rollback_deployment"), \
             patch("app.executor.k8s.delete_pod"), \
             patch("app.executor.k8s.update_deployment_resources"), \
             patch("app.executor.k8s.delete_network_policy"):
            report = executor.run(plan)

        assert report.success_count == 4
        assert report.failure_count == 0
        assert not report.aborted_early
