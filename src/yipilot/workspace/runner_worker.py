"""One approved Run -> one frozen remote Job; no local repository execution."""

import hashlib
import json
import time
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import httpx

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ArtifactRef, ExecutionPolicy, fingerprint_model
from yipilot.domain.remote_execution import (
    TOOLS_IMAGE,
    TOOLS_PROFILE,
    JobRequest,
    JobResult,
    JobStatus,
)
from yipilot.domain.sandbox import (
    SandboxOutputManifest,
    SandboxRequest,
    SandboxResult,
    SourceFile,
    SourceManifest,
)
from yipilot.persistence.artifact_store import (
    FileArtifactStore,
    artifact_reference,
    redact_artifact_bytes,
)
from yipilot.persistence.filesystem import atomic_write_bytes
from yipilot.persistence.ownership import project_lock
from yipilot.persistence.task_store import FileTaskStore
from yipilot.security.paths import read_evidence, require_plain_path
from yipilot.workspace.remote_bundle import (
    bounded_zip,
    freeze_bundle,
    read_entry,
    reconstruct_result,
    result_metadata,
    validate_source_bytes,
    verify_input_bytes,
    write_source,
)
from yipilot.workspace.runner_client import RunnerClient
from yipilot.workspace.sandbox_sources import (
    PreparedSources,
    SandboxSourceService,
    scan_source_tree,
)

SCRIPT = "yipilot-runner-main.py"
RESULT_ARTIFACTS = ("remote-job/result.json", "remote-job/job-request.json", "remote-job/status.json",
                    "remote-job/business-request.json", "remote-job/policy.json")


def read_remote_request(root: Path) -> SandboxRequest:
    policy = ExecutionPolicy.model_validate_json(read_evidence(root / "remote-job/policy.json"))
    return SandboxRequest.model_validate_json(read_evidence(root / "remote-job/business-request.json"), context={"policy": policy})


def cancel_remote_job(root: Path, request: SandboxRequest, runner: RunnerClient) -> JobStatus | None:
    """Reconcile/cancel only; absence or a lost connection never starts another Job."""
    if not (root / "remote-job/submission.json").exists():
        return None
    if (root / "remote-job/result.json").exists():
        verify_remote_result(root, request)
        return JobStatus.model_validate_json(read_evidence(root / "remote-job/status.json"))
    if runner.endpoint != request.runner_endpoint:
        raise DomainError("REMOTE_RESULT_PENDING", "Restore the approved Runner endpoint before reconciliation")
    try:
        job = validate_job(root, request)
        status = runner.lookup(job)
        if status is None:
            raise DomainError("REMOTE_RESULT_PENDING", "Submission outcome remains unknown; no replay")
        if status.state in {"queued", "running"}:
            status = runner.cancel(status.job_id)
        deadline = time.monotonic() + 30
        while status.state in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(.2)
            status = runner.status(status.job_id)
        if status.request_fingerprint != job.fingerprint or status.state not in {"succeeded", "failed", "cancelled", "uncertain"}:
            raise DomainError("REMOTE_RESULT_PENDING", "Remote stop/cleanup remains unconfirmed")
        atomic_write_bytes(root / "remote-job/status.json", status.model_dump_json().encode())
        return status
    except httpx.HTTPError as error:
        raise DomainError("REMOTE_RESULT_PENDING", "Original Job is unavailable for reconciliation") from error


def launcher(request: SandboxRequest) -> bytes:
    return ("import pathlib,subprocess\npathlib.Path(" + repr(SCRIPT) + ").unlink()\n"
            "raise SystemExit(subprocess.run(" + repr(list(request.argv)) + ",check=False).returncode)\n").encode()


def validate_job(root: Path, request: SandboxRequest) -> JobRequest:
    if read_remote_request(root) != request:
        raise ValueError("Prepared business request differs")
    seed = SourceManifest.model_validate_json(read_evidence(root / "input-manifest.json"))
    if ((seed.task_id, seed.operation_id, seed.run_id) != (request.task_id, request.operation_id, request.run_id)
            or fingerprint_model(seed) != request.input_manifest_hash):
        raise ValueError("Approved seed differs")
    content = launcher(request)
    script = SourceFile(path=SCRIPT, sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content), git_mode="100644")
    if any(f.path.split("/")[0].casefold() == SCRIPT.casefold() for f in seed.files):
        raise ValueError("Reserved Runner launcher path")
    expected = JobRequest(request_key=request.run_id, task_id=request.task_id, operation_id=request.operation_id,
        policy_revision="python-isolated-script-v2",
        run_id=request.run_id, script=SCRIPT, files=tuple(sorted((*seed.files, script), key=lambda f: f.path)),
        timeout_seconds=int(request.budgets.wall_clock_seconds), memory_bytes=request.budgets.memory_bytes,
        cpu_millis=int(request.budgets.cpu_count * 1000), stdout_bytes=request.budgets.stdout_bytes,
        stderr_bytes=request.budgets.stderr_bytes, profile=TOOLS_PROFILE, image=request.image_digest)
    verify_input_bytes(root / "remote-job/input.zip", expected)
    if JobRequest.model_validate_json(read_evidence(root / "remote-job/job-request.json")) != expected:
        raise ValueError("Frozen Job differs from approved request")
    return expected


