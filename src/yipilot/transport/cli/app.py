import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, TypeVar

import typer
from pydantic import BaseModel

from yipilot import __version__
from yipilot.application.project_service import ProjectService
from yipilot.domain.errors import DomainError

app = typer.Typer(no_args_is_help=True, add_completion=False, invoke_without_command=True)
task_app = typer.Typer(no_args_is_help=True)
run_app = typer.Typer(no_args_is_help=True)
app.add_typer(task_app, name="task")
app.add_typer(run_app, name="run")

Result = TypeVar("Result")
_DEFAULT_PROJECT = Path(".")


@app.callback()
def main(
    version: bool = typer.Option(False, "--version"),
    output: str = typer.Option("human", "--output"),
) -> None:
    if not version:
        return

    if output == "json":
        typer.echo(json.dumps({"name": "yipilot", "version": __version__}, separators=(",", ":")))
    elif output == "human":
        typer.echo(f"yipilot {__version__}")
    else:
        raise typer.BadParameter("must be either 'human' or 'json'", param_hint="--output")

    raise typer.Exit()


@app.command("init")
def init_project(
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    record = _invoke(output, lambda: _project_service(project).initialize_project())
    if record is not None:
        _render(output, record, "Initialized Yipilot project.")


@task_app.command("init")
def task_init(
    task_id: Annotated[str, typer.Option("--taskid")],
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    record = _invoke(output, lambda: _project_service(project).initialize_task(task_id))
    if record is not None:
        _render(output, record, f"Initialized task {record.task_id}.")


@task_app.command("show")
def task_show(
    task_id: Annotated[str, typer.Option("--taskid")],
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    record = _invoke(output, lambda: _project_service(project).show_task(task_id))
    if record is not None:
        _render(output, record, f"Task {record.task_id}.")


@run_app.command("list")
def run_list(
    task_id: Annotated[str, typer.Option("--taskid")],
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    records = _invoke(output, lambda: _project_service(project).list_runs(task_id))
    if records is None:
        return
    value = {
        "task_id": task_id,
        "runs": [record.model_dump(mode="json") for record in records],
    }
    _render_value(output, value, f"Task {task_id} has {len(records)} run(s).")


@run_app.command("show")
def run_show(
    task_id: Annotated[str, typer.Option("--taskid")],
    run_id: Annotated[str, typer.Option("--run-id")],
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    record = _invoke(
        output, lambda: _project_service(project).show_run(task_id, run_id)
    )
    if record is not None:
        _render(output, record, f"Run {record.run_id} is {record.status}.")


def _project_service(project: Path) -> ProjectService:
    return ProjectService(project)


def _invoke(
    output: str, action: Callable[[], Result]
) -> Result | None:
    _validate_output(output)
    try:
        return action()
    except DomainError as error:
        _render_value(
            output,
            {"code": error.code, "message": error.message},
            str(error),
        )
        raise typer.Exit(code=2 if error.code == "INVALID_TASK_ID" else 1) from error


def _render(output: str, record: BaseModel, human: str) -> None:
    _render_value(output, record.model_dump(mode="json"), human)


def _render_value(output: str, value: object, human: str) -> None:
    _validate_output(output)
    if output == "json":
        typer.echo(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(human)


def _validate_output(output: str) -> None:
    if output not in {"human", "json"}:
        raise typer.BadParameter("must be either 'human' or 'json'", param_hint="--output")
