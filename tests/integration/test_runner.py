"""Real bundles, SQLite and authenticated HTTP; no host repository execution."""

import hashlib
import json
import os
import stat
import threading
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from yipilot.domain.errors import DomainError
from yipilot.domain.remote_execution import (
    IMAGE,
    PROFILE,
    JobRequest,
    JobResult,
    manifest_hash,
)
from yipilot.domain.sandbox import SourceFile
from yipilot.runner.http import make_server
from yipilot.runner.runtime import OpenSandboxRuntime
from yipilot.runner.service import Runner
from yipilot.workspace.remote_bundle import (
    bounded_zip,
    freeze_bundle,
    publish_result,
    reconstruct_result,
    unpack_input,
)
from yipilot.workspace.runner_client import RunnerClient


def frozen(tmp_path, *, key="example", timeout=60):
    source = tmp_path / key
    source.mkdir()
    (source / "main.py").write_text("print('fixture')\n", encoding="utf-8")
    (source / "unchanged.txt").write_text("frozen\n", encoding="utf-8")
    bundle = tmp_path / (key + ".zip")
    request = freeze_bundle(source, bundle, request_key=key, task_id="1", operation_id="operation",
                            run_id=key, script="main.py", timeout_seconds=timeout)
    return source, bundle, request


def rewritten(path, target, *, request_changes=None, source_changes=None, extra=None):
    with zipfile.ZipFile(path) as old, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as new:
        for item in old.infolist():
            data = old.read(item.filename)
            if item.filename == "request.json" and request_changes:
                request = json.loads(data)
                request.update(request_changes)
                data = json.dumps(request).encode()
            if source_changes and item.filename in source_changes:
                data = source_changes[item.filename]
            new.writestr(item, data)
        if extra:
            new.writestr(*extra)


class Runtime:
    def __init__(self, *, block=False, cleanup_fails=False):
        self.block = block
        self.cleanup_fails = cleanup_fails
        self.calls = 0
        self.cleanups = 0
        self.entered = threading.Event()

    def execute(self, job_id, request, work, store):
        self.calls += 1
        store.update(job_id, sandbox_id="12345678-1234-1234-1234-123456789abc")
        self.entered.set()
        if self.block:
            for _ in range(300):
                if store.get(job_id)[1].cancel_requested:
                    return "cancelled", None, b"", b""
                time.sleep(.01)
            raise AssertionError("Fixture did not receive cancellation")
        (work / "main.py").write_text("print('changed')\n", encoding="utf-8")
        (work / "new.txt").write_text("new\n", encoding="utf-8")
        return "exited", 0, b"ok\n", b""

    def cleanup(self, job_id, sandbox_id):
        self.cleanups += 1
        if self.cleanup_fails:
            raise OSError("Trusted cleanup failure")


def terminal(runner, job_id):
    for _ in range(100):
        status = runner.store.get(job_id)[1]
        if status.state not in {"running", "queued"}:
            return status
        time.sleep(.02)
    raise AssertionError("Runner did not finish")


def test_complete_job_reuses_frozen_input_and_durable_deduplication(tmp_path):
    source, bundle, request = frozen(tmp_path)
    runtime = Runtime()
    runner = Runner(tmp_path / "runner", runtime, disk_reserve=0)
    status = runner.submit(bundle)
    assert status.state == "queued" and runtime.calls == 0
    assert runner.submit(bundle).job_id == status.job_id
    (source / "unchanged.txt").write_text("changed live workspace\n")
    runner.start()
    try:
        assert terminal(runner, status.job_id).state == "succeeded"
        assert runtime.cleanups == 1
        result = reconstruct_result(bundle, runner.result_path(status.job_id), tmp_path / "verified", job_id=status.job_id)
        assert result.request_fingerprint == request.fingerprint
        assert result.changed == ("main.py", "new.txt") and result.deleted == ()
        assert (tmp_path / "verified/unchanged.txt").read_text() == "frozen\n"
        assert (tmp_path / "verified/new.txt").read_text() == "new\n"
    finally:
        runner.close()
    reopened = Runner(tmp_path / "runner", Runtime(), disk_reserve=0)
    assert reopened.submit(bundle).job_id == status.job_id
    assert reopened.store.get(status.job_id)[1].state == "succeeded"


