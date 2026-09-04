"""Run the fixed Change Handoff verification policy for an applied proposal."""

import hashlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

from yipilot.application.approval import ApprovalGate, approval_action_type
from yipilot.application.tool_registry import ToolRegistry
from yipilot.application.workspace_service import PreparedWorkspace, WorkspaceService
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    VERIFICATION_POLICY_REVISION,
    VERIFICATION_PYTEST_TOOL_ID,
    ApprovalDecision,
    ApprovalRecord,
    ArgvRule,
    ArtifactRef,
    ExecutionPolicy,
    ToolKind,
    ToolPolicy,
    ToolRequest,
    ToolResult,
    ToolRunStatus,
)
from yipilot.harness.git_workspace import GitWorkspaceHarness
from yipilot.harness.models import HarnessReport, HarnessStatus
from yipilot.harness.report_store import HarnessReportStore
from yipilot.infrastructure.approval_store import FileApprovalStore
from yipilot.infrastructure.artifact_store import ArtifactRecord, FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.executors import LocalCommandRunner
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.task_store import FileTaskStore, RunRecord, RunStatus

_CONTEXT_PATH = "artifacts/verification-context.json"
_REQUESTS_PATH = "artifacts/verification-requests.json"
_HANDOFF_PATH = "artifacts/change-handoff.json"
_HARNESS_REPORT_NAME = "change-verification-harness-report.json"
_MAX_OUTPUT = 128 * 1024
_TIMEOUT = 600.0
_ENV_NAMES = ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT")
_TOOLS = (
    (VERIFICATION_PYTEST_TOOL_ID, (sys.executable, "-m", "pytest")),
    ("verification-ruff", ("ruff", "check", "src", "tests")),
    ("verification-mypy", ("mypy", "src")),
)


@dataclass(frozen=True)
class VerificationCandidate:
    apply_run_id: str
    proposal_run_id: str
    repository_alias: str
    base_sha: str
    diff_hash: str
    title: str


@dataclass(frozen=True)
class VerificationPreparation:
    run_id: str
    apply_run_id: str
    approvals: dict[str, tuple[str, str]]


@dataclass(frozen=True)
class VerificationResult:
    task_id: str
    operation_id: str
    run_id: str
    status: str
    error_code: str | None
    tool_results: tuple[ToolResult, ...] = ()
    handoff_artifact_ref: dict[str, str] | None = None


