from typer.testing import CliRunner

from yipilot.transport.cli.app import app
from yipilot.workspace.docker_worker import DockerSandboxWorker


def test_sandbox_doctor_renders_structured_preflight(monkeypatch) -> None:
    monkeypatch.setattr(
        DockerSandboxWorker,
        "doctor",
        lambda digest: {"image_digest": digest, "server_version": "29.2.1"},
    )
    result = CliRunner().invoke(
        app,
        [
            "sandbox",
            "doctor",
            "--image-digest",
            "sha256:" + "a" * 64,
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0
    assert '"server_version":"29.2.1"' in result.stdout


def test_sandbox_register_and_doctor_resolve_the_project_digest(tmp_path, monkeypatch) -> None:
    digest = "sha256:" + "a" * 64
    monkeypatch.setattr(
        DockerSandboxWorker,
        "doctor",
        lambda value: {"image_digest": value, "server_version": "29.2.1"},
    )
    runner = CliRunner()
    registered = runner.invoke(
        app,
        ["sandbox", "register", "--project", str(tmp_path), "--image-digest", digest, "--output", "json"],
    )
    checked = runner.invoke(
        app, ["sandbox", "doctor", "--project", str(tmp_path), "--output", "json"]
    )
    assert registered.exit_code == checked.exit_code == 0
    assert digest in registered.stdout and digest in checked.stdout
