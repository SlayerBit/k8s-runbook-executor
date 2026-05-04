"""
executor.py — Step-by-step execution of a validated ExecutionPlan.

Design principles:
  - Operates on already-validated actions only
  - Stops on the first fatal failure (default) or continues if stop_on_failure=False
  - Supports dry-run mode: logs what would happen without touching Kubernetes
  - Records cooldown timestamps after each successful execution
  - Returns a structured ExecutionReport for observability
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List

from kubernetes.client.exceptions import ApiException

from app import kubernetes_client as k8s
from app.config import settings
from app.logging_config import get_logger
from app.models import (
    ActionType,
    DeleteNetworkPolicyAction,
    DeletePodAction,
    ExecutionAction,
    ExecutionPlan,
    RollbackDeploymentAction,
    ScaleDeploymentAction,
    UpdateResourcesAction,
)
from app.utils import CooldownTracker

logger = get_logger(__name__)


class StepStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"
    DRY_RUN = "dry_run"


@dataclass
class StepResult:
    action: str
    deployment: str
    namespace: str
    status: StepStatus
    detail: str = ""
    duration_ms: float = 0.0


@dataclass
class ExecutionReport:
    runbook_id: str
    steps: List[StepResult] = field(default_factory=list)
    aborted_early: bool = False

    @property
    def success_count(self) -> int:
        return sum(1 for s in self.steps if s.status == StepStatus.SUCCESS)

    @property
    def failure_count(self) -> int:
        return sum(1 for s in self.steps if s.status == StepStatus.FAILED)

    @property
    def dry_run_count(self) -> int:
        return sum(1 for s in self.steps if s.status == StepStatus.DRY_RUN)

    def summary(self) -> str:
        return (
            f"runbook={self.runbook_id} "
            f"steps={len(self.steps)} "
            f"ok={self.success_count} "
            f"failed={self.failure_count} "
            f"dry_run={self.dry_run_count} "
            f"aborted={self.aborted_early}"
        )


class Executor:
    """
    Executes an approved ExecutionPlan against the live Kubernetes API.

    Parameters
    ----------
    cooldown        : CooldownTracker shared with the validator
    target_namespace: Fallback namespace when an action omits one
    dry_run         : If True, log intent but do not call Kubernetes
    stop_on_failure : If True (default), abort remaining steps on first error
    """

    def __init__(
        self,
        cooldown: CooldownTracker,
        target_namespace: str,
        dry_run: bool = False,
        stop_on_failure: bool = True,
    ) -> None:
        self._cooldown = cooldown
        self._namespace = target_namespace
        self._dry_run = dry_run or not settings.ENABLE_EXECUTION
        self._stop_on_failure = stop_on_failure

    def run(self, plan: ExecutionPlan) -> ExecutionReport:
        """Execute every action in the plan and return an ExecutionReport."""
        report = ExecutionReport(runbook_id=plan.runbook_id)

        if not plan.actions:
            logger.info("Execution plan is empty — nothing to do", extra={"runbook_id": plan.runbook_id})
            return report

        logger.info(
            "Starting execution",
            extra={
                "runbook_id": plan.runbook_id,
                "steps": len(plan.actions),
                "dry_run": self._dry_run,
            },
        )

        for idx, action in enumerate(plan.actions, start=1):
            ns = action.namespace or self._namespace
            logger.info(
                "Executing step",
                extra={
                    "runbook_id": plan.runbook_id,
                    "step": f"{idx}/{len(plan.actions)}",
                    "action": action.action.value,
                    "deployment": action.deployment,
                    "namespace": ns,
                    "dry_run": self._dry_run,
                },
            )

            result = self._execute_action(plan.runbook_id, action, ns)
            report.steps.append(result)

            if result.status == StepStatus.FAILED and self._stop_on_failure:
                logger.error(
                    "Aborting remaining steps due to failure",
                    extra={"runbook_id": plan.runbook_id, "failed_step": idx},
                )
                report.aborted_early = True
                break

        logger.info("Execution complete", extra={"summary": report.summary()})
        return report

    def _execute_action(
        self, runbook_id: str, action: ExecutionAction, namespace: str
    ) -> StepResult:
        t0 = time.monotonic()
        action_name = action.action.value

        if self._dry_run:
            detail = self._dry_run_description(action, namespace)
            logger.info(
                "[DRY-RUN] Would execute",
                extra={"runbook_id": runbook_id, "detail": detail},
            )
            # Still record the cooldown so repeated dry-runs behave consistently
            self._cooldown.record(action_name, action.deployment)
            return StepResult(
                action=action_name,
                deployment=action.deployment,
                namespace=namespace,
                status=StepStatus.DRY_RUN,
                detail=detail,
                duration_ms=_elapsed_ms(t0),
            )

        try:
            self._dispatch(action, namespace)
            self._cooldown.record(action_name, action.deployment)
            return StepResult(
                action=action_name,
                deployment=action.deployment,
                namespace=namespace,
                status=StepStatus.SUCCESS,
                detail="OK",
                duration_ms=_elapsed_ms(t0),
            )
        except ApiException as exc:
            detail = f"ApiException status={exc.status} reason={exc.reason}"
            logger.error(
                "Kubernetes API error during execution",
                extra={
                    "runbook_id": runbook_id,
                    "action": action_name,
                    "deployment": action.deployment,
                    "namespace": namespace,
                    "status": exc.status,
                    "reason": exc.reason,
                },
            )
            return StepResult(
                action=action_name,
                deployment=action.deployment,
                namespace=namespace,
                status=StepStatus.FAILED,
                detail=detail,
                duration_ms=_elapsed_ms(t0),
            )
        except Exception as exc:
            detail = f"Unexpected error: {exc}"
            logger.exception(
                "Unexpected error during execution",
                extra={
                    "runbook_id": runbook_id,
                    "action": action_name,
                    "deployment": action.deployment,
                },
            )
            return StepResult(
                action=action_name,
                deployment=action.deployment,
                namespace=namespace,
                status=StepStatus.FAILED,
                detail=detail,
                duration_ms=_elapsed_ms(t0),
            )

    @staticmethod
    def _dispatch(action: ExecutionAction, namespace: str) -> None:
        """Call the appropriate Kubernetes helper based on action type."""
        if action.action == ActionType.SCALE_DEPLOYMENT:
            assert isinstance(action, ScaleDeploymentAction)
            k8s.scale_deployment(
                deployment=action.deployment,
                replicas=action.replicas,
                namespace=namespace,
            )
        elif action.action == ActionType.RESTART_DEPLOYMENT:
            k8s.restart_deployment(
                deployment=action.deployment,
                namespace=namespace,
            )
        elif action.action == ActionType.ROLLBACK_DEPLOYMENT:
            assert isinstance(action, RollbackDeploymentAction)
            k8s.rollback_deployment(
                deployment=action.deployment,
                namespace=namespace,
            )
        elif action.action == ActionType.DELETE_POD:
            assert isinstance(action, DeletePodAction)
            k8s.delete_pod(
                pod=action.pod,
                namespace=namespace,
            )
        elif action.action == ActionType.UPDATE_RESOURCES:
            assert isinstance(action, UpdateResourcesAction)
            k8s.update_deployment_resources(
                deployment=action.deployment,
                namespace=namespace,
                cpu=action.cpu,
                memory=action.memory,
            )
        elif action.action == ActionType.DELETE_NETWORK_POLICY:
            assert isinstance(action, DeleteNetworkPolicyAction)
            k8s.delete_network_policy(
                name=action.name,
                namespace=namespace,
            )
        else:
            raise ValueError(f"Unhandled action type: {action.action}")

    @staticmethod
    def _dry_run_description(action: ExecutionAction, namespace: str) -> str:
        if action.action == ActionType.SCALE_DEPLOYMENT:
            assert isinstance(action, ScaleDeploymentAction)
            return (
                f"scale deployment/{action.deployment} "
                f"to {action.replicas} replicas in {namespace}"
            )
        if action.action == ActionType.RESTART_DEPLOYMENT:
            return f"rolling restart deployment/{action.deployment} in {namespace}"
        if action.action == ActionType.ROLLBACK_DEPLOYMENT:
            assert isinstance(action, RollbackDeploymentAction)
            return f"rollback deployment/{action.deployment} to previous revision in {namespace}"
        if action.action == ActionType.DELETE_POD:
            assert isinstance(action, DeletePodAction)
            return f"delete pod/{action.pod} in {namespace}"
        if action.action == ActionType.UPDATE_RESOURCES:
            assert isinstance(action, UpdateResourcesAction)
            return (
                f"update resources for deployment/{action.deployment} "
                f"→ cpu={action.cpu} memory={action.memory} in {namespace}"
            )
        if action.action == ActionType.DELETE_NETWORK_POLICY:
            assert isinstance(action, DeleteNetworkPolicyAction)
            return f"delete networkpolicy/{action.name} in {namespace}"
        return f"unknown action {action.action}"


def _elapsed_ms(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000, 2)
