import json
from pathlib import Path

import pytest
from pydantic import BaseModel, Field
from typer.testing import CliRunner

from yipilot.harness.quality import SrsHarness
from yipilot.persistence.task_store import FileTaskStore
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

@pytest.mark.parametrize(
    ("arguments", "expected_code"),
    (
        (
            (
                "quality", "generate", "--taskid", "1001", "--prd",
                "Authorization: Bearer test-secret-value", "--profile-revision", "0",
            ),
            "VALIDATION_ERROR",
        ),
        (
            (
                "quality", "pytest", "--taskid", "1001", "--prepare", "--alias",
                "sample", "--timeout-seconds", "0",
            ),
            "VALIDATION_ERROR",
        ),
        (
            (
                "quality", "pytest", "--taskid", "1001", "--prepare", "--alias",
                "sample", "--timeout-seconds", "601",
            ),
            "VALIDATION_ERROR",
        ),
        (
            (
                "quality", "harness", "--taskid", "1001", "--run-id", "missing",
                "--kind", "srs", "--semantic-review",
            ),
            "SEMANTIC_REVIEW_KIND_INVALID",
        ),
    ),
)
def test_quality_cli_rejects_invalid_input_as_one_redacted_json_object(
    tmp_path: Path, arguments: tuple[str, ...], expected_code: str
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

    prior_runs = FileTaskStore(project).list_runs("1001")
    result = runner.invoke(
        app, [*arguments, "--project", str(project), "--output", "json"]
    )

    assert result.exit_code != 0
    assert result.stdout.count("\n") == 1
    value = json.loads(result.stdout)
    assert value["code"] == expected_code
    assert "traceback" not in result.output.casefold()
    assert "test-secret-value" not in result.output
    assert "Authorization: Bearer" not in result.output
    assert FileTaskStore(project).list_runs("1001") == prior_runs

class _InvalidHarnessInput(BaseModel):
    value: int = Field(gt=0)


def test_quality_cli_normalizes_pydantic_validation_error_from_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    run_id = FileTaskStore(project).list_runs("1001")[0].run_id

    def invalid_harness_run(_harness: SrsHarness) -> None:
        _InvalidHarnessInput(value=0)

    monkeypatch.setattr(SrsHarness, "run", invalid_harness_run)
    result = runner.invoke(
        app,
        [
            "quality", "harness", "--project", str(project), "--taskid", "1001",
            "--run-id", run_id, "--kind", "srs", "--output", "json",
        ],
    )

    assert result.exit_code != 0
    assert result.stdout == '{"code":"VALIDATION_ERROR","message":"input validation failed"}\n'
    assert "traceback" not in result.output.casefold()
    assert "greater_than" not in result.output

def test_non_quality_cli_domain_error_keeps_existing_output(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0

    result = runner.invoke(
        app,
        [
            "task", "show", "--project", str(project), "--taskid", "1001",
            "--output", "json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "TASK_NOT_FOUND"
