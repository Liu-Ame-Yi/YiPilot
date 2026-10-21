import hashlib
import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    ApprovalDecision,
    ApprovalRecord,
    ArgvRule,
    ArtifactRef,
    ExecutionPolicy,
    ToolKind,
    ToolPolicy,
    fingerprint_model,
)
from yipilot.domain.remote_execution import TOOLS_IMAGE, RunnerSettings
from yipilot.domain.sandbox import (
    SandboxBudgets,
    SandboxMode,
    SandboxRequest,
    SandboxResult,
    source_path,
)
from yipilot.persistence.approval_store import FileApprovalStore
from yipilot.persistence.artifact_store import FileArtifactStore
from yipilot.persistence.task_store import FileTaskStore
from yipilot.workspace import sandbox_output as output_module
from yipilot.workspace import sandbox_sources as module
from yipilot.workspace.git_runner import GitRunner
from yipilot.workspace.repo_service import RepoService
from yipilot.workspace.runner_client import RunnerClient
from yipilot.workspace.sandbox_output import (
    SandboxOutputService,
    output_approval_fingerprint,
)
from yipilot.workspace.sandbox_sources import SandboxSourceService


def git(root: Path, *argv: str) -> str:
    return GitRunner().run(["git", *argv], cwd=root).stdout.strip()


