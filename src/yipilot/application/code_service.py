"""Proposal-only CodeAgent entry point."""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from yipilot.application.agent_runtime import AgentRuntime
from yipilot.application.model_planner import ModelPlanner
from yipilot.application.model_selection_service import ModelSelectionService
from yipilot.application.runtime_adapters import RegistryToolGateway
from yipilot.application.tool_registry import ToolRegistry
from yipilot.application.workspace_service import PreparedWorkspace, WorkspaceService
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ExecutionPolicy
from yipilot.domain.model import ModelPort, ModelRequest, model_request_fingerprint
from yipilot.domain.provider import ResolvedModel
from yipilot.harness.models import HarnessStatus
from yipilot.harness.report_store import (
    SDD_HARNESS_REPORT_NAME,
    SRS_HARNESS_REPORT_NAME,
    HarnessReportStore,
)
from yipilot.infrastructure.artifact_store import ArtifactRecord, FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.runtime_evidence import FileRuntimeEvidence
from yipilot.infrastructure.task_store import FileTaskStore, RunRecord

_PLAN_PATH = "artifacts/agent-plan.json"
_PROPOSAL_PATH = "artifacts/patch-proposal.json"
_RESOLVED_PATH = "artifacts/resolved-model.json"
_CONTEXT_PATH = "artifacts/live-context.md"
_MAX_CONTEXT_FILES = 16
_MAX_CONTEXT_BYTES = 24 * 1024
_MAX_FILE_BYTES = 8 * 1024


@dataclass(frozen=True)
class CodeGenerateResult:
    task_id: str
    operation_id: str
    run_id: str
    status: str
    error_code: str | None
    plan_artifact_ref: dict[str, object] | None
    patch_proposal_artifact_ref: dict[str, object] | None

    def value(self) -> dict[str, object]:
        return {
            "error_code": self.error_code,
            "operation_id": self.operation_id,
            "plan_artifact_ref": self.plan_artifact_ref,
            "patch_proposal_artifact_ref": self.patch_proposal_artifact_ref,
            "run_id": self.run_id,
            "status": self.status,
            "task_id": self.task_id,
        }


