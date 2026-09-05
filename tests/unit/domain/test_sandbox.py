from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from yipilot.domain.execution import (
    ArgvRule,
    ArtifactRef,
    ExecutionPolicy,
    ToolKind,
    ToolPolicy,
)
from yipilot.domain.sandbox import (
    SandboxBudgets,
    SandboxDiffEvidence,
    SandboxMode,
    SandboxOutputManifest,
    SandboxRequest,
    SandboxResult,
    SourceFile,
)


def policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        revision="sandbox-v1",
        tools=(
            ToolPolicy(
                tool_id="tests",
                kind=ToolKind.TEST,
                executable="python",
                argv_schema=(ArgvRule(literal="-m"), ArgvRule(literal="pytest")),
                max_output_bytes=1024,
                max_timeout_seconds=60,
            ),
        ),
    )


def request(tmp_path: Path, **changes: object) -> SandboxRequest:
    values = {
        "task_id": "1",
        "operation_id": "op-1",
        "run_id": "run-1",
        "policy_revision": "sandbox-v1",
        "mode": SandboxMode.OFFLINE_WORKSPACE_WRITE,
        "execution_directory": str(tmp_path / ".yipilot/tasks/1/runs/run-1/execution"),
        "baseline_manifest_hash": "a" * 64,
        "image_digest": "sha256:" + "b" * 64,
        "tool_id": "tests",
        "argv": ("python", "-m", "pytest"),
        "budgets": SandboxBudgets(
            wall_clock_seconds=60,
            cpu_count=1,
            memory_bytes=1024**3,
            pid_limit=64,
            stdout_bytes=1024,
            stderr_bytes=1024,
        ),
    }
    values.update(changes)
    return SandboxRequest.model_validate(values, context={"policy": policy()})


