from typer.testing import CliRunner

from yipilot.transport.cli.app import app


def test_version_json_is_stable() -> None:
    result = CliRunner().invoke(app, ["--version", "--output", "json"])

    assert result.exit_code == 0
    assert result.stdout == '{"name":"yipilot","version":"0.1.0"}\n'
