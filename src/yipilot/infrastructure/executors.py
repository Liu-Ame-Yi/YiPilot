"""Deterministic and host-trusted executors for policy-approved local tools."""

import subprocess
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import BinaryIO
from uuid import uuid4

from yipilot.application.approval import ApprovalGate, requires_approval
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    ArtifactRef,
    ExecutionTrustLevel,
    ToolChangeSummary,
    ToolKind,
    ToolPolicy,
    ToolRequest,
    ToolResult,
    ToolRunStatus,
    is_generic_python_executable,
    is_quality_pytest_request,
)
from yipilot.infrastructure.artifact_store import ArtifactRecord, FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.filesystem import atomic_write_bytes, project_path
from yipilot.infrastructure.tool_run_store import FileToolRunStore

_CHUNK_SIZE = 64 * 1024
_OUTPUT_LIMIT_DIAGNOSTIC = b"YIPILOT_OUTPUT_LIMIT_EXCEEDED\n"


class FakeExecutor:
    """Return test-provided output without creating a subprocess."""

    trust_level = ExecutionTrustLevel.FAKE

    def __init__(
        self,
        artifact_store: FileArtifactStore,
        journal: JsonlEventJournal,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int | None = 0,
        status: ToolRunStatus | None = None,
    ) -> None:
        self._artifact_store = artifact_store
        self._tool_runs = FileToolRunStore(artifact_store.task_store)
        self._journal = journal
        self._stdout = stdout
        self._stderr = stderr
        self._exit_code = exit_code
        self._status = status

    def execute(
        self,
        request: ToolRequest,
        tool: ToolPolicy,
        *,
        cwd: Path,
        project_root: Path | None = None,
        environment: Mapping[str, str] | None = None,
        cancel_event: Event | None = None,
        approval_gate: ApprovalGate | None = None,
        policy_revision: str | None = None,
        approval_id: str | None = None,
        confirm_token: str | None = None,
    ) -> ToolResult:
        del environment
        _require_workspace_cwd(project_root, request, cwd)
        _require_approval(
            request,
            tool,
            self.trust_level,
            approval_gate,
            policy_revision,
            approval_id,
            confirm_token,
        )
        started_at = datetime.now(UTC)
        tool_run_id = str(uuid4())
        _started(self._journal, tool_run_id, request.tool_id)
        status = self._status or (
            ToolRunStatus.CANCELLED
            if cancel_event is not None and cancel_event.is_set()
            else ToolRunStatus.SUCCEEDED
            if self._exit_code == 0
            else ToolRunStatus.FAILED
        )
        stdout, stderr, exceeded = _limit_output(
            self._stdout, self._stderr, request.output_budget_bytes
        )
        if exceeded:
            status = ToolRunStatus.FAILED
            stderr += _OUTPUT_LIMIT_DIAGNOSTIC
        result = _result(
            self._artifact_store,
            tool_run_id,
            request,
            started_at,
            status,
            self._exit_code,
            stdout,
            stderr,
            ExecutionTrustLevel.FAKE,
        )
        self._tool_runs.save(result)
        _completed(self._journal, result, request.tool_id)
        return result