def test_immutable_registered_request_and_json_roundtrip(tmp_path: Path) -> None:
    item = request(tmp_path)
    assert (
        SandboxRequest.model_validate_json(
            item.model_dump_json(), context={"policy": policy()}
        ).fingerprint
        == item.fingerprint
    )
    with pytest.raises(ValidationError):
        item.run_id = "other"
    with pytest.raises(ValidationError):
        item.budgets.pid_limit = 100
    with pytest.raises(ValidationError, match="trusted registered"):
        SandboxRequest.model_validate_json(item.model_dump_json())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "01"),
        ("run_id", "../run"),
        ("operation_id", "x/y"),
        ("policy_revision", "other"),
        ("tool_id", "unregistered"),
        ("mode", "host_trusted"),
        ("argv", "python -m pytest"),
        ("argv", ("python -m pytest",)),
        ("argv", ("sh", "-c", "echo hi")),
        ("argv", ("python", "-c", "print(1)")),
        ("argv", ("python", "-m", "pytest\n")),
        ("argv", ()),
        ("argv", ["python", "-m", "pytest"]),
        ("baseline_manifest_hash", "a" * 63),
        ("baseline_manifest_hash", "a" * 64 + "\n"),
        ("image_digest", "python:latest"),
        ("image_digest", "sha256:" + "G" * 64),
        ("execution_directory", "relative/execution"),
        ("execution_directory", "C:execution"),
        ("execution_directory", "/tmp/execution"),
    ],
)
def test_request_rejects_invalid_inputs(
    tmp_path: Path, field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        request(tmp_path, **{field: value})


@pytest.mark.parametrize(
    "suffix",
    [
        ".yipilot/tasks/2/runs/run-1/execution",
        ".yipilot/tasks/1/runs/other/execution",
        ".yipilot/tasks/1/runs/run-1/baseline",
        ".yipilot/tasks/1/runs/run-1/../execution",
    ],
)
def test_request_rejects_cross_run_paths(tmp_path: Path, suffix: str) -> None:
    with pytest.raises(ValidationError):
        request(tmp_path, execution_directory=str(tmp_path / suffix))


@pytest.mark.parametrize(
    "field",
    [
        "wall_clock_seconds",
        "cpu_count",
        "memory_bytes",
        "pid_limit",
        "stdout_bytes",
        "stderr_bytes",
    ],
)
@pytest.mark.parametrize(
    "value", [0, -1, True, "10", float("inf"), float("nan"), 10**20]
)
def test_budgets_are_strict_and_bounded(
    tmp_path: Path, field: str, value: object
) -> None:
    values = request(tmp_path).budgets.model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        SandboxBudgets.model_validate(values)


def test_policy_budget_is_enforced(tmp_path: Path) -> None:
    budgets = request(tmp_path).budgets.model_dump()
    budgets["wall_clock_seconds"] = 61
    with pytest.raises(ValidationError, match="budgets exceed"):
        request(tmp_path, budgets=SandboxBudgets.model_validate(budgets))


def result(tmp_path: Path, **changes: object) -> SandboxResult:
    req = request(tmp_path)
    ref = ArtifactRef(
        task_id="1",
        operation_id="op-1",
        run_id="run-1",
        artifact_id="log",
        relative_path="artifacts/stdout.txt",
        sha256="d" * 64,
    )
    values = {
        "task_id": "1",
        "operation_id": "op-1",
        "run_id": "run-1",
        "request_fingerprint": req.fingerprint,
        "container_id": "c" * 64,
        "image_digest": req.image_digest,
        "docker_version": "29.2.1",
        "backend": "unknown",
        "enhanced_container_isolation": None,
        "network": "none",
        "effective_uid": 1000,
        "effective_gid": 1000,
        "resource_limits": req.budgets,
        "reason": "exited",
        "exit_code": 0,
        "started_at": datetime.now(UTC),
        "ended_at": datetime.now(UTC),
        "stdout_artifact_ref": ref,
        "stderr_artifact_ref": ref,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "output_manifest_hash": "e" * 64,
        "fallback_occurred": False,
    }
    values.update(changes)
    return SandboxResult.model_validate(values)


def test_result_is_immutable_evidence(tmp_path: Path) -> None:
    item = result(tmp_path)
    assert SandboxResult.model_validate_json(item.model_dump_json()) == item
    with pytest.raises(ValidationError):
        item.network = "host"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("network", "host"),
        ("effective_uid", 0),
        ("fallback_occurred", True),
        ("stdout_bytes", 1025),
        ("exit_code", None),
        ("container_id", "short"),
        ("output_manifest_hash", "invalid"),
        ("started_at", datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)),
        ("run_id", "different"),
    ],
)
def test_result_rejects_incomplete_or_cross_run_evidence(
    tmp_path: Path, field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        result(tmp_path, **{field: value})


def test_output_manifest_hash_is_stable_and_content_bound(tmp_path):
    req = request(tmp_path)
    manifest = SandboxOutputManifest(
        task_id=req.task_id,
        operation_id=req.operation_id,
        run_id=req.run_id,
        request_fingerprint=req.fingerprint,
        baseline_manifest_hash=req.baseline_manifest_hash,
        files=(
            SourceFile(
                path="app.py", sha256="a" * 64, size_bytes=10, git_mode="100644"
            ),
        ),
    )
    assert (
        SandboxOutputManifest.model_validate_json(manifest.canonical_bytes) == manifest
    )
    assert (
        manifest.model_copy(update={"run_id": "other"}).fingerprint
        != manifest.fingerprint
    )
    assert manifest.model_copy(update={"files": ()}).fingerprint != manifest.fingerprint
    with pytest.raises(ValidationError):
        manifest.files[0].size_bytes = 11
    with pytest.raises(ValidationError):
        manifest.run_id = "other"


def test_output_evidence_requires_timestamp_and_hashes():
    with pytest.raises(ValidationError):
        SandboxDiffEvidence.model_validate(
            {"task_id": "1", "operation_id": "op", "run_id": "run"}
        )
