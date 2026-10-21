"""Existing development stages, independent of terminal input and rendering.

Approval callbacks return human intent only. This module binds and persists that
intent; workspace services still revalidate policy and evidence before writing.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ApprovalDecision, ApprovalRecord
from yipilot.domain.patch_proposal import PatchProposal
from yipilot.domain.remote_execution import TOOLS_IMAGE
from yipilot.domain.sandbox import SandboxRequest
from yipilot.models.selection import ModelSelectionService
from yipilot.persistence.approval_store import FileApprovalStore
from yipilot.persistence.artifact_store import ArtifactRecord
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.ownership import exclusive_execution, project_lock
from yipilot.persistence.task_store import RunRecord
from yipilot.workflow.code_service import CodeGenerateResult, CodeService
from yipilot.workflow.project_service import ProjectService
from yipilot.workflow.recovery_service import RecoveryService
from yipilot.workflow.repair_service import RepairResult, RepairService
from yipilot.workflow.sandbox_application import SandboxCliService, apply_policy
from yipilot.workflow.sandbox_verification import (
    REVISION,
    SandboxVerificationService,
)
from yipilot.workflow.specification_service import (
    SpecificationService,
    SpecificationState,
)
from yipilot.workspace.repo_service import RepoService
from yipilot.workspace.runner_client import RunnerClient
from yipilot.workspace.sandbox_output import (
    SandboxOutputService,
    output_approval_fingerprint,
)


@dataclass(frozen=True)
class ApprovalPrompt:
    task_id: str
    operation_id: str
    run_id: str
    action: Literal["sandbox_execution", "sandbox_output", "sandbox_verification"]
    fingerprint: str
    revision: str
    requests: tuple[SandboxRequest, ...] = ()
    patch: str | None = None
    patch_sha256: str | None = None
    apply_run_id: str | None = None


@dataclass(frozen=True)
class VerificationOutcome:
    handoff: ArtifactRecord | None = None
    error: DomainError | None = None
    repair: RepairResult | None = None
    repair_error: DomainError | None = None
    cancelled: bool = False


class ProjectApplication:
    """Composition and business entry for the currently implemented stages."""

    def __init__(self, project: Path, *, settings_dir: Path | None = None,
                 models: ModelSelectionService | None = None, runner: RunnerClient | None = None) -> None:
        self.models = models or ModelSelectionService(settings_dir)
        self.tasks = ProjectService(project)
        self.runner = runner or RunnerClient(settings_dir=settings_dir)
        self.sandbox = SandboxCliService(project, runner=self.runner)
        self.recovery = RecoveryService(self.sandbox.store, self.runner)
        self.repos = RepoService(project, self.sandbox.git)
        self.code = CodeService(project, model_selection_service=self.models,
                                store=self.sandbox.store, git=self.sandbox.git)
        self.verification = SandboxVerificationService(project,
            store=self.sandbox.store, git=self.sandbox.git, runner=self.runner)
        self.output = SandboxOutputService(self.sandbox.root, self.sandbox.git)
        self.repairs = RepairService(project, model_selection_service=self.models,
            sandbox=self.sandbox, verification=self.verification, output=self.output)
        self.specifications = SpecificationService(self.sandbox.store, self.models, self.sandbox.git)

    def recover(self, run_id: str) -> RunRecord:
        return self.recovery.recover(run_id)

    def start_specification(self, task_id: str, *, instruction: str = "",
                            prd_path: Path | None = None, alias: str | None = None,
                            context_files: Iterable[str] = (), request_id: str | None = None,
                            model_selector: str | None = None, max_revisions: int = 2) -> SpecificationState:
        return self.specifications.start(task_id, instruction=instruction, prd_path=prd_path,
            alias=alias, context_files=context_files, request_id=request_id,
            model_selector=model_selector, max_revisions=max_revisions)

    def specification_status(self, task_id: str, flow_id: str | None = None) -> SpecificationState:
        return self.specifications.status(task_id, flow_id)

    def resume_specification(self, task_id: str, flow_id: str | None = None) -> SpecificationState:
        return self.specifications.resume(task_id, flow_id)

    def clarify_specification(self, task_id: str, flow_id: str, answers: dict[str, str], *,
                              request_id: str, expected_input_sha256: str) -> SpecificationState:
        return self.specifications.clarify(task_id, flow_id, answers, request_id=request_id,
                                          expected_input_sha256=expected_input_sha256)

    @exclusive_execution
    def generate(self, task_id: str, alias: str, instruction: str, *,
                 request_id: str | None = None, model_selector: str | None = None,
                 context_files: Iterable[str] = ()) -> CodeGenerateResult:
        return self.code.generate(task_id, alias, instruction, request_id=request_id,
                                  model_selector=model_selector, context_files=context_files)

    def proposals(self, task_id: str) -> list[dict[str, object]]:
        return self.code.proposals(task_id)

    def apply(self, task_id: str, proposal_run_id: str, *, proposal_hash: str,
              confirm: Callable[[ApprovalPrompt], bool], request_id: str | None = None) -> str | None:
        selected = next((p for p in self.proposals(task_id)
                         if p["proposal_run_id"] == proposal_run_id), None)
        if selected is None:
            raise DomainError("PATCH_PROPOSAL_NOT_FOUND", "no valid proposal Run")
        if selected["proposal_hash"] != proposal_hash:
            raise DomainError("APPROVAL_STALE", "reviewed proposal changed")
        if "parent_diff" in selected:
            raise DomainError("SANDBOX_UNAVAILABLE", "旧 Repair Proposal 尚未迁移；请生成新的 Proposal")
        digest = TOOLS_IMAGE
        request = self.sandbox.prepare(
            task_id, str(selected["repository_alias"]), str(selected["base_sha"]),
            cast(PatchProposal, selected["proposal"]), digest,
            proposal_run_id=proposal_run_id, request_id=request_id,
            root_proposal_run_id=str(selected.get("root_proposal_run_id", proposal_run_id)))
        with project_lock(self.sandbox.root, "run-" + request.run_id):
            existing = self.sandbox.store.get_run(task_id, request.run_id)
            if existing.status.value == "succeeded":
                self.sandbox.accepted(task_id, request.run_id)
                return request.run_id
            if existing.status.value in {"failed", "cancelled"}:
                return None
            if existing.status.value != "pending":
                raise DomainError("EXTERNAL_RESULT_UNCERTAIN", "existing apply requires reconciliation")
            try:
                approval = self._approve(ApprovalPrompt(task_id, request.operation_id,
                    request.run_id, "sandbox_execution", request.fingerprint,
                    request.policy_revision, requests=(request,)), confirm)
                if approval is None:
                    return None
                result = self.sandbox.execute(request, approval)
                copies, _ = self.sandbox.load(task_id, request.run_id)
                output = self.output.inspect(copies, request, apply_policy())
                approval = self._approve(ApprovalPrompt(task_id, request.operation_id,
                    request.run_id, "sandbox_output",
                    output_approval_fingerprint(request, output.patch_sha256),
                    request.policy_revision, patch=output.patch.decode("utf-8"),
                    patch_sha256=output.patch_sha256), confirm)
                if approval is None:
                    return None
                self.sandbox.apply(request, result, output.patch_sha256, approval)
                return request.run_id
            finally:
                self.sandbox.cancel(task_id, request.run_id)

    def applied_runs(self, task_id: str) -> list[RunRecord]:
        return [r for r in self.sandbox.store.list_runs(task_id)
                if r.stage == "sandbox_apply" and r.status.value == "succeeded"]

    def verify(self, task_id: str, apply_run_id: str, *,
               confirm: Callable[[ApprovalPrompt], bool],
               model_selector: str | None = None, request_id: str | None = None) -> VerificationOutcome:
        prepared = None
        try:
            if not any(r.run_id == apply_run_id for r in self.applied_runs(task_id)):
                raise DomainError("APPLY_RUN_NOT_FOUND", "no successful sandbox apply Run")
            copies, request, evidence = self.sandbox.accepted(task_id, apply_run_id)
            prepared = self.verification.prepare(copies, request, apply_policy(), evidence, request_id=request_id)
            existing = self.sandbox.store.get_run(task_id, prepared.run_id)
            if existing.status.value == "succeeded":
                return VerificationOutcome(handoff=self.verification.handoff(copies, request, apply_policy(), evidence, prepared.run_id))
            if existing.status.value in {"failed", "cancelled"}:
                return VerificationOutcome(error=DomainError(existing.error_code or "VERIFICATION_FAILED", "previous verification result"), cancelled=existing.status.value == "cancelled")
            if existing.status.value != "pending":
                raise DomainError("EXTERNAL_RESULT_UNCERTAIN", "existing verification requires reconciliation")
            approval = self._approve(ApprovalPrompt(task_id, prepared.operation_id,
                prepared.run_id, "sandbox_verification", prepared.approval_fingerprint,
                REVISION, requests=prepared.requests, patch_sha256=evidence.patch_sha256,
                apply_run_id=apply_run_id), confirm)
            if approval is None:
                return VerificationOutcome(cancelled=True)
            self.verification.run(copies, request, apply_policy(), evidence,
                                  prepared.run_id, approval_id=approval)
            return VerificationOutcome(handoff=self.verification.handoff(
                copies, request, apply_policy(), evidence, prepared.run_id))
        except (DomainError, ValueError, OSError) as error:
            failure = _domain_error(error)
            if prepared is not None and self.sandbox.store.get_run(
                    task_id, prepared.run_id).status.value == "failed":
                try:
                    repair = self.repairs.repair(task_id, prepared.run_id,
                                                 model_selector=model_selector)
                    return VerificationOutcome(error=failure, repair=repair)
                except (DomainError, ValueError, OSError) as repair_error:
                    return VerificationOutcome(error=failure, repair_error=_domain_error(repair_error))
            return VerificationOutcome(error=failure)
        finally:
            if prepared is not None:
                for run in self.sandbox.store.list_runs(task_id):
                    if run.operation_id == prepared.operation_id:
                        self.sandbox.cancel(task_id, run.run_id)

    def _approve(self, prompt: ApprovalPrompt,
                 confirm: Callable[[ApprovalPrompt], bool]) -> str | None:
        approved = confirm(prompt)
        if type(approved) is not bool:
            raise DomainError("APPROVAL_CONFIRMATION_INVALID", "approval intent must be boolean")
        run = self.sandbox.store.get_run(prompt.task_id, prompt.run_id)
        expected = {"sandbox_execution": ("sandbox_apply", "pending"),
                    "sandbox_output": ("sandbox_apply", "running"),
                    "sandbox_verification": ("sandbox_verification", "pending")}
        if run.operation_id != prompt.operation_id or (run.stage, run.status.value) != expected[prompt.action]:
            raise DomainError("APPROVAL_STALE", "approval does not match the active stage")
        record = ApprovalRecord(approval_id=str(uuid4()), task_id=prompt.task_id,
            operation_id=prompt.operation_id, run_id=prompt.run_id,
            action_type=prompt.action, target_fingerprint=prompt.fingerprint,
            policy_revision=prompt.revision,
            decision=ApprovalDecision.APPROVED if approved else ApprovalDecision.DENIED,
            decided_at=datetime.now(UTC))
        with self.sandbox.store.db.transaction():
            FileApprovalStore(self.sandbox.store).save(record)
            JsonlEventJournal(self.sandbox.store.event_journal_path(run.run_id),
                task_id=run.task_id, run_id=run.run_id, stage=run.stage).append(
                    "stage.progress", {"message": "approval decided", "approval_id": record.approval_id,
                        "action_type": prompt.action, "decision": record.decision.value,
                        "target_fingerprint": prompt.fingerprint})
        return record.approval_id if approved else None


def _domain_error(error: DomainError | ValueError | OSError) -> DomainError:
    return error if isinstance(error, DomainError) else DomainError("SANDBOX_EVIDENCE_FAILED", str(error))
