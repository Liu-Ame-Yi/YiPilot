import json

from typer.testing import CliRunner

from yipilot.domain.remote_execution import IMAGES
from yipilot.transport.cli.app import app
from yipilot.workspace.runner_client import RunnerClient


def test_sandbox_doctor_renders_remote_health(monkeypatch):
    monkeypatch.setattr(RunnerClient, "doctor", lambda self: {"status": "healthy", "profiles": IMAGES})
    result = CliRunner().invoke(app, ["sandbox", "doctor", "--output", "json"])
    assert result.exit_code == 0 and '\"status\":\"healthy\"' in result.stdout


def test_configure_saves_only_endpoint_and_secret_reference(tmp_path):
    result = CliRunner().invoke(app, ["sandbox", "configure", "--endpoint", "http://127.0.0.1:18090",
        "--credential-ref", "env:YIPILOT_RUNNER_KEY", "--settings-dir", str(tmp_path), "--output", "json"])
    assert result.exit_code == 0, result.output
    saved = json.loads((tmp_path / "runner.json").read_text())
    assert saved == {"schema_version": 1, "endpoint": "http://127.0.0.1:18090", "credential_ref": "env:YIPILOT_RUNNER_KEY"}


def test_unencrypted_nonloopback_endpoint_and_url_credentials_are_rejected(tmp_path):
    for endpoint in ("http://example.invalid", "https://name:password@example.invalid", "https://example.invalid/elsewhere"):
        result = CliRunner().invoke(app, ["sandbox", "configure", "--endpoint", endpoint,
            "--credential-ref", "env:KEY", "--settings-dir", str(tmp_path), "--output", "json"])
        assert result.exit_code != 0
        assert not (tmp_path / "runner.json").exists()