@pytest.mark.parametrize("changes", [
    {"schema_version": 1}, {"script": "../main.py"}, {"script": "-c"}, {"image": "python:latest"},
    {"profile": "yipilot-python-v1"}, {"argv": ["sh"]}, {"host": "/root"}, {"timeout_seconds": 601},
    {"cpu_millis": 0}, {"memory_bytes": 1}, {"stdout_bytes": 9999999}, {"request_key": "../key"},
])
def test_invalid_contract_cannot_be_accepted(tmp_path, changes):
    _, bundle, _ = frozen(tmp_path)
    altered = tmp_path / "altered.zip"
    rewritten(bundle, altered, request_changes=changes)
    runner = Runner(tmp_path / "runner", Runtime(), disk_reserve=0)
    with pytest.raises(ValueError):
        runner.submit(altered)
    assert runner.store.list() == [] and list(runner.jobs.iterdir()) == []


def test_conflicting_key_and_corrupted_input_are_rejected(tmp_path):
    _, bundle, request = frozen(tmp_path)
    runner = Runner(tmp_path / "runner", Runtime(), disk_reserve=0)
    runner.submit(bundle)
    altered = tmp_path / "conflict.zip"
    rewritten(bundle, altered, request_changes={"timeout_seconds": request.timeout_seconds + 1})
    with pytest.raises(DomainError, match="REQUEST_CONFLICT"):
        runner.submit(altered)
    corrupt = tmp_path / "corrupt.zip"
    rewritten(bundle, corrupt, source_changes={"source/main.py": b"bad\n"})
    with pytest.raises(ValueError, match="differs"):
        runner.submit(corrupt)
    assert len(runner.store.list()) == 1


@pytest.mark.parametrize("extra", [("source/../escape", b"x"), ("unexpected", b"x"), ("request.json", b"{}")])
def test_extra_or_duplicate_archive_entries_are_rejected(tmp_path, extra):
    _, bundle, _ = frozen(tmp_path)
    altered = tmp_path / "extra.zip"
    rewritten(bundle, altered, extra=extra)
    with pytest.raises(ValueError):
        unpack_input(altered, tmp_path / "tree")


def test_archive_links_and_oversized_directory_are_rejected(tmp_path):
    _, bundle, _ = frozen(tmp_path)
    link = zipfile.ZipInfo("source/link")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    altered = tmp_path / "link.zip"
    rewritten(bundle, altered, extra=(link, b"/etc/passwd"))
    with pytest.raises(ValueError):
        unpack_input(altered, tmp_path / "tree")
    invalid = tmp_path / "many.zip"
    data = bytearray(bundle.read_bytes())
    end = data.rfind(b"PK\x05\x06")
    data[end + 8:end + 12] = b"\xff" * 4
    invalid.write_bytes(data)
    with pytest.raises(ValueError, match="directory"), bounded_zip(invalid):
        pytest.fail("Oversized directory was accepted")


def test_cancel_queued_and_running_jobs(tmp_path):
    _, bundle, _ = frozen(tmp_path)
    runtime = Runtime(block=True)
    runner = Runner(tmp_path / "runner", runtime, disk_reserve=0)
    status = runner.submit(bundle)
    runner.start()
    try:
        assert runtime.entered.wait(2)
        assert runner.cancel(status.job_id).cancel_requested
        assert terminal(runner, status.job_id).state == "cancelled"
        _, second, _ = frozen(tmp_path, key="second")
        status2 = runner.submit(second)
        runner.cancel(status2.job_id)
        assert terminal(runner, status2.job_id).state == "cancelled"
    finally:
        runner.close()


