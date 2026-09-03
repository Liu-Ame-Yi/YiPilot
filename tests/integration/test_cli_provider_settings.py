import json
from pathlib import Path

from typer.testing import CliRunner

from yipilot.transport.cli.app import app


def test_provider_cli_add_list_show_use_and_local_test(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIPILOT_TEST_KEY", "provider-test-secret")
    runner = CliRunner()
    common = ["--settings-dir", str(tmp_path), "--output", "json"]
    added = runner.invoke(app, ["settings", "provider", "add", "--name", "local", "--base-url", "https://example.test/v1", "--model", "test-model", "--api-key-ref", "env:YIPILOT_TEST_KEY", *common])
    assert added.exit_code == 0
    assert "provider-test-secret" not in added.output
    listed = runner.invoke(app, ["settings", "provider", "list", *common])
    assert listed.exit_code == 0
    assert json.loads(listed.stdout)["profiles"][0]["api_key_ref"] == "env:YIPILOT_TEST_KEY"
    assert runner.invoke(app, ["settings", "provider", "use", "--name", "local", *common]).exit_code == 0
    checked = runner.invoke(app, ["settings", "provider", "test", *common])
    assert checked.exit_code == 0
    assert json.loads(checked.stdout)["status"] == "local_validation_passed"


def test_provider_configure_creates_default_env_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YIPILOT_TEST_KEY", "provider-test-secret")
    result = CliRunner().invoke(
        app,
        ["settings", "provider", "configure", "--settings-dir", str(tmp_path)],
        input=(
            "local\n"
            "https://example.test/v1\n"
            "test-model\n"
            "n\n"
            "YIPILOT_TEST_KEY\n"
            "y\n"
            "n\n"
        ),
    )
    assert result.exit_code == 0
    assert "configured and locally validated" in result.output
    assert "provider-test-secret" not in result.output
    settings = json.loads((tmp_path / "providers.json").read_text(encoding="utf-8"))
    assert settings["default_model_selector"] == "local/test-model"
    assert settings["connections"][0]["credential_ref"] == "env:YIPILOT_TEST_KEY"



def test_provider_cli_has_no_api_key_option_and_errors_do_not_echo_key(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["settings", "provider", "set-key", "--help"])
    assert "--api-key" not in result.output
    invalid = runner.invoke(app, ["settings", "provider", "add", "--name", "local", "--base-url", "https://example.test", "--model", "test", "--api-key-ref", "raw-test-secret", "--settings-dir", str(tmp_path), "--output", "json"])
    assert invalid.exit_code == 1
    assert "raw-test-secret" not in invalid.output
    assert json.loads(invalid.stderr)["code"] == "PROVIDER_PROFILE_INVALID"


def test_provider_cli_request_uses_a_mocked_explicit_probe(tmp_path: Path, monkeypatch) -> None:
    import importlib

    from yipilot.application.provider_service import ProviderService

    monkeypatch.setenv("YIPILOT_TEST_KEY", "provider-test-secret")
    runner = CliRunner()
    common = ["--settings-dir", str(tmp_path), "--output", "json"]
    assert runner.invoke(
        app,
        [
            "settings", "provider", "add", "--name", "local",
            "--base-url", "https://example.test/v1", "--model", "test-model",
            "--api-key-ref", "env:YIPILOT_TEST_KEY", *common,
        ],
    ).exit_code == 0
    assert runner.invoke(app, ["settings", "provider", "use", "--name", "local", *common]).exit_code == 0

    requests = []

    class ProbePort:
        def stream(self, request):
            requests.append(request)
            return ()

    cli_module = importlib.import_module("yipilot.transport.cli.app")
    monkeypatch.setattr(
        cli_module,
        "ProviderService",
        lambda settings_dir: ProviderService(settings_dir, port_factory=lambda model, secrets, reference: ProbePort()),
    )

    result = runner.invoke(app, ["settings", "provider", "test", "--request", *common])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "request_passed"
    assert len(requests) == 1
    assert requests[0].user_instruction == "Reply with OK."
    assert "provider-test-secret" not in result.output
