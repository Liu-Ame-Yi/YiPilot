import json
from pathlib import Path

from typer.testing import CliRunner

from yipilot.transport.cli.app import app

runner = CliRunner()


def test_quality_cli_json_is_single_redacted_object_and_semantic_review_is_explicit(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    assert (
        runner.invoke(
            app, ["task", "init", "--project", str(project), "--taskid", "1001"]
        ).exit_code
        == 0
    )
    generated = runner.invoke(
        app,
        [
            "quality",
            "generate",
            "--project",
            str(project),
            "--taskid",
            "1001",
            "--prd",
            "Build beta capability.",
            "--model",
            "fake",
            "--output",
            "json",
        ],
    )
    assert generated.exit_code == 0, generated.output
    value = json.loads(generated.stdout)
    run_id = str(value["run_id"])
    assert generated.stdout.count("\n") == 1

    semantic = runner.invoke(
        app,
        [
            "quality",
            "harness",
            "--project",
            str(project),
            "--taskid",
            "1001",
            "--run-id",
            run_id,
            "--kind",
            "sdd",
            "--profile-revision",
            "1",
            "--semantic-review",
            "--output",
            "json",
        ],
    )
    assert semantic.exit_code == 0, semantic.output
    semantic_value = json.loads(semantic.stdout)
    assert semantic_value["status"] == "failed"
    assert semantic.stdout.count("\n") == 1

    denied = runner.invoke(
        app,
        [
            "quality",
            "generate",
            "--project",
            str(project),
            "--taskid",
            "1001",
            "--prd",
            "Authorization: Bearer test-secret-value",
            "--model",
            "fake",
            "--output",
            "json",
        ],
    )
    assert denied.exit_code == 1
    assert json.loads(denied.stdout)["code"] == "QUALITY_FIXTURE_NOT_FOUND"
    assert "test-secret-value" not in denied.stdout
    assert denied.stdout.count("\n") == 1