@pytest.fixture
def setup(tmp_path: Path):
    source = tmp_path / "repo"
    source.mkdir()
    git(source, "init", "--initial-branch=main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    (source / "app.py").write_bytes(b"value = 1\n")
    (source / ".gitignore").write_bytes(b".env\n.venv/\nignored.txt\n")
    for name in (".env", "ignored.txt", "untracked.txt"):
        (source / name).write_text("local secret", encoding="utf-8")
    (source / ".venv").mkdir()
    (source / ".venv/secret").write_text("secret", encoding="utf-8")
    (source / ".yipilot").mkdir()
    (source / ".yipilot/settings.json").write_text("secret", encoding="utf-8")
    git(source, "add", "app.py", ".gitignore", ".yipilot")
    git(source, "commit", "-m", "base")
    base = git(source, "rev-parse", "HEAD")
    (source / "app.py").write_bytes(b"value = 2\n")
    git(source, "add", "app.py")
    git(source, "commit", "-m", "later")
    project = tmp_path / "project"
    project.mkdir()
    store = FileTaskStore(project)
    store.initialize_project()
    task = store.create_task("1")
    op = store.create_operation("1", "sandbox", "key", {})
    run = store.create_run("1", op.operation_id, "sandbox")
    metadata = RepoService(project, GitRunner()).add_and_sync("1", str(source), "repo")
    return SimpleNamespace(
        source=source,
        project=project,
        store=store,
        task=task,
        run=run,
        base=base,
        cache=metadata.cache_path,
        root=store.event_journal_path(run.run_id).parent,
        service=SandboxSourceService(project, GitRunner()),
    )


def prepare(s):
    return s.service.prepare(s.task, "repo", s.run, s.base)


@pytest.fixture
def configured_runner(tmp_path):
    RunnerClient.configure(RunnerSettings(endpoint="http://127.0.0.1:18090", credential_ref="env:FIXTURE_KEY"), tmp_path / "settings")
    return RunnerClient(settings_dir=tmp_path / "settings")


def test_cli_prepares_frozen_patch_data_and_requires_exact_human_approval(setup, monkeypatch, configured_runner):
    from yipilot.domain.patch_proposal import PatchProposal
    from yipilot.workflow.sandbox_application import SandboxCliService
    from yipilot.workspace.runner_worker import RunnerSandboxWorker

    s = setup
    monkeypatch.setattr(RunnerSandboxWorker, "execute", lambda *_a, **_k: pytest.fail("Worker invoked"))
    service = SandboxCliService(s.project, runner=configured_runner)
    proposal = PatchProposal(summary="change", patches=({"path": "app.py",
        "unified_diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 3\n"},))
    request = service.prepare("1", "repo", s.base, proposal, TOOLS_IMAGE)
    copies, loaded = service.load("1", request.run_id)
    assert loaded == request
    assert (copies.baseline / "app.py").read_bytes() == b"value = 1\n"
    assert (copies.execution / "app.py").read_bytes() == b"value = 1\n"
    assert request.input_manifest_hash
    with pytest.raises(DomainError, match="APPROVAL_NOT_FOUND"):
        service.execute(request, "missing")
    old = ApprovalRecord(approval_id="old-policy", task_id="1", operation_id=request.operation_id,
        run_id=request.run_id, action_type="sandbox_execution", target_fingerprint=request.fingerprint,
        policy_revision="sandbox-cli-apply-v1", decision=ApprovalDecision.APPROVED, decided_at=datetime.now(UTC))
    FileApprovalStore(s.store).save(old)
    with pytest.raises(DomainError, match="APPROVAL_STALE"):
        service.execute(request, old.approval_id)
    approval = ApprovalRecord(approval_id="fixture", task_id="1", operation_id=request.operation_id,
        run_id=request.run_id, action_type="sandbox_execution", target_fingerprint=request.fingerprint,
        policy_revision=request.policy_revision, decision=ApprovalDecision.APPROVED,
        decided_at=datetime.now(UTC), simulation_only=True)
    FileApprovalStore(s.store).save(approval)
    with pytest.raises(DomainError, match="human approval"):
        service.execute(request, approval.approval_id)
    service.cancel("1", request.run_id)
    assert s.store.get_run("1", request.run_id).status.value == "cancelled"


def test_cli_invalid_input_cancels_new_run_without_worker(setup, configured_runner):
    from yipilot.domain.patch_proposal import PatchProposal
    from yipilot.workflow.sandbox_application import SandboxCliService

    s = setup
    proposal = PatchProposal(summary="change", patches=({"path": "app.py",
        "unified_diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 3\n"},))
    with pytest.raises(ValueError):
        SandboxCliService(s.project, runner=configured_runner).prepare("1", "repo", s.base, proposal, "mutable:tag")
    new = next(r for r in s.store.list_runs("1") if r.stage == "sandbox_apply")
    assert new.status.value == "cancelled"
    assert not (s.store.event_journal_path(new.run_id).parent / "sandbox-cli-request.json").exists()
    assert not (s.store.event_journal_path(new.run_id).parent / "execution").exists()


def assert_clean(s) -> None:
    assert not (s.root / "baseline").exists()
    assert not (s.root / "execution").exists()
    assert not (s.root / "source-manifest.json").exists()


def test_frozen_commit_export_independent_copies_and_manifest(setup) -> None:
    s = setup
    copies = prepare(s)
    manifest_bytes = copies.manifest_path.read_bytes()
    assert copies.baseline == s.root / "baseline"
    assert copies.execution == s.root / "execution"
    for tree in (copies.baseline, copies.execution):
        assert sorted(p.name for p in tree.iterdir()) == [".gitignore", "app.py"]
        assert (tree / "app.py").read_bytes() == b"value = 1\n"
    assert not os.path.samefile(copies.baseline / "app.py", copies.execution / "app.py")
    assert copies.manifest.base_sha == s.base
    assert copies.manifest.operation_id == s.run.operation_id
    assert copies.manifest.repository_alias == "repo"
    assert copies.manifest_hash == hashlib.sha256(manifest_bytes).hexdigest()
    (copies.execution / "app.py").write_bytes(b"value = 3\n")
    s.service.validate(copies)
    assert (copies.baseline / "app.py").read_bytes() == b"value = 1\n"
    assert copies.manifest_path.read_bytes() == manifest_bytes
    assert (s.source / "app.py").read_bytes() == b"value = 2\n"


@pytest.mark.parametrize(
    "base", ["HEAD", "../main", "a" * 39, "f" * 40, "a" * 40 + "\n"]
)
def test_invalid_commit_leaves_no_copies(setup, base: str) -> None:
    with pytest.raises(DomainError, match="SANDBOX_BASE_INVALID"):
        setup.service.prepare(setup.task, "repo", setup.run, base)
    assert_clean(setup)


@pytest.mark.parametrize("name", ["baseline", "execution", "source-manifest.json"])
def test_directory_conflicts_are_not_overwritten(setup, name: str) -> None:
    conflict = setup.root / name
    conflict.mkdir()
    with pytest.raises(DomainError, match="SANDBOX_CONFLICT"):
        prepare(setup)
    assert conflict.is_dir()
    assert list(conflict.iterdir()) == []
    if name != "execution":
        assert not (setup.root / "execution").exists()


def test_completed_run_copies_are_never_reused(setup) -> None:
    copies = prepare(setup)
    before = copies.manifest_path.read_bytes()
    with pytest.raises(DomainError, match="SANDBOX_CONFLICT"):
        prepare(setup)
    assert copies.manifest_path.read_bytes() == before


def test_manifest_write_failure_cleans_both_copies(setup, monkeypatch) -> None:
    def fail(path, *args):
        path.write_text("partial", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(module, "atomic_write_json", fail)
    with pytest.raises(DomainError, match="SANDBOX_COPY_FAILED"):
        prepare(setup)
    assert_clean(setup)


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "C:/absolute",
        "C:relative",
        "a/../b",
        "a\\b",
        "a//b",
        "a/./b",
        "a\nname",
        "name:stream",
        "NUL.txt",
        "aux",
        "a.",
        "a /b",
    ],
)
def test_unsafe_export_paths_rejected(path: str) -> None:
    with pytest.raises(ValueError):
        source_path(path)


