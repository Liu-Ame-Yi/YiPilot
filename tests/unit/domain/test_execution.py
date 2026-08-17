from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    ApprovalDecision,
    ApprovalRecord,
    ArgvRule,
    ArtifactRef,
    ExecutionPolicy,
    ExecutionTrustLevel,
    ToolChangeSummary,
    ToolKind,
    ToolPolicy,
    ToolRequest,
    ToolResult,
    ToolRunStatus,
    transition_tool_run,
)


def _request(**overrides: object) -> ToolRequest:
    values: dict[str, object] = {
        "task_id": "1001",
        "operation_id": "operation-1",
        "run_id": "run-1",
        "workspace_relative_path": ".yipilot/tasks/1001/repos/sample/workspace",
        "tool_id": "python-tests",
        "tool_kind": ToolKind.TEST,
        "argv": ("python", "-m", "pytest"),
        "timeout_seconds": 60,
        "output_budget_bytes": 1024,
        "input_artifact_refs": (),
        "input_fingerprint": "a" * 64,
    }
    values.update(overrides)
    return ToolRequest.model_validate(values)


def _policy(**tool_overrides: object) -> ExecutionPolicy:
    values: dict[str, object] = {
        "tool_id": "python-tests",
        "kind": ToolKind.TEST,
        "executable": "python",
        "argv_schema": (
            ArgvRule(literal="-m"),
            ArgvRule(literal="pytest"),
        ),
        "allowed_cwds": (".",),
        "allowed_env_names": ("PYTHONUTF8",),
        "max_output_bytes": 4096,
        "max_timeout_seconds": 120,
    }
    values.update(tool_overrides)
    return ExecutionPolicy(
        revision="policy-v1",
        tools=(ToolPolicy.model_validate(values),),
    )


@pytest.mark.parametrize(
    "current",
    [
        ToolRunStatus.SUCCEEDED,
        ToolRunStatus.FAILED,
        ToolRunStatus.TIMED_OUT,
        ToolRunStatus.CANCELLED,
    ],
)
def test_tool_run_terminal_status_cannot_transition_to_running(
    current: ToolRunStatus,
) -> None:
    with pytest.raises(DomainError, match="^TOOL_RUN_TERMINAL:"):
        transition_tool_run(current, ToolRunStatus.RUNNING)


def test_tool_run_cannot_skip_from_pending_to_succeeded() -> None:
    with pytest.raises(DomainError, match="^TOOL_RUN_TRANSITION_INVALID:"):
        transition_tool_run(ToolRunStatus.PENDING, ToolRunStatus.SUCCEEDED)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", 0),
        ("timeout_seconds", -1),
        ("timeout_seconds", 3601),
        ("timeout_seconds", float("inf")),
        ("output_budget_bytes", 0),
        ("output_budget_bytes", 16 * 1024 * 1024 + 1),
    ],
)
def test_tool_request_rejects_invalid_or_out_of_range_limits(
    field: str, value: float
) -> None:
    values: dict[str, object] = {
        "task_id": "1001",
        "operation_id": "operation-1",
        "run_id": "run-1",
        "workspace_relative_path": ".yipilot/tasks/1001/repos/sample/workspace",
        "tool_id": "python-tests",
        "tool_kind": ToolKind.TEST,
        "argv": ("python", "-m", "pytest"),
        "timeout_seconds": 60,
        "output_budget_bytes": 1024,
        "input_artifact_refs": (),
        "input_fingerprint": "a" * 64,
    }
    values[field] = value

    with pytest.raises(ValidationError):
        ToolRequest.model_validate(values)


@pytest.mark.parametrize("task_id", ["-1", "01", "task"])
def test_tool_request_preserves_canonical_task_id_invariant(task_id: str) -> None:
    with pytest.raises(ValidationError):
        _request(task_id=task_id)


@pytest.mark.parametrize(
    "workspace_relative_path",
    [
        "../outside",
        r"..\outside",
        "/outside",
        "C:/outside",
        r"C:\outside",
    ],
)
def test_tool_request_rejects_workspace_escape_paths(
    workspace_relative_path: str,
) -> None:
    with pytest.raises(ValidationError):
        ToolRequest(
            task_id="1001",
            operation_id="operation-1",
            run_id="run-1",
            workspace_relative_path=workspace_relative_path,
            tool_id="python-tests",
            tool_kind=ToolKind.TEST,
            argv=("python", "-m", "pytest"),
            timeout_seconds=60,
            output_budget_bytes=1024,
            input_fingerprint="a" * 64,
        )