def business_result(request: SandboxRequest, job: JobResult, refs: tuple[ArtifactRef, ArtifactRef],
                    stdout: bytes, stderr: bytes) -> SandboxResult:
    output = SandboxOutputManifest(task_id=request.task_id, operation_id=request.operation_id, run_id=request.run_id,
        request_fingerprint=request.fingerprint, baseline_manifest_hash=request.baseline_manifest_hash, files=job.files)
    return SandboxResult(task_id=request.task_id, operation_id=request.operation_id, run_id=request.run_id,
        request_fingerprint=request.fingerprint, remote_request_fingerprint=job.request_fingerprint,
        job_id=job.job_id, sandbox_id=job.sandbox_id, profile=TOOLS_PROFILE, image_digest=job.image,
        effective_uid=job.task_uid, effective_gid=job.task_gid, resource_limits=request.budgets,
        reason=job.reason, exit_code=job.exit_code, started_at=job.started_at, ended_at=job.ended_at,
        stdout_artifact_ref=refs[0], stderr_artifact_ref=refs[1], stdout_bytes=len(redact_artifact_bytes(stdout)),
        stderr_bytes=len(redact_artifact_bytes(stderr)), output_manifest_hash=output.fingerprint,
        writers_stopped=job.writers_stopped, resources_removed=job.resources_removed)


def verify_remote_result(root: Path, request: SandboxRequest, result: SandboxResult | None = None) -> SandboxResult:
    """Recheck immutable packages; output collection separately hashes the live tree."""
    try:
        frozen = validate_job(root, request)
        stored = SandboxResult.model_validate_json(read_evidence(root / "remote-job/result.json"))
        if result is not None and stored != result:
            raise ValueError("Business result differs")
        status = JobStatus.model_validate_json(read_evidence(root / "remote-job/status.json"))
        if (status.request_fingerprint != frozen.fingerprint or status.job_id != stored.job_id
                or status.state not in {"succeeded", "failed", "cancelled"} or status.result_expired):
            raise ValueError("Terminal Job binding differs")
        with bounded_zip(root / "remote-job/result.zip") as bundle:
            job = result_metadata(bundle, frozen, stored.job_id)
            for file in job.files:
                if file.path in job.changed:
                    validate_source_bytes(file, read_entry(bundle, "source/" + file.path, file.size_bytes))
        expected_state = "succeeded" if job.reason == "exited" and job.exit_code == 0 else (
            "cancelled" if job.reason == "cancelled" else "failed")
        if status.state != expected_state or status.sandbox_id != job.sandbox_id:
            raise ValueError("Terminal state or sandbox identity differs")
        if any(f.path.split("/")[0].casefold() == SCRIPT.casefold() for f in job.files):
            raise ValueError("Launcher leaked into output")
        with bounded_zip(root / "remote-job/result.zip") as bundle:
            streams = tuple(read_entry(bundle, name + ".log", getattr(request.budgets, name + "_bytes"))
                            for name in ("stdout", "stderr"))
        refs = (stored.stdout_artifact_ref, stored.stderr_artifact_ref)
        for ref, content in zip(refs, streams, strict=True):
            if ref.sha256 != hashlib.sha256(redact_artifact_bytes(content)).hexdigest():
                raise ValueError("Redacted log differs from verified Job log")
        if stored != business_result(request, job, refs, streams[0], streams[1]):
            raise ValueError("Remote execution facts differ")
        return stored
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        raise DomainError("SANDBOX_EVIDENCE_FAILED", "Remote execution evidence is incomplete or stale") from error


