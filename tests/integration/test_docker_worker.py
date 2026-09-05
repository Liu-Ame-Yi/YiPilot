import json
import os
import threading
import time
from datetime import UTC, datetime

import pytest

from yipilot.application.repo_service import RepoService
from yipilot.application.sandbox_output import (
    SandboxOutputService,
    output_approval_fingerprint,
)
from yipilot.application.sandbox_sources import SandboxSourceService
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    ApprovalDecision,
    ApprovalRecord,
    ArgvRule,
    ExecutionPolicy,
    ToolKind,
    ToolPolicy,
)
from yipilot.domain.sandbox import SandboxBudgets, SandboxMode, SandboxRequest
from yipilot.infrastructure.approval_store import FileApprovalStore
from yipilot.infrastructure.docker_worker import DockerSandboxWorker
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.task_store import FileTaskStore

IMAGE = os.environ.get("YIPILOT_DOCKER_TEST_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="explicit local Docker image digest required"
)


@pytest.fixture
def worker_setup(tmp_path):
    def prepare(script, *, timeout=10.0, output=8192):
        source = tmp_path / "source"
        source.mkdir()
        git = GitRunner()
        for args in (
            ("init", "--initial-branch=main"),
            ("config", "user.name", "Fixture"),
            ("config", "user.email", "fixture@example.invalid"),
        ):
            git.run(["git", *args], cwd=source)
        (source / "worker.py").write_text(script, encoding="utf-8")
        git.run(["git", "add", "worker.py"], cwd=source)
        git.run(["git", "commit", "-m", "fixture"], cwd=source)
        base = git.run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
        project = tmp_path / "project"
        project.mkdir()
        store = FileTaskStore(project)
        store.initialize_project()
        task = store.create_task("1")
        operation = store.create_operation("1", "docker_fixture", "fixture", {})
        run = store.create_run("1", operation.operation_id, "docker_fixture")
        RepoService(project, git).add_and_sync("1", str(source), "repo")
        copies = SandboxSourceService(project, git).prepare(task, "repo", run, base)
        policy = ExecutionPolicy(
            revision="docker-fixture-v1",
            tools=(
                ToolPolicy(
                    tool_id="fixture",
                    kind=ToolKind.TEST,
                    executable="python",
                    argv_schema=(ArgvRule(literal="worker.py"),),
                    max_output_bytes=output,
                    max_timeout_seconds=timeout,
                ),
            ),
        )
        request = SandboxRequest.model_validate(
            {
                "task_id": "1",
                "operation_id": run.operation_id,
                "run_id": run.run_id,
                "policy_revision": policy.revision,
                "mode": SandboxMode.OFFLINE_WORKSPACE_WRITE,
                "execution_directory": str(copies.execution),
                "baseline_manifest_hash": copies.manifest_hash,
                "image_digest": IMAGE,
                "tool_id": "fixture",
                "argv": ("python", "worker.py"),
                "budgets": SandboxBudgets(
                    wall_clock_seconds=timeout,
                    cpu_count=1,
                    memory_bytes=512 * 1024**2,
                    pid_limit=64,
                    stdout_bytes=output,
                    stderr_bytes=output,
                ),
            },
            context={"policy": policy},
        )
        return DockerSandboxWorker(project, policy, copies), request, copies

    return prepare


def test_real_offline_non_root_worker_writes_only_execution(worker_setup):
    worker, request, copies = worker_setup(
        "from pathlib import Path\nPath('changed.txt').write_text('inside')\nprint('done')\n"
    )
    result = worker.execute(request)
    assert result.exit_code == 0 and result.reason == "exited"
    assert result.network == "none" and result.effective_uid == 10001
    assert result.fallback_occurred is False
    assert (copies.execution / "changed.txt").read_text() == "inside"
    assert not (copies.baseline / "changed.txt").exists()
    evidence = copies.execution.parent / "docker-worker"
    assert json.loads((evidence / "cleanup.json").read_bytes())["container_removed"]
    before = json.loads((evidence / "container-before.json").read_bytes())
    assert before["HostConfig"]["ReadonlyRootfs"]
    assert before["HostConfig"]["NetworkMode"] == "none"
    output = SandboxOutputService(worker._root, GitRunner())
    candidate = output.inspect(copies, request, worker._policy)
    FileApprovalStore(worker._store).save(
        ApprovalRecord(
            approval_id="fixture-output",
            task_id=request.task_id,
            operation_id=request.operation_id,
            run_id=request.run_id,
            policy_revision=request.policy_revision,
            action_type="sandbox_output",
            target_fingerprint=output_approval_fingerprint(
                request, candidate.patch_sha256
            ),
            decision=ApprovalDecision.APPROVED,
            decided_at=datetime.now(UTC),
        )
    )
    accepted = output.collect(
        copies,
        request,
        worker._policy,
        result,
        approved_patch_sha256=candidate.patch_sha256,
        approval_id="fixture-output",
    )
    assert b"+inside" in output.verify(copies, request, worker._policy, accepted)


def test_timeout_kills_container_and_descendants(worker_setup):
    worker, request, copies = worker_setup(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', \"import time; time.sleep(2); open('escaped.txt','w').write('bad')\"])\n"
        "time.sleep(30)\n",
        timeout=0.5,
    )
    result = worker.execute(request)
    assert result.reason == "timed_out"
    time.sleep(2)
    assert not (copies.execution / "escaped.txt").exists()


