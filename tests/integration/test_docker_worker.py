import json
import os
import threading
import time
from datetime import UTC, datetime

import pytest

from yipilot.application.repo_service import RepoService
from yipilot.application.sandbox_apply import SandboxApplyService
from yipilot.application.sandbox_output import (
    SandboxOutputService,
    output_approval_fingerprint,
)
from yipilot.application.sandbox_sources import SandboxSourceService
from yipilot.application.sandbox_verification import (
    REVISION,
    SandboxVerificationService,
)
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
    def prepare(script, *, timeout=10.0, output=8192, files=None):
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
        for name, content in (files or {}).items():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_text(content, encoding="utf-8")
        git.run(["git", "add", "--", "worker.py", *(files or {})], cwd=source)
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


def approve_fixture_output(worker, request, copies, result):
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
    output.verify(copies, request, worker._policy, accepted)
    return accepted


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
    accepted = approve_fixture_output(worker, request, copies, result)
    workspace = SandboxApplyService(worker._root, GitRunner()).apply(
        copies, request, worker._policy, accepted
    )
    assert (workspace.path / "changed.txt").read_text() == "inside"
    assert not (worker._root.parent / "source/changed.txt").exists()
    assert (
        GitRunner().run(["git", "rev-parse", "HEAD"], cwd=workspace.path).stdout.strip()
        == copies.manifest.base_sha
    )
    with pytest.raises(DomainError, match="SANDBOX_CONFLICT"):
        SandboxApplyService(worker._root, GitRunner()).apply(
            copies, request, worker._policy, accepted
        )


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


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_trusted_apply_preserves_exact_worker_bytes(worker_setup, change):
    script = (
        "from pathlib import Path\nPath('worker.py').write_bytes(b'# updated\\r\\n')\n"
        if change == "edit"
        else "from pathlib import Path\nPath('worker.py').unlink()\nPath('empty.txt').touch()\n"
    )
    worker, request, copies = worker_setup(script)
    accepted = approve_fixture_output(worker, request, copies, worker.execute(request))
    workspace = SandboxApplyService(worker._root, GitRunner()).apply(
        copies, request, worker._policy, accepted
    )
    if change == "edit":
        assert (workspace.path / "worker.py").read_bytes() == b"# updated\r\n"
    else:
        assert not (workspace.path / "worker.py").exists()
        assert (workspace.path / "empty.txt").read_bytes() == b""
    assert (copies.baseline / "worker.py").read_text() == script


@pytest.mark.parametrize(
    "failure", ["cleanup", "approval", "drift", "destination", "evidence", "post_apply"]
)
def test_trusted_apply_fails_closed(worker_setup, monkeypatch, failure):
    from yipilot.application import sandbox_apply as apply_module

    worker, request, copies = worker_setup(
        "from pathlib import Path\nPath('added').write_text('new')\n"
    )
    accepted = approve_fixture_output(worker, request, copies, worker.execute(request))
    root = copies.execution.parent
    service = SandboxApplyService(worker._root, GitRunner())
    target = worker._root / f".yipilot/tasks/1/repos/repo/workspaces/{request.run_id}"
    if failure == "cleanup":
        (root / "docker-worker/cleanup.json").unlink()
    elif failure == "approval":
        (root / "approvals/fixture-output.json").unlink()
    elif failure == "drift":
        (copies.execution / "added").write_text("drift")
    elif failure == "destination":
        target.mkdir(parents=True)
        (target / "owned.txt").write_text("preserve")
    elif failure == "evidence":
        original = apply_module.atomic_write_bytes

        def fail_result(path, content):
            if path == root / "sandbox-apply/result.json":
                raise OSError("fixture disk failure")
            original(path, content)

        monkeypatch.setattr(apply_module, "atomic_write_bytes", fail_result)
    else:
        original_run = service._run

        def drift(cwd, *args, **kwargs):
            result = original_run(cwd, *args, **kwargs)
            if args[0] == "write-tree" and (cwd / "added").exists():
                (cwd / "added").write_text("unexpected")
            return result

        monkeypatch.setattr(service, "_run", drift)
    with pytest.raises(DomainError):
        service.apply(copies, request, worker._policy, accepted)
    assert not (root / "sandbox-apply/result.json").exists()
    if failure == "destination":
        assert (target / "owned.txt").read_text() == "preserve"
    elif failure in {"evidence", "post_apply"}:
        assert (root / "sandbox-apply/failure.json").is_file()
        with pytest.raises(DomainError, match="SANDBOX_CONFLICT"):
            service.apply(copies, request, worker._policy, accepted)
    else:
        assert not target.exists()