@pytest.mark.parametrize(
    ("mode", "path"),
    [
        ("120000", "link"),
        ("160000", "submodule"),
        ("100644", "../escape"),
        ("100644", "/absolute"),
        ("100644", "NUL.txt"),
    ],
)
def test_unsafe_git_entries_fail_before_materialization(
    setup, monkeypatch, mode, path
) -> None:
    real = setup.service._git.run_bytes

    def injected(argv, **kwargs):
        if argv[1] == "ls-tree":
            return subprocess.CompletedProcess(
                argv, 0, f"{mode} blob {'a' * 40}\t{path}\0".encode(), b""
            )
        return real(argv, **kwargs)

    monkeypatch.setattr(setup.service._git, "run_bytes", injected)
    with pytest.raises(DomainError):
        prepare(setup)
    assert_clean(setup)


def test_git_committed_symlink_is_rejected_without_host_symlink_permission(
    setup,
) -> None:
    # Git index fixture creates an actual committed symlink, never a host link.
    oid = git(setup.source, "hash-object", "-w", "app.py")
    git(setup.source, "update-index", "--add", "--cacheinfo", f"120000,{oid},link")
    git(setup.source, "commit", "-m", "link")
    git(setup.cache, "fetch", "origin")
    setup.base = git(setup.source, "rev-parse", "HEAD")
    with pytest.raises(DomainError, match="SANDBOX_SOURCE_DENIED"):
        prepare(setup)
    assert_clean(setup)