class LocalCommandRunner:
    """Run one already-approved argv with bounded stream collection."""

    trust_level = ExecutionTrustLevel.HOST_TRUSTED

    def __init__(
        self, artifact_store: FileArtifactStore, journal: JsonlEventJournal
    ) -> None:
        self._artifact_store = artifact_store
        self._tool_runs = FileToolRunStore(artifact_store.task_store)
        self._journal = journal

    def execute(
        self,
        request: ToolRequest,
        tool: ToolPolicy,
        *,
        cwd: Path,
        project_root: Path | None = None,
        environment: Mapping[str, str] | None = None,
        cancel_event: Event | None = None,
        approval_gate: ApprovalGate | None = None,
        policy_revision: str | None = None,
        approval_id: str | None = None,
        confirm_token: str | None = None,
    ) -> ToolResult:
        tool.validate_request(request)
        _require_workspace_cwd(project_root, request, cwd)
        if is_generic_python_executable(
            tool.executable
        ) and not is_quality_pytest_request(tool, request, policy_revision):
            raise DomainError(
                "TOOL_EXECUTABLE_DENIED",
                "generic Python interpreters are only allowed for registered test tools",
            )
        _require_approval(
            request,
            tool,
            self.trust_level,
            approval_gate,
            policy_revision,
            approval_id,
            confirm_token,
        )
        environment = environment or {}
        missing = set(request.env_names).difference(environment)
        if missing:
            raise DomainError(
                "TOOL_ENV_VALUE_MISSING", "approved environment value is missing"
            )
        env = {name: environment[name] for name in request.env_names}
        started_at = datetime.now(UTC)
        tool_run_id = str(uuid4())
        process = subprocess.Popen(
            list(request.argv),
            cwd=str(cwd),
            env=env,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _started(self._journal, tool_run_id, request.tool_id)
        stdout, stderr, exceeded, readers = _read_streams(
            process, request.output_budget_bytes
        )
        status = _wait_for_process(process, request.timeout_seconds, cancel_event)
        for reader in readers:
            reader.join()
        if exceeded.is_set():
            _terminate(process)
            status = ToolRunStatus.FAILED
            stderr += _OUTPUT_LIMIT_DIAGNOSTIC
        exit_code = process.returncode
        if status is ToolRunStatus.SUCCEEDED and exit_code != 0:
            status = ToolRunStatus.FAILED
        result = _result(
            self._artifact_store,
            tool_run_id,
            request,
            started_at,
            status,
            exit_code,
            stdout,
            stderr,
            ExecutionTrustLevel.HOST_TRUSTED,
        )
        self._tool_runs.save(result)
        _completed(self._journal, result, request.tool_id)
        return result


class ControlledWorkspaceWriter:
    """Perform the one fixed Task 11 workspace write without a shell."""

    trust_level = ExecutionTrustLevel.SANDBOXED

    def __init__(
        self, artifact_store: FileArtifactStore, journal: JsonlEventJournal
    ) -> None:
        self._artifact_store = artifact_store
        self._tool_runs = FileToolRunStore(artifact_store.task_store)
        self._journal = journal

    def execute(
        self,
        request: ToolRequest,
        tool: ToolPolicy,
        *,
        cwd: Path,
        project_root: Path | None = None,
        environment: Mapping[str, str] | None = None,
        cancel_event: Event | None = None,
        approval_gate: ApprovalGate | None = None,
        policy_revision: str | None = None,
        approval_id: str | None = None,
        confirm_token: str | None = None,
    ) -> ToolResult:
        del environment, cancel_event
        tool.validate_request(request)
        _require_workspace_cwd(project_root, request, cwd)
        if tool.kind is not ToolKind.WORKSPACE_WRITE or request.argv != (
            "yipilot.write-file",
            "approval-marker.txt",
            "approved",
        ):
            raise DomainError(
                "TOOL_WRITE_DENIED", "workspace write is not Task 11's fixed action"
            )
        _require_approval(
            request,
            tool,
            self.trust_level,
            approval_gate,
            policy_revision,
            approval_id,
            confirm_token,
        )
        started_at = datetime.now(UTC)
        tool_run_id = str(uuid4())
        _started(self._journal, tool_run_id, request.tool_id)
        atomic_write_bytes(cwd / "approval-marker.txt", b"approved\n")
        result = _result(
            self._artifact_store,
            tool_run_id,
            request,
            started_at,
            ToolRunStatus.SUCCEEDED,
            0,
            b"",
            b"",
            ExecutionTrustLevel.SANDBOXED,
            ("approval-marker.txt",),
        )
        self._tool_runs.save(result)
        _completed(self._journal, result, request.tool_id)
        return result


def _read_streams(
    process: subprocess.Popen[bytes], output_budget_bytes: int
) -> tuple[bytearray, bytearray, threading.Event, list[threading.Thread]]:
    stdout = bytearray()
    stderr = bytearray()
    exceeded = threading.Event()
    remaining = output_budget_bytes
    lock = threading.Lock()

    def read_stream(stream: BinaryIO, destination: bytearray) -> None:
        nonlocal remaining
        while True:
            chunk = stream.read(_CHUNK_SIZE)
            if not chunk:
                return
            with lock:
                allowed = min(len(chunk), max(remaining, 0))
                destination.extend(chunk[:allowed])
                remaining -= allowed
                if allowed != len(chunk):
                    exceeded.set()
                    _terminate(process)
                    return

    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=read_stream, args=(process.stdout, stdout)),
        threading.Thread(target=read_stream, args=(process.stderr, stderr)),
    ]
    for reader in readers:
        reader.start()
    return stdout, stderr, exceeded, readers


def _require_approval(
    request: ToolRequest,
    tool: ToolPolicy,
    trust_level: ExecutionTrustLevel,
    approval_gate: ApprovalGate | None,
    policy_revision: str | None,
    approval_id: str | None,
    confirm_token: str | None,
) -> None:
    if not requires_approval(tool, trust_level):
        return
    if approval_gate is None or policy_revision is None:
        raise DomainError(
            "APPROVAL_REQUIRED", "high-risk tool execution requires approval"
        )
    approval_gate.require_approved(
        request,
        tool,
        trust_level,
        approval_id=approval_id,
        confirm_token=confirm_token,
        policy_revision=policy_revision,
    )


