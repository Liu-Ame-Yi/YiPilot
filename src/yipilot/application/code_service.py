"""Proposal-only CodeAgent entry point."""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from yipilot.application.agent_runtime import AgentRuntime
from yipilot.application.approval import ApprovalGate, approval_action_type
from yipilot.application.model_planner import ModelPlanner
from yipilot.application.model_selection_service import ModelSelectionService
from yipilot.application.runtime_adapters import RegistryToolGateway
from yipilot.application.tool_registry import ToolRegistry
from yipilot.application.workspace_service import PreparedWorkspace, WorkspaceService
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    ApprovalDecision,
    ApprovalRecord,
    ArgvRule,
    ExecutionPolicy,
    ExecutionTrustLevel,
    ToolKind,
    ToolPolicy,
    ToolRequest,
)
from yipilot.domain.model import ModelPort, ModelRequest, model_request_fingerprint
from yipilot.domain.patch_proposal import PatchProposal
from yipilot.domain.provider import ResolvedModel
from yipilot.harness.models import HarnessStatus
from yipilot.harness.report_store import (
    SDD_HARNESS_REPORT_NAME,
    SRS_HARNESS_REPORT_NAME,
    HarnessReportStore,
)
from yipilot.infrastructure.approval_store import FileApprovalStore
from yipilot.infrastructure.artifact_store import ArtifactRecord, FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.executors import LocalCommandRunner
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
_APPLY_POLICY_REVISION = "code-apply-v1"
_APPLY_TOOL_ID = "git-apply-approved-proposal"
_APPLY_PATCH_PATH = "artifacts/approved-patch-proposal.diff"
_APPLY_EVIDENCE_PATH = "artifacts/apply-evidence.json"


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