@pytest.mark.parametrize(
    "workspace_relative_path",
    [
        "src",
        ".yipilot/tasks/1002/repos/sample/workspace",
        ".yipilot/tasks/1001/repos/sample/not-workspace",
    ],
)
def test_tool_request_requires_its_task_owned_workspace(
    workspace_relative_path: str,
) -> None:
    with pytest.raises(ValidationError, match="Task workspace"):
        _request(workspace_relative_path=workspace_relative_path)


def test_tool_request_rejects_cross_run_artifact_reference() -> None:
    foreign_artifact = ArtifactRef(
        artifact_id="artifact-1",
        task_id="1001",
        operation_id="operation-1",
        run_id="run-2",
        relative_path="artifacts/input.json",
        sha256="b" * 64,
    )

    with pytest.raises(ValidationError, match="same Task, Operation and Run"):
        ToolRequest(
            task_id="1001",
            operation_id="operation-1",
            run_id="run-1",
            workspace_relative_path=".yipilot/tasks/1001/repos/sample/workspace",
            tool_id="python-tests",
            tool_kind=ToolKind.TEST,
            argv=("python", "-m", "pytest"),
            timeout_seconds=60,
            output_budget_bytes=1024,
            input_artifact_refs=(foreign_artifact,),
            input_fingerprint="a" * 64,
        )


def test_tool_request_fingerprint_survives_json_round_trip() -> None:
    request = _request()

    restored = ToolRequest.model_validate_json(request.model_dump_json())

    assert len(request.fingerprint) == 64
    assert restored.fingerprint == request.fingerprint


def test_execution_policy_denies_unknown_tools_by_default() -> None:
    policy = ExecutionPolicy(revision="policy-v1")

    with pytest.raises(DomainError, match="^TOOL_DENIED:"):
        policy.validate_request(_request())


def test_execution_policy_accepts_only_declared_argv_schema() -> None:
    policy = _policy()

    allowed = policy.validate_request(_request(env_names=("PYTHONUTF8",)))

    assert allowed.executable == "python"
    with pytest.raises(DomainError, match="^TOOL_ARGV_DENIED:"):
        policy.validate_request(_request(argv=("python", "-m", "unittest")))


def test_execution_policy_rejects_argv_executable_mismatch() -> None:
    with pytest.raises(DomainError, match="^TOOL_EXECUTABLE_DENIED:"):
        _policy().validate_request(
            _request(argv=("another-python", "-m", "pytest"))
        )


@pytest.mark.parametrize(
    "request_value",
    [
        {"argv": "python -m pytest"},
        {"argv": ("python -m pytest",)},
    ],
)
def test_tool_request_rejects_shell_command_strings_or_uncontrolled_argv(
    request_value: dict[str, object],
) -> None:
    if isinstance(request_value["argv"], str):
        with pytest.raises(ValidationError):
            _request(**request_value)
    else:
        with pytest.raises(DomainError, match="^TOOL_EXECUTABLE_DENIED:"):
            _policy().validate_request(_request(**request_value))


@pytest.mark.parametrize(
    "executable",
    ["cmd.exe", "powershell", "pwsh.exe", "bash", "bash.exe", "sh"],
)
def test_execution_policy_rejects_shell_executables(executable: str) -> None:
    with pytest.raises(ValidationError, match="non-shell"):
        _policy(executable=executable)


def test_execution_policy_allows_one_executable_path_token_with_spaces() -> None:
    executable = r"C:\Program Files\Python\python.exe"
    policy = _policy(executable=executable)

    accepted = policy.validate_request(
        _request(argv=(executable, "-m", "pytest"))
    )

    assert accepted.executable == executable


def test_execution_policy_fingerprint_survives_json_round_trip() -> None:
    policy = _policy()

    restored = ExecutionPolicy.model_validate_json(policy.model_dump_json())

    assert len(policy.fingerprint) == 64
    assert restored.fingerprint == policy.fingerprint


def test_execution_policy_rejects_duplicate_tool_ids() -> None:
    tool = _policy().tools[0]

    with pytest.raises(ValidationError, match="tool IDs must be unique"):
        ExecutionPolicy(revision="policy-v1", tools=(tool, tool))