def test_cleanup_failure_holds_budget_and_prevents_next_job(tmp_path):
    _, bundle, _ = frozen(tmp_path)
    runtime = Runtime(cleanup_fails=True)
    runner = Runner(tmp_path / "runner", runtime, disk_reserve=0)
    first = runner.submit(bundle)
    runner.start()
    try:
        assert terminal(runner, first.job_id).state == "cleanup_failed"
        _, second, _ = frozen(tmp_path, key="second")
        second_job = runner.submit(second)
        time.sleep(.3)
        assert runner.store.get(second_job.job_id)[1].state == "queued" and runtime.calls == 1
        with pytest.raises(DomainError, match="RESULT_UNAVAILABLE"):
            runner.result_path(first.job_id)
    finally:
        runner.close()


def test_restart_reconciles_without_replaying_attempt(tmp_path):
    _, bundle, _ = frozen(tmp_path)
    runner = Runner(tmp_path / "runner", Runtime(), disk_reserve=0)
    status = runner.submit(bundle)
    runner.store.update(status.job_id, state="running", sandbox_id="12345678-1234-1234-1234-123456789abc")
    runtime = Runtime()
    restarted = Runner(tmp_path / "runner", runtime, disk_reserve=0)
    restarted.start()
    try:
        assert terminal(restarted, status.job_id).state == "uncertain"
        assert runtime.calls == 0 and runtime.cleanups == 1
    finally:
        restarted.close()


def test_unresolved_creation_is_quarantined():
    runtime = OpenSandboxRuntime(None)
    with pytest.raises(ValueError, match="unknown"):
        runtime.cleanup("a" * 32, None)


def test_admission_includes_sidecar_and_preserves_requested_memory(tmp_path):
    _, bundle, _ = frozen(tmp_path)
    runner = Runner(tmp_path / "runner", Runtime(), memory_budget=128 * 1024**2, disk_reserve=0)
    with pytest.raises(DomainError, match="BUDGET_UNAVAILABLE"):
        runner.submit(bundle)
    assert runner.store.list() == []