def test_output_flood_is_bounded_and_container_removed(worker_setup):
    worker, request, copies = worker_setup(
        "import os\nwhile True: os.write(1, b'x' * 8192)\n",
        output=1024,
    )
    result = worker.execute(request)
    assert result.reason == "output_limit" and result.stdout_bytes <= 1024
    assert (copies.execution.parent / "docker-worker/cleanup.json").is_file()


def test_cancel_stops_running_container(worker_setup):
    worker, request, copies = worker_setup(
        "from pathlib import Path\nimport time\nPath('ready').touch()\ntime.sleep(30)\n"
    )
    cancel = threading.Event()

    def wait_for_start():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (copies.execution / "ready").exists():
                cancel.set()
                return
            time.sleep(0.02)

    watcher = threading.Thread(target=wait_for_start, daemon=True)
    watcher.start()
    result = worker.execute(request, cancel_event=cancel)
    watcher.join(timeout=1)
    assert result.reason == "cancelled"


def test_network_credentials_and_docker_socket_unavailable(worker_setup):
    worker, request, _ = worker_setup(
        "import os, pathlib, socket\n"
        "assert os.getuid() != 0\n"
        "for path in ('/workspace/.git', '/workspace/.env', '/workspace/.yipilot', '/var/run/docker.sock', '/root/.ssh', '/host_mnt'):\n"
        "    try: exists = pathlib.Path(path).exists()\n"
        "    except PermissionError: exists = False\n"
        "    assert not exists, path\n"
        "assert not any('API_KEY' in key or 'TOKEN' in key for key in os.environ)\n"
        "with socket.socket() as s:\n"
        "    s.settimeout(1)\n"
        "    assert s.connect_ex(('198.51.100.1', 443)) != 0\n"
        "try: socket.getaddrinfo('example.invalid', 443)\n"
        "except socket.gaierror: pass\n"
        "else: raise AssertionError('DNS available')\n"
        "print('isolated')\n"
    )
    assert worker.execute(request).exit_code == 0


def test_missing_image_fails_closed(worker_setup):
    worker, request, copies = worker_setup("raise AssertionError('must not run')\n")
    request = request.model_copy(update={"image_digest": "sha256:" + "f" * 64})
    with pytest.raises(DomainError, match="SANDBOX_UNAVAILABLE"):
        worker.execute(request)
    assert not (
        copies.execution.parent / "docker-worker/container-before.json"
    ).exists()


def test_run_cannot_relaunch_worker(worker_setup):
    worker, request, _ = worker_setup("print('once')\n")
    worker.execute(request)
    with pytest.raises(DomainError, match="SANDBOX_CONFLICT"):
        worker.execute(request)


def test_fixed_pytest_ruff_and_mypy_execute_inside_container(worker_setup):
    worker, request, copies = worker_setup(
        "from pathlib import Path\nimport subprocess, sys\n"
        "Path('sample.py').write_text('value: int = 1\\n')\n"
        "Path('test_sample.py').write_text('from sample import value\\ndef test_value(): assert value == 1\\n')\n"
        "subprocess.run([sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', 'test_sample.py'], check=True)\n"
        "subprocess.run(['ruff', 'check', 'sample.py'], check=True)\n"
        "subprocess.run(['mypy', 'sample.py'], check=True)\n",
        timeout=30.0,
    )
    result = worker.execute(request)
    assert result.reason == "exited" and result.exit_code == 0
    assert not (copies.baseline / "sample.py").exists()


def test_nonzero_test_exit_is_recorded_without_success_fallback(worker_setup):
    worker, request, _ = worker_setup("raise SystemExit(3)\n")
    result = worker.execute(request)
    assert result.exit_code == 3 and result.reason == "exited"
    assert result.fallback_occurred is False


@pytest.mark.parametrize("failed_record", ["policy.json", "result.json"])
def test_evidence_failure_never_returns_success(
    worker_setup, monkeypatch, failed_record
):
    worker, request, copies = worker_setup("print('fixture')\n")
    original = worker._write

    def write(directory, name, value):
        if name == failed_record:
            raise OSError("fixture evidence failure")
        original(directory, name, value)

    monkeypatch.setattr(worker, "_write", write)
    with pytest.raises(DomainError, match="SANDBOX_UNAVAILABLE"):
        worker.execute(request)
    evidence = copies.execution.parent / "docker-worker"
    assert not (evidence / "result.json").exists()
    assert (evidence / "failure.json").is_file()
    if failed_record == "policy.json":
        assert not (evidence / "container-before.json").exists()
    else:
        assert json.loads((evidence / "cleanup.json").read_bytes())["container_removed"]


def test_worker_created_control_directory_is_rejected_after_cleanup(worker_setup):
    worker, request, copies = worker_setup(
        "from pathlib import Path\nPath('.git').mkdir()\n"
        "Path('.git/config').write_text('untrusted')\n"
    )
    with pytest.raises(DomainError):
        worker.execute(request)
    evidence = copies.execution.parent / "docker-worker"
    assert json.loads((evidence / "cleanup.json").read_bytes())["container_removed"]
    assert not (evidence / "result.json").exists()
    assert not (copies.baseline / ".git").exists()
