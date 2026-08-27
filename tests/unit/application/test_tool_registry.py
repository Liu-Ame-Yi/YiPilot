import hashlib
import io
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yipilot.application.approval import ApprovalGate, approval_action_type
from yipilot.application.tool_registry import ToolRegistry
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    QUALITY_PYTEST_ALLOWED_ENV_NAMES,
    QUALITY_PYTEST_MAX_OUTPUT_BYTES,
    QUALITY_PYTEST_POLICY_REVISION,
    QUALITY_PYTEST_TOOL_ID,
    ApprovalDecision,
    ApprovalRecord,
    ArgvRule,
    ExecutionPolicy,
    ExecutionTrustLevel,
    ToolKind,
    ToolPolicy,
    ToolRequest,
    ToolRunStatus,
)
from yipilot.infrastructure.approval_store import FileApprovalStore
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.executors import FakeExecutor, LocalCommandRunner
from yipilot.infrastructure.task_store import FileTaskStore, RunRecord


class _Process:
    def __init__(
        self, *, stdout: bytes = b"", stderr: bytes = b"", running: bool = False
    ) -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode: int | None = None if running else 0
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -1

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = -1
        return self.returncode

    def kill(self) -> None:
        self.terminate()


def _context(tmp_path: Path) -> tuple[FileTaskStore, RunRecord, Path, JsonlEventJournal]:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    operation = store.create_operation("1001", "tool", "tool-1001", {})
    run = store.create_run("1001", operation.operation_id, "tool")
    workspace = tmp_path / ".yipilot/tasks/1001/repos/sample/workspace"
    workspace.mkdir(parents=True)
    journal = JsonlEventJournal(
        store.event_journal_path(run.run_id), task_id=run.task_id, run_id=run.run_id
    )
    return store, run, workspace, journal


def _request(run: RunRecord, argv: tuple[str, ...], **changes: object) -> ToolRequest:
    values: dict[str, object] = {
        "task_id": run.task_id,
        "operation_id": run.operation_id,
        "run_id": run.run_id,
        "workspace_relative_path": ".yipilot/tasks/1001/repos/sample/workspace",
        "tool_id": "diagnostic",
        "argv": argv,
        "timeout_seconds": 2,
        "output_budget_bytes": 4096,
        "input_fingerprint": "a" * 64,
    }
    values.update(changes)
    return ToolRequest.model_validate(values)


def _policy(
    argv: tuple[str, ...], *, kind: ToolKind = ToolKind.READ_ONLY, **changes: object
) -> ExecutionPolicy:
    values: dict[str, object] = {
        "tool_id": "diagnostic",
        "kind": kind,
        "executable": argv[0],
        "argv_schema": tuple(ArgvRule(literal=value) for value in argv[1:]),
        "max_output_bytes": 4096,
        "max_timeout_seconds": 2,
    }
    values.update(changes)
    return ExecutionPolicy(revision="task-10", tools=(ToolPolicy(**values),))


def _approved_registry(
    store: FileTaskStore, policy: ExecutionPolicy, runner: LocalCommandRunner,
    request: ToolRequest,
) -> tuple[ToolRegistry, str]:
    tool = policy.validate_request(request)
    approval = ApprovalRecord(
        approval_id="approval-1", task_id=request.task_id, operation_id=request.operation_id,
        run_id=request.run_id,
        action_type=approval_action_type(tool, ExecutionTrustLevel.HOST_TRUSTED),
        target_fingerprint=request.fingerprint, policy_revision=policy.revision,
        decision=ApprovalDecision.APPROVED, decided_at=datetime.now(UTC),
    )
    approvals = FileApprovalStore(store)
    approvals.save(approval)
    return ToolRegistry(policy, runner, ApprovalGate(approvals)), approval.approval_id


@pytest.mark.parametrize(
    ("request_changes", "policy_changes", "error_code"),
    [
        ({"tool_id": "missing"}, {}, "TOOL_DENIED"),
        ({"argv": ("other",)}, {}, "TOOL_EXECUTABLE_DENIED"),
        ({"env_names": ("SECRET",)}, {}, "TOOL_ENV_DENIED"),
        ({"timeout_seconds": 2}, {"max_timeout_seconds": 1}, "TOOL_TIMEOUT_DENIED"),
        ({"output_budget_bytes": 2}, {"max_output_bytes": 1}, "TOOL_BUDGET_DENIED"),
    ],
)
def test_registry_rejects_requests_before_starting_a_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_changes: dict[str, object],
    policy_changes: dict[str, object],
    error_code: str,
) -> None:
    store, run, _, journal = _context(tmp_path)
    argv = ("diagnostic", "--version")
    runner = LocalCommandRunner(FileArtifactStore(store), journal)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("Popen"))
    changed_argv = request_changes.get("argv", argv)
    remaining_changes = {
        key: value for key, value in request_changes.items() if key != "argv"
    }

    with pytest.raises(DomainError, match=rf"^{error_code}:"):
        ToolRegistry(_policy(argv, **policy_changes), runner).execute(
            tmp_path, _request(run, changed_argv, **remaining_changes)
        )


