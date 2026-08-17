import io
import os
import subprocess
import threading
from pathlib import Path

import pytest

from yipilot.application.tool_registry import ToolRegistry
from yipilot.domain.execution import (
    ArgvRule,
    ExecutionPolicy,
    ToolKind,
    ToolPolicy,
    ToolRequest,
    ToolRunStatus,
)
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.executors import FakeExecutor, LocalCommandRunner
from yipilot.infrastructure.task_store import FileTaskStore


class _CompletedProcess:
    def __init__(self) -> None:
        self.stdout = io.BytesIO(b"local stdout\n")
        self.stderr = io.BytesIO(b"local stderr\n")
        self.returncode: int | None = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -1

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.terminate()


def test_fake_and_local_execution_persist_run_scoped_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    argv = ("where.exe", "where.exe")
    request = ToolRequest(
        task_id=run.task_id,
        operation_id=run.operation_id,
        run_id=run.run_id,
        workspace_relative_path=".yipilot/tasks/1001/repos/sample/workspace",
        tool_id="diagnostic",
        argv=argv,
        timeout_seconds=1,
        output_budget_bytes=1024,
        input_fingerprint="a" * 64,
    )
    policy = ExecutionPolicy(
        revision="task-10",
        tools=(
            ToolPolicy(
                tool_id="diagnostic",
                kind=ToolKind.READ_ONLY,
                executable=argv[0],
                argv_schema=(ArgvRule(literal=argv[1]),),
                max_output_bytes=1024,
                max_timeout_seconds=1,
            ),
        ),
    )
    artifacts = FileArtifactStore(store)

    fake_result = ToolRegistry(
        policy, FakeExecutor(artifacts, journal, stdout=b"fake stdout\n")
    ).execute(tmp_path, request)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: _CompletedProcess())
    local_result = ToolRegistry(policy, LocalCommandRunner(artifacts, journal)).execute(
        tmp_path, request
    )

    run_root = store.event_journal_path(run.run_id).parent
    assert (run_root / fake_result.stdout_artifact_ref.relative_path).read_bytes() == b"fake stdout\n"
    assert (run_root / local_result.stderr_artifact_ref.relative_path).read_bytes() == b"local stderr\n"
    events = journal.read_after(0)
    assert [event.type for event in events] == [
        "tool.started",
        "tool.completed",
        "tool.started",
        "tool.completed",
    ]
    assert all("fake stdout" not in str(event.payload) for event in events)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows built-in diagnostics")
@pytest.mark.parametrize(
    ("timeout_seconds", "cancel", "expected"),
    [
        (0.05, False, ToolRunStatus.TIMED_OUT),
        (1, True, ToolRunStatus.CANCELLED),
    ],
)
def test_windows_local_runner_terminates_real_read_only_diagnostic(
    tmp_path: Path,
    timeout_seconds: float,
    cancel: bool,
    expected: ToolRunStatus,
) -> None:
    where = Path(os.environ["SystemRoot"]) / "System32" / "where.exe"
    if not where.is_file():
        pytest.skip("where.exe is unavailable")
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    operation = store.create_operation("1001", "tool", "tool-1001", {})
    run = store.create_run("1001", operation.operation_id, "tool")
    workspace = tmp_path / ".yipilot/tasks/1001/repos/sample/workspace"
    workspace.mkdir(parents=True)
    argv = (str(where), "/r", os.environ["SystemRoot"], "*.yipilot-never")
    request = ToolRequest(
        task_id=run.task_id,
        operation_id=run.operation_id,
        run_id=run.run_id,
        workspace_relative_path=".yipilot/tasks/1001/repos/sample/workspace",
        tool_id="diagnostic",
        argv=argv,
        timeout_seconds=timeout_seconds,
        output_budget_bytes=4096,
        input_fingerprint="a" * 64,
    )
    policy = ExecutionPolicy(
        revision="task-10",
        tools=(
            ToolPolicy(
                tool_id="diagnostic",
                kind=ToolKind.READ_ONLY,
                executable=argv[0],
                argv_schema=tuple(ArgvRule(literal=value) for value in argv[1:]),
                max_output_bytes=4096,
                max_timeout_seconds=1,
            ),
        ),
    )
    signal = threading.Event()
    if cancel:
        signal.set()
    journal = JsonlEventJournal(
        store.event_journal_path(run.run_id), task_id=run.task_id, run_id=run.run_id
    )

    result = ToolRegistry(policy, LocalCommandRunner(FileArtifactStore(store), journal)).execute(
        tmp_path, request, cancel_event=signal
    )

    assert result.status is expected
    assert [event.type for event in journal.read_after(0)] == ["tool.started", "tool.completed"]
