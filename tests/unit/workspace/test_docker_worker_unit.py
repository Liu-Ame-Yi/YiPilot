import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ArgvRule, ExecutionPolicy, ToolKind, ToolPolicy
from yipilot.domain.sandbox import SandboxBudgets, SandboxMode, SandboxRequest
from yipilot.workspace.docker_worker import DockerSandboxWorker


@pytest.fixture
def sandbox_request(tmp_path):
    policy = ExecutionPolicy(
        revision="fixture",
        tools=(
            ToolPolicy(
                tool_id="fixture",
                kind=ToolKind.TEST,
                executable="python",
                argv_schema=(ArgvRule(literal="worker.py"),),
                max_output_bytes=1024,
                max_timeout_seconds=60,
            ),
        ),
    )
    return SandboxRequest.model_validate(
        {
            "task_id": "1",
            "operation_id": "op-1",
            "run_id": "run-1",
            "policy_revision": "fixture",
            "mode": SandboxMode.OFFLINE_WORKSPACE_WRITE,
            "execution_directory": str(
                tmp_path / ".yipilot/tasks/1/runs/run-1/execution"
            ),
            "baseline_manifest_hash": "a" * 64,
            "image_digest": "sha256:" + "b" * 64,
            "tool_id": "fixture",
            "argv": ("python", "worker.py"),
            "budgets": SandboxBudgets(
                wall_clock_seconds=60,
                cpu_count=1,
                memory_bytes=512 * 1024**2,
                pid_limit=64,
                stdout_bytes=1024,
                stderr_bytes=1024,
            ),
        },
        context={"policy": policy},
    )


def test_fixed_argv_contains_only_execution_bind_and_no_pull(sandbox_request):
    argv = DockerSandboxWorker._create_argv(
        sandbox_request,
        "fixture",
        Path(sandbox_request.execution_directory),
        sandbox_request.argv,
    )
    assert argv.count("--mount") == 1
    assert (
        argv[argv.index("--mount") + 1]
        == f"type=bind,src={sandbox_request.execution_directory},dst=/workspace,readonly=false"
    )
    assert "--pull=never" in argv and "--read-only" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--user") + 1] == "10001:10001"
    assert argv[-2:] == [sandbox_request.image_digest, "worker.py"]
    assert not any(
        flag in argv
        for flag in ("--privileged", "--device", "--publish", "--volume", "--env-file")
    )


def test_mount_separator_cannot_inject_another_mount_option(sandbox_request, tmp_path):
    with pytest.raises(DomainError, match="SANDBOX_PATH_DENIED"):
        DockerSandboxWorker._create_argv(
            sandbox_request,
            "fixture",
            tmp_path / "evil,src=other",
            sandbox_request.argv,
        )


def test_missing_cli_never_starts_host_command(monkeypatch):
    worker = object.__new__(DockerSandboxWorker)
    worker._docker = None

    def fail(*args, **kwargs):
        pytest.fail("must not start a host or Docker command")

    monkeypatch.setattr("subprocess.run", fail)
    with pytest.raises(DomainError, match="DOCKER_CLI_UNAVAILABLE"):
        worker._cli("info")


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (
            (
                b"WARNING: config.json: Access is denied.\n"
                b"open //./pipe/docker_engine: The system cannot find the file specified."
            ),
            "DOCKER_DAEMON_UNAVAILABLE",
        ),
        (
            b"permission denied while trying to connect to the docker API",
            "DOCKER_ACCESS_DENIED",
        ),
    ],
)
def test_docker_control_error_classification(monkeypatch, stderr, code):
    worker = object.__new__(DockerSandboxWorker)
    worker._docker = "docker.exe"
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr=stderr),
    )
    with pytest.raises(DomainError, match=code):
        worker._cli("info")


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("windows", "DOCKER_NOT_LINUX_MODE"),
        ("wrong_digest", "SANDBOX_IMAGE_DIGEST_MISMATCH"),
        ("volume", "SANDBOX_POLICY_UNAVAILABLE"),
        ("secret_env", "SANDBOX_POLICY_UNAVAILABLE"),
    ],
)
def test_readiness_rejects_incompatible_daemon_and_image(
    sandbox_request, monkeypatch, change, code
):
    worker = object.__new__(DockerSandboxWorker)
    info = {
        "OSType": "linux",
        "OperatingSystem": "Docker Desktop",
        "ServerVersion": "29.2.1",
    }
    image = {"Id": sandbox_request.image_digest, "Os": "linux", "Config": {"Env": []}}
    if change == "windows":
        info["OSType"] = "windows"
    elif change == "wrong_digest":
        image["Id"] = "sha256:" + "c" * 64
    elif change == "volume":
        image["Config"]["Volumes"] = {"/data": {}}
    else:
        image["Config"]["Env"] = ["PROVIDER_API_KEY=fixture"]
    monkeypatch.setattr(
        worker,
        "_cli",
        lambda *args: json.dumps(info if args[0] == "info" else [image]).encode(),
    )
    with pytest.raises(DomainError, match=code):
        worker._readiness(sandbox_request)


def test_cleanup_must_verify_container_absence(monkeypatch):
    worker = object.__new__(DockerSandboxWorker)
    monkeypatch.setattr(
        worker, "_cli", lambda *args: b"" if args[0] == "rm" else b"a" * 64
    )
    with pytest.raises(DomainError, match="SANDBOX_CLEANUP_FAILED"):
        worker._remove("a" * 64)


def test_name_conflict_does_not_delete_existing_container(monkeypatch):
    worker = object.__new__(DockerSandboxWorker)
    calls = []

    def cli(*args):
        calls.append(args)
        return b"a" * 64

    monkeypatch.setattr(worker, "_cli", cli)
    with pytest.raises(DomainError, match="SANDBOX_CONFLICT"):
        worker._create(["create", "--name", "fixture"])
    assert len(calls) == 1 and calls[0][0] == "ps"


def test_lost_create_response_cleans_only_its_owned_container(monkeypatch):
    worker = object.__new__(DockerSandboxWorker)
    calls = []

    def cli(*args):
        calls.append(args)
        if args[0] == "create":
            raise DomainError("SANDBOX_UNAVAILABLE", "lost response")
        return b"" if len(calls) == 1 else b"a" * 64

    monkeypatch.setattr(worker, "_cli", cli)
    monkeypatch.setattr(
        worker,
        "_inspect",
        lambda _: {"Config": {"Labels": {"yipilot.request": "fingerprint"}}},
    )
    removed = []
    monkeypatch.setattr(worker, "_remove", removed.append)
    with pytest.raises(DomainError, match="SANDBOX_UNAVAILABLE"):
        worker._create(
            ["create", "--name", "fixture", "--label", "yipilot.request=fingerprint"]
        )
    assert removed == ["a" * 64]
