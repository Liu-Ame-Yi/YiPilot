"""Authenticated Runner transport. A lost response never starts a new Job key."""

from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from yipilot.domain.remote_execution import (
    MAX_BUNDLE_BYTES,
    JobRequest,
    JobResult,
    JobStatus,
)
from yipilot.security.paths import require_plain_path
from yipilot.workspace.remote_bundle import bounded_zip, read_entry, reconstruct_result


class RunnerClient:
    def __init__(self, client: httpx.Client) -> None:
        self._http = client

    def submit(self, frozen_bundle: Path) -> JobStatus:
        require_plain_path(frozen_bundle)
        size = frozen_bundle.stat().st_size
        if not 0 < size <= MAX_BUNDLE_BYTES:
            raise ValueError("Input bundle exceeds budget")
        with bounded_zip(frozen_bundle) as bundle:
            request = JobRequest.model_validate_json(read_entry(bundle, "request.json", 4 * 1024**2))
        with frozen_bundle.open("rb") as source:
            response = self._http.post("/v1/jobs", content=source,
                                       headers={"Content-Type": "application/zip", "Content-Length": str(size)})
        response.raise_for_status()
        status = JobStatus.model_validate_json(response.content)
        if status.request_fingerprint != request.fingerprint:
            raise ValueError("Runner accepted a different input")
        return status

    def status(self, job_id: str) -> JobStatus:
        return self._status_request("GET", job_id, "")

    def cancel(self, job_id: str) -> JobStatus:
        return self._status_request("POST", job_id, "/cancel")

    def _status_request(self, method: str, job_id: str, suffix: str) -> JobStatus:
        self._validate_id(job_id)
        response = self._http.request(method, "/v1/jobs/" + job_id + suffix)
        response.raise_for_status()
        status = JobStatus.model_validate_json(response.content)
        if status.job_id != job_id:
            raise ValueError("Runner returned a different Job")
        return status

    def collect(self, job_id: str, frozen_bundle: Path, destination: Path) -> JobResult:
        self._validate_id(job_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        require_plain_path(destination.parent)
        with TemporaryDirectory(dir=destination.parent, prefix=".runner-download-") as temporary:
            output = Path(temporary) / "result.zip"
            with self._http.stream("GET", "/v1/jobs/" + job_id + "/result") as response:
                response.raise_for_status()
                count = 0
                with output.open("xb") as handle:
                    for chunk in response.iter_bytes(64 * 1024):
                        count += len(chunk)
                        if count > MAX_BUNDLE_BYTES:
                            raise ValueError("Result transfer exceeds budget")
                        handle.write(chunk)
            return reconstruct_result(frozen_bundle, output, destination, job_id=job_id)

    @staticmethod
    def _validate_id(job_id: str) -> None:
        import re
        if re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
            raise ValueError("Invalid Job ID")