@pytest.mark.parametrize(
    ("request_change", "error_code"),
    [
        ({"timeout_seconds": 121}, "TOOL_TIMEOUT_DENIED"),
        ({"output_budget_bytes": 4097}, "TOOL_BUDGET_DENIED"),
        ({"env_names": ("SECRET",)}, "TOOL_ENV_DENIED"),
    ],
)
def test_execution_policy_rejects_requests_outside_declared_limits(
    request_change: dict[str, object], error_code: str
) -> None:
    with pytest.raises(DomainError, match=rf"^{error_code}:"):
        _policy().validate_request(_request(**request_change))


def test_execution_policy_rejects_non_workspace_root_cwd() -> None:
    with pytest.raises(ValidationError, match="workspace root"):
        _policy(allowed_cwds=("src",))


@given(
    st.sampled_from(["sample", "repo-a", "repo_2"]),
)
def test_valid_policy_request_cwd_is_its_workspace(
    repo_alias: str,
) -> None:
    policy = _policy()
    request = _request(
        workspace_relative_path=(
            f".yipilot/tasks/1001/repos/{repo_alias}/workspace"
        )
    )

    with TemporaryDirectory() as directory:
        project_root = Path(directory).resolve()
        workspace_root = project_root / request.workspace_relative_path
        workspace_root.mkdir(parents=True)
        resolved = policy.resolve_cwd(project_root, request)

    assert resolved == workspace_root


def test_policy_rejects_workspace_symlink_escape(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace_link = (
        project_root
        / ".yipilot"
        / "tasks"
        / "1001"
        / "repos"
        / "sample"
        / "workspace"
    )
    workspace_link.parent.mkdir(parents=True)
    try:
        workspace_link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")

    with pytest.raises(DomainError, match="^PATH_DENIED:"):
        _policy().resolve_cwd(
            project_root,
            _request(),
        )


def test_tool_result_uses_artifact_refs_instead_of_inline_output() -> None:
    started_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
    stdout_ref = ArtifactRef(
        artifact_id="stdout-1",
        task_id="1001",
        operation_id="operation-1",
        run_id="run-1",
        relative_path="tool-runs/tool-run-1/stdout.txt",
        sha256="b" * 64,
    )
    stderr_ref = ArtifactRef(
        artifact_id="stderr-1",
        task_id="1001",
        operation_id="operation-1",
        run_id="run-1",
        relative_path="tool-runs/tool-run-1/stderr.txt",
        sha256="d" * 64,
    )
    result = ToolResult(
        tool_run_id="tool-run-1",
        task_id="1001",
        operation_id="operation-1",
        run_id="run-1",
        request_fingerprint="c" * 64,
        argv=("python", "-m", "pytest"),
        status=ToolRunStatus.SUCCEEDED,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=2),
        exit_code=0,
        timed_out=False,
        cancelled=False,
        stdout_artifact_ref=stdout_ref,
        stderr_artifact_ref=stderr_ref,
        change_summary=ToolChangeSummary(changed_file_count=0),
        trust_level=ExecutionTrustLevel.FAKE,
    )

    payload = result.model_dump(mode="json")

    assert payload["stdout_artifact_ref"]["artifact_id"] == "stdout-1"
    assert payload["stderr_artifact_ref"]["artifact_id"] == "stderr-1"
    assert payload["argv"] == ["python", "-m", "pytest"]
    assert "stdout" not in payload
    with pytest.raises(ValidationError):
        ToolResult.model_validate({**payload, "stdout": "x" * 1_000_000})


@pytest.mark.parametrize(
    "missing_field",
    ["argv", "stdout_artifact_ref", "stderr_artifact_ref"],
)
def test_tool_result_requires_execution_provenance_and_stream_artifacts(
    missing_field: str,
) -> None:
    started_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
    artifact = ArtifactRef(
        artifact_id="stream-1",
        task_id="1001",
        operation_id="operation-1",
        run_id="run-1",
        relative_path="tool-runs/tool-run-1/stream.txt",
        sha256="b" * 64,
    )
    payload: dict[str, object] = {
        "tool_run_id": "tool-run-1",
        "task_id": "1001",
        "operation_id": "operation-1",
        "run_id": "run-1",
        "request_fingerprint": "c" * 64,
        "argv": ("python", "-m", "pytest"),
        "status": ToolRunStatus.SUCCEEDED,
        "started_at": started_at,
        "ended_at": started_at + timedelta(seconds=2),
        "exit_code": 0,
        "timed_out": False,
        "cancelled": False,
        "stdout_artifact_ref": artifact,
        "stderr_artifact_ref": artifact,
        "change_summary": ToolChangeSummary(changed_file_count=0),
        "trust_level": ExecutionTrustLevel.FAKE,
    }
    del payload[missing_field]

    with pytest.raises(ValidationError):
        ToolResult.model_validate(payload)