class RunnerSandboxWorker:
    def __init__(self, project_root: Path, policy: ExecutionPolicy, prepared: PreparedSources,
                 *, input_manifest: SourceManifest, runner: RunnerClient) -> None:
        self._root, self._policy, self._prepared = project_root, policy, prepared
        self._seed, self._runner = input_manifest, runner
        self._store = FileTaskStore(project_root)
        from yipilot.workspace.git_runner import GitRunner
        self._sources = SandboxSourceService(project_root, GitRunner())

    def freeze(self, request: SandboxRequest) -> None:
        root = self._prepared.execution.parent
        if (not isinstance(self._seed, SourceManifest)
                or (self._seed.task_id, self._seed.operation_id, self._seed.run_id) != (request.task_id, request.operation_id, request.run_id)
                or fingerprint_model(self._seed) != request.input_manifest_hash):
            raise DomainError("SANDBOX_MANIFEST_INVALID", "Approved seed association is invalid")
        self._sources.validate(self._prepared)
        files, contents = scan_source_tree(self._prepared.execution, self._seed.files)
        if files != self._seed.files or fingerprint_model(self._seed) != request.input_manifest_hash:
            raise DomainError("SANDBOX_OUTPUT_DRIFT", "Input changed before freezing")
        remote = root / "remote-job"
        require_plain_path(remote)
        remote.mkdir()
        atomic_write_bytes(remote / "business-request.json", request.model_dump_json().encode())
        atomic_write_bytes(remote / "policy.json", self._policy.model_dump_json().encode())
        with TemporaryDirectory(dir=root, prefix=".freeze-job-") as temporary:
            source = Path(temporary)
            for file in files:
                write_source(source, file, contents[file.path])
            if any(f.path.split("/")[0].casefold() == SCRIPT.casefold() for f in files):
                raise DomainError("SANDBOX_SOURCE_DENIED", "Reserved Runner launcher path")
            (source / SCRIPT).write_bytes(launcher(request))
            job = freeze_bundle(source, remote / "input.zip", request_key=request.run_id,
                task_id=request.task_id, operation_id=request.operation_id, run_id=request.run_id, script=SCRIPT,
                frozen_files=files, timeout_seconds=int(request.budgets.wall_clock_seconds),
                memory_bytes=request.budgets.memory_bytes, cpu_millis=int(request.budgets.cpu_count * 1000),
                stdout_bytes=request.budgets.stdout_bytes, stderr_bytes=request.budgets.stderr_bytes,
                profile=TOOLS_PROFILE, image=TOOLS_IMAGE, policy_revision="python-isolated-script-v2")
        atomic_write_bytes(remote / "job-request.json", job.model_dump_json().encode())
        validate_job(root, request)

    def execute(self, request: SandboxRequest, *, cancel_event: Event | None = None) -> SandboxResult:
        with project_lock(self._root, "run-" + request.run_id):
            self._phase = "validation"
            try:
                return self._execute(request, cancel_event)
            except httpx.HTTPStatusError as error:
                if self._phase == "submit" and error.response.status_code in {400, 401, 403, 404, 409, 410}:
                    raise DomainError("RUNNER_REQUEST_REJECTED", "Runner rejected submission; no new Job was created") from error
                raise DomainError("REMOTE_RESULT_PENDING", "Query the original remote Job before taking further action") from error
            except httpx.HTTPError as error:
                raise DomainError("REMOTE_RESULT_PENDING", "Runner connection lost; original Job was not replayed") from error
            except DomainError as error:
                if self._phase in {"lookup", "poll"} and error.code in {
                        "RUNNER_NOT_CONFIGURED", "RUNNER_CONFIG_INVALID", "SECRET_NOT_FOUND", "SECRET_BACKEND_UNAVAILABLE"}:
                    raise DomainError("REMOTE_RESULT_PENDING", "Restore Runner access before reconciling the original Job") from error
                raise
            except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
                raise DomainError("SANDBOX_EVIDENCE_FAILED", "Remote input or output validation failed") from error

    def _execute(self, request: SandboxRequest, cancel_event: Event | None) -> SandboxResult:
        request = SandboxRequest.model_validate_json(request.model_dump_json(), context={"policy": self._policy})
        run = self._store.get_run(request.task_id, request.run_id)
        if run.operation_id != request.operation_id or run.status.value != "running":
            raise DomainError("SANDBOX_CONFLICT", "Only the active approved Run may execute")
        if self._runner.endpoint != request.runner_endpoint:
            code = "REMOTE_RESULT_PENDING" if (self._prepared.execution.parent / "remote-job/submission.json").exists() else "RUNNER_CONFIG_CHANGED"
            raise DomainError(code, "Runner endpoint changed after preparation")
        root, copies = self._prepared.execution.parent, self._prepared
        frozen = validate_job(root, request)
        if (root / "remote-job/result.json").exists():
            return verify_remote_result(root, request)
        self._sources.validate(copies)
        if scan_source_tree(copies.execution, self._seed.files)[0] != self._seed.files:
            raise DomainError("SANDBOX_OUTPUT_DRIFT", "Frozen execution tree changed")
        intent = root / "remote-job/submission.json"
        require_plain_path(intent)
        if intent.exists():
            if json.loads(read_evidence(intent)) != {"request_fingerprint": request.fingerprint, "job_fingerprint": frozen.fingerprint}:
                raise DomainError("SANDBOX_EVIDENCE_FAILED", "Submission binding changed")
            self._phase = "lookup"
            status = self._runner.lookup(frozen)
            if status is None:
                raise DomainError("REMOTE_RESULT_PENDING", "Submission outcome unknown; original request was not resubmitted")
        else:
            atomic_write_bytes(intent, json.dumps({"request_fingerprint": request.fingerprint, "job_fingerprint": frozen.fingerprint}).encode())
            self._phase = "submit"
            status = self._runner.submit(root / "remote-job/input.zip")
        self._phase = "poll"
        deadline = time.monotonic() + request.budgets.wall_clock_seconds + 180
        cancelling = False
        while status.state in {"queued", "running"}:
            atomic_write_bytes(root / "remote-job/status.json", status.model_dump_json().encode())
            if (cancel_event is not None and cancel_event.is_set()) or time.monotonic() > deadline:
                if not cancelling:
                    self._runner.cancel(status.job_id)
                    cancelling, deadline = True, time.monotonic() + 30
                elif time.monotonic() > deadline:
                    raise DomainError("REMOTE_RESULT_PENDING", "Remote cancellation has not been confirmed")
            try:
                time.sleep(.2)
            except KeyboardInterrupt:
                self._runner.cancel(status.job_id)
                cancelling, deadline = True, time.monotonic() + 30
            status = self._runner.status(status.job_id)
            if status.request_fingerprint != frozen.fingerprint:
                raise DomainError("SANDBOX_EVIDENCE_FAILED", "Remote Job identity changed")
        atomic_write_bytes(root / "remote-job/status.json", status.model_dump_json().encode())
        if status.result_expired:
            raise DomainError("RESULT_EXPIRED", "Original Job result expired; no replay was authorized")
        if status.state not in {"succeeded", "failed", "cancelled"}:
            raise DomainError("REMOTE_RESULT_PENDING", "Remote result is uncertain, expired or awaiting cleanup")
        if status.state == "cancelled" or cancelling:
            raise DomainError("SANDBOX_CANCELLED", "Remote execution stopped")
        if status.state == "failed" and status.error_code == "OUTPUT_INVALID":
            try:
                diagnostic = self._runner.diagnostics(status.job_id)
            except httpx.HTTPError:
                diagnostic = None
            if diagnostic is not None:
                if (diagnostic.request_fingerprint != frozen.fingerprint
                        or diagnostic.sandbox_id != status.sandbox_id
                        or len(diagnostic.stdout.encode()) > request.budgets.stdout_bytes
                        or len(diagnostic.stderr.encode()) > request.budgets.stderr_bytes):
                    raise ValueError("Diagnostic request or sandbox differs")
                FileArtifactStore(self._store).write_bytes(request.task_id, request.run_id,
                    producer="runner_diagnostics", relative_path="remote-job/diagnostics.json",
                    content=diagnostic.model_dump_json().encode(), input_fingerprint=request.fingerprint)
            raise DomainError("REMOTE_OUTPUT_INVALID", "Original Job executed; output collection failed; "
                + ("diagnostics retained" if diagnostic is not None else "diagnostics unavailable"))
        self._phase = "collect"
        with TemporaryDirectory(dir=root, prefix=".collect-job-") as temporary:
            staging = Path(temporary)
            self._runner.download(status.job_id, staging / "result.zip")
            job = reconstruct_result(root / "remote-job/input.zip", staging / "result.zip", staging / "execution", job_id=status.job_id)
            if any(f.path.split("/")[0].casefold() == SCRIPT.casefold() for f in job.files):
                raise DomainError("SANDBOX_OUTPUT_INVALID", "Reserved launcher remains in output")
            if scan_source_tree(copies.execution, self._seed.files)[0] != self._seed.files:
                raise DomainError("SANDBOX_OUTPUT_DRIFT", "Local snapshot changed during remote execution")
            with bounded_zip(staging / "result.zip") as bundle:
                streams = tuple(read_entry(bundle, stream + ".log", getattr(request.budgets, stream + "_bytes"))
                                for stream in ("stdout", "stderr"))
            artifacts = FileArtifactStore(self._store)
            refs = tuple(artifact_reference(artifacts.write_bytes(request.task_id, request.run_id,
                producer="runner", relative_path="artifacts/runner/" + name + ".log", content=data,
                input_fingerprint=request.fingerprint)) for name, data in zip(("stdout", "stderr"), streams, strict=True))
            result = business_result(request, job, (refs[0], refs[1]), streams[0], streams[1])
            copies.execution.rename(root / "remote-job/original-execution")
            (staging / "execution").rename(copies.execution)
            (staging / "result.zip").rename(root / "remote-job/result.zip")
            atomic_write_bytes(root / "remote-job/result.json", result.model_dump_json().encode())
        return verify_remote_result(root, request, result)
