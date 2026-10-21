"""Bounded, proposal-only repairs driven by failed OpenSandbox verification."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from yipilot.agent.model_planner import ModelPlanner
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ExecutionPolicy
from yipilot.domain.model import ModelRequest, model_request_fingerprint
from yipilot.domain.models import RunStatus
from yipilot.models.selection import ModelSelectionService
from yipilot.persistence.artifact_store import FileArtifactStore, artifact_reference
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.ownership import exclusive_execution
from yipilot.persistence.runtime_evidence import FileRuntimeEvidence
from yipilot.persistence.task_store import FileTaskStore, RunRecord
from yipilot.workflow.run_executor import RunExecutor
from yipilot.workflow.sandbox_application import SandboxCliService, apply_policy
from yipilot.workflow.sandbox_verification import SandboxVerificationService
from yipilot.workspace.sandbox_output import SandboxOutputService
from yipilot.workspace.sandbox_sources import PreparedSources, scan_source_tree
from yipilot.workspace.tool_gateway import RegistryToolGateway
from yipilot.workspace.tool_registry import ToolRegistry

MAX_REPAIR_ATTEMPTS = 2
MAX_REPAIR_CHAIN_SECONDS = 15 * 60
MAX_REPAIR_CONTEXT_BYTES = 24 * 1024
_CONTEXT_PATH = "artifacts/repair-context.md"
_CHAIN_PATH = "artifacts/repair-chain.json"


@dataclass(frozen=True)
class RepairResult:
    run_id: str
    status: str
    error_code: str | None


class RepairService:
    """Create one repair proposal from a failed, evidenced OpenSandbox check."""

    def __init__(
        self,
        project_root: Path,
        *,
        settings_dir: Path | None = None,
        model_selection_service: ModelSelectionService | None = None,
        sandbox: SandboxCliService | None = None,
        verification: SandboxVerificationService | None = None,
        output: SandboxOutputService | None = None,
    ) -> None:
        self._project_root = Path(project_root).resolve(strict=True)
        self._sandbox = sandbox or SandboxCliService(self._project_root)
        self._store = self._sandbox.store
        self._verification = verification or SandboxVerificationService(self._project_root)
        self._output = output or SandboxOutputService(self._project_root, self._sandbox.git)
        self._models = model_selection_service or ModelSelectionService(settings_dir)

    @exclusive_execution
    def repair(
        self,
        task_id: str,
        verification_run_id: str,
        *,
        model_selector: str | None = None,
        request_id: str | None = None,
    ) -> RepairResult:
        verification = self._store.get_run(task_id, verification_run_id)
        details, copies, diagnostics = self._verification_details(task_id, verification)
        key = f"repair:{task_id}:{verification_run_id}:{request_id or verification_run_id}"
        previous = self._store.find_operation(task_id, key)
        if previous:
            payload = json.loads(previous.request_json)["payload"]
            if model_selector is not None and model_selector not in {payload["model_selector"], payload.get("requested_model_selector")}:
                raise DomainError("OPERATION_CONFLICT", "request model changed")
            run, _ = self._store.get_or_create_run(task_id, previous.operation_id, "code_repair")
            return RepairResult(run.run_id, run.status.value, run.error_code)
        attempt = self._attempts(task_id, details["root_proposal_run_id"])
        if attempt >= MAX_REPAIR_ATTEMPTS:
            raise DomainError("REPAIR_ATTEMPT_LIMIT", "repair attempt limit has been reached")
        if self._expired(self._chain_started(task_id, details["root_proposal_run_id"], verification.created_at)):
            raise DomainError("REPAIR_TIME_LIMIT", "repair chain time limit has been reached")
        resolved = self._models.resolve(explicit_selector=model_selector)
        port = self._models.model_port(resolved)
        operation = self._store.create_operation(
            task_id,
            "code_repair",
            key,
            {
                "verification_run_id": verification_run_id,
                "apply_run_id": details["apply_run_id"],
                "root_proposal_run_id": details["root_proposal_run_id"],
                "model_selector": resolved.selector,
                "requested_model_selector": model_selector,
                "provider_revision": resolved.connection_revision,
            },
        )
        run, created = self._store.get_or_create_run(task_id, operation.operation_id, "code_repair")
        if not created:
            close = getattr(port, "close", None)
            if callable(close):
                close()
            return RepairResult(run.run_id, run.status.value, run.error_code)
        artifacts = FileArtifactStore(self._store)
        journal = _journal(self._store, run)
        try:
            task = self._store.get_task(task_id)
            context = self._context(task.development_goal, details["patch"], diagnostics, copies)
            context_ref = artifacts.write_bytes(
                task_id,
                run.run_id,
                producer="code_repair",
                relative_path=_CONTEXT_PATH,
                content=context.encode(),
                input_fingerprint=_hash(context),
                operation_id=run.operation_id,
            )
            chain = {
                "schema_version": 2,
                "verification_run_id": verification_run_id,
                "apply_run_id": details["apply_run_id"],
                "root_proposal_run_id": details["root_proposal_run_id"],
                "repository_alias": details["repository_alias"],
                "base_sha": details["base_sha"],
                "parent_diff_hash": details["patch_sha256"],
                "attempt": attempt + 1,
                "maximum_attempts": MAX_REPAIR_ATTEMPTS,
                "maximum_chain_seconds": MAX_REPAIR_CHAIN_SECONDS,
                "maximum_context_bytes": MAX_REPAIR_CONTEXT_BYTES,
                "model_selector": resolved.selector,
                "requested_model_selector": model_selector,
                "model_revision": resolved.connection_revision,
                "diagnostic_source_run": verification_run_id,
            }
            artifacts.write_bytes(
                task_id,
                run.run_id,
                producer="code_repair",
                relative_path=_CHAIN_PATH,
                content=json.dumps(chain, sort_keys=True).encode(),
                input_fingerprint=_hash(json.dumps(chain, sort_keys=True)),
                operation_id=run.operation_id,
            )
            request = ModelRequest(
                task_id=task_id,
                operation_id=run.operation_id,
                run_id=run.run_id,
                provider_profile_name=resolved.connection_id,
                provider_type=resolved.provider_type,
                model=resolved.model_id,
                profile_revision=resolved.connection_revision,
                timeout_seconds=resolved.timeout_seconds,
                output_budget_bytes=min(resolved.output_limit, 64 * 1024),
                request_options=resolved.request_options,
                allowed_input_artifact_refs=(artifact_reference(context_ref),),
                input_fingerprint=model_request_fingerprint(context, (artifact_reference(context_ref),)),
                user_instruction=context,
            )
            evidence = FileRuntimeEvidence(artifacts, journal)
            RunExecutor(
                self._store,
                ModelPlanner(port, request, evidence),
                RegistryToolGateway(
                    self._project_root,
                    ToolRegistry(ExecutionPolicy(revision="codeagent-repair-v2")),
                ),
                evidence,
            ).execute(task_id, run.operation_id, run.run_id, task.development_goal, harness=lambda: True)
        except DomainError as error:
            current = self._store.get_run(task_id, run.run_id)
            if current.status is RunStatus.PENDING:
                self._store.set_run_status(run.run_id, RunStatus.RUNNING)
                journal.append("run.started", {})
                journal.append("turn.started", {"turn": 1})
                current = self._store.get_run(task_id, run.run_id)
            if current.status is RunStatus.RUNNING:
                journal.append("turn.failed", {"turn": 1, "error_code": error.code})
                self._store.set_run_status(run.run_id, RunStatus.FAILED, error_code=error.code)
                journal.append("run.failed", {"error_code": error.code})
        finally:
            close = getattr(port, "close", None)
            if callable(close):
                close()
        current = self._store.get_run(task_id, run.run_id)
        return RepairResult(current.run_id, current.status.value, current.error_code)

    def _verification_details(
        self, task_id: str, verification: RunRecord
    ) -> tuple[dict[str, str], PreparedSources, str]:
        if verification.stage != "sandbox_verification" or verification.status is not RunStatus.FAILED:
            raise DomainError("REPAIR_VERIFICATION_INVALID", "verification Run is not a failed repair candidate")
        payload = json.loads(self._store.get_operation(task_id, verification.operation_id).request_json)["payload"]
        apply_run_id = payload.get("apply_run_id")
        if not isinstance(apply_run_id, str) or not apply_run_id:
            raise DomainError("REPAIR_EVIDENCE_DRIFT", "verification has no applied OpenSandbox input")
        copies, request, evidence = self._sandbox.accepted(task_id, apply_run_id)
        output = self._output.inspect(copies, request, apply_policy())
        if output.patch_sha256 != evidence.patch_sha256:
            raise DomainError("REPAIR_EVIDENCE_DRIFT", "applied OpenSandbox diff changed")
        diagnostics = self._verification.failed_diagnostics(
            copies, request, apply_policy(), evidence, verification.run_id
        )
        apply_payload = json.loads(self._store.get_operation(task_id, request.operation_id).request_json)["payload"]
        root = apply_payload.get("root_proposal_run_id") or apply_payload.get("proposal_run_id") or apply_run_id
        if not isinstance(root, str) or not root:
            raise DomainError("REPAIR_EVIDENCE_DRIFT", "repair chain root is invalid")
        return ({
            "apply_run_id": apply_run_id,
            "root_proposal_run_id": root,
            "repository_alias": copies.manifest.repository_alias,
            "base_sha": copies.manifest.base_sha,
            "patch": output.patch.decode("utf-8"),
            "patch_sha256": output.patch_sha256,
        }, copies, diagnostics)

    def _context(self, goal: str, patch: str, diagnostics: str, copies: PreparedSources) -> str:
        before = {file.path: file for file in copies.manifest.files}
        after, contents = scan_source_tree(copies.execution, copies.manifest.files)
        current = {file.path: file for file in after}
        changed = sorted(path for path in before.keys() | current.keys() if before.get(path) != current.get(path))
        files = []
        for path in changed[:16]:
            raw = contents.get(path)
            if raw is not None:
                files.append(f"### {path}\n{raw[:4096].decode('utf-8', errors='replace')}")
        value = (
            "# Bounded OpenSandbox repair proposal\n\n"
            "Generate only a JSON PatchProposal relative to the base source. Preserve the approved change while fixing the failed check. Do not use shell commands.\n\n"
            f"## Development goal\n{goal}\n\n## Approved OpenSandbox patch\n{patch}\n\n"
            f"## Failed fixed OpenSandbox verification diagnostics\n{diagnostics}\n\n"
            "## Current changed source\n" + "\n\n".join(files)
        )
        if len(value.encode()) > MAX_REPAIR_CONTEXT_BYTES:
            raise DomainError("MODEL_CONTEXT_LIMIT_EXCEEDED", "repair context exceeds its byte limit")
        return value

    def _attempts(self, task_id: str, root_proposal_run_id: str) -> int:
        count = 0
        for run in self._store.list_runs(task_id):
            if run.stage != "code_repair":
                continue
            try:
                if self._chain(task_id, run.run_id).get("root_proposal_run_id") == root_proposal_run_id:
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
                if self._chain(task_id, run.run_id).get("root_proposal_run_id") == root_proposal_run_id:
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


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _journal(store: FileTaskStore, run: RunRecord) -> JsonlEventJournal:
    return JsonlEventJournal(store.event_journal_path(run.run_id), task_id=run.task_id, run_id=run.run_id, stage=run.stage)