def test_authenticated_http_is_independent_of_client_connection(tmp_path):
    _, bundle, _ = frozen(tmp_path)
    runtime = Runtime()
    runner = Runner(tmp_path / "runner", runtime, disk_reserve=0)
    runner.start()
    server = make_server(runner, "fixture-key-" + "x" * 32, 0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        url = "http://127.0.0.1:" + str(server.server_address[1])
        with httpx.Client(base_url=url, trust_env=False) as http:
            assert http.get("/health").status_code == 401
            assert http.get("/health", headers={"Authorization": b"Bearer \xff"}).status_code == 401
            assert http.post("/v1/jobs", content=b"invalid").status_code == 401
            assert not runner.store.list()
        with httpx.Client(base_url=url, trust_env=False, headers={"Authorization": "Bearer fixture-key-" + "x" * 32}) as http:
            client = RunnerClient(http)
            assert http.get("/health").status_code == 200
            job = client.submit(bundle)
        assert terminal(runner, job.job_id).state == "succeeded"
        with httpx.Client(base_url=url, trust_env=False, headers={"Authorization": "Bearer fixture-key-" + "x" * 32}) as http:
            client = RunnerClient(http)
            assert client.submit(bundle).job_id == job.job_id
            assert client.collect(job.job_id, bundle, tmp_path / "verified").exit_code == 0
            assert http.get("/v1/jobs/../../etc/passwd").status_code == 404
        assert runtime.calls == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        runner.close()


def test_result_delta_and_modes_round_trip_and_tamper_is_rejected(tmp_path):
    _, bundle, request = frozen(tmp_path)
    now = datetime.now(UTC)
    changed = b"print('new')\n"
    files = (SourceFile(path="main.py", sha256=hashlib.sha256(changed).hexdigest(), size_bytes=len(changed), git_mode="100755"),)
    result = JobResult(job_id="a" * 32, request_fingerprint=request.fingerprint,
        input_manifest_hash=manifest_hash(request.files), output_manifest_hash=manifest_hash(files),
        sandbox_id="12345678-1234-1234-1234-123456789abc", reason="exited", exit_code=7,
        started_at=now, ended_at=now, files=files, changed=("main.py",), deleted=("unchanged.txt",),
        stdout_sha256=hashlib.sha256(b"").hexdigest(), stderr_sha256=hashlib.sha256(b"").hexdigest(), stdout_bytes=0, stderr_bytes=0)
    output = tmp_path / "result.zip"
    publish_result(output, result, {"main.py": changed}, b"", b"")
    verified = reconstruct_result(bundle, output, tmp_path / "verified", job_id="a" * 32)
    assert verified.exit_code == 7 and verified.files[0].git_mode == "100755"
    assert not (tmp_path / "verified/unchanged.txt").exists()
    if os.name != "nt":
        assert (tmp_path / "verified/main.py").stat().st_mode & 0o111
    altered = tmp_path / "bad-output.zip"
    rewritten(output, altered, source_changes={"source/main.py": b"tampered\n"})
    with pytest.raises(ValueError):
        reconstruct_result(bundle, altered, tmp_path / "bad", job_id="a" * 32)
    assert not (tmp_path / "bad").exists()


def test_contract_matches_fixed_server_profile():
    import runpy
    profile = runpy.run_path(str(Path(__file__).parents[2] / "deploy/opensandbox/policy.py"))
    assert IMAGE == profile["IMAGE"] and PROFILE == profile["PROFILE"]
    assert JobRequest.model_fields["schema_version"].default == 2


def test_manifest_rejects_oversized_path_components(tmp_path):
    _, _, request = frozen(tmp_path)
    path = "x" * 256 + ".py"
    file = SourceFile(path=path, sha256="0" * 64, size_bytes=0, git_mode="100644")
    with pytest.raises(ValueError, match="component"):
        JobRequest(**{**request.model_dump(), "script": path, "files": (file,)})


@pytest.mark.parametrize("sdk_timeout", [False, True])
def test_sdk_records_identity_before_readiness_and_awaits_manager_factory(tmp_path, monkeypatch, sdk_timeout):
    sdk = pytest.importorskip("opensandbox")
    _, bundle, request = frozen(tmp_path)
    runner = Runner(tmp_path / "runner", Runtime(), disk_reserve=0)
    job = runner.submit(bundle)
    work = tmp_path / "work"
    work.mkdir()
    calls = []
    sandbox_id = "12345678-1234-1234-1234-123456789abc"

    async def check_ready(**options):
        assert runner.store.get(job.job_id)[1].sandbox_id == sandbox_id
        calls.append("ready")

    async def command(argv, **options):
        assert argv == ["python", "-B", "./main.py"]
        assert options["opts"].uid == options["opts"].gid == 10001
        assert options["handlers"].skip_accumulation
        calls.append("command")
        return SimpleNamespace(exit_code=1 if sdk_timeout else 0,
                               error=SimpleNamespace(name="Timed out") if sdk_timeout else None)

    async def close():
        calls.append("close")

    async def create(*args, **options):
        assert options["skip_health_check"] is True and "health_check" not in options
        return SimpleNamespace(id=sandbox_id, check_ready=check_ready,
                               commands=SimpleNamespace(run=command), close=close)

    async def manager_create(connection):
        async def kill(identity):
            assert identity == sandbox_id
            calls.append("kill")
        return SimpleNamespace(kill_sandbox=kill, close=close)

    monkeypatch.setattr(sdk.Sandbox, "create", create)
    monkeypatch.setattr(sdk.SandboxManager, "create", manager_create)
    import yipilot.runner.runtime as runtime_module
    monkeypatch.setattr(runtime_module.os, "chown", lambda *_: None, raising=False)
    monkeypatch.setattr(runtime_module, "docker", lambda *args: [{
        "Config": {"Labels": {"yipilot.job_id": job.job_id}}, "State": {"Running": True, "Pid": 1}}])
    original_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **kw:
                        "0::/owned\n" if self.name == "cgroup" else original_read(self, *a, **kw))
    runtime = OpenSandboxRuntime(None, disk_reserve=0)
    outcome = runtime.execute(job.job_id, request, work, runner.store)
    assert outcome == (("timed_out", 1, b"", b"") if sdk_timeout else ("exited", 0, b"", b""))
    assert calls[:2] == ["ready", "command"]
    monkeypatch.setattr(runtime_module.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=b""))
    runtime.cleanup(job.job_id, sandbox_id)
    assert "kill" in calls