def test_trusted_apply_never_runs_checkout_hooks_or_filters(worker_setup, tmp_path):
    worker, request, copies = worker_setup(
        "from pathlib import Path\nPath('worker.py').write_text('# changed\\n')\n",
        files={".gitattributes": "*.py filter=trap\n"},
    )
    accepted = approve_fixture_output(worker, request, copies, worker.execute(request))
    git = GitRunner()
    cache = RepoService(worker._root, git).get_metadata("1", "repo").cache_path
    marker = tmp_path / "host-command-ran"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("post-checkout", "reference-transaction", "post-index-change"):
        hook = hooks / name
        hook.write_text(f'#!/bin/sh\necho unexpected > "{marker.as_posix()}"\n')
        hook.chmod(0o755)
    for key, value in (
        ("core.hooksPath", str(hooks)),
        ("filter.trap.smudge", f'echo unexpected > "{marker.as_posix()}"'),
        ("filter.trap.clean", f'echo unexpected > "{marker.as_posix()}"'),
        ("filter.trap.required", "true"),
    ):
        git.run(["git", "config", key, value], cwd=cache)
    workspace = SandboxApplyService(worker._root, git).apply(
        copies, request, worker._policy, accepted
    )
    assert not marker.exists()
    assert (workspace.path / "worker.py").read_bytes() == b"# changed\n"


def test_trusted_apply_rejects_filtered_base_without_materializing_credentials(
    worker_setup,
):
    worker, request, copies = worker_setup(
        "print('done')\n", files={".env": "fixture-secret"}
    )
    accepted = approve_fixture_output(worker, request, copies, worker.execute(request))
    with pytest.raises(DomainError, match="SANDBOX_SOURCE_DENIED"):
        SandboxApplyService(worker._root, GitRunner()).apply(
            copies, request, worker._policy, accepted
        )
    assert not (
        worker._root / f".yipilot/tasks/1/repos/repo/workspaces/{request.run_id}"
    ).exists()


def verification_fixture(worker_setup, *, test_code=None, value="value: int = 2\n"):
    worker, request, copies = worker_setup(
        "from pathlib import Path\nPath('src/value.py').write_text("
        + repr(value)
        + ")\n",
        files={
            "src/value.py": "value: int = 1\n",
            "ruff.toml": '[lint]\nselect = ["E4", "E7", "E9", "F"]\n',
            "tests/test_value.py": test_code
            or "from pathlib import Path\n\ndef test_value():\n    assert '2' in Path('src/value.py').read_text()\n",
        },
    )
    accepted = approve_fixture_output(worker, request, copies, worker.execute(request))
    workspace = SandboxApplyService(worker._root, GitRunner()).apply(
        copies, request, worker._policy, accepted
    )
    service = SandboxVerificationService(worker._root)
    preparation = service.prepare(copies, request, worker._policy, accepted)
    FileApprovalStore(worker._store).save(
        ApprovalRecord(
            approval_id="verification-approval",
            task_id=request.task_id,
            operation_id=preparation.operation_id,
            run_id=preparation.run_id,
            action_type="sandbox_verification",
            policy_revision=REVISION,
            target_fingerprint=preparation.approval_fingerprint,
            decision=ApprovalDecision.APPROVED,
            decided_at=datetime.now(UTC),
        )
    )
    return worker, request, copies, accepted, workspace, service, preparation


