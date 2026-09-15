"""JSON input/output only; specification orchestration belongs to ProjectApplication."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from yipilot.app import ProjectApplication
from yipilot.domain.errors import DomainError
from yipilot.workflow.specification_service import SpecificationState

app = typer.Typer(no_args_is_help=True, help="Real model SRS and SDD with independent reviews.")


def _emit(action: Callable[[], SpecificationState]) -> None:
    try:
        state = action()
    except (DomainError, ValueError, OSError) as error:
        typer.echo(json.dumps({"error": error.code if isinstance(error, DomainError) else "INPUT_INVALID"}))
        raise typer.Exit(1) from error
    typer.echo(state.model_dump_json())
    if state.status in {"stopped", "superseded"}:
        raise typer.Exit(1)


@app.command("start")
def start(
    task: Annotated[str, typer.Option("--task")],
    project: Annotated[Path, typer.Option("--project")] = Path("."),
    instruction: Annotated[str, typer.Option("--instruction")] = "",
    prd: Annotated[Path | None, typer.Option("--prd")] = None,
    repo: Annotated[str | None, typer.Option("--repo")] = None,
    context: Annotated[list[str] | None, typer.Option("--context-file")] = None,
    request_id: Annotated[str | None, typer.Option("--request-id")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
    max_revisions: Annotated[int, typer.Option("--max-revisions", min=0, max=5)] = 2,
) -> None:
    _emit(lambda: ProjectApplication(project, settings_dir=settings_dir).start_specification(
        task, instruction=instruction, prd_path=prd, alias=repo, context_files=context or (),
        request_id=request_id, model_selector=model, max_revisions=max_revisions))


@app.command("status")
def status(
    task: Annotated[str, typer.Option("--task")],
    project: Annotated[Path, typer.Option("--project")] = Path("."),
    flow: Annotated[str | None, typer.Option("--flow")] = None,
) -> None:
    _emit(lambda: ProjectApplication(project).specification_status(task, flow))


@app.command("resume")
def resume(
    task: Annotated[str, typer.Option("--task")],
    project: Annotated[Path, typer.Option("--project")] = Path("."),
    flow: Annotated[str | None, typer.Option("--flow")] = None,
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
) -> None:
    _emit(lambda: ProjectApplication(project, settings_dir=settings_dir).resume_specification(task, flow))


@app.command("clarify")
def clarify(
    task: Annotated[str, typer.Option("--task")],
    flow: Annotated[str, typer.Option("--flow")],
    answers: Annotated[str, typer.Option("--answers", help='JSON object: {"question-id":"answer"}')],
    input_sha256: Annotated[str, typer.Option("--input-sha256")],
    request_id: Annotated[str, typer.Option("--request-id")],
    project: Annotated[Path, typer.Option("--project")] = Path("."),
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
) -> None:
    _emit(lambda: ProjectApplication(project, settings_dir=settings_dir).clarify_specification(
        task, flow, json.loads(answers), request_id=request_id, expected_input_sha256=input_sha256))