def _require_workspace_cwd(
    project_root: Path | None, request: ToolRequest, cwd: Path
) -> None:
    if project_root is None:
        raise DomainError("PATH_DENIED", "executor requires the project root")
    root = Path(project_root).resolve(strict=True)
    expected = project_path(root, request.workspace_relative_path).resolve(strict=False)
    actual = Path(cwd).resolve(strict=False)
    if (expected != root and root not in expected.parents) or expected != actual:
        raise DomainError(
            "PATH_DENIED", "executor cwd does not match the task workspace"
        )


def _limit_output(
    stdout: bytes, stderr: bytes, budget: int
) -> tuple[bytes, bytes, bool]:
    if len(stdout) + len(stderr) <= budget:
        return stdout, stderr, False
    kept_stdout = stdout[:budget]
    kept_stderr = stderr[: max(budget - len(kept_stdout), 0)]
    return kept_stdout, kept_stderr, True


def _started(journal: JsonlEventJournal, tool_run_id: str, tool_id: str) -> None:
    journal.append("tool.started", {"tool_run_id": tool_run_id, "tool_id": tool_id})


def _completed(journal: JsonlEventJournal, result: ToolResult, tool_id: str) -> None:
    journal.append(
        "tool.completed",
        {
            "tool_run_id": result.tool_run_id,
            "tool_id": tool_id,
            "status": result.status.value,
            "exit_code": result.exit_code,
            "stdout_artifact_ref": result.stdout_artifact_ref.model_dump(mode="json"),
            "stderr_artifact_ref": result.stderr_artifact_ref.model_dump(mode="json"),
        },
    )


def _result(
    artifact_store: FileArtifactStore,
    tool_run_id: str,
    request: ToolRequest,
    started_at: datetime,
    status: ToolRunStatus,
    exit_code: int | None,
    stdout: bytes,
    stderr: bytes,
    trust_level: ExecutionTrustLevel,
    changed_paths: tuple[str, ...] = (),
) -> ToolResult:
    stdout_ref = _artifact_ref(
        artifact_store.write_bytes(
            request.task_id,
            request.run_id,
            producer="tool_executor",
            relative_path=f"artifacts/tool-runs/{tool_run_id}/stdout.bin",
            content=stdout,
            input_fingerprint=request.fingerprint,
            operation_id=request.operation_id,
        )
    )
    stderr_ref = _artifact_ref(
        artifact_store.write_bytes(
            request.task_id,
            request.run_id,
            producer="tool_executor",
            relative_path=f"artifacts/tool-runs/{tool_run_id}/stderr.bin",
            content=stderr,
            input_fingerprint=request.fingerprint,
            operation_id=request.operation_id,
        )
    )
    changed_paths_ref = None
    if changed_paths:
        changed_paths_ref = _artifact_ref(
            artifact_store.write_bytes(
                request.task_id,
                request.run_id,
                producer="tool_executor",
                relative_path=f"artifacts/tool-runs/{tool_run_id}/changed-paths.nul",
                content="\0".join(changed_paths).encode("utf-8") + b"\0",
                input_fingerprint=request.fingerprint,
                operation_id=request.operation_id,
            )
        )
    return ToolResult(
        tool_run_id=tool_run_id,
        task_id=request.task_id,
        operation_id=request.operation_id,
        run_id=request.run_id,
        request_fingerprint=request.fingerprint,
        argv=request.argv,
        status=status,
        started_at=started_at,
        ended_at=datetime.now(UTC),
        exit_code=exit_code,
        stdout_artifact_ref=stdout_ref,
        stderr_artifact_ref=stderr_ref,
        change_summary=ToolChangeSummary(
            changed_file_count=len(changed_paths),
            changed_paths_artifact_ref=changed_paths_ref,
        ),
        trust_level=trust_level,
    )


def _wait_for_process(
    process: subprocess.Popen[bytes], timeout_seconds: float, cancel_event: Event | None
) -> ToolRunStatus:
    deadline = time.monotonic() + timeout_seconds
    while process.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            _terminate(process)
            return ToolRunStatus.CANCELLED
        if time.monotonic() >= deadline:
            _terminate(process)
            return ToolRunStatus.TIMED_OUT
        time.sleep(0.01)
    return ToolRunStatus.SUCCEEDED


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _artifact_ref(record: ArtifactRecord) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=record.artifact_id,
        task_id=record.task_id,
        operation_id=record.operation_id,
        run_id=record.run_id,
        relative_path=record.relative_path,
        sha256=record.sha256,
    )