@dataclass(frozen=True)
class CodeApplyResult:
    task_id: str
    operation_id: str
    run_id: str
    proposal_run_id: str
    status: str
    error_code: str | None

    def value(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "operation_id": self.operation_id,
            "run_id": self.run_id,
            "proposal_run_id": self.proposal_run_id,
            "status": self.status,
            "error_code": self.error_code,
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

    def proposals(self, task_id: str) -> list[dict[str, object]]:
        """Return only verified, successful proposal Runs owned by this Task."""
        self._store.get_task(task_id)
        return [self._proposal_details(task_id, run.run_id) for run in self._store.list_runs(task_id)
                if run.stage in {"code_generate", "code_repair"} and run.status.value == "succeeded"
                if self._is_proposal(task_id, run.run_id)]

    def apply(self, task_id: str, proposal_run_id: str, *, request_id: str | None = None) -> CodeApplyResult:
        """Apply one verified Proposal once, through the approved Git tool only."""
        proposal = self._proposal_details(task_id, proposal_run_id)
        key = request_id or str(proposal["proposal_hash"])
        operation = self._store.create_operation(
            task_id, "code_apply",
            f"code-apply:{task_id}:{proposal_run_id}:{key}",
            {"proposal_run_id": proposal_run_id, "proposal_hash": proposal["proposal_hash"],
             "repository_alias": proposal["repository_alias"], "request_id": key},
        )
        run, created = self._store.get_or_create_run(task_id, operation.operation_id, "code_apply")
        if not created:
            return _apply_result(run, proposal_run_id)
        artifacts = FileArtifactStore(self._store)
        journal = _journal(self._store, run)
        try:
            self._store.set_run_status(run.run_id, "running")
            journal.append("run.started", {"proposal_run_id": proposal_run_id})
            workspace = WorkspaceService(self._project_root, GitRunner()).prepare(
                task_id, str(proposal["repository_alias"]), run.run_id
            )
            if workspace.base_sha != proposal["base_sha"]:
                raise DomainError("PROPOSAL_BASE_SHA_DRIFT", "proposal base SHA no longer matches the repository")
            if _git_output(GitRunner(), ["git", "status", "--porcelain=v1"], workspace.path):
                raise DomainError("WORKSPACE_DIRTY", "new apply workspace is unexpectedly dirty")
            parent_diff = str(proposal.get("parent_diff", ""))
            if parent_diff:
                _apply_diff(GitRunner(), workspace.path, parent_diff)
                parent_actual = _git_output(GitRunner(), ["git", "diff", "--binary", "--no-ext-diff", "--no-textconv", "--"], workspace.path)
                if _canonical_diff(parent_actual) != _canonical_diff(parent_diff):
                    raise DomainError("APPLY_PARENT_DIFF_MISMATCH", "repair parent diff cannot be reconstructed")
            source_path = Path(str(proposal["proposal_path"]))
            patch_bytes = source_path.read_bytes()
            parsed_proposal = PatchProposal.model_validate_json(patch_bytes)
            diff_bytes = "".join(patch.unified_diff for patch in parsed_proposal.patches).encode("utf-8")
            artifacts.write_bytes(
                task_id, run.run_id, producer="code_apply", relative_path="artifacts/approved-patch-proposal.json",
                content=patch_bytes, input_fingerprint=str(proposal["proposal_hash"]), operation_id=run.operation_id,
            )
            patch_ref = artifacts.write_bytes(
                task_id, run.run_id, producer="code_apply", relative_path=_APPLY_PATCH_PATH,
                content=diff_bytes, input_fingerprint=str(proposal["proposal_hash"]), operation_id=run.operation_id,
            )
            binding = _sha256(json.dumps({
                "task_id": task_id, "operation_id": run.operation_id, "run_id": run.run_id,
                "proposal_artifact_hash": proposal["proposal_hash"], "proposal_run_id": proposal_run_id,
                "repository_alias": proposal["repository_alias"], "base_sha": workspace.base_sha,
                "policy_revision": _APPLY_POLICY_REVISION,
            }, sort_keys=True, separators=(",", ":")))
            patch_path = self._store.event_journal_path(run.run_id).parent / _APPLY_PATCH_PATH
            request = ToolRequest(
                task_id=task_id, operation_id=run.operation_id, run_id=run.run_id,
                workspace_relative_path=workspace.path.relative_to(self._project_root).as_posix(),
                tool_id=_APPLY_TOOL_ID,
                argv=("git", "apply", "--whitespace=nowarn", "--", str(patch_path)),
                timeout_seconds=60, output_budget_bytes=128 * 1024,
                input_artifact_refs=(_ref(patch_ref),), input_fingerprint=binding,
            )
            tool = ToolPolicy(
                tool_id=_APPLY_TOOL_ID, kind=ToolKind.WORKSPACE_WRITE, executable="git",
                argv_schema=(ArgvRule(literal="apply"), ArgvRule(literal="--whitespace=nowarn"),
                             ArgvRule(literal="--"), ArgvRule(literal=str(patch_path))),
                max_output_bytes=128 * 1024, max_timeout_seconds=60,
            )
            policy = ExecutionPolicy(revision=_APPLY_POLICY_REVISION, tools=(tool,))
            approval = ApprovalRecord(
                approval_id=str(uuid4()), task_id=task_id, operation_id=run.operation_id, run_id=run.run_id,
                action_type=approval_action_type(tool, ExecutionTrustLevel.HOST_TRUSTED),
                target_fingerprint=request.fingerprint, policy_revision=_APPLY_POLICY_REVISION,
                decision=ApprovalDecision.APPROVED, decided_at=datetime.now(UTC),
            )
            FileApprovalStore(self._store).save(approval)
            artifacts.write_bytes(task_id, run.run_id, producer="code_apply", relative_path="artifacts/apply-request.json",
                content=json.dumps({"base_sha": workspace.base_sha, "proposal_run_id": proposal_run_id,
                    "proposal_hash": proposal["proposal_hash"], "repository_alias": proposal["repository_alias"],
                    "approval_id": approval.approval_id, "target_fingerprint": request.fingerprint,
                    "policy_revision": _APPLY_POLICY_REVISION}, sort_keys=True).encode(),
                input_fingerprint=binding, operation_id=run.operation_id)
            GitRunner().run(["git", "apply", "--check", "--whitespace=nowarn", "--", str(patch_path)], cwd=workspace.path)
            journal.append("stage.progress", {"message": "proposal verified; applying to isolated worktree"})
            result = ToolRegistry(policy, LocalCommandRunner(artifacts, journal), ApprovalGate(FileApprovalStore(self._store))).execute(
                self._project_root, request, approval_id=approval.approval_id, confirm_token=request.fingerprint)
            if result.status.value != "succeeded":
                raise DomainError("GIT_APPLY_FAILED", "approved patch could not be applied")
            actual_diff = _git_output(GitRunner(), ["git", "diff", "--binary", "--no-ext-diff", "--no-textconv", "--"], workspace.path)
            expected_diff = "".join(patch.unified_diff for patch in parsed_proposal.patches)
            if not parent_diff and _canonical_diff(actual_diff) != _canonical_diff(expected_diff):
                raise DomainError("APPLY_DIFF_MISMATCH", "actual worktree diff differs from the approved proposal")
            paths = _git_output(GitRunner(), ["git", "diff", "--name-only", "--"], workspace.path).splitlines()
            artifacts.write_bytes(task_id, run.run_id, producer="code_apply", relative_path="artifacts/applied.diff",
                content=actual_diff.encode(), input_fingerprint=binding, operation_id=run.operation_id)
            artifacts.write_bytes(task_id, run.run_id, producer="code_apply", relative_path=_APPLY_EVIDENCE_PATH,
                content=json.dumps({"base_sha": workspace.base_sha, "head_sha": _git_output(GitRunner(), ["git", "rev-parse", "HEAD"], workspace.path).strip(),
                    "proposal_run_id": proposal_run_id, "proposal_hash": proposal["proposal_hash"], "changed_paths": paths,
                    "tool_run_id": result.tool_run_id, "approval_id": approval.approval_id,
                    "parent_diff_hash": proposal.get("parent_diff_hash"),
                    "root_proposal_run_id": proposal.get("root_proposal_run_id", proposal_run_id)}, sort_keys=True).encode(),
                input_fingerprint=binding, operation_id=run.operation_id)
            self._store.set_run_status(run.run_id, "succeeded")
            journal.append("run.succeeded", {"proposal_run_id": proposal_run_id})
        except DomainError as error:
            if self._store.get_run(task_id, run.run_id).status.value == "running":
                self._store.set_run_status(run.run_id, "failed", error_code=error.code)
                journal.append("run.failed", {"error_code": error.code})
        return _apply_result(self._store.get_run(task_id, run.run_id), proposal_run_id)

    def _is_proposal(self, task_id: str, run_id: str) -> bool:
        try:
            self._proposal_details(task_id, run_id)
        except DomainError:
            return False
        return True

    def _proposal_details(self, task_id: str, run_id: str) -> dict[str, object]:
        run = self._store.get_run(task_id, run_id)
        if run.stage not in {"code_generate", "code_repair"} or run.status.value != "succeeded":
            raise DomainError("PATCH_PROPOSAL_NOT_FOUND", "proposal Run is not a successful code-generation Run")
        artifacts = FileArtifactStore(self._store)
        records = [record for record in artifacts.list(task_id, run_id) if record.relative_path == _PROPOSAL_PATH]
        if len(records) != 1:
            raise DomainError("PATCH_PROPOSAL_NOT_FOUND", "proposal evidence is missing")
        record = artifacts.verify(task_id, run_id, records[0].artifact_id)
        path = self._store.event_journal_path(run_id).parent / record.relative_path
        try:
            proposal = PatchProposal.model_validate_json(path.read_bytes())
            payload = json.loads(self._store.get_operation(task_id, run.operation_id).request_json)["payload"]
            if run.stage == "code_repair":
                chain = _repair_chain(self._store, task_id, run_id)
                alias = str(chain["repository_alias"])
                base_sha = str(chain["base_sha"])
            else:
                alias = str(payload["alias"])
                base_sha = WorkspaceService(self._project_root, GitRunner()).prepare(task_id, alias, run_id).base_sha
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise DomainError("PATCH_PROPOSAL_INVALID", "proposal evidence is invalid") from error
        result = {"proposal_run_id": run_id, "proposal": proposal, "proposal_hash": record.sha256,
                "proposal_path": str(path), "repository_alias": alias, "base_sha": base_sha,
                "summary": proposal.summary, "paths": [patch.path for patch in proposal.patches],
                "model_selector": str(payload.get("model_selector", "unknown")),
                "model_revision": payload.get("provider_revision", "unknown")}
        if run.stage == "code_repair":
            result.update({"parent_diff": str(chain["parent_diff"]), "parent_diff_hash": str(chain["parent_diff_hash"]),
                "root_proposal_run_id": str(chain["root_proposal_run_id"])})
        return result

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


def _apply_result(run: RunRecord, proposal_run_id: str) -> CodeApplyResult:
    return CodeApplyResult(
        task_id=run.task_id, operation_id=run.operation_id, run_id=run.run_id,
        proposal_run_id=proposal_run_id, status=run.status.value, error_code=run.error_code,
    )


def _git_output(git: GitRunner, argv: list[str], cwd: Path) -> str:
    return git.run(argv, cwd=cwd).stdout


def _canonical_diff(value: str) -> str:
    """Compare proposal and Git diff content without Git's descriptive preamble."""
    return "\n".join(
        line for line in value.replace("\r\n", "\n").split("\n")
        if not line.startswith(("diff --git ", "index "))
    ).strip() + "\n"


def _apply_diff(git: GitRunner, workspace: Path, diff: str) -> None:
    """Replay verified parent evidence only inside a new isolated worktree."""
    patch = workspace / ".yipilot-parent.diff"
    try:
        patch.write_text(diff, encoding="utf-8", newline="\n")
        git.run(["git", "apply", "--check", "--whitespace=nowarn", "--", str(patch)], cwd=workspace)
        git.run(["git", "apply", "--whitespace=nowarn", "--", str(patch)], cwd=workspace)
    finally:
        patch.unlink(missing_ok=True)


def _repair_chain(store: FileTaskStore, task_id: str, run_id: str) -> dict[str, object]:
    artifacts = FileArtifactStore(store)
    record = next((item for item in artifacts.list(task_id, run_id)
                   if item.relative_path == "artifacts/repair-chain.json"), None)
    if record is None:
        raise DomainError("REPAIR_CHAIN_MISSING", "repair proposal chain evidence is missing")
    artifacts.verify(task_id, run_id, record.artifact_id)
    try:
        value = json.loads((store.event_journal_path(run_id).parent / record.relative_path).read_text(encoding="utf-8"))
        required = ("repository_alias", "base_sha", "parent_diff", "parent_diff_hash", "root_proposal_run_id")
        if not all(isinstance(value.get(key), str) and value[key] for key in required):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError) as error:
        raise DomainError("REPAIR_CHAIN_INVALID", "repair proposal chain evidence is invalid") from error


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