class CodeService:
    """Freeze one selected model and run it through the sole CodeAgent Runtime."""

    def __init__(
        self,
        project_root: Path,
        *,
        settings_dir: Path | None = None,
        model_selection_service: ModelSelectionService | None = None,
    ) -> None:
        self._project_root = Path(project_root).resolve(strict=True)
        self._store = FileTaskStore(self._project_root)
        self._models = model_selection_service or ModelSelectionService(settings_dir)

    def generate(
        self,
        task_id: str,
        alias: str,
        instruction: str,
        *,
        request_id: str | None = None,
        model_selector: str | None = None,
        context_files: Iterable[str] = (),
    ) -> CodeGenerateResult:
        if not instruction:
            raise DomainError("INSTRUCTION_INVALID", "instruction is required")
        self._store.get_task(task_id)
        WorkspaceService(self._project_root, GitRunner()).validate_repository(task_id, alias)
        resolved = self._models.resolve(explicit_selector=model_selector)
        port = self._models.model_port(resolved)
        fingerprint = _sha256(instruction)
        key = request_id or fingerprint
        operation = self._store.create_operation(
            task_id,
            "code_generate",
            f"code-generate:{task_id}:{alias}:{resolved.selector}:{resolved.connection_revision}:{key}",
            {
                "alias": alias,
                "instruction_fingerprint": fingerprint,
                "model_selector": resolved.selector,
                "provider_revision": resolved.connection_revision,
                "request_id": key,
            },
        )
        run, created = self._store.get_or_create_run(task_id, operation.operation_id, "code_generate")
        if not created:
            _close_port(port)
            return _result(self._store, run)
        try:
            return self._run(run, alias, instruction, fingerprint, resolved, port, tuple(context_files))
        finally:
            _close_port(port)

    def _run(
        self,
        run: RunRecord,
        alias: str,
        instruction: str,
        fingerprint: str,
        resolved: ResolvedModel,
        port: ModelPort,
        context_files: tuple[str, ...],
    ) -> CodeGenerateResult:
        artifacts = FileArtifactStore(self._store)
        journal = _journal(self._store, run)
        try:
            workspace = WorkspaceService(self._project_root, GitRunner()).prepare(run.task_id, alias, run.run_id)
            self._write_resolved(artifacts, run, resolved, fingerprint)
            documents = self._approved_documents(run.task_id)
            files = _context_files(workspace, context_files)
            prompt = _prompt(instruction, documents, files)
            context = artifacts.write_bytes(
                run.task_id, run.run_id, producer="code_service", relative_path=_CONTEXT_PATH,
                content=_context_manifest(documents, files).encode("utf-8"), input_fingerprint=_sha256(prompt),
                operation_id=run.operation_id,
            )
            context_ref = _ref(context)
            request = ModelRequest(
                task_id=run.task_id, operation_id=run.operation_id, run_id=run.run_id,
                provider_profile_name=resolved.connection_id, provider_type=resolved.provider_type,
                model=resolved.model_id, profile_revision=resolved.connection_revision,
                timeout_seconds=resolved.timeout_seconds, output_budget_bytes=resolved.output_limit,
                request_options=resolved.request_options, allowed_input_artifact_refs=(context_ref,),
                input_fingerprint=model_request_fingerprint(prompt, (context_ref,)), user_instruction=prompt,
            )
            evidence = FileRuntimeEvidence(artifacts, journal)
            runtime = AgentRuntime(
                self._store, ModelPlanner(port, request, evidence),
                RegistryToolGateway(self._project_root, ToolRegistry(ExecutionPolicy(revision="codeagent-proposal-v1"))),
                evidence,
            )
            runtime.execute(run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: True)
        except DomainError as error:
            if self._store.get_run(run.task_id, run.run_id).status.value == "pending":
                self._store.set_run_status(run.run_id, "running")
                journal.append("run.started", {})
                journal.append("turn.started", {"turn": 1})
            if self._store.get_run(run.task_id, run.run_id).status.value == "running":
                self._store.set_run_status(run.run_id, "failed", error_code=error.code)
                journal.append("turn.failed", {"turn": 1, "error_code": error.code})
                journal.append("run.failed", {"error_code": error.code})
        return _result(self._store, self._store.get_run(run.task_id, run.run_id))

    @staticmethod
    def _write_resolved(
        artifacts: FileArtifactStore, run: RunRecord, resolved: ResolvedModel, fingerprint: str
    ) -> None:
        artifacts.write_bytes(
            run.task_id, run.run_id, producer="code_service", relative_path=_RESOLVED_PATH,
            content=json.dumps(resolved.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode(),
            input_fingerprint=fingerprint, operation_id=run.operation_id,
        )

    def _approved_documents(self, task_id: str) -> list[tuple[ArtifactRecord, str]]:
        artifacts = FileArtifactStore(self._store)
        reports = HarnessReportStore(self._store)
        for source_run in reversed(self._store.list_runs(task_id)):
            if source_run.stage != "quality_artifacts" or source_run.status.value != "succeeded":
                continue
            try:
                if not all(reports.load(source_run.run_id, name).status is HarnessStatus.PASSED for name in (SRS_HARNESS_REPORT_NAME, SDD_HARNESS_REPORT_NAME)):
                    continue
            except DomainError:
                continue
            records = {record.relative_path: record for record in artifacts.list(task_id, source_run.run_id)}
            paths = ("artifacts/srs.md", "artifacts/sdd.md", "artifacts/test-plan.md")
            if not all(path in records for path in paths):
                continue
            values: list[tuple[ArtifactRecord, str]] = []
            total = 0
            for path in paths:
                record = artifacts.verify(task_id, source_run.run_id, records[path].artifact_id)
                raw = (self._store.event_journal_path(source_run.run_id).parent / record.relative_path).read_bytes()
                total += len(raw)
                if total > _MAX_CONTEXT_BYTES:
                    raise DomainError("MODEL_CONTEXT_LIMIT_EXCEEDED", "approved documents exceed the context limit")
                try:
                    values.append((record, raw.decode("utf-8")))
                except UnicodeDecodeError as error:
                    raise DomainError("MODEL_CONTEXT_INVALID", "approved document is not UTF-8") from error
            return values
        return []


def _result(store: FileTaskStore, run: RunRecord) -> CodeGenerateResult:
    records = FileArtifactStore(store).list(run.task_id, run.run_id)
    plan = next((record for record in records if record.relative_path == _PLAN_PATH), None)
    proposal = next((record for record in records if record.relative_path == _PROPOSAL_PATH), None)
    return CodeGenerateResult(
        task_id=run.task_id, operation_id=run.operation_id, run_id=run.run_id,
        status=run.status.value, error_code=run.error_code,
        plan_artifact_ref=_record_value(plan) if plan else None,
        patch_proposal_artifact_ref=_record_value(proposal) if proposal else None,
    )


def _record_value(record: ArtifactRecord) -> dict[str, object]:
    return {"artifact_id": record.artifact_id, "operation_id": record.operation_id,
            "relative_path": record.relative_path, "run_id": record.run_id,
            "sha256": record.sha256, "task_id": record.task_id}


def _context_files(workspace: PreparedWorkspace, values: tuple[str, ...]) -> list[tuple[str, str, str]]:
    if len(values) > _MAX_CONTEXT_FILES or len(set(values)) != len(values):
        raise DomainError("MODEL_CONTEXT_INVALID", "context files must be unique and within the file limit")
    selected: list[tuple[str, str, str]] = []
    total = 0
    for value in values:
        posix, windows = PurePosixPath(value), PureWindowsPath(value)
        if (not value or posix.is_absolute() or windows.is_absolute() or bool(windows.drive)
                or bool(windows.root) or ".." in posix.parts or ".." in windows.parts
                or ".git" in {part.casefold() for part in posix.parts}):
            raise DomainError("MODEL_CONTEXT_PATH_DENIED", "context path is not in the task worktree")
        candidate = workspace.path.joinpath(*posix.parts)
        if candidate.is_symlink() or not candidate.is_file() or workspace.path not in candidate.resolve(strict=True).parents:
            raise DomainError("MODEL_CONTEXT_PATH_DENIED", "context file must be a regular task-worktree file")
        raw = candidate.read_bytes()
        total += len(raw)
        if len(raw) > _MAX_FILE_BYTES or total > _MAX_CONTEXT_BYTES:
            raise DomainError("MODEL_CONTEXT_LIMIT_EXCEEDED", "context files exceed the live context limit")
        try:
            selected.append((value.replace("\\", "/"), _sha256_bytes(raw), raw.decode("utf-8")))
        except UnicodeDecodeError as error:
            raise DomainError("MODEL_CONTEXT_INVALID", "context file is not UTF-8") from error
    return selected


def _prompt(
    instruction: str,
    documents: list[tuple[ArtifactRecord, str]],
    files: list[tuple[str, str, str]],
) -> str:
    value = {"instruction": instruction, "approved_documents": [{"artifact_id": record.artifact_id, "path": record.relative_path, "content": content} for record, content in documents], "response_contract": {"schema_version": 1, "summary": "short summary", "patches": [{"path": "relative/path.py", "unified_diff": "unified diff"}]}, "worktree_files": [{"path": path, "sha256": digest, "content": content} for path, digest, content in files]}
    prompt = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(prompt.encode("utf-8")) > 32 * 1024:
        raise DomainError("MODEL_CONTEXT_LIMIT_EXCEEDED", "model prompt exceeds its byte limit")
    return prompt


def _context_manifest(
    documents: list[tuple[ArtifactRecord, str]], files: list[tuple[str, str, str]]
) -> str:
    document_lines = [f"- `{record.artifact_id}` `{record.relative_path}` sha256:{record.sha256}" for record, _content in documents]
    file_lines = [f"- `{path}` sha256:{digest}" for path, digest, _content in files]
    return "# Approved CodeAgent context\n\n## Documents\n" + "\n".join(document_lines) + "\n\n## Worktree\n" + "\n".join(file_lines) + "\n"


def _ref(record: ArtifactRecord):
    from yipilot.domain.execution import ArtifactRef
    return ArtifactRef(artifact_id=record.artifact_id, task_id=record.task_id, operation_id=record.operation_id,
                       run_id=record.run_id, relative_path=record.relative_path, sha256=record.sha256)


def _journal(store: FileTaskStore, run: RunRecord) -> JsonlEventJournal:
    return JsonlEventJournal(store.event_journal_path(run.run_id), task_id=run.task_id, run_id=run.run_id, stage=run.stage)


def _close_port(port: object) -> None:
    close = getattr(port, "close", None)
    if callable(close):
        close()


def _sha256(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