def test_reparse_point_rejected_during_validation(setup, monkeypatch) -> None:
    copies = prepare(setup)
    target = copies.execution / "app.py"
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == target:
            return SimpleNamespace(
                st_mode=stat.S_IFREG,
                st_nlink=1,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(DomainError, match="SANDBOX_PATH_DENIED"):
        setup.service.validate(copies)


def test_execution_symlink_is_rejected(setup) -> None:
    copies = prepare(setup)
    link = copies.execution / "link"
    try:
        link.symlink_to(copies.baseline, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"host symlink privilege unavailable: {error}")
    with pytest.raises(DomainError, match="SANDBOX_PATH_DENIED"):
        setup.service.validate(copies)


@pytest.mark.parametrize("target", ["baseline", "manifest_path"])
def test_frozen_evidence_drift_is_rejected(setup, target) -> None:
    copies = prepare(setup)
    path = copies.baseline / "app.py" if target == "baseline" else copies.manifest_path
    path.write_bytes(b"tampered\n")
    with pytest.raises(DomainError, match="SANDBOX_MANIFEST_INVALID"):
        setup.service.validate(copies)


def test_no_docker_or_repository_execution_during_preparation(
    setup, monkeypatch
) -> None:
    actual = subprocess.run
    calls = []

    def guarded(argv, **kwargs):
        assert argv[:2] in (
            ["git", "rev-parse"],
            ["git", "ls-tree"],
            ["git", "cat-file"],
            ["git", "ls-files"],
        )
        assert kwargs["shell"] is False
        calls.append(argv)
        return actual(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)
    prepare(setup)
    assert calls


def test_cross_run_association_is_rejected(setup) -> None:
    wrong = setup.run.model_copy(update={"operation_id": "other"})
    with pytest.raises(DomainError, match="SANDBOX_ASSOCIATION_INVALID"):
        setup.service.prepare(setup.task, "repo", wrong, setup.base)
    assert_clean(setup)


@pytest.mark.parametrize("content", [b"binary\x00file", b"\xff\xfe"])
def test_unpatchable_blob_rejected_and_cleaned(setup, content) -> None:
    (setup.source / "bad.bin").write_bytes(content)
    git(setup.source, "add", "bad.bin")
    git(setup.source, "commit", "-m", "binary")
    git(setup.cache, "fetch", "origin")
    setup.base = git(setup.source, "rev-parse", "HEAD")
    with pytest.raises(DomainError, match="SANDBOX_SOURCE_DENIED"):
        prepare(setup)
    assert_clean(setup)


def test_force_tracked_private_configuration_is_excluded(setup) -> None:
    git(setup.source, "add", "-f", ".env", ".venv", "ignored.txt")
    git(setup.source, "commit", "-m", "private files")
    git(setup.cache, "fetch", "origin")
    setup.base = git(setup.source, "rev-parse", "HEAD")
    copies = prepare(setup)
    for tree in (copies.baseline, copies.execution):
        assert sorted(p.name for p in tree.iterdir()) == [".gitignore", "app.py"]


def test_replace_refs_cannot_change_frozen_source(setup) -> None:
    later = git(setup.cache, "rev-parse", "HEAD")
    git(setup.cache, "replace", setup.base, later)
    copies = prepare(setup)
    assert (copies.baseline / "app.py").read_bytes() == b"value = 1\n"


def test_reparse_point_rejected_before_creating_copies(setup, monkeypatch) -> None:
    target = setup.root / "execution"
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == target:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR,
                st_nlink=1,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(DomainError, match="SANDBOX_PATH_DENIED"):
        prepare(setup)
    assert_clean(setup)


def test_hardlink_cannot_alias_baseline(setup) -> None:
    copies = prepare(setup)
    os.link(copies.baseline / "app.py", copies.execution / "alias.py")
    with pytest.raises(DomainError, match="SANDBOX_PATH_DENIED"):
        setup.service.validate(copies)


def test_worktree_scan_exception_is_exact_and_never_applies_to_worker_tree(tmp_path):
    pointer = b"gitdir: controller-owned\n"
    (tmp_path / ".git").write_bytes(pointer)
    with pytest.raises(DomainError, match="SANDBOX_SOURCE_DENIED"):
        module.scan_source_tree(tmp_path, ())
    assert module.scan_source_tree(tmp_path, (), git_pointer=pointer)[0] == ()
    (tmp_path / ".git").write_bytes(b"gitdir: different\n")
    with pytest.raises(DomainError, match="SANDBOX_OUTPUT_DRIFT"):
        module.scan_source_tree(tmp_path, (), git_pointer=pointer)
    (tmp_path / ".git").unlink()
    with pytest.raises(DomainError, match="SANDBOX_OUTPUT_DRIFT"):
        module.scan_source_tree(tmp_path, (), git_pointer=pointer)


@pytest.fixture
def output_setup(setup, monkeypatch):
    monkeypatch.setattr(output_module, "verify_remote_result", lambda root, request, result: result)
    s = setup
    s.copies = prepare(s)
    s.output = SandboxOutputService(s.project, GitRunner())
    s.policy = ExecutionPolicy(
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
    s.request = SandboxRequest.model_validate(
        {
            "task_id": s.task.task_id,
            "operation_id": s.run.operation_id,
            "run_id": s.run.run_id,
            "policy_revision": s.policy.revision,
            "mode": SandboxMode.OFFLINE_WORKSPACE_WRITE,
            "execution_directory": str(s.copies.execution),
            "baseline_manifest_hash": s.copies.manifest_hash,
            "image_digest": TOOLS_IMAGE,
            "runner_endpoint": "http://127.0.0.1:8090",
            "input_manifest_hash": fingerprint_model(s.copies.manifest),
            "tool_id": "tests",
            "argv": ("python", "-m", "pytest"),
            "budgets": SandboxBudgets(
                wall_clock_seconds=60,
                cpu_count=1,
                memory_bytes=512 * 1024**2,
                pid_limit=128,
                stdout_bytes=1024,
                stderr_bytes=1024,
            ),
        },
        context={"policy": s.policy},
    )
    records = []
    for stream in ("stdout", "stderr"):
        record = FileArtifactStore(s.store).write_bytes(
            s.task.task_id,
            s.run.run_id,
            producer="fixture",
            relative_path=f"artifacts/{stream}.txt",
            content=b"",
            input_fingerprint=s.request.fingerprint,
        )
        records.append(
            ArtifactRef(
                task_id=record.task_id,
                operation_id=record.operation_id,
                run_id=record.run_id,
                artifact_id=record.artifact_id,
                relative_path=record.relative_path,
                sha256=record.sha256,
            )
        )
    (s.copies.execution / "app.py").write_bytes(b"value = 3\n")
    s.result = SandboxResult(
        task_id=s.task.task_id,
        operation_id=s.run.operation_id,
        run_id=s.run.run_id,
        request_fingerprint=s.request.fingerprint,
        job_id="b" * 32,
        remote_request_fingerprint="b" * 64,
        sandbox_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        profile="yipilot-tools-v3",
        writers_stopped=True, resources_removed=True,
        image_digest=s.request.image_digest,
        effective_uid=10001,
        effective_gid=10001,
        resource_limits=s.request.budgets,
        reason="exited",
        exit_code=0,
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        stdout_artifact_ref=records[0],
        stderr_artifact_ref=records[1],
        stdout_bytes=0,
        stderr_bytes=0,
        output_manifest_hash="c" * 64,
    )
    approve_output(s)
    return s


def approve_output(s):
    # Fixture evidence only: no Docker worker or repository command is simulated.
    s.candidate = s.output.inspect(s.copies, s.request, s.policy)
    s.result = s.result.model_copy(
        update={"output_manifest_hash": s.candidate.manifest.fingerprint}
    )
    s.approval = ApprovalRecord(
        approval_id=f"output-approval-{len(FileApprovalStore(s.store).list(s.task.task_id, s.run.run_id))}",
        task_id=s.task.task_id,
        operation_id=s.run.operation_id,
        run_id=s.run.run_id,
        action_type="sandbox_output",
        policy_revision=s.policy.revision,
        target_fingerprint=output_approval_fingerprint(
            s.request, s.candidate.patch_sha256
        ),
        decision=ApprovalDecision.APPROVED,
        decided_at=datetime.now(UTC),
    )
    FileApprovalStore(s.store).save(s.approval)


@pytest.mark.parametrize("invalid", ["missing", "wrong_run", "wrong_hash", "unbound"])
def test_worker_rejects_invalid_seed_before_any_docker_command(
    output_setup, monkeypatch, invalid
):
    from yipilot.domain.execution import fingerprint_model
    from yipilot.workspace.runner_worker import RunnerSandboxWorker

    s = output_setup
    seed = s.copies.manifest.model_copy(
        update={
            "files": module.scan_source_tree(
                s.copies.execution, s.copies.manifest.files
            )[0]
        }
    )
    if invalid == "wrong_run":
        seed = seed.model_copy(update={"run_id": "another-run"})
    (s.root / "input-manifest.json").write_text(
        seed.model_dump_json(), encoding="utf-8"
    )
    request = s.request.model_copy(
        update={"input_manifest_hash": fingerprint_model(seed)}
    )
    if invalid == "wrong_hash":
        request = request.model_copy(update={"input_manifest_hash": "d" * 64})
    elif invalid == "unbound":
        request = request.model_copy(update={"input_manifest_hash": None})
    worker = RunnerSandboxWorker(
        s.project,
        s.policy,
        s.copies,
        input_manifest=None if invalid == "missing" else seed,
        runner=RunnerClient(),
    )

    def forbidden(*args):
        pytest.fail("invalid seed must not reach Docker")

    monkeypatch.setattr(worker._runner, "submit", forbidden)
    with pytest.raises(DomainError, match="SANDBOX_MANIFEST_INVALID"):
        worker.freeze(request)


def collect_output(s):
    return s.output.collect(
        s.copies,
        s.request,
        s.policy,
        s.result,
        approved_patch_sha256=s.candidate.patch_sha256,
        approval_id=s.approval.approval_id,
    )


def assert_no_output(s):
    assert not (s.root / "sandbox-output").exists()
    assert not list(s.root.glob(".sandbox-output-*.tmp"))
    assert not list(s.root.glob(".diff-*"))


@pytest.mark.parametrize(
    "change",
    ["modify", "add", "delete", "empty", "no-newline", "crlf", "unicode", "no-op"],
)
def test_trusted_diff_roundtrip_and_complete_bundle(output_setup, change):
    s = output_setup
    if change == "add":
        (s.copies.execution / "new.py").write_bytes(b"new = 1\n")
    elif change == "delete":
        (s.copies.execution / "app.py").unlink()
    elif change == "empty":
        (s.copies.execution / "empty.py").write_bytes(b"")
    elif change == "no-newline":
        (s.copies.execution / "app.py").write_bytes(b"value = 3")
    elif change == "crlf":
        (s.copies.execution / "app.py").write_bytes(b"value = 3\r\n")
    elif change == "unicode":
        (s.copies.execution / "中文 name.py").write_bytes(b"value = 3\n")
    elif change == "no-op":
        (s.copies.execution / "app.py").write_bytes(b"value = 1\n")
    approve_output(s)
    evidence = collect_output(s)
    patch = s.output.verify(s.copies, s.request, s.policy, evidence)
    assert patch == s.candidate.patch
    assert evidence.output_manifest_hash == s.result.output_manifest_hash
    assert {p.name for p in (s.root / "sandbox-output").iterdir()} == {
        "patch.diff",
        "manifest.json",
        "request.json",
        "result.json",
        "policy.json",
        "evidence.json",
    }
    assert (s.copies.baseline / "app.py").read_bytes() == b"value = 1\n"
    assert (s.source / "app.py").read_bytes() == b"value = 2\n"
    # Prove patch compatibility only on a disposable test copy, never the task worktree.
    if patch:
        target = s.root / "patch-check"
        shutil.copytree(s.copies.baseline, target)
        git(target, "apply", "--check", str(s.root / "sandbox-output/patch.diff"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_digest", "sha256:" + "d" * 64),
        ("request_fingerprint", "d" * 64),
        ("output_manifest_hash", "d" * 64),
        ("exit_code", 1),
        ("reason", "timed_out"),
        ("reason", "cancelled"),
        ("reason", "output_limit"),
        ("reason", "runtime_error"),
        ("writers_stopped", False),
        ("resources_removed", False),
        ("effective_uid", 0),
        ("operation_id", "other"),
        ("run_id", "other"),
        ("stdout_bytes", 1),
    ],
)
def test_result_facts_drift_never_publishes_output(output_setup, field, value):
    s = output_setup
    s.result = s.result.model_copy(update={field: value})
    with pytest.raises(DomainError):
        collect_output(s)
    assert_no_output(s)


def test_resource_limit_drift_is_rejected(output_setup):
    s = output_setup
    s.result = s.result.model_copy(
        update={
            "resource_limits": s.request.budgets.model_copy(update={"pid_limit": 32})
        }
    )
    with pytest.raises(DomainError, match="SANDBOX_OUTPUT_INVALID"):
        collect_output(s)
    assert_no_output(s)


@pytest.mark.parametrize("target", ["baseline", "manifest", "execution", "stdout"])
def test_live_source_or_log_drift_is_rejected(output_setup, target):
    s = output_setup
    path = {
        "baseline": s.copies.baseline / "app.py",
        "manifest": s.copies.manifest_path,
        "execution": s.copies.execution / "app.py",
        "stdout": s.root / "artifacts/stdout.txt",
    }[target]
    path.write_bytes(b"tampered\n")
    with pytest.raises(DomainError):
        collect_output(s)
    assert_no_output(s)


@pytest.mark.parametrize(
    "field",
    ["policy_revision", "run_id", "target_fingerprint", "simulation_only", "decision"],
)
def test_missing_or_stale_approval_is_rejected(output_setup, field):
    s = output_setup
    value = {
        "policy_revision": "other",
        "run_id": "other",
        "target_fingerprint": "e" * 64,
        "simulation_only": True,
        "decision": ApprovalDecision.DENIED,
    }[field]
    approval = s.approval.model_copy(update={field: value})
    with FileTaskStore(s.project).db.transaction() as connection:
        connection.execute("UPDATE records SET body=? WHERE kind='approval' AND id=?", (approval.model_dump_json(), s.approval.approval_id))
    with pytest.raises(DomainError):
        collect_output(s)
    assert_no_output(s)


@pytest.mark.parametrize(
    "name", [".git", ".env", ".venv", ".yipilot", ".ssh", ".gitconfig"]
)
def test_forbidden_output_is_not_hidden_by_diff(output_setup, name):
    s = output_setup
    (s.copies.execution / name).write_bytes(b"forbidden")
    with pytest.raises(DomainError):
        collect_output(s)
    assert_no_output(s)


@pytest.mark.parametrize(
    "name",
    [
        "patch.diff",
        "manifest.json",
        "request.json",
        "result.json",
        "evidence.json",
        "policy.json",
    ],
)
def test_consumption_rechecks_every_evidence_file(output_setup, name):
    s = output_setup
    evidence = collect_output(s)
    (s.root / "sandbox-output" / name).write_bytes(b"tampered")
    with pytest.raises(DomainError):
        s.output.verify(s.copies, s.request, s.policy, evidence)


def test_post_collection_execution_changes_invalidate_evidence(output_setup):
    s = output_setup
    evidence = collect_output(s)
    (s.copies.execution / "app.py").write_bytes(b"later mutation")
    with pytest.raises(DomainError):
        s.output.verify(s.copies, s.request, s.policy, evidence)


def test_partial_evidence_write_failure_leaves_no_consumable_bundle(
    output_setup, monkeypatch
):
    s = output_setup
    actual = output_module.atomic_write_bytes

    def fail(path, content):
        if path.name == "result.json":
            raise OSError("disk full")
        actual(path, content)

    monkeypatch.setattr(output_module, "atomic_write_bytes", fail)
    with pytest.raises(DomainError, match="SANDBOX_EVIDENCE_FAILED"):
        collect_output(s)
    assert_no_output(s)


def test_execution_change_during_git_diff_is_rejected(output_setup, monkeypatch):
    s = output_setup
    actual = s.output._git.run_bytes

    def mutate(argv, **kwargs):
        result = actual(argv, **kwargs)
        (s.copies.execution / "app.py").write_bytes(b"concurrent mutation")
        return result

    monkeypatch.setattr(s.output._git, "run_bytes", mutate)
    with pytest.raises(DomainError, match="SANDBOX_OUTPUT_DRIFT"):
        collect_output(s)
    assert_no_output(s)


def test_no_docker_apply_or_repository_execution_in_collector(
    output_setup, monkeypatch
):
    s = output_setup
    actual = subprocess.run

    def guarded(argv, **kwargs):
        assert argv[0] == "git"
        assert "diff" in argv and "--no-index" in argv
        assert "--no-ext-diff" in argv and "--no-textconv" in argv
        assert "apply" not in argv
        assert kwargs["shell"] is False
        return actual(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)
    collect_output(s)


def test_exact_patch_bytes_are_not_redacted(output_setup):
    s = output_setup
    content = b'api_key = "sk-fixture-not-a-real-secret-12345678901234567890"\n'
    (s.copies.execution / "app.py").write_bytes(content)
    approve_output(s)
    evidence = collect_output(s)
    patch = s.output.verify(s.copies, s.request, s.policy, evidence)
    assert b"+" + content in patch
    assert hashlib.sha256(patch).hexdigest() == evidence.patch_sha256


def test_output_conflict_preserves_existing_evidence(output_setup):
    s = output_setup
    first = collect_output(s)
    with pytest.raises(DomainError, match="SANDBOX_CONFLICT"):
        collect_output(s)
    assert s.output.verify(s.copies, s.request, s.policy, first) == s.candidate.patch


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "binary", "case-rename", "budget"]
)
def test_unsafe_execution_never_produces_a_diff_bundle(output_setup, kind):
    s = output_setup
    path = s.copies.execution / "unsafe"
    if kind == "symlink":
        path.symlink_to(s.copies.baseline, target_is_directory=True)
    elif kind == "hardlink":
        os.link(s.copies.baseline / "app.py", path)
    elif kind == "binary":
        path.write_bytes(b"\x00")
    elif kind == "case-rename":
        (s.copies.execution / "app.py").rename(s.copies.execution / "APP.py")
    else:
        path.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(DomainError):
        collect_output(s)
    assert_no_output(s)