@pytest.mark.parametrize(
    ("status", "timed_out", "cancelled"),
    [
        (ToolRunStatus.TIMED_OUT, True, False),
        (ToolRunStatus.CANCELLED, False, True),
    ],
)
def test_tool_result_expresses_timeout_and_cancellation_as_terminal_states(
    status: ToolRunStatus, timed_out: bool, cancelled: bool
) -> None:
    started_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)

    result = ToolResult(
        tool_run_id="tool-run-1",
        task_id="1001",
        operation_id="operation-1",
        run_id="run-1",
        request_fingerprint="c" * 64,
        argv=("python", "-m", "pytest"),
        status=status,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=2),
        exit_code=None,
        timed_out=timed_out,
        cancelled=cancelled,
        stdout_artifact_ref=ArtifactRef(
            artifact_id="stdout-1",
            task_id="1001",
            operation_id="operation-1",
            run_id="run-1",
            relative_path="tool-runs/tool-run-1/stdout.txt",
            sha256="b" * 64,
        ),
        stderr_artifact_ref=ArtifactRef(
            artifact_id="stderr-1",
            task_id="1001",
            operation_id="operation-1",
            run_id="run-1",
            relative_path="tool-runs/tool-run-1/stderr.txt",
            sha256="d" * 64,
        ),
        change_summary=ToolChangeSummary(changed_file_count=0),
        trust_level=ExecutionTrustLevel.FAKE,
    )

    assert result.status is status
    assert result.timed_out is timed_out
    assert result.cancelled is cancelled


def test_tool_result_rejects_cross_run_output_artifact_reference() -> None:
    started_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
    foreign_stdout = ArtifactRef(
        artifact_id="stdout-1",
        task_id="1001",
        operation_id="operation-1",
        run_id="run-2",
        relative_path="tool-runs/tool-run-1/stdout.txt",
        sha256="b" * 64,
    )

    with pytest.raises(ValidationError, match="same Task, Operation and Run"):
        ToolResult(
            tool_run_id="tool-run-1",
            task_id="1001",
            operation_id="operation-1",
            run_id="run-1",
            request_fingerprint="c" * 64,
            argv=("python", "-m", "pytest"),
            status=ToolRunStatus.FAILED,
            started_at=started_at,
            ended_at=started_at + timedelta(seconds=2),
            exit_code=1,
            timed_out=False,
            cancelled=False,
            stdout_artifact_ref=foreign_stdout,
            stderr_artifact_ref=foreign_stdout,
            change_summary=ToolChangeSummary(changed_file_count=0),
            trust_level=ExecutionTrustLevel.FAKE,
        )


def test_approval_is_stale_after_target_policy_or_run_changes() -> None:
    approval = ApprovalRecord(
        approval_id="approval-1",
        task_id="1001",
        operation_id="operation-1",
        run_id="run-1",
        action_type="workspace_write",
        target_fingerprint="a" * 64,
        policy_revision="policy-v1",
        decision=ApprovalDecision.APPROVED,
        decided_at=datetime(2026, 8, 13, 8, 0, tzinfo=UTC),
    )

    assert approval.require_fresh(
        task_id="1001",
        operation_id="operation-1",
        action_type="workspace_write",
        target_fingerprint="a" * 64,
        policy_revision="policy-v1",
        run_id="run-1",
    ) is approval

    stale_inputs = [
        {"task_id": "1002"},
        {"operation_id": "operation-2"},
        {"target_fingerprint": "b" * 64},
        {"policy_revision": "policy-v2"},
        {"run_id": "run-2"},
        {"action_type": "test_execute"},
    ]
    for changed in stale_inputs:
        context = {
            "task_id": "1001",
            "operation_id": "operation-1",
            "action_type": "workspace_write",
            "target_fingerprint": "a" * 64,
            "policy_revision": "policy-v1",
            "run_id": "run-1",
            **changed,
        }
        with pytest.raises(DomainError, match="^APPROVAL_STALE:"):
            approval.require_fresh(**context)