@pytest.mark.parametrize("kind", [ToolKind.WORKSPACE_WRITE, ToolKind.TEST])
def test_registry_rejects_non_read_only_before_starting_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: ToolKind
) -> None:
    store, run, _, journal = _context(tmp_path)
    argv = ("diagnostic", "--version")
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("Popen"))

    with pytest.raises(DomainError, match="^APPROVAL_REQUIRED:"):
        ToolRegistry(_policy(argv, kind=kind), LocalCommandRunner(FileArtifactStore(store), journal)).execute(
            tmp_path, _request(run, argv)
        )


def test_fake_executor_writes_stream_artifacts_and_stable_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run, _, journal = _context(tmp_path)
    argv = ("diagnostic", "--version")
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("Popen"))
    result = ToolRegistry(
        _policy(argv), FakeExecutor(FileArtifactStore(store), journal, stdout=b"out", stderr=b"err")
    ).execute(tmp_path, _request(run, argv))

    assert result.status is ToolRunStatus.SUCCEEDED
    run_root = store.event_journal_path(run.run_id).parent
    assert (run_root / result.stdout_artifact_ref.relative_path).read_bytes() == b"out"
    assert (run_root / result.stderr_artifact_ref.relative_path).read_bytes() == b"err"
    assert [event.type for event in journal.read_after(0)] == ["tool.started", "tool.completed"]


def test_local_runner_uses_approved_argv_shell_false_and_workspace_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run, workspace, journal = _context(tmp_path)
    argv = ("where.exe", "where.exe")
    observed: dict[str, object] = {}
    process = _Process(stdout=b"ok\n", stderr=b"diagnostic\n")

    def record(*args: object, **kwargs: object) -> _Process:
        observed.update(kwargs)
        observed["args"] = args
        return process

    monkeypatch.setattr(subprocess, "Popen", record)
    request = _request(run, argv)
    registry, approval_id = _approved_registry(store, _policy(argv), LocalCommandRunner(FileArtifactStore(store), journal), request)
    result = registry.execute(
        tmp_path, request, approval_id=approval_id, confirm_token=request.fingerprint
    )

    assert result.status is ToolRunStatus.SUCCEEDED
    assert observed["args"] == (list(argv),)
    assert observed["shell"] is False
    assert observed["cwd"] == str(workspace)
    run_root = store.event_journal_path(run.run_id).parent
    assert (run_root / result.stdout_artifact_ref.relative_path).read_bytes() == b"ok\n"
    assert (run_root / result.stderr_artifact_ref.relative_path).read_bytes() == b"diagnostic\n"
    assert hashlib.sha256(
        (run_root / result.stdout_artifact_ref.relative_path).read_bytes()
    ).hexdigest() == result.stdout_artifact_ref.sha256
    assert hashlib.sha256(
        (run_root / result.stderr_artifact_ref.relative_path).read_bytes()
    ).hexdigest() == result.stderr_artifact_ref.sha256
    completed = journal.read_after(0)[1]
    assert "ok" not in str(completed.payload)
    assert completed.payload["stdout_artifact_ref"] == result.stdout_artifact_ref.model_dump(mode="json")


@pytest.mark.parametrize(
    ("timeout_seconds", "cancel", "expected"),
    [
        (0.05, False, ToolRunStatus.TIMED_OUT),
        (2, True, ToolRunStatus.CANCELLED),
    ],
)
def test_local_runner_terminates_timeout_and_cancelled_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout_seconds: float,
    cancel: bool,
    expected: ToolRunStatus,
) -> None:
    store, run, _, journal = _context(tmp_path)
    argv = ("where.exe", "where.exe")
    process = _Process(running=True)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    signal = threading.Event()
    if cancel:
        signal.set()
    request = _request(run, argv, timeout_seconds=timeout_seconds)
    registry, approval_id = _approved_registry(store, _policy(argv), LocalCommandRunner(FileArtifactStore(store), journal), request)
    result = registry.execute(
        tmp_path, request, cancel_event=signal, approval_id=approval_id, confirm_token=request.fingerprint
    )

    assert result.status is expected
    assert process.terminated


