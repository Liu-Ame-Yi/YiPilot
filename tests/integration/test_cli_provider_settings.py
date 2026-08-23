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
    request = runner.invoke(app, ["settings", "provider", "test", "--request", *common])
    assert request.exit_code == 1
    assert request.stdout == ""
    assert json.loads(request.stderr)["code"] == "LIVE_PROVIDER_TEST_NOT_IMPLEMENTED"


def test_provider_cli_has_no_api_key_option_and_errors_do_not_echo_key(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["settings", "provider", "set-key", "--help"])
    assert "--api-key" not in result.output
    invalid = runner.invoke(app, ["settings", "provider", "add", "--name", "local", "--base-url", "https://example.test", "--model", "test", "--api-key-ref", "raw-test-secret", "--settings-dir", str(tmp_path), "--output", "json"])
    assert invalid.exit_code == 1
    assert "raw-test-secret" not in invalid.output
    assert json.loads(invalid.stderr)["code"] == "PROVIDER_PROFILE_INVALID"