def test_sandbox_verification_creates_fresh_handoff(worker_setup):
    worker, request, copies, accepted, workspace, service, preparation = (
        verification_fixture(worker_setup)
    )
    record = service.run(
        copies,
        request,
        worker._policy,
        accepted,
        preparation.run_id,
        approval_id="verification-approval",
    )
    assert (
        service.handoff(copies, request, worker._policy, accepted, preparation.run_id)
        == record
    )
    root = worker._store.event_journal_path(preparation.run_id).parent
    handoff = json.loads((root / record.relative_path).read_bytes())
    assert handoff["trust_level"] == "sandboxed"
    assert len(handoff["tool_runs"]) == 3
    assert len({r["container_id"] for r in handoff["tool_runs"]}) == 3
    assert all(
        r["exit_code"] == 0 and r["network"] == "none" for r in handoff["tool_runs"]
    )
    assert (workspace.path / "src/value.py").read_text() == "value: int = 2\n"
    with pytest.raises(DomainError, match="SANDBOX_CONFLICT"):
        service.run(
            copies,
            request,
            worker._policy,
            accepted,
            preparation.run_id,
            approval_id="verification-approval",
        )
    child_root = worker._store.event_journal_path(preparation.requests[0].run_id).parent
    state_path = child_root / "docker-worker/container-after.json"
    original_state = state_path.read_bytes()
    state = json.loads(original_state)
    state["State"]["Status"] = "created"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(DomainError):
        service.handoff(copies, request, worker._policy, accepted, preparation.run_id)
    state_path.write_bytes(original_state)
    for path in (
        root / "harness/sandbox-verification-report.json",
        root / "approvals/verification-approval.json",
        child_root / "artifacts/docker-worker/stdout.bin",
        child_root / "input-manifest.json",
        child_root / "docker-worker/cleanup.json",
        workspace.path / ".git",
    ):
        original = path.read_bytes()
        with path.open("r+b") as handle:
            handle.write(b"corrupted")
            handle.truncate()
        with pytest.raises(DomainError):
            service.handoff(
                copies, request, worker._policy, accepted, preparation.run_id
            )
        with path.open("r+b") as handle:
            handle.write(original)
            handle.truncate()
    (workspace.path / "src/value.py").write_text("drift")
    with pytest.raises(DomainError):
        service.handoff(copies, request, worker._policy, accepted, preparation.run_id)


@pytest.mark.parametrize(
    "failure",
    ["pytest", "ruff", "mypy", "mutation", "approval", "seed", "cancel", "handoff"],
)
def test_sandbox_verification_failure_cannot_create_handoff(
    worker_setup, monkeypatch, failure
):
    test_code = None
    if failure == "pytest":
        test_code = "def test_bad():\n    assert False\n"
    elif failure == "ruff":
        test_code = "import os\n\ndef test_ok():\n    assert True\n"
    elif failure == "mutation":
        test_code = "from pathlib import Path\n\ndef test_mutate():\n    Path('src/value.py').write_text('changed')\n"
    worker, request, copies, accepted, _, service, preparation = verification_fixture(
        worker_setup,
        test_code=test_code,
        value="value: int = '2'\n" if failure == "mypy" else "value: int = 2\n",
    )
    root = worker._store.event_journal_path(preparation.run_id).parent
    cancel = threading.Event()
    if failure == "approval":
        (root / "approvals/verification-approval.json").unlink()
    elif failure == "seed":
        from pathlib import Path

        (Path(preparation.requests[0].execution_directory) / "src/value.py").write_text(
            "unapproved"
        )
    elif failure == "cancel":
        cancel.set()
    elif failure == "handoff":
        from yipilot.infrastructure.artifact_store import FileArtifactStore

        original = FileArtifactStore.write_bytes

        def write(self, *args, **kwargs):
            if kwargs.get("relative_path") == "artifacts/change-handoff.json":
                raise OSError("fixture handoff failure")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(FileArtifactStore, "write_bytes", write)
    with pytest.raises(DomainError):
        service.run(
            copies,
            request,
            worker._policy,
            accepted,
            preparation.run_id,
            approval_id="verification-approval",
            cancel_event=cancel,
        )
    assert not (root / "artifacts/change-handoff.json").exists()
    assert worker._store.get_run(request.task_id, preparation.run_id).status.value == (
        "cancelled" if failure == "cancel" else "failed"
    )
    with pytest.raises(DomainError):
        service.handoff(copies, request, worker._policy, accepted, preparation.run_id)


def test_verification_preparation_failure_cancels_only_its_new_runs(
    worker_setup, monkeypatch
):
    from yipilot.application import sandbox_verification as module

    worker, request, copies, accepted, _, service, preparation = verification_fixture(
        worker_setup
    )
    previous = {r.run_id for r in worker._store.list_runs(request.task_id)}
    original = module.atomic_write_bytes

    def fail_seed(path, data):
        if path.name == "input-manifest.json":
            raise OSError("fixture seed failure")
        original(path, data)

    monkeypatch.setattr(module, "atomic_write_bytes", fail_seed)
    with pytest.raises(DomainError, match="SANDBOX_EVIDENCE_FAILED"):
        service.prepare(copies, request, worker._policy, accepted)
    created = [
        r for r in worker._store.list_runs(request.task_id) if r.run_id not in previous
    ]
    assert created and all(r.status.value == "cancelled" for r in created)
    assert (
        worker._store.get_run(request.task_id, preparation.run_id).status.value
        == "pending"
    )
