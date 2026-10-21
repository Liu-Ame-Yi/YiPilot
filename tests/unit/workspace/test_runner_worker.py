import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    ArgvRule,
    ExecutionPolicy,
    ToolKind,
    ToolPolicy,
    fingerprint_model,
)
from yipilot.domain.remote_execution import TOOLS_IMAGE, JobRequest
from yipilot.domain.sandbox import (
    SandboxBudgets,
    SandboxMode,
    SandboxRequest,
    SourceFile,
    SourceManifest,
)
from yipilot.persistence.task_store import FileTaskStore
from yipilot.workspace.runner_client import RunnerClient
from yipilot.workspace.runner_worker import RunnerSandboxWorker
from yipilot.workspace.sandbox_sources import PreparedSources


@pytest.mark.parametrize("failure", ["rejected_submit", "lost_authorization", "unknown_submission", "invalid_output", "missing_diagnostics", "unavailable_diagnostics", "wrong_diagnostics"])
def test_request_rejection_and_unknown_remote_outcome_are_distinct(tmp_path, failure):
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1")
    operation = store.create_operation("1", "sandbox_apply", "once", {})
    run = store.create_run("1", operation.operation_id, "sandbox_apply")
    root = store.event_journal_path(run.run_id).parent
    content = b"value = 1\n"
    file = SourceFile(path="value.py", sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content), git_mode="100644")
    seed = SourceManifest(task_id="1", operation_id=operation.operation_id, run_id=run.run_id,
        repository_alias="repo", base_sha="a" * 40, created_at=datetime.now(UTC), files=(file,))
    for name in ("baseline", "execution"):
        (root / name).mkdir()
        (root / name / file.path).write_bytes(content)
    raw = seed.model_dump_json().encode()
    (root / "source-manifest.json").write_bytes(raw)
    (root / "input-manifest.json").write_bytes(raw)
    copies = PreparedSources(root / "baseline", root / "execution", root / "source-manifest.json",
                             hashlib.sha256(raw).hexdigest(), seed)
    policy = ExecutionPolicy(revision="fixture-v2", tools=(ToolPolicy(tool_id="tests", kind=ToolKind.TEST,
        executable="python", argv_schema=(ArgvRule(literal="-m"), ArgvRule(literal="pytest")),
        max_timeout_seconds=60, max_output_bytes=1024),))
    request = SandboxRequest.model_validate({"task_id": "1", "operation_id": operation.operation_id,
        "run_id": run.run_id, "policy_revision": policy.revision, "mode": SandboxMode.OFFLINE_WORKSPACE_WRITE,
        "execution_directory": str(copies.execution), "baseline_manifest_hash": copies.manifest_hash,
        "input_manifest_hash": fingerprint_model(seed), "image_digest": TOOLS_IMAGE,
        "runner_endpoint": "http://127.0.0.1", "tool_id": "tests", "argv": ("python", "-m", "pytest"),
        "budgets": SandboxBudgets(wall_clock_seconds=60, cpu_count=1, memory_bytes=512 * 1024**2,
                                  pid_limit=128, stdout_bytes=1024, stderr_bytes=1024)}, context={"policy": policy})
    calls = []
    paths = []
    def handle(incoming):
        calls.append(incoming.method)
        paths.append(incoming.url.path)
        job = JobRequest.model_validate_json((root / "remote-job/job-request.json").read_bytes())
        if incoming.url.path.endswith("/diagnostics"):
            assert incoming.method == "GET"
            if failure == "missing_diagnostics":
                return httpx.Response(404)
            if failure == "unavailable_diagnostics":
                raise httpx.ConnectError("lost diagnostic connection")
            diagnostic = {"job_id": "b" * 32, "request_fingerprint": job.fingerprint,
                "sandbox_id": None, "error_code": "OUTPUT_INVALID", "failure_stage": "output_collection",
                "reason": "exited", "exit_code": 0, "started_at": datetime.now(UTC).isoformat(),
                "ended_at": datetime.now(UTC).isoformat(), "stdout": "cached test output", "stderr": ""}
            if failure == "wrong_diagnostics":
                diagnostic["request_fingerprint"] = "0" * 64
            return httpx.Response(200, json=diagnostic)
        if incoming.method == "POST":
            assert "Transfer-Encoding" not in incoming.headers
            assert int(incoming.headers["Content-Length"]) == len(incoming.read())
            if failure == "rejected_submit":
                return httpx.Response(409, json={"error": "BUDGET_UNAVAILABLE"})
            job = JobRequest.model_validate_json((root / "remote-job/job-request.json").read_bytes())
            return httpx.Response(202, json={"job_id": "b" * 32, "request_fingerprint": job.fingerprint, "state": "queued"})
        if failure in {"invalid_output", "missing_diagnostics", "unavailable_diagnostics", "wrong_diagnostics"}:
            return httpx.Response(200, json={"job_id": "b" * 32, "request_fingerprint": job.fingerprint,
                                            "state": "failed", "error_code": "OUTPUT_INVALID"})
        return httpx.Response(404 if failure == "unknown_submission" else 401)
    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handle)) as http:
        worker = RunnerSandboxWorker(tmp_path, policy, copies, input_manifest=seed, runner=RunnerClient(http))
        worker.freeze(request)
        if failure == "unknown_submission":
            job = JobRequest.model_validate_json((root / "remote-job/job-request.json").read_bytes())
            (root / "remote-job/submission.json").write_text(json.dumps({"request_fingerprint": request.fingerprint,
                                                                       "job_fingerprint": job.fingerprint}))
        store.set_run_status(run.run_id, "running")
        expected = ("RUNNER_REQUEST_REJECTED" if failure == "rejected_submit" else
                    "SANDBOX_EVIDENCE_FAILED" if failure == "wrong_diagnostics" else
                    "REMOTE_OUTPUT_INVALID" if failure in {"invalid_output", "missing_diagnostics", "unavailable_diagnostics"} else
                    "REMOTE_RESULT_PENDING")
        with pytest.raises(DomainError, match=expected):
            worker.execute(request)
    assert calls.count("POST") == (0 if failure == "unknown_submission" else 1)
    assert (copies.execution / file.path).read_bytes() == content
    assert not (root / "remote-job/result.json").exists()
    if failure in {"invalid_output", "missing_diagnostics", "unavailable_diagnostics", "wrong_diagnostics"}:
        assert paths[-1].endswith("/diagnostics") and not any(p.endswith("/result") for p in paths)
    if failure == "invalid_output":
        diagnostic = json.loads((root / "remote-job/diagnostics.json").read_bytes())
        assert diagnostic["stdout"] == "cached test output"
        assert diagnostic["job_id"] == "b" * 32
    elif failure in {"missing_diagnostics", "unavailable_diagnostics", "wrong_diagnostics"}:
        assert not (root / "remote-job/diagnostics.json").exists()