class VerificationService:
    """Verify exactly one successful apply Run without model or Git delivery authority."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = Path(project_root).resolve(strict=True)
        self._store = FileTaskStore(self._project_root)
        self._git = GitRunner()

    def candidates(self, task_id: str) -> list[VerificationCandidate]:
        task = self._store.get_task(task_id)
        values: list[VerificationCandidate] = []
        for run in self._store.list_runs(task_id):
            if run.stage == "code_apply" and run.status is RunStatus.SUCCEEDED:
                try:
                    details = self._apply_details(task_id, run.run_id)
                except DomainError:
                    continue
                values.append(
                    VerificationCandidate(
                        apply_run_id=run.run_id,
                        proposal_run_id=details["proposal_run_id"],
                        repository_alias=details["repository_alias"],
                        base_sha=details["base_sha"],
                        diff_hash=details["diff_hash"],
                        title=task.title or "未命名任务",
                    )
                )
        return values

    def prepare(
        self, task_id: str, apply_run_id: str, *, request_id: str | None = None
    ) -> VerificationPreparation:
        """Create the verification Run only after the caller accepted its preview."""
        details = self._apply_details(task_id, apply_run_id)
        operation = self._store.create_operation(
            task_id,
            "change_verification",
            f"verify:{task_id}:{apply_run_id}:{request_id or uuid4()}",
            {"apply_run_id": apply_run_id, "diff_hash": details["diff_hash"]},
        )
        run, created = self._store.get_or_create_run(
            task_id, operation.operation_id, "change_verification"
        )
        if not created:
            return self._preparation(task_id, run.run_id, apply_run_id)
        artifacts = FileArtifactStore(self._store)
        journal = _journal(self._store, run)
        try:
            workspace = WorkspaceService(self._project_root, self._git).prepare(
                task_id,
                details["repository_alias"],
                run.run_id,
                base_sha=details["base_sha"],
            )
            self._apply_exact_diff(workspace, details["diff"])
            context = {
                "schema_version": 1,
                "task_id": task_id,
                "operation_id": run.operation_id,
                "verification_run_id": run.run_id,
                "apply_run_id": apply_run_id,
                "proposal_run_id": details["proposal_run_id"],
                "proposal_hash": details["proposal_hash"],
                "repository_alias": details["repository_alias"],
                "base_sha": details["base_sha"],
                "diff_hash": details["diff_hash"],
                "policy_revision": VERIFICATION_POLICY_REVISION,
            }
            context_ref = artifacts.write_bytes(
                task_id,
                run.run_id,
                producer="change_verification",
                relative_path=_CONTEXT_PATH,
                content=_json(context),
                input_fingerprint=_hash_json(context),
                operation_id=run.operation_id,
            )
            requests = self._requests(run, workspace, details, _ref(context_ref))
            approvals = self._save_approvals(run, requests)
            artifacts.write_bytes(
                task_id,
                run.run_id,
                producer="change_verification",
                relative_path=_REQUESTS_PATH,
                content=_json(
                    {
                        "schema_version": 1,
                        "requests": [
                            request.model_dump(mode="json") for request in requests
                        ],
                    }
                ),
                input_fingerprint=_hash_json(context),
                operation_id=run.operation_id,
            )
            journal.append(
                "approval.required",
                {"tool_ids": [request.tool_id for request in requests]},
            )
            return VerificationPreparation(run.run_id, apply_run_id, approvals)
        except DomainError as error:
            self._fail(run, journal, error.code)
            raise

    def run(
        self,
        task_id: str,
        run_id: str,
        *,
        approvals: dict[str, tuple[str, str]],
        cancel_event: Event | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> VerificationResult:
        run = self._store.get_run(task_id, run_id)
        if run.stage != "change_verification":
            raise DomainError(
                "VERIFICATION_RUN_INVALID", "run is not a verification Run"
            )
        journal = _journal(self._store, run)
        results: list[ToolResult] = []
        try:
            details, workspace, requests = self._prepared_inputs(task_id, run)
            policy = _policy()
            gate = ApprovalGate(FileApprovalStore(self._store))
            # All approvals are rechecked before the first process is constructed.
            for request in requests:
                try:
                    approval_id, token = approvals[request.tool_id]
                except KeyError as error:
                    raise DomainError(
                        "APPROVAL_REQUIRED", "verification approval is missing"
                    ) from error
                gate.require_approved(
                    request,
                    policy.validate_request(request),
                    LocalCommandRunner.trust_level,
                    approval_id=approval_id,
                    confirm_token=token,
                    policy_revision=policy.revision,
                )
            self._store.set_run_status(run.run_id, RunStatus.RUNNING)
            journal.append("run.started", {"apply_run_id": details["apply_run_id"]})
            before = GitWorkspaceHarness(workspace).run()
            if before.status is not HarnessStatus.PASSED:
                raise DomainError(
                    "VERIFICATION_GIT_EVIDENCE_INVALID",
                    "verification workspace evidence is invalid",
                )
            registry = ToolRegistry(
                policy,
                LocalCommandRunner(FileArtifactStore(self._store), journal),
                gate,
            )
            environment = {
                name: os.environ[name] for name in _ENV_NAMES if name in os.environ
            }
            environment["PATH"] = (
                str(Path(sys.executable).parent)
                + os.pathsep
                + environment.get("PATH", "")
            )
            for request in requests:
                if on_progress is not None:
                    on_progress(request.tool_id)
                journal.append("stage.progress", {"tool_id": request.tool_id})
                approval_id, token = approvals[request.tool_id]
                result = registry.execute(
                    self._project_root,
                    request,
                    environment=environment,
                    approval_id=approval_id,
                    confirm_token=token,
                    cancel_event=cancel_event,
                )
                results.append(result)
                if result.status is not ToolRunStatus.SUCCEEDED:
                    self._skipped(
                        FileArtifactStore(self._store), run, requests, request.tool_id
                    )
                    raise DomainError(
                        "VERIFICATION_TOOL_FAILED", f"{request.tool_id} did not succeed"
                    )
            after = GitWorkspaceHarness(workspace).verify()
            report_ref = self._save_harness(run, after, details["diff_hash"])
            if after.status is not HarnessStatus.PASSED:
                raise DomainError(
                    "VERIFICATION_GIT_EVIDENCE_STALE",
                    "verification changed the applied diff",
                )
            handoff = self._handoff(run, details, results, report_ref)
            self._store.set_run_status(run.run_id, RunStatus.SUCCEEDED)
            journal.append(
                "harness.completed",
                {"report_artifact_id": report_ref.artifact_id, "status": "passed"},
            )
            journal.append(
                "run.succeeded", {"handoff_artifact_id": handoff.artifact_id}
            )
            return _result(run, results, _value(handoff))
        except DomainError as error:
            self._fail(run, journal, error.code)
            return _result(self._store.get_run(task_id, run_id), results, None)

    def latest_status(self, task_id: str) -> str | None:
        runs = [
            run
            for run in self._store.list_runs(task_id)
            if run.stage == "change_verification"
        ]
        if not runs:
            return None
        run = runs[-1]
        handoff = any(
            record.relative_path == _HANDOFF_PATH
            for record in FileArtifactStore(self._store).list(task_id, run.run_id)
        )
        return f"最近 verification Run：{run.status.value} · {run.run_id} · Change Handoff：{'已生成' if handoff else '未生成'}"

    def _preparation(
        self, task_id: str, run_id: str, apply_run_id: str
    ) -> VerificationPreparation:
        requests = self._load_requests(task_id, run_id)
        approvals = FileApprovalStore(self._store).list(task_id, run_id)
        return VerificationPreparation(
            run_id,
            apply_run_id,
            {
                request.tool_id: next(
                    (item.approval_id, request.fingerprint)
                    for item in approvals
                    if item.target_fingerprint == request.fingerprint
                )
                for request in requests
            },
        )

    def _prepared_inputs(
        self, task_id: str, run: RunRecord
    ) -> tuple[dict[str, str], PreparedWorkspace, tuple[ToolRequest, ...]]:
        context = self._context(task_id, run.run_id)
        details = self._apply_details(task_id, context["apply_run_id"])
        if any(
            context[key] != details[key]
            for key in (
                "proposal_run_id",
                "proposal_hash",
                "repository_alias",
                "base_sha",
                "diff_hash",
            )
        ):
            raise DomainError(
                "VERIFICATION_EVIDENCE_DRIFT",
                "apply evidence no longer matches verification target",
            )
        workspace = WorkspaceService(self._project_root, self._git).prepare(
            task_id,
            details["repository_alias"],
            run.run_id,
            base_sha=details["base_sha"],
        )
        if workspace.base_sha != details["base_sha"]:
            raise DomainError(
                "VERIFICATION_BASE_SHA_DRIFT",
                "verification workspace base SHA drifted",
            )
        self._check_workspace(workspace, details["diff"])
        return details, workspace, self._load_requests(task_id, run.run_id)

    def _apply_details(self, task_id: str, apply_run_id: str) -> dict[str, str]:
        run = self._store.get_run(task_id, apply_run_id)
        if run.stage != "code_apply" or run.status is not RunStatus.SUCCEEDED:
            raise DomainError("APPLY_RUN_NOT_FOUND", "apply Run is not successful")
        artifacts = FileArtifactStore(self._store)
        records = {
            record.relative_path: record
            for record in artifacts.list(task_id, apply_run_id)
        }
        required = (
            "artifacts/apply-evidence.json",
            "artifacts/applied.diff",
            "artifacts/approved-patch-proposal.diff",
            "artifacts/approved-patch-proposal.json",
        )
        if any(path not in records for path in required):
            raise DomainError("APPLY_EVIDENCE_MISSING", "apply evidence is incomplete")
        for path in required:
            artifacts.verify(task_id, apply_run_id, records[path].artifact_id)
        try:
            evidence = json.loads(
                _artifact_path(
                    self._store, apply_run_id, records["artifacts/apply-evidence.json"]
                ).read_text(encoding="utf-8")
            )
            proposal_hash = str(evidence["proposal_hash"])
            proposal_run_id = str(evidence["proposal_run_id"])
            base_sha = str(evidence["base_sha"])
            alias = self._apply_alias(task_id, run)
            if (
                records["artifacts/approved-patch-proposal.json"].sha256
                != proposal_hash
            ):
                raise ValueError
            proposal = _artifact_path(
                self._store,
                apply_run_id,
                records["artifacts/approved-patch-proposal.diff"],
            ).read_text(encoding="utf-8")
            applied = _artifact_path(
                self._store, apply_run_id, records["artifacts/applied.diff"]
            ).read_text(encoding="utf-8")
        except (OSError, TypeError, ValueError, KeyError) as error:
            raise DomainError(
                "APPLY_EVIDENCE_INVALID", "apply evidence is invalid"
            ) from error
        parent_diff_hash = evidence.get("parent_diff_hash")
        if parent_diff_hash is None and _canonical_diff(proposal) != _canonical_diff(applied):
            raise DomainError(
                "APPLY_DIFF_MISMATCH", "applied diff does not match approved proposal"
            )
        if parent_diff_hash is not None and (not isinstance(parent_diff_hash, str) or len(parent_diff_hash) != 64):
            raise DomainError("APPLY_EVIDENCE_INVALID", "repair parent diff evidence is invalid")
        workspace = WorkspaceService(self._project_root, self._git).prepare(
            task_id, alias, apply_run_id
        )
        if workspace.base_sha != base_sha:
            raise DomainError(
                "APPLY_BASE_SHA_DRIFT", "apply workspace base SHA drifted"
            )
        self._check_workspace(workspace, applied)
        expected_paths = evidence.get("changed_paths")
        paths = _git(
            self._git, ["git", "diff", "--name-only", "--"], workspace.path
        ).splitlines()
        if not isinstance(expected_paths, list) or paths != expected_paths:
            raise DomainError(
                "APPLY_EVIDENCE_DRIFT", "apply changed-path evidence drifted"
            )
        result = {
            "apply_run_id": apply_run_id,
            "proposal_run_id": proposal_run_id,
            "proposal_hash": proposal_hash,
            "repository_alias": alias,
            "base_sha": base_sha,
            "diff_hash": _hash(_canonical_diff(applied).encode()),
            "diff": applied,
        }
        if parent_diff_hash is not None:
            root = evidence.get("root_proposal_run_id")
            if not isinstance(root, str) or not root:
                raise DomainError("APPLY_EVIDENCE_INVALID", "repair root proposal evidence is invalid")
            result["root_proposal_run_id"] = root
        return result

    def _apply_alias(self, task_id: str, run: RunRecord) -> str:
        try:
            payload = json.loads(
                self._store.get_operation(task_id, run.operation_id).request_json
            )["payload"]
            return str(payload["repository_alias"])
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError(
                "APPLY_EVIDENCE_INVALID", "apply repository evidence is invalid"
            ) from error

    def _apply_exact_diff(self, workspace: PreparedWorkspace, diff: str) -> None:
        patch = workspace.path / ".yipilot-verified.diff"
        try:
            patch.write_text(diff, encoding="utf-8", newline="\n")
            self._git.run(
                ["git", "apply", "--check", "--whitespace=nowarn", "--", str(patch)],
                cwd=workspace.path,
            )
            self._git.run(
                ["git", "apply", "--whitespace=nowarn", "--", str(patch)],
                cwd=workspace.path,
            )
        finally:
            if patch.exists():
                patch.unlink()
        self._check_workspace(workspace, diff)

    def _check_workspace(self, workspace: PreparedWorkspace, diff: str) -> None:
        if (
            _git(self._git, ["git", "rev-parse", "HEAD"], workspace.path).strip()
            != workspace.base_sha
        ):
            raise DomainError(
                "VERIFICATION_BASE_SHA_DRIFT",
                "workspace HEAD no longer matches apply base",
            )
        actual = _git(
            self._git,
            ["git", "diff", "--binary", "--no-ext-diff", "--no-textconv", "--"],
            workspace.path,
        )
        if _canonical_diff(actual) != _canonical_diff(diff):
            raise DomainError(
                "VERIFICATION_DIFF_MISMATCH",
                "workspace diff differs from applied proposal",
            )

    def _requests(
        self,
        run: RunRecord,
        workspace: PreparedWorkspace,
        details: dict[str, str],
        context: ArtifactRef,
    ) -> tuple[ToolRequest, ...]:
        env = tuple(name for name in _ENV_NAMES if name in os.environ)
        requests: list[ToolRequest] = []
        for tool_id, argv in _TOOLS:
            binding = _hash_json(
                {
                    "task_id": run.task_id,
                    "operation_id": run.operation_id,
                    "run_id": run.run_id,
                    "apply_run_id": details["apply_run_id"],
                    "proposal_run_id": details["proposal_run_id"],
                    "repository_alias": details["repository_alias"],
                    "base_sha": details["base_sha"],
                    "diff_hash": details["diff_hash"],
                    "policy_revision": VERIFICATION_POLICY_REVISION,
                    "tool_id": tool_id,
                    "argv": argv,
                }
            )
            requests.append(
                ToolRequest(
                    task_id=run.task_id,
                    operation_id=run.operation_id,
                    run_id=run.run_id,
                    workspace_relative_path=workspace.path.relative_to(
                        self._project_root
                    ).as_posix(),
                    tool_id=tool_id,
                    argv=argv,
                    timeout_seconds=_TIMEOUT,
                    output_budget_bytes=_MAX_OUTPUT,
                    env_names=env,
                    input_artifact_refs=(context,),
                    input_fingerprint=binding,
                )
            )
        return tuple(requests)

    def _save_approvals(
        self, run: RunRecord, requests: tuple[ToolRequest, ...]
    ) -> dict[str, tuple[str, str]]:
        store = FileApprovalStore(self._store)
        policy = _policy()
        saved: dict[str, tuple[str, str]] = {}
        for request in requests:
            approval = ApprovalRecord(
                approval_id=str(uuid4()),
                task_id=run.task_id,
                operation_id=run.operation_id,
                run_id=run.run_id,
                action_type=approval_action_type(
                    policy.validate_request(request), LocalCommandRunner.trust_level
                ),
                target_fingerprint=request.fingerprint,
                policy_revision=VERIFICATION_POLICY_REVISION,
                decision=ApprovalDecision.APPROVED,
                decided_at=datetime.now(UTC),
            )
            store.save(approval)
            saved[request.tool_id] = (approval.approval_id, request.fingerprint)
        return saved

    def _load_requests(self, task_id: str, run_id: str) -> tuple[ToolRequest, ...]:
        artifacts = FileArtifactStore(self._store)
        record = next(
            (
                item
                for item in artifacts.list(task_id, run_id)
                if item.relative_path == _REQUESTS_PATH
            ),
            None,
        )
        if record is None:
            raise DomainError(
                "VERIFICATION_REQUESTS_MISSING", "verification requests are missing"
            )
        artifacts.verify(task_id, run_id, record.artifact_id)
        try:
            value = json.loads(
                _artifact_path(self._store, run_id, record).read_text(encoding="utf-8")
            )
            requests = tuple(
                ToolRequest.model_validate(item) for item in value["requests"]
            )
        except (OSError, TypeError, ValueError, KeyError) as error:
            raise DomainError(
                "VERIFICATION_REQUESTS_INVALID", "verification requests are invalid"
            ) from error
        if tuple(request.tool_id for request in requests) != tuple(
            item[0] for item in _TOOLS
        ):
            raise DomainError(
                "VERIFICATION_REQUESTS_INVALID", "verification policy order is invalid"
            )
        return requests

    def _context(self, task_id: str, run_id: str) -> dict[str, str]:
        artifacts = FileArtifactStore(self._store)
        record = next(
            (
                item
                for item in artifacts.list(task_id, run_id)
                if item.relative_path == _CONTEXT_PATH
            ),
            None,
        )
        if record is None:
            raise DomainError(
                "VERIFICATION_CONTEXT_MISSING", "verification target is missing"
            )
        artifacts.verify(task_id, run_id, record.artifact_id)
        try:
            value = json.loads(
                _artifact_path(self._store, run_id, record).read_text(encoding="utf-8")
            )
            return {
                key: str(value[key])
                for key in (
                    "apply_run_id",
                    "proposal_run_id",
                    "proposal_hash",
                    "repository_alias",
                    "base_sha",
                    "diff_hash",
                )
            }
        except (OSError, TypeError, ValueError, KeyError) as error:
            raise DomainError(
                "VERIFICATION_CONTEXT_INVALID", "verification target is invalid"
            ) from error

    def _save_harness(
        self, run: RunRecord, report: HarnessReport, fingerprint: str
    ) -> ArtifactRecord:
        fixed = report.model_copy(
            update={
                "operation_id": run.operation_id,
                "run_id": run.run_id,
                "stage": run.stage,
            }
        )
        HarnessReportStore(self._store).save(fixed, _HARNESS_REPORT_NAME)
        return FileArtifactStore(self._store).register(
            run.task_id,
            run.run_id,
            producer="change_verification",
            relative_path=f"harness/{_HARNESS_REPORT_NAME}",
            input_fingerprint=fingerprint,
            operation_id=run.operation_id,
        )

    def _handoff(
        self,
        run: RunRecord,
        details: dict[str, str],
        results: list[ToolResult],
        report: ArtifactRecord,
    ) -> ArtifactRecord:
        artifacts = FileArtifactStore(self._store)
        proposal = next(
            item
            for item in artifacts.list(run.task_id, details["apply_run_id"])
            if item.relative_path == "artifacts/approved-patch-proposal.json"
        )
        artifacts.verify(run.task_id, details["apply_run_id"], proposal.artifact_id)
        evidence = _git(
            self._git,
            ["git", "diff", "--name-only", "--"],
            WorkspaceService(self._project_root, self._git)
            .prepare(
                run.task_id,
                details["repository_alias"],
                run.run_id,
                base_sha=details["base_sha"],
            )
            .path,
        ).splitlines()
        value = {
            "schema_version": 1,
            "status": "passed",
            "created_at": datetime.now(UTC).isoformat(),
            "task_id": run.task_id,
            "proposal_run_id": details["proposal_run_id"],
            "apply_run_id": details["apply_run_id"],
            "verification_run_id": run.run_id,
            "repository_alias": details["repository_alias"],
            "base_sha": details["base_sha"],
            "verified_head_sha": details["base_sha"],
            "diff_hash": details["diff_hash"],
            "changed_paths": evidence,
            "patch_proposal_hash": details["proposal_hash"],
            "patch_proposal_artifact_ref": _value(proposal),
            "tool_runs": [
                {
                    "tool_run_id": item.tool_run_id,
                    "status": item.status.value,
                    "stdout_artifact_ref": item.stdout_artifact_ref.model_dump(
                        mode="json"
                    ),
                    "stderr_artifact_ref": item.stderr_artifact_ref.model_dump(
                        mode="json"
                    ),
                }
                for item in results
            ],
            "harness_report_ref": _value(report),
            "verification_policy_revision": VERIFICATION_POLICY_REVISION,
        }
        return artifacts.write_bytes(
            run.task_id,
            run.run_id,
            producer="change_verification",
            relative_path=_HANDOFF_PATH,
            content=_json(value),
            input_fingerprint=details["diff_hash"],
            operation_id=run.operation_id,
        )

    @staticmethod
    def _skipped(
        artifacts: FileArtifactStore,
        run: RunRecord,
        requests: tuple[ToolRequest, ...],
        failed_tool: str,
    ) -> None:
        remaining = [
            request.tool_id
            for request in requests[
                tuple(request.tool_id for request in requests).index(failed_tool) + 1 :
            ]
        ]
        if remaining:
            artifacts.write_bytes(
                run.task_id,
                run.run_id,
                producer="change_verification",
                relative_path="artifacts/verification-skipped.json",
                content=_json(
                    {"skipped": remaining, "reason": f"{failed_tool} did not succeed"}
                ),
                input_fingerprint=_hash(failed_tool.encode()),
                operation_id=run.operation_id,
            )

    def _fail(self, run: RunRecord, journal: JsonlEventJournal, code: str) -> None:
        current = self._store.get_run(run.task_id, run.run_id)
        if current.status in {RunStatus.PENDING, RunStatus.RUNNING}:
            self._store.set_run_status(run.run_id, RunStatus.FAILED, error_code=code)
            journal.append("run.failed", {"error_code": code})


def _policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        revision=VERIFICATION_POLICY_REVISION,
        tools=(
            ToolPolicy(
                tool_id=VERIFICATION_PYTEST_TOOL_ID,
                kind=ToolKind.TEST,
                executable=sys.executable,
                argv_schema=(ArgvRule(literal="-m"), ArgvRule(literal="pytest")),
                allowed_env_names=_ENV_NAMES,
                max_output_bytes=_MAX_OUTPUT,
                max_timeout_seconds=_TIMEOUT,
            ),
            ToolPolicy(
                tool_id="verification-ruff",
                kind=ToolKind.TEST,
                executable="ruff",
                argv_schema=(
                    ArgvRule(literal="check"),
                    ArgvRule(literal="src"),
                    ArgvRule(literal="tests"),
                ),
                allowed_env_names=_ENV_NAMES,
                max_output_bytes=_MAX_OUTPUT,
                max_timeout_seconds=_TIMEOUT,
            ),
            ToolPolicy(
                tool_id="verification-mypy",
                kind=ToolKind.TEST,
                executable="mypy",
                argv_schema=(ArgvRule(literal="src"),),
                allowed_env_names=_ENV_NAMES,
                max_output_bytes=_MAX_OUTPUT,
                max_timeout_seconds=_TIMEOUT,
            ),
        ),
    )


def _result(
    run: RunRecord, results: list[ToolResult], handoff: dict[str, str] | None
) -> VerificationResult:
    return VerificationResult(
        run.task_id,
        run.operation_id,
        run.run_id,
        run.status.value,
        run.error_code,
        tuple(results),
        handoff,
    )


def _journal(store: FileTaskStore, run: RunRecord) -> JsonlEventJournal:
    return JsonlEventJournal(
        store.event_journal_path(run.run_id),
        task_id=run.task_id,
        run_id=run.run_id,
        stage=run.stage,
    )


def _artifact_path(store: FileTaskStore, run_id: str, record: ArtifactRecord) -> Path:
    return store.event_journal_path(run_id).parent / record.relative_path


def _ref(record: ArtifactRecord) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=record.artifact_id,
        task_id=record.task_id,
        operation_id=record.operation_id,
        run_id=record.run_id,
        relative_path=record.relative_path,
        sha256=record.sha256,
    )


def _value(record: ArtifactRecord) -> dict[str, str]:
    return {
        "artifact_id": record.artifact_id,
        "task_id": record.task_id,
        "operation_id": record.operation_id,
        "run_id": record.run_id,
        "relative_path": record.relative_path,
        "sha256": record.sha256,
    }


def _git(git: GitRunner, argv: list[str], cwd: Path) -> str:
    return git.run(argv, cwd=cwd).stdout


def _canonical_diff(value: str) -> str:
    return (
        "\n".join(
            line
            for line in value.replace("\r\n", "\n").split("\n")
            if not line.startswith(("diff --git ", "index "))
        ).strip()
        + "\n"
    )


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_json(value: object) -> str:
    return _hash(_json(value))


def _json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
