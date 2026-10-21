"""Approved, fixed OpenSandbox verification and a revalidated Change Handoff gate."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

from pydantic import JsonValue

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    ArgvRule,
    ExecutionPolicy,
    ToolKind,
    ToolPolicy,
    fingerprint_model,
)
from yipilot.domain.sandbox import (
    SandboxBudgets,
    SandboxDiffEvidence,
    SandboxMode,
    SandboxRequest,
    SandboxResult,
    SourceManifest,
    source_path,
)
from yipilot.harness.models import HarnessCheck, HarnessReport, HarnessStatus
from yipilot.harness.report_store import HarnessReportStore
from yipilot.persistence.approval_store import FileApprovalStore
from yipilot.persistence.artifact_store import ArtifactRecord, FileArtifactStore
from yipilot.persistence.filesystem import atomic_write_bytes, project_path
from yipilot.persistence.ownership import project_lock
from yipilot.persistence.task_store import FileTaskStore
from yipilot.security.paths import read_evidence, require_plain_path
from yipilot.workspace.git_runner import GitRunner
from yipilot.workspace.runner_client import RunnerClient
from yipilot.workspace.runner_worker import (
    RESULT_ARTIFACTS,
    RunnerSandboxWorker,
    cancel_remote_job,
    read_remote_request,
    verify_remote_result,
)
from yipilot.workspace.sandbox_apply import SandboxApplyService
from yipilot.workspace.sandbox_output import SandboxOutputService
from yipilot.workspace.sandbox_sources import (
    PreparedSources,
    SandboxSourceService,
    scan_source_tree,
)

REVISION = "sandbox-runner-verification-v2"
_COMMANDS = (
    (
        "verification-pytest",
        ("python", "-I", "-B", "-m", "pytest", "-p", "no:cacheprovider"),
    ),
    ("verification-ruff", ("ruff", "check", "src", "tests")),
    ("verification-mypy", ("mypy", "src")),
)
_BUDGETS = SandboxBudgets(
    wall_clock_seconds=600,
    cpu_count=1,
    memory_bytes=512 * 1024**2,
    pid_limit=128,
    stdout_bytes=128 * 1024,
    stderr_bytes=128 * 1024,
)
_HANDOFF = "artifacts/change-handoff.json"


def verification_policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        revision=REVISION,
        tools=tuple(
            ToolPolicy(
                tool_id=tool,
                kind=ToolKind.TEST,
                executable=argv[0],
                argv_schema=tuple(ArgvRule(literal=arg) for arg in argv[1:]),
                max_output_bytes=_BUDGETS.stdout_bytes,
                max_timeout_seconds=_BUDGETS.wall_clock_seconds,
            )
            for tool, argv in _COMMANDS
        ),
    )


def _json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


@dataclass(frozen=True)
class SandboxVerificationPreparation:
    task_id: str
    operation_id: str
    run_id: str
    approval_fingerprint: str
    requests: tuple[SandboxRequest, ...]


class SandboxVerificationService:
    def __init__(self, project_root: Path, *, store: FileTaskStore | None = None,
                 git: GitRunner | None = None, runner: RunnerClient | None = None) -> None:
        self._root = project_root.resolve(strict=True)
        self._store = store or FileTaskStore(self._root)
        self._git = git or GitRunner()
        self._runner = runner or RunnerClient()
        self._apply = SandboxApplyService(self._root, self._git)
        self._output = SandboxOutputService(self._root, self._git)

    def prepare(
        self,
        prepared: PreparedSources,
        request: SandboxRequest,
        policy: ExecutionPolicy,
        evidence: SandboxDiffEvidence,
        *, request_id: str | None = None,
    ) -> SandboxVerificationPreparation:
        """Freeze three independent inputs; the caller must obtain a human approval."""
        with project_lock(self._root, "executor", timeout=30):
            self._apply.verify(prepared, request, policy, evidence)
            output = self._output.inspect(prepared, request, policy)
            files, contents = scan_source_tree(prepared.execution, prepared.manifest.files)
            if files != output.manifest.files:
                raise DomainError("SANDBOX_OUTPUT_DRIFT", "verification seed changed")
            operation = self._store.create_operation(
                request.task_id,
                "sandbox_verification",
                f"sandbox-verify:{request.task_id}:{request_id or uuid4()}",
                {"apply_run_id": request.run_id, "patch_sha256": evidence.patch_sha256},
            )
            run, created = self._store.get_or_create_run(
                request.task_id, operation.operation_id, "sandbox_verification"
            )
            if not created:
                context, previous_requests = self._inputs(prepared, request, policy, evidence, run.run_id)
                return SandboxVerificationPreparation(request.task_id, operation.operation_id,
                    run.run_id, hashlib.sha256(_json(context)).hexdigest(), previous_requests)
            root = self._store.event_journal_path(run.run_id).parent
            requests = []
            try:
                for tool, argv in _COMMANDS:
                    child = self._store.create_run(request.task_id, run.operation_id, tool)
                    copies = SandboxSourceService(self._root, self._git).prepare(
                        self._store.get_task(request.task_id),
                        prepared.manifest.repository_alias,
                        child,
                        prepared.manifest.base_sha,
                    )
                    # Fresh controller-owned copies only. Each tool receives identical bytes.
                    for old in copies.manifest.files:
                        path = copies.execution / old.path
                        require_plain_path(path)
                        path.unlink()
                    for directory in sorted(
                        copies.execution.rglob("*"),
                        key=lambda p: len(p.parts),
                        reverse=True,
                    ):
                        require_plain_path(directory)
                        if directory.is_dir():
                            directory.rmdir()
                    for file in files:
                        path = project_path(copies.execution, file.path)
                        require_plain_path(path)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        atomic_write_bytes(path, contents[file.path])
                        path.chmod(0o755 if file.git_mode == "100755" else 0o644)
                    seed = SourceManifest.model_validate_json(
                        copies.manifest.model_copy(
                            update={"files": files}
                        ).model_dump_json()
                    )
                    atomic_write_bytes(
                        copies.execution.parent / "input-manifest.json",
                        seed.model_dump_json().encode(),
                    )
                    item = SandboxRequest.model_validate(
                        {
                            "task_id": request.task_id,
                            "operation_id": run.operation_id,
                            "run_id": child.run_id,
                            "policy_revision": REVISION,
                            "mode": SandboxMode.OFFLINE_WORKSPACE_WRITE,
                            "execution_directory": str(copies.execution),
                            "baseline_manifest_hash": copies.manifest_hash,
                            "input_manifest_hash": fingerprint_model(seed),
                            "image_digest": request.image_digest,
                            "runner_endpoint": request.runner_endpoint,
                            "tool_id": tool,
                            "argv": argv,
                            "budgets": _BUDGETS,
                        },
                        context={"policy": verification_policy()},
                    )
                    atomic_write_bytes(copies.execution.parent / "sandbox-cli-request.json", item.model_dump_json().encode())
                    RunnerSandboxWorker(self._root, verification_policy(), copies, input_manifest=seed, runner=self._runner).freeze(item)
                    requests.append(item)
                self._apply.verify(prepared, request, policy, evidence)
                context = {
                    "schema_version": 1,
                    "task_id": request.task_id,
                    "operation_id": run.operation_id,
                    "run_id": run.run_id,
                    "apply_run_id": request.run_id,
                    "parent_request_fingerprint": request.fingerprint,
                    "evidence_fingerprint": fingerprint_model(evidence),
                    "requests": [item.model_dump(mode="json") for item in requests],
                }
                data = _json(context)
                atomic_write_bytes(root / "sandbox-verification.json", data)
                return SandboxVerificationPreparation(
                    request.task_id,
                    run.operation_id,
                    run.run_id,
                    hashlib.sha256(data).hexdigest(),
                    tuple(requests),
                )
            except BaseException as error:
                self._abort(request.task_id, run.operation_id, "SANDBOX_EVIDENCE_FAILED")
                if isinstance(error, (OSError, ValueError)):
                    raise DomainError(
                        "SANDBOX_EVIDENCE_FAILED", "verification preparation failed"
                    ) from error
                raise

    def run(
        self,
        prepared: PreparedSources,
        request: SandboxRequest,
        policy: ExecutionPolicy,
        evidence: SandboxDiffEvidence,
        run_id: str,
        *,
        approval_id: str,
        cancel_event: Event | None = None,
    ) -> ArtifactRecord:
        root = self._store.event_journal_path(run_id).parent
        lock = project_path(root, ".sandbox-verification.lock")
        require_plain_path(lock)
        with project_lock(self._root, "run-" + run_id):
            run = self._store.get_run(request.task_id, run_id)
            if run.stage != "sandbox_verification" or run.status.value != "pending":
                raise DomainError(
                    "SANDBOX_CONFLICT", "verification retry requires a new Run"
                )
            artifacts = FileArtifactStore(self._store)
            try:
                started_at = datetime.now(UTC)
                self._store.set_run_status(run_id, "running")
                context, requests = self._inputs(
                    prepared, request, policy, evidence, run_id
                )
                self._approval(run_id, request.task_id, approval_id, context)
                atomic_write_bytes(
                    root / "verification-approval.json",
                    _json({"approval_id": approval_id}),
                )
                for item in requests:
                    self._approval(run_id, request.task_id, approval_id, context)
                    self._apply.verify(prepared, request, policy, evidence)
                    if cancel_event is not None and cancel_event.is_set():
                        raise DomainError("SANDBOX_CANCELLED", "verification cancelled")
                    copies, seed = self._copies(item)
                    self._store.set_run_status(item.run_id, "running")
                    result = RunnerSandboxWorker(
                        self._root, verification_policy(), copies, input_manifest=seed, runner=self._runner
                    ).execute(item, cancel_event=cancel_event)
                    self._verify_tool(item, result, seed)
                    for relative in RESULT_ARTIFACTS:
                        FileArtifactStore(self._store).register(item.task_id, item.run_id,
                            producer="sandbox_verification", relative_path=relative, input_fingerprint=item.fingerprint)
                    self._store.set_run_status(item.run_id, "succeeded")
                self._apply.verify(prepared, request, policy, evidence)
                self._approval(run_id, request.task_id, approval_id, context)
                for item in requests:
                    _, seed = self._copies(item)
                    self._verify_tool(item, self._result(item), seed)
                if cancel_event is not None and cancel_event.is_set():
                    raise DomainError("SANDBOX_CANCELLED", "verification cancelled")
                fingerprint = hashlib.sha256(_json(context)).hexdigest()
                report = HarnessReport(
                    report_id=str(uuid4()),
                    task_id=request.task_id,
                    operation_id=run.operation_id,
                    run_id=run_id,
                    stage="sandbox_verification",
                    harness_name="sandbox-verification",
                    harness_version="1",
                    started_at=started_at,
                    ended_at=datetime.now(UTC),
                    input_fingerprint=fingerprint,
                    inputs=[
                        {
                            "apply_run_id": request.run_id,
                            "patch_sha256": evidence.patch_sha256,
                        }
                    ],
                    status=HarnessStatus.PASSED,
                    checks=[
                        HarnessCheck(
                            id=tool,
                            status=HarnessStatus.PASSED,
                            description="fixed OpenSandbox tool passed; output and cleanup verified",
                        )
                        for tool, _ in _COMMANDS
                    ]
                    + [
                        HarnessCheck(
                            id="sandbox-applied-output",
                            status=HarnessStatus.PASSED,
                            description="approved worktree, HEAD and index remain unchanged",
                        )
                    ],
                    summary="All fixed OpenSandbox checks and trusted output checks passed",
                )
                HarnessReportStore(self._store).save(
                    report, "sandbox-verification-report.json"
                )
                report_ref = artifacts.register(
                    request.task_id,
                    run_id,
                    producer="sandbox_verification",
                    relative_path="harness/sandbox-verification-report.json",
                    input_fingerprint=fingerprint,
                )
                value = {
                    "schema_version": 1,
                    "status": "passed",
                    "trust_level": "sandboxed",
                    "created_at": datetime.now(UTC).isoformat(),
                    "task_id": request.task_id,
                    "operation_id": run.operation_id,
                    "apply_run_id": request.run_id,
                    "verification_run_id": run_id,
                    "repository_alias": prepared.manifest.repository_alias,
                    "base_sha": prepared.manifest.base_sha,
                    "verified_head_sha": prepared.manifest.base_sha,
                    "diff_hash": evidence.patch_sha256,
                    "output_manifest_hash": evidence.output_manifest_hash,
                    "approval_id": approval_id,
                    "harness_report_ref": report_ref.model_dump(mode="json"),
                    "verification_policy_revision": REVISION,
                    "verification_fingerprint": hashlib.sha256(
                        _json(context)
                    ).hexdigest(),
                    "tool_runs": [
                        self._result(item).model_dump(mode="json") for item in requests
                    ],
                }
                record = artifacts.write_bytes(
                    request.task_id,
                    run_id,
                    producer="sandbox_verification",
                    relative_path=_HANDOFF,
                    content=_json(value),
                    input_fingerprint=str(value["verification_fingerprint"]),
                    operation_id=run.operation_id,
                )
                artifacts.verify(request.task_id, run_id, record.artifact_id)
                self._store.set_run_status(run_id, "succeeded")
                return record
            except BaseException as error:
                handoff = root / _HANDOFF
                require_plain_path(handoff)
                handoff.unlink(missing_ok=True)
                self._abort(
                    request.task_id,
                    run.operation_id,
                    error.code
                    if isinstance(error, DomainError)
                    else "SANDBOX_EVIDENCE_FAILED",
                )
                if isinstance(error, (OSError, ValueError, KeyError, TypeError)):
                    raise DomainError(
                        "SANDBOX_EVIDENCE_FAILED", "verification evidence failed"
                    ) from error
                raise

    def handoff(
        self,
        prepared: PreparedSources,
        request: SandboxRequest,
        policy: ExecutionPolicy,
        evidence: SandboxDiffEvidence,
        run_id: str,
    ) -> ArtifactRecord:
        """Revalidate an existing Handoff; malformed or stale evidence fails closed."""
        try:
            return self._handoff(prepared, request, policy, evidence, run_id)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise DomainError(
                "SANDBOX_EVIDENCE_FAILED", "Handoff evidence is incomplete"
            ) from error

    def failed_diagnostics(
        self,
        prepared: PreparedSources,
        request: SandboxRequest,
        policy: ExecutionPolicy,
        evidence: SandboxDiffEvidence,
        run_id: str,
    ) -> str:
        """Return bounded output only for an evidenced failing OpenSandbox check."""
        run = self._store.get_run(request.task_id, run_id)
        if run.stage != "sandbox_verification" or run.status.value != "failed":
            raise DomainError("REPAIR_VERIFICATION_INVALID", "verification Run is not a failed repair candidate")
        _, requests = self._inputs(prepared, request, policy, evidence, run_id)
        artifacts = FileArtifactStore(self._store)
        values: list[str] = []
        for item in requests:
            child = self._store.get_run(request.task_id, item.run_id)
            if child.status.value != "failed":
                continue
            result = self._result(item)
            verify_remote_result(self._store.event_journal_path(item.run_id).parent, item, result)
            if (
                (result.task_id, result.operation_id, result.run_id)
                != (item.task_id, item.operation_id, item.run_id)
                or result.request_fingerprint != item.fingerprint
                or result.image_digest != item.image_digest
                or result.resource_limits != item.budgets
                or result.reason != "exited"
                or result.exit_code in {None, 0}
            ):
                continue
            output: list[str] = []
            root = self._store.event_journal_path(item.run_id).parent
            for ref in (result.stdout_artifact_ref, result.stderr_artifact_ref):
                record = artifacts.verify(request.task_id, item.run_id, ref.artifact_id)
                if (
                    record.artifact_id != ref.artifact_id
                    or record.sha256 != ref.sha256
                    or record.relative_path != ref.relative_path
                    or record.input_fingerprint != item.fingerprint
                ):
                    raise DomainError("SANDBOX_EVIDENCE_FAILED", "verification output binding changed")
                output.append(read_evidence(root / record.relative_path)[:4096].decode("utf-8", errors="replace"))
            values.append(f"## {item.tool_id}\n" + "\n".join(output))
        diagnostics = "\n\n".join(values)[:8192]
        if not diagnostics.strip():
            raise DomainError("REPAIR_DIAGNOSTICS_MISSING", "no evidenced failing OpenSandbox check output")
        return diagnostics

    def _handoff(
        self,
        prepared: PreparedSources,
        request: SandboxRequest,
        policy: ExecutionPolicy,
        evidence: SandboxDiffEvidence,
        run_id: str,
    ) -> ArtifactRecord:
        """Consumers must use this freshness gate, not merely find a JSON file."""
        if self._store.get_run(request.task_id, run_id).status.value != "succeeded":
            raise DomainError("SANDBOX_EVIDENCE_FAILED", "verification did not succeed")
        context, requests = self._inputs(prepared, request, policy, evidence, run_id)
        root = self._store.event_journal_path(run_id).parent
        approval_id = json.loads(read_evidence(root / "verification-approval.json"))[
            "approval_id"
        ]
        self._approval(run_id, request.task_id, approval_id, context)
        for item in requests:
            _, seed = self._copies(item)
            if (
                self._store.get_run(request.task_id, item.run_id).status.value
                != "succeeded"
            ):
                raise DomainError("SANDBOX_EVIDENCE_FAILED", "tool did not succeed")
            self._verify_tool(item, self._result(item), seed)
        artifacts = FileArtifactStore(self._store)
        value = json.loads(read_evidence(root / _HANDOFF))
        if (
            value["tool_runs"]
            != [self._result(item).model_dump(mode="json") for item in requests]
            or value["diff_hash"] != evidence.patch_sha256
            or value["apply_run_id"] != request.run_id
        ):
            raise DomainError("SANDBOX_EVIDENCE_FAILED", "Handoff contents changed")
        report_ref = ArtifactRecord.model_validate(value["harness_report_ref"])
        if (
            artifacts.verify(request.task_id, run_id, report_ref.artifact_id)
            != report_ref
        ):
            raise DomainError("SANDBOX_EVIDENCE_FAILED", "Harness reference changed")
        report = HarnessReportStore(self._store).load(
            run_id, "sandbox-verification-report.json"
        )
        if (
            report.status != HarnessStatus.PASSED
            or report.input_fingerprint != hashlib.sha256(_json(context)).hexdigest()
        ):
            raise DomainError("SANDBOX_EVIDENCE_FAILED", "Harness report changed")
        records = [
            r
            for r in artifacts.list(request.task_id, run_id)
            if r.relative_path == _HANDOFF
        ]
        if (
            len(records) != 1
            or records[0].input_fingerprint
            != hashlib.sha256(_json(context)).hexdigest()
        ):
            raise DomainError("SANDBOX_EVIDENCE_FAILED", "handoff binding differs")
        return artifacts.verify(request.task_id, run_id, records[0].artifact_id)

    def _abort(self, task_id: str, operation_id: str, code: str) -> None:
        runs = [r for r in self._store.list_runs(task_id) if r.operation_id == operation_id]
        uncertain = code == "REMOTE_RESULT_PENDING"
        for child in runs:
            root = self._store.event_journal_path(child.run_id).parent
            if child.status.value == "running" and (root / "remote-job/submission.json").exists():
                try:
                    cancel_remote_job(root, read_remote_request(root), self._runner)
                except (DomainError, OSError, ValueError):
                    uncertain = True
        for child in runs:
            if child.status.value not in {"pending", "running"}:
                continue
            if uncertain and child.status.value == "running":
                self._store.set_run_status(child.run_id, "paused", error_code="REMOTE_RESULT_PENDING")
            else:
                status = "cancelled" if child.status.value == "pending" or code == "SANDBOX_CANCELLED" else "failed"
                self._store.set_run_status(child.run_id, status, error_code=code)

    def _inputs(
        self,
        prepared: PreparedSources,
        request: SandboxRequest,
        policy: ExecutionPolicy,
        evidence: SandboxDiffEvidence,
        run_id: str,
    ) -> tuple[dict[str, JsonValue], tuple[SandboxRequest, ...]]:
        self._apply.verify(prepared, request, policy, evidence)
        run = self._store.get_run(request.task_id, run_id)
        context = json.loads(
            read_evidence(
                self._store.event_journal_path(run_id).parent
                / "sandbox-verification.json"
            )
        )
        if not isinstance(context, dict) or not isinstance(context.get("requests"), list):
            raise DomainError("SANDBOX_EVIDENCE_FAILED", "invalid verification context")
        requests = tuple(
            SandboxRequest.model_validate_json(
                _json(item), context={"policy": verification_policy()}
            )
            for item in context["requests"]
        )
        if (
            context
            != {
                "schema_version": 1,
                "task_id": request.task_id,
                "operation_id": run.operation_id,
                "run_id": run_id,
                "apply_run_id": request.run_id,
                "parent_request_fingerprint": request.fingerprint,
                "evidence_fingerprint": fingerprint_model(evidence),
                "requests": [r.model_dump(mode="json") for r in requests],
            }
            or len(requests) != 3
            or len({r.run_id for r in requests}) != 3
        ):
            raise DomainError("SANDBOX_EVIDENCE_FAILED", "verification context changed")
        for item, (tool, argv) in zip(requests, _COMMANDS, strict=True):
            child = self._store.get_run(request.task_id, item.run_id)
            if (
                item.task_id != request.task_id
                or item.operation_id != run.operation_id
                or child.operation_id != run.operation_id
                or child.stage != tool
                or item.tool_id != tool
                or item.argv != argv
                or item.image_digest != request.image_digest
                or item.budgets != _BUDGETS
            ):
                raise DomainError(
                    "SANDBOX_EVIDENCE_FAILED", "fixed verification policy changed"
                )
            copies, seed = self._copies(item)
            if scan_source_tree(copies.execution, seed.files)[0] != seed.files:
                raise DomainError("SANDBOX_OUTPUT_DRIFT", "verification source changed")
            if (
                seed.files
                != scan_source_tree(prepared.execution, prepared.manifest.files)[0]
                or copies.manifest.base_sha != prepared.manifest.base_sha
                or copies.manifest.repository_alias
                != prepared.manifest.repository_alias
            ):
                raise DomainError(
                    "SANDBOX_OUTPUT_DRIFT",
                    "verification input differs from approved apply",
                )
        return context, requests

    def _copies(
        self, request: SandboxRequest
    ) -> tuple[PreparedSources, SourceManifest]:
        root = self._store.event_journal_path(request.run_id).parent
        data = read_evidence(root / "source-manifest.json")
        manifest = SourceManifest.model_validate_json(data)
        copies = PreparedSources(
            root / "baseline",
            root / "execution",
            root / "source-manifest.json",
            hashlib.sha256(data).hexdigest(),
            manifest,
        )
        seed = SourceManifest.model_validate_json(read_evidence(root / "input-manifest.json"))
        if (
            request.input_manifest_hash != fingerprint_model(seed)
            or request.baseline_manifest_hash != copies.manifest_hash
            or seed.model_dump(exclude={"files"})
            != manifest.model_dump(exclude={"files"})
            or (manifest.task_id, manifest.operation_id, manifest.run_id)
            != (request.task_id, request.operation_id, request.run_id)
        ):
            raise DomainError("SANDBOX_MANIFEST_INVALID", "seed manifest hash changed")
        SandboxSourceService(self._root, self._git).validate(copies)
        return copies, seed

    def _approval(
        self, run_id: str, task_id: str, approval_id: str, context: dict[str, JsonValue]
    ) -> None:
        source_path(approval_id)
        if "/" in approval_id:
            raise DomainError("APPROVAL_REQUIRED", "invalid approval identifier")
        run = self._store.get_run(task_id, run_id)
        approval = FileApprovalStore(self._store).get(task_id, run_id, approval_id)
        approval.require_fresh(
            task_id=task_id,
            operation_id=run.operation_id,
            run_id=run_id,
            action_type="sandbox_verification",
            policy_revision=REVISION,
            target_fingerprint=hashlib.sha256(_json(context)).hexdigest(),
        )
        if approval.simulation_only:
            raise DomainError(
                "APPROVAL_REQUIRED", "real verification approval required"
            )

    def _result(self, request: SandboxRequest) -> SandboxResult:
        return SandboxResult.model_validate_json(
            read_evidence(
                self._store.event_journal_path(request.run_id).parent
                / "remote-job/result.json"
            )
        )

    def _verify_tool(
        self, request: SandboxRequest, result: SandboxResult, seed: SourceManifest
    ) -> None:
        root = self._store.event_journal_path(request.run_id).parent
        self._output.validate_result(root, request, result)
        verify_remote_result(root, request, result)
        copies, _ = self._copies(request)
        output = self._output.inspect(copies, request, verification_policy())
        if (
            output.manifest.files != seed.files
            or output.manifest.fingerprint != result.output_manifest_hash
        ):
            raise DomainError(
                "SANDBOX_OUTPUT_DRIFT", "verification modified approved source"
            )
