import json

import httpx
import pytest

from yipilot.domain.errors import DomainError
from yipilot.domain.remote_execution import IMAGES, JobRequest, RunnerSettings
from yipilot.domain.sandbox import SourceFile
from yipilot.workspace import runner_client as module
from yipilot.workspace.runner_client import RunnerClient


def test_configuration_is_lazy_and_contains_no_secret(tmp_path):
    class Secrets:
        def read(self, ref):
            pytest.fail("Preparing an approval must not read the key")
    client = RunnerClient(settings_dir=tmp_path, secrets_store=Secrets())
    with pytest.raises(DomainError, match="RUNNER_NOT_CONFIGURED"):
        _ = client.endpoint
    RunnerClient.configure(RunnerSettings(endpoint="http://127.0.0.1:18090", credential_ref="env:RUNNER_KEY"), tmp_path)
    assert client.endpoint == "http://127.0.0.1:18090"
    assert json.loads((tmp_path / "runner.json").read_text())["credential_ref"] == "env:RUNNER_KEY"


def test_read_only_lookup_matches_fingerprint_and_doctor_validates_profiles():
    request = JobRequest(request_key="once", task_id="1", operation_id="operation", run_id="run", script="main.py",
        files=(SourceFile(path="main.py", sha256="0" * 64, size_bytes=0, git_mode="100644"),))
    calls = []
    def handle(incoming):
        calls.append((incoming.method, incoming.url.path))
        if incoming.url.path == "/v1/doctor":
            return httpx.Response(200, json={"status": "healthy", "profiles": IMAGES})
        return httpx.Response(200, json={"job_id": "a" * 32, "request_fingerprint": request.fingerprint, "state": "queued"})
    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handle)) as http:
        client = RunnerClient(http)
        assert client.lookup(request).job_id == "a" * 32
        assert client.doctor()["profiles"] == IMAGES
    assert all(method == "GET" for method, _ in calls)


@pytest.mark.parametrize("problem", ["deadline", "encoding"])
def test_failed_result_transfer_removes_only_its_partial_file(tmp_path, monkeypatch, problem):
    times = iter([0, 1201])
    monkeypatch.setattr(module, "monotonic", lambda: next(times))
    headers = {"Content-Encoding": "gzip"} if problem == "encoding" else {}
    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers=headers, stream=httpx.ByteStream(b"partial")))) as http:
        destination = tmp_path / "result.zip"
        with pytest.raises(httpx.TimeoutException if problem == "deadline" else ValueError):
            RunnerClient(http).download("a" * 32, destination)
        assert not destination.exists()
        destination.write_bytes(b"owned")
        with pytest.raises(FileExistsError):
            RunnerClient(http).download("a" * 32, destination)
        assert destination.read_bytes() == b"owned"