def test_local_runner_bounds_output_and_keeps_secrets_out_of_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run, _, journal = _context(tmp_path)
    argv = ("where.exe", "where.exe")
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: _Process(
            stdout=b"Bearer top-secret\n" + b"x" * 100000
        ),
    )
    request = _request(run, argv, output_budget_bytes=32)
    registry, approval_id = _approved_registry(store, _policy(argv), LocalCommandRunner(FileArtifactStore(store), journal), request)
    result = registry.execute(
        tmp_path, request, approval_id=approval_id, confirm_token=request.fingerprint
    )

    assert result.status is ToolRunStatus.FAILED
    run_root = store.event_journal_path(run.run_id).parent
    assert (run_root / result.stderr_artifact_ref.relative_path).read_bytes().endswith(
        b"YIPILOT_OUTPUT_LIMIT_EXCEEDED\n"
    )
    event_text = store.event_journal_path(run.run_id).read_text(encoding="utf-8")
    assert "top-secret" not in event_text
    assert hashlib.sha256(
        (run_root / result.stdout_artifact_ref.relative_path).read_bytes()
    ).hexdigest() == result.stdout_artifact_ref.sha256


def test_registry_rejects_generic_python_and_direct_non_read_only_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run, workspace, journal = _context(tmp_path)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("Popen"))
    python_argv = ("python.exe", "--version")

    with pytest.raises(DomainError, match="^TOOL_EXECUTABLE_DENIED:"):
        ToolRegistry(_policy(python_argv), LocalCommandRunner(FileArtifactStore(store), journal)).execute(
            tmp_path, _request(run, python_argv)
        )
    request = _request(run, ("where.exe", "where.exe"))
    policy = _policy(("where.exe", "where.exe"), kind=ToolKind.TEST)
    with pytest.raises(DomainError, match="^APPROVAL_REQUIRED:"):
        ToolRegistry(policy, LocalCommandRunner(FileArtifactStore(store), journal)).execute(
            tmp_path, request
        )
    with pytest.raises(DomainError, match="^APPROVAL_REQUIRED:"):
        LocalCommandRunner(FileArtifactStore(store), journal).execute(
            request, policy.tools[0], cwd=workspace, project_root=tmp_path
        )
    assert journal.read_after(0) == []
    with pytest.raises(DomainError, match="^TOOL_EXECUTABLE_DENIED:"):
        LocalCommandRunner(FileArtifactStore(store), journal).execute(
            _request(run, python_argv),
            _policy(python_argv).tools[0],
            cwd=workspace,
            project_root=tmp_path,
        )

def test_quality_pytest_policy_rejects_forged_python_and_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run, workspace, journal = _context(tmp_path)
    runner = LocalCommandRunner(FileArtifactStore(store), journal)
    gate = ApprovalGate(FileApprovalStore(store))
    monkeypatch.setattr(
        subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("Popen")
    )

    def request(argv: tuple[str, ...] = (sys.executable, "-m", "pytest")) -> ToolRequest:
        return _request(
            run,
            argv,
            tool_id=QUALITY_PYTEST_TOOL_ID,
            timeout_seconds=60,
            output_budget_bytes=QUALITY_PYTEST_MAX_OUTPUT_BYTES,
        )

    def policy(value: ToolRequest, **changes: object) -> ExecutionPolicy:
        tool = {
            "tool_id": QUALITY_PYTEST_TOOL_ID,
            "kind": ToolKind.TEST,
            "executable": value.argv[0],
            "argv_schema": tuple(ArgvRule(literal=part) for part in value.argv[1:]),
            "allowed_env_names": QUALITY_PYTEST_ALLOWED_ENV_NAMES,
            "max_output_bytes": QUALITY_PYTEST_MAX_OUTPUT_BYTES,
            "max_timeout_seconds": value.timeout_seconds,
        }
        revision = changes.pop("revision", QUALITY_PYTEST_POLICY_REVISION)
        tool.update(changes)
        return ExecutionPolicy(revision=str(revision), tools=(ToolPolicy(**tool),))

    def assert_denied(value: ToolRequest, configured: ExecutionPolicy) -> None:
        with pytest.raises(DomainError, match="^TOOL_EXECUTABLE_DENIED:"):
            ToolRegistry(configured, runner, gate).validate_request(value)
        with pytest.raises(DomainError, match="^TOOL_EXECUTABLE_DENIED:"):
            runner.execute(
                value,
                configured.tools[0],
                cwd=workspace,
                project_root=tmp_path,
                policy_revision=configured.revision,
            )

    fixed = request()
    assert ToolRegistry(policy(fixed), runner, gate).validate_request(fixed).tool_id == QUALITY_PYTEST_TOOL_ID
    assert_denied(fixed, policy(fixed, revision="forged"))
    assert_denied(request((r"C:\fake\python.exe", "-m", "pytest")), policy(request((r"C:\fake\python.exe", "-m", "pytest"))))
    assert_denied(
        request((sys.executable, "-m", "pytest", "--verbose")),
        policy(request((sys.executable, "-m", "pytest", "--verbose"))),
    )
    assert_denied(fixed, policy(fixed, allowed_env_names=(*QUALITY_PYTEST_ALLOWED_ENV_NAMES, "SECRET")))
    assert_denied(fixed, policy(fixed, max_output_bytes=QUALITY_PYTEST_MAX_OUTPUT_BYTES + 1))
    assert_denied(fixed, policy(fixed, max_timeout_seconds=61))
