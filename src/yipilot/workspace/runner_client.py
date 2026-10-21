"""Authenticated Runner transport. A lost response never starts a new Job key."""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import httpx

from yipilot.domain.errors import DomainError
from yipilot.domain.remote_execution import (
    IMAGES,
    MAX_BUNDLE_BYTES,
    JobRequest,
    JobResult,
    JobStatus,
    RunnerSettings,
)
from yipilot.models.secrets import SecretStore
from yipilot.models.store import settings_directory
from yipilot.persistence.filesystem import atomic_write_bytes
from yipilot.security.paths import require_plain_path
from yipilot.workspace.remote_bundle import bounded_zip, read_entry, reconstruct_result


def bounded_transfer(chunks: Iterable[bytes]) -> Iterator[bytes]:
    deadline = monotonic() + 1200
    for chunk in chunks:
        if monotonic() > deadline:
            raise httpx.TimeoutException("Runner transfer deadline exceeded")
        yield chunk


class RunnerClient:
    def __init__(self, client: httpx.Client | None = None, *, settings_dir: Path | None = None,
                 secrets_store: SecretStore | None = None) -> None:
        self._client = client
        self._settings_path = settings_directory(settings_dir) / "runner.json"
        self._secrets = secrets_store or SecretStore(settings_dir=settings_dir)

    @staticmethod
    def configure(settings: RunnerSettings, settings_dir: Path | None = None) -> None:
        path = settings_directory(settings_dir) / "runner.json"
        require_plain_path(path)
        atomic_write_bytes(path, settings.model_dump_json().encode())

    def _settings(self) -> RunnerSettings:
        require_plain_path(self._settings_path)
        try:
            if self._settings_path.stat().st_size > 16384:
                raise ValueError("Oversized settings")
            return RunnerSettings.model_validate_json(self._settings_path.read_bytes())
        except FileNotFoundError as error:
            raise DomainError("RUNNER_NOT_CONFIGURED", "Run yipilot sandbox configure before remote execution") from error
        except (OSError, ValueError) as error:
            raise DomainError("RUNNER_CONFIG_INVALID", "Runner settings are invalid") from error

    @property
    def endpoint(self) -> str:
        return str(self._client.base_url).rstrip("/") if self._client is not None else self._settings().endpoint

    @contextmanager
    def _connection(self) -> Iterator[httpx.Client]:
        if self._client is not None:
            yield self._client
        else:
            settings = self._settings()
            token = self._secrets.read(settings.credential_ref)
            with httpx.Client(base_url=settings.endpoint, trust_env=False, timeout=40,
                              headers={"Authorization": "Bearer " + token}) as client:
                yield client

    def doctor(self) -> dict[str, object]:
        try:
            with self._connection() as client:
                response = client.get("/v1/doctor")
                response.raise_for_status()
                value = response.json()
            if not isinstance(value, dict) or value.get("status") != "healthy" or value.get("profiles") != IMAGES:
                raise ValueError("Runner capabilities differ")
            return value
        except (httpx.HTTPError, ValueError) as error:
            raise DomainError("RUNNER_UNAVAILABLE", "Runner or its pinned runtime is unavailable") from error

    def lookup(self, request: JobRequest) -> JobStatus | None:
        with self._connection() as client:
            response = client.get("/v1/requests/" + request.request_key + "/" + request.fingerprint, timeout=5)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        status = JobStatus.model_validate_json(response.content)
        if status.request_fingerprint != request.fingerprint:
            raise ValueError("Runner returned a different request")
        return status

    def submit(self, frozen_bundle: Path) -> JobStatus:
        require_plain_path(frozen_bundle)
        size = frozen_bundle.stat().st_size
        if not 0 < size <= MAX_BUNDLE_BYTES:
            raise ValueError("Input bundle exceeds budget")
        with bounded_zip(frozen_bundle) as bundle:
            request = JobRequest.model_validate_json(read_entry(bundle, "request.json", 4 * 1024**2))
        with self._connection() as client, frozen_bundle.open("rb") as source:
            chunks = bounded_transfer(iter(lambda: source.read(64 * 1024), b""))
            response = client.post("/v1/jobs", content=chunks,
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
        with self._connection() as client:
            response = client.request(method, "/v1/jobs/" + job_id + suffix, timeout=5)
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
            self.download(job_id, output)
            return reconstruct_result(frozen_bundle, output, destination, job_id=job_id)

    def download(self, job_id: str, destination: Path) -> None:
        self._validate_id(job_id)
        require_plain_path(destination)
        with destination.open("xb") as handle:
            try:
                with self._connection() as client, client.stream("GET", "/v1/jobs/" + job_id + "/result",
                                                               headers={"Accept-Encoding": "identity"}) as response:
                    response.raise_for_status()
                    if response.headers.get("Content-Encoding", "identity") != "identity":
                        raise ValueError("Compressed result transport is unavailable")
                    count = 0
                    for chunk in bounded_transfer(response.iter_bytes()):
                        count += len(chunk)
                        if count > MAX_BUNDLE_BYTES:
                            raise ValueError("Result transfer exceeds budget")
                        handle.write(chunk)
            except BaseException:
                handle.close()
                destination.unlink(missing_ok=True)
                raise

    @staticmethod
    def _validate_id(job_id: str) -> None:
        import re
        if re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
            raise ValueError("Invalid Job ID")
