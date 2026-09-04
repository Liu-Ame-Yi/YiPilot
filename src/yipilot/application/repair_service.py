"""Bounded, proposal-only repair Runs for failed change verification."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from yipilot.application.agent_runtime import AgentRuntime
from yipilot.application.code_service import CodeService, _canonical_diff, _ref
from yipilot.application.model_planner import ModelPlanner
from yipilot.application.model_selection_service import ModelSelectionService
from yipilot.application.runtime_adapters import RegistryToolGateway
from yipilot.application.tool_registry import ToolRegistry
from yipilot.application.verification_service import VerificationService
from yipilot.application.workspace_service import WorkspaceService
from yipilot.domain.errors import DomainError
from yipilot.domain.model import ModelRequest, model_request_fingerprint
from yipilot.domain.models import RunStatus
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.runtime_evidence import FileRuntimeEvidence
from yipilot.infrastructure.task_store import FileTaskStore, RunRecord
from yipilot.infrastructure.tool_run_store import FileToolRunStore

MAX_REPAIR_ATTEMPTS = 2
MAX_REPAIR_CHAIN_SECONDS = 15 * 60
MAX_REPAIR_CONTEXT_BYTES = 24 * 1024
_CONTEXT_PATH = "artifacts/repair-context.md"
_CHAIN_PATH = "artifacts/repair-chain.json"
_PROPOSAL_PATH = "artifacts/patch-proposal.json"


@dataclass(frozen=True)
class RepairCandidate:
    verification_run_id: str
    apply_run_id: str
    proposal_run_id: str
    root_proposal_run_id: str
    repository_alias: str
    base_sha: str
    parent_diff: str
    parent_diff_hash: str
    attempt: int
    title: str


@dataclass(frozen=True)
class RepairResult:
    run_id: str
    status: str
    error_code: str | None


class RepairService:
    """Create one auditable repair proposal; it never applies source changes."""

    def __init__(self, project_root: Path, *, settings_dir: Path | None = None,
                 model_selection_service: ModelSelectionService | None = None) -> None:
        self._project_root = Path(project_root).resolve(strict=True)
        self._store = FileTaskStore(self._project_root)
        self._models = model_selection_service or ModelSelectionService(settings_dir)
        self._git = GitRunner()

    def candidates(self, task_id: str) -> list[RepairCandidate]:
        task = self._store.get_task(task_id)
        values: list[RepairCandidate] = []
        for run in self._store.list_runs(task_id):
            if run.stage != "change_verification" or run.status is not RunStatus.FAILED:
                continue
            try:
                details = self._verification_details(task_id, run)
                attempt = self._attempts(task_id, details["root_proposal_run_id"])
                if attempt >= MAX_REPAIR_ATTEMPTS or self._expired(
                    self._chain_started(task_id, details["root_proposal_run_id"], run.created_at)
                ):
                    continue
                values.append(RepairCandidate(run.run_id, details["apply_run_id"], details["proposal_run_id"],
                    details["root_proposal_run_id"], details["repository_alias"], details["base_sha"],
                    details["diff"], details["diff_hash"], attempt, task.title or "未命名任务"))
            except DomainError:
                continue
        return values

    def repair(self, task_id: str, verification_run_id: str, *, model_selector: str | None = None,
               request_id: str | None = None) -> RepairResult:
        verification = self._store.get_run(task_id, verification_run_id)
        details = self._verification_details(task_id, verification)
        attempt = self._attempts(task_id, details["root_proposal_run_id"])
        if attempt >= MAX_REPAIR_ATTEMPTS:
            raise DomainError("REPAIR_ATTEMPT_LIMIT", "repair attempt limit has been reached")
        if self._expired(self._chain_started(task_id, details["root_proposal_run_id"], verification.created_at)):
            raise DomainError("REPAIR_TIME_LIMIT", "repair chain time limit has been reached")
        resolved = self._models.resolve(explicit_selector=model_selector)
        port = self._models.model_port(resolved)
        key = request_id or str(uuid4())
        operation = self._store.create_operation(task_id, "code_repair",
            f"repair:{task_id}:{verification_run_id}:{key}",
            {"verification_run_id": verification_run_id, "apply_run_id": details["apply_run_id"],
             "root_proposal_run_id": details["root_proposal_run_id"], "attempt": attempt + 1,
             "model_selector": resolved.selector, "provider_revision": resolved.connection_revision})
        run, created = self._store.get_or_create_run(task_id, operation.operation_id, "code_repair")
        if not created:
            return RepairResult(run.run_id, run.status.value, run.error_code)
        artifacts = FileArtifactStore(self._store)
        journal = _journal(self._store, run)
        try:
            workspace = WorkspaceService(self._project_root, self._git).prepare(
                task_id, details["repository_alias"], run.run_id, base_sha=details["base_sha"])
            self._apply_diff(workspace.path, details["parent_diff"])
            actual = _git(self._git, ["git", "diff", "--binary", "--no-ext-diff", "--no-textconv", "--"], workspace.path)
            if _canonical_diff(actual) != _canonical_diff(details["parent_diff"]):
                raise DomainError("REPAIR_PARENT_DIFF_MISMATCH", "reconstructed change chain does not match verification")
            diagnostics = self._diagnostics(task_id, verification_run_id)
            task = self._store.get_task(task_id)
            context = self._context(task_id, task.development_goal, details, diagnostics, workspace.path)
            context_ref = artifacts.write_bytes(task_id, run.run_id, producer="code_repair",
                relative_path=_CONTEXT_PATH, content=context.encode(), input_fingerprint=_hash(context), operation_id=run.operation_id)
            chain = {"schema_version": 1, "verification_run_id": verification_run_id,
                "apply_run_id": details["apply_run_id"], "proposal_run_id": details["proposal_run_id"],
                "root_proposal_run_id": details["root_proposal_run_id"], "repository_alias": details["repository_alias"],
                "base_sha": details["base_sha"], "parent_diff_hash": details["diff_hash"],
                "parent_diff": details["parent_diff"], "attempt": attempt + 1, "maximum_attempts": MAX_REPAIR_ATTEMPTS,
                "maximum_chain_seconds": MAX_REPAIR_CHAIN_SECONDS, "maximum_context_bytes": MAX_REPAIR_CONTEXT_BYTES,
                "model_selector": resolved.selector, "model_revision": resolved.connection_revision,
                "diagnostic_source_run": verification_run_id}
            artifacts.write_bytes(task_id, run.run_id, producer="code_repair", relative_path=_CHAIN_PATH,
                content=json.dumps(chain, sort_keys=True).encode(), input_fingerprint=_hash(json.dumps(chain, sort_keys=True)), operation_id=run.operation_id)
            request = ModelRequest(task_id=task_id, operation_id=run.operation_id, run_id=run.run_id,
                provider_profile_name=resolved.connection_id, provider_type=resolved.provider_type, model=resolved.model_id,
                profile_revision=resolved.connection_revision, timeout_seconds=resolved.timeout_seconds,
                output_budget_bytes=min(resolved.output_limit, 64 * 1024), request_options=resolved.request_options,
                allowed_input_artifact_refs=(_ref(context_ref),),
                input_fingerprint=model_request_fingerprint(context, (_ref(context_ref),)), user_instruction=context)
            evidence = FileRuntimeEvidence(artifacts, journal)
            AgentRuntime(self._store, ModelPlanner(port, request, evidence),
                RegistryToolGateway(self._project_root, ToolRegistry(__import__("yipilot.domain.execution", fromlist=["ExecutionPolicy"]).ExecutionPolicy(revision="codeagent-repair-v1"))),
                evidence).execute(task_id, run.operation_id, run.run_id, task.development_goal, harness=lambda: True)
        except DomainError as error:
            current = self._store.get_run(task_id, run.run_id)
            if current.status is RunStatus.PENDING:
                self._store.set_run_status(run.run_id, RunStatus.RUNNING)
                journal.append("run.started", {})
                journal.append("turn.started", {"turn": 1})
                current = self._store.get_run(task_id, run.run_id)
            if current.status is RunStatus.RUNNING:
                self._store.set_run_status(run.run_id, RunStatus.FAILED, error_code=error.code)
                journal.append("turn.failed", {"turn": 1, "error_code": error.code})
                journal.append("run.failed", {"error_code": error.code})
        finally:
            close = getattr(port, "close", None)
            if callable(close):
                close()
        current = self._store.get_run(task_id, run.run_id)
        return RepairResult(current.run_id, current.status.value, current.error_code)

    def latest_status(self, task_id: str) -> str | None:
        repairs = [run for run in self._store.list_runs(task_id) if run.stage == "code_repair"]
        if not repairs:
            return None
        run = repairs[-1]
        try:
            chain = self._chain(task_id, run.run_id)
            attempt = cast(int, chain["attempt"])
            return f"最近 Repair Run：{run.status.value} · {run.run_id} · 修复次数：{attempt} / {MAX_REPAIR_ATTEMPTS} · 剩余：{MAX_REPAIR_ATTEMPTS - attempt}"
        except DomainError:
            return f"最近 Repair Run：{run.status.value} · {run.run_id}"

    def _verification_details(self, task_id: str, verification: RunRecord) -> dict[str, str]:
        if verification.stage != "change_verification" or verification.status is not RunStatus.FAILED:
            raise DomainError("REPAIR_VERIFICATION_INVALID", "verification Run is not a failed repair candidate")
        service = VerificationService(self._project_root)
        context = service._context(task_id, verification.run_id)
        details = service._apply_details(task_id, context["apply_run_id"])
        if any(context[key] != details[key] for key in ("proposal_run_id", "proposal_hash", "repository_alias", "base_sha", "diff_hash")):
            raise DomainError("REPAIR_EVIDENCE_DRIFT", "verification evidence does not match applied change")
        tools = FileToolRunStore(self._store).list(task_id, verification.run_id)
        ids = {
            "verification-pytest" if item.argv[1:] == ("-m", "pytest")
            else "verification-ruff" if item.argv == ("ruff", "check", "src", "tests")
            else "verification-mypy" if item.argv == ("mypy", "src") else "invalid"
            for item in tools
        }
        if not ids.intersection({"verification-pytest", "verification-ruff", "verification-mypy"}) or not any(item.status.value != "succeeded" for item in tools):
            raise DomainError("REPAIR_DIAGNOSTICS_MISSING", "failed fixed verification diagnostics are incomplete")
        root = details.get("root_proposal_run_id", details["proposal_run_id"])
        return {**details, "root_proposal_run_id": root, "parent_diff": details["diff"]}

    def _diagnostics(self, task_id: str, run_id: str) -> str:
        artifacts = FileArtifactStore(self._store)
        values: list[str] = []
        for tool in FileToolRunStore(self._store).list(task_id, run_id):
            if tool.status.value == "succeeded":
                continue
            for ref in (tool.stdout_artifact_ref, tool.stderr_artifact_ref):
                record = artifacts.verify(task_id, run_id, ref.artifact_id)
                path = self._store.event_journal_path(run_id).parent / record.relative_path
                values.append(path.read_text(encoding="utf-8", errors="replace")[:4096])
        value = "\n".join(values)[:8192]
        if not value:
            raise DomainError("REPAIR_DIAGNOSTICS_MISSING", "failed verification output is missing")
        return value

    def _context(self, task_id: str, goal: str, details: dict[str, str], diagnostics: str, workspace: Path) -> str:
        documents = "\n\n".join(content for _, content in CodeService(
            self._project_root, model_selection_service=self._models
        )._approved_documents(task_id))
        paths = _git(self._git, ["git", "diff", "--name-only", "--"], workspace).splitlines()[:16]
        files: list[str] = []
        remaining = MAX_REPAIR_CONTEXT_BYTES - len(goal.encode()) - len(diagnostics.encode()) - 1024
        for path in paths:
            candidate = workspace / path
            if candidate.is_file() and remaining > 0:
                content = candidate.read_text(encoding="utf-8", errors="replace")[:4096]
                remaining -= len(content.encode())
                files.append(f"### {path}\n{content}")
        value = ("# Bounded repair proposal\n\n## Development goal\n" + goal + "\n\n"
            "Generate only a JSON PatchProposal relative to the current applied state. Do not use shell commands.\n\n"
            "## Approved task documents\n" + documents + "\n\n## Current change\n" + details["diff_hash"] + "\n\n## Failed fixed verification diagnostics\n" + diagnostics
            + "\n\n## Allowed current worktree context\n" + "\n\n".join(files))
        if len(value.encode()) > MAX_REPAIR_CONTEXT_BYTES:
            value = value.encode()[:MAX_REPAIR_CONTEXT_BYTES].decode("utf-8", errors="ignore")
        return value

    def _attempts(self, task_id: str, root_proposal_run_id: str) -> int:
        count = 0
        for run in self._store.list_runs(task_id):
            if run.stage != "code_repair":
                continue
            try:
                if self._chain(task_id, run.run_id)["root_proposal_run_id"] == root_proposal_run_id:
                    count += 1
            except DomainError:
                continue
        return count

    def _chain_started(self, task_id: str, root_proposal_run_id: str, fallback: str) -> str:
        values = [fallback]
        for run in self._store.list_runs(task_id):
            if run.stage != "code_repair":
                continue
            try:
                if self._chain(task_id, run.run_id)["root_proposal_run_id"] == root_proposal_run_id:
                    values.append(run.created_at)
            except DomainError:
                continue
        return min(values)

    def _chain(self, task_id: str, run_id: str) -> dict[str, object]:
        artifacts = FileArtifactStore(self._store)
        record = next((item for item in artifacts.list(task_id, run_id) if item.relative_path == _CHAIN_PATH), None)
        if record is None:
            raise DomainError("REPAIR_CHAIN_MISSING", "repair chain evidence is missing")
        artifacts.verify(task_id, run_id, record.artifact_id)
        try:
            return json.loads((self._store.event_journal_path(run_id).parent / _CHAIN_PATH).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise DomainError("REPAIR_CHAIN_INVALID", "repair chain evidence is invalid") from error

    @staticmethod
    def _expired(created_at: str) -> bool:
        try:
            return datetime.now(UTC) - datetime.fromisoformat(created_at) > timedelta(seconds=MAX_REPAIR_CHAIN_SECONDS)
        except ValueError as error:
            raise DomainError("REPAIR_TIME_EVIDENCE_INVALID", "repair chain timestamp is invalid") from error

    def _apply_diff(self, workspace: Path, diff: str) -> None:
        if not diff.strip():
            return
        patch = workspace / ".yipilot-repair-parent.diff"
        try:
            patch.write_text(diff, encoding="utf-8", newline="\n")
            self._git.run(["git", "apply", "--check", "--whitespace=nowarn", "--", str(patch)], cwd=workspace)
            self._git.run(["git", "apply", "--whitespace=nowarn", "--", str(patch)], cwd=workspace)
        finally:
            patch.unlink(missing_ok=True)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _git(git: GitRunner, argv: list[str], cwd: Path) -> str:
    return git.run(argv, cwd=cwd).stdout


def _journal(store: FileTaskStore, run: RunRecord) -> JsonlEventJournal:
    return JsonlEventJournal(store.event_journal_path(run.run_id), task_id=run.task_id, run_id=run.run_id, stage=run.stage)