def test_same_size_log_tampering_is_rejected(output_setup):
    s = output_setup
    store = FileArtifactStore(s.store)
    record = store.write_bytes(
        s.task.task_id,
        s.run.run_id,
        producer="fixture",
        relative_path="artifacts/new-stdout.txt",
        content=b"yes",
        input_fingerprint=s.request.fingerprint,
    )
    ref = ArtifactRef(
        task_id=record.task_id,
        operation_id=record.operation_id,
        run_id=record.run_id,
        artifact_id=record.artifact_id,
        relative_path=record.relative_path,
        sha256=record.sha256,
    )
    s.result = s.result.model_copy(
        update={"stdout_artifact_ref": ref, "stdout_bytes": 3}
    )
    (s.root / "artifacts/new-stdout.txt").write_bytes(b"bad")
    with pytest.raises(DomainError, match="ARTIFACT_STALE"):
        collect_output(s)
    assert_no_output(s)


def test_plain_patch_disagrees_with_approved_diff(output_setup):
    s = output_setup
    (s.copies.execution / "app.py").write_bytes(b"value = 99\n")
    # Even if a reported manifest matches the changed output, exact approval must hold.
    candidate = s.output.inspect(s.copies, s.request, s.policy)
    s.result = s.result.model_copy(
        update={"output_manifest_hash": candidate.manifest.fingerprint}
    )
    with pytest.raises(DomainError, match="SANDBOX_OUTPUT_UNAPPROVED"):
        collect_output(s)
    assert_no_output(s)
