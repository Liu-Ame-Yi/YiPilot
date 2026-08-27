import json
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, TypeVar, cast

import typer
from pydantic import BaseModel

from yipilot import __version__
from yipilot.application.code_service import CodeService
from yipilot.application.project_service import ProjectService
from yipilot.application.provider_service import ProviderService
from yipilot.application.quality_artifacts import QualityArtifactService
from yipilot.application.repo_service import RepoMetadata, RepoService
from yipilot.application.workspace_service import PreparedWorkspace, WorkspaceService
from yipilot.domain.errors import DomainError
from yipilot.harness.models import HarnessReport
from yipilot.harness.quality import SddHarness, SrsHarness
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.task_store import FileTaskStore
from yipilot.security.redaction import SecretRedactor
from yipilot.transport.cli.events import (
    HumanEventRenderer,
    JsonlEventRenderer,
    is_terminal_event,
)

app = typer.Typer(
    no_args_is_help=True, add_completion=False, invoke_without_command=True
)
task_app = typer.Typer(no_args_is_help=True)
run_app = typer.Typer(no_args_is_help=True)
repo_app = typer.Typer(no_args_is_help=True)
workspace_app = typer.Typer(no_args_is_help=True)
code_app = typer.Typer(no_args_is_help=True)
quality_app = typer.Typer(no_args_is_help=True)
settings_app = typer.Typer(no_args_is_help=True)
provider_app = typer.Typer(no_args_is_help=True)
app.add_typer(task_app, name="task")
app.add_typer(run_app, name="run")
app.add_typer(code_app, name="code")
app.add_typer(quality_app, name="quality")
app.add_typer(settings_app, name="settings")
task_app.add_typer(repo_app, name="repo")
task_app.add_typer(workspace_app, name="workspace")
settings_app.add_typer(provider_app, name="provider")

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
        typer.echo(
            json.dumps(
                {"name": "yipilot", "version": __version__}, separators=(",", ":")
            )
        )
    elif output == "human":
        typer.echo(f"yipilot {__version__}")
    else:
        raise typer.BadParameter(
            "must be either 'human' or 'json'", param_hint="--output"
        )

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


@repo_app.command("add")
def repo_add(
    task_id: Annotated[str, typer.Option("--taskid")],
    url: Annotated[str, typer.Option("--url")],
    alias: Annotated[str, typer.Option("--alias")],
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    sync: Annotated[bool, typer.Option("--sync")] = False,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    """Register a repository and synchronize it into the task-local cache."""
    del sync  # M0-M2 only supports the safe add-and-sync operation.
    metadata = _invoke(
        output,
        lambda: RepoService(project, GitRunner()).add_and_sync(task_id, url, alias),
    )
    if metadata is not None:
        _render_value(
            output,
            _repo_value(metadata),
            f"Synchronized repository {metadata.alias} for task {metadata.task_id}.",
        )


@repo_app.command("sync")
def repo_sync(
    task_id: Annotated[str, typer.Option("--taskid")],
    alias: Annotated[str, typer.Option("--alias")],
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    """Synchronize an already registered repository."""
    metadata = _invoke(
        output,
        lambda: _sync_registered_repository(project, task_id, alias),
    )
    if metadata is not None:
        _render_value(
            output,
            _repo_value(metadata),
            f"Synchronized repository {metadata.alias} for task {metadata.task_id}.",
        )


@workspace_app.command("prepare")
def workspace_prepare(
    task_id: Annotated[str, typer.Option("--taskid")],
    alias: Annotated[str, typer.Option("--alias")],
    run_id: Annotated[str, typer.Option("--run-id")],
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    """Prepare an isolated Git worktree for one task and run."""
    workspace = _invoke(
        output,
        lambda: WorkspaceService(project, GitRunner()).prepare(task_id, alias, run_id),
    )
    if workspace is not None:
        _render_value(
            output,
            _workspace_value(workspace),
            f"Prepared workspace {workspace.path}.",
        )


@quality_app.command("generate")
def quality_generate(
    task_id: Annotated[str, typer.Option("--taskid")],
    prd: Annotated[str, typer.Option("--prd")] = "",
    source_run_id: Annotated[str | None, typer.Option("--source-run-id")] = None,
    prd_artifact_id: Annotated[str | None, typer.Option("--prd-artifact-id")] = None,
    model: Annotated[str, typer.Option("--model")] = "fake",
    profile_revision: Annotated[int, typer.Option("--profile-revision")] = 1,
    provider_profile: Annotated[
        str, typer.Option("--provider-profile")
    ] = "offline-quality",
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
    request_id: Annotated[str | None, typer.Option("--request-id")] = None,
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    result = _invoke(
        output,
        lambda: QualityArtifactService(project, settings_dir=settings_dir).generate(
            task_id,
            prd_text=prd or None,
            source_run_id=source_run_id,
            prd_artifact_id=prd_artifact_id,
            model_mode=model,
            profile_revision=profile_revision,
            provider_profile_name=provider_profile,
            request_id=request_id,
        ),
    )
    if result is not None:
        _render_value(
            output,
            result.value(),
            f"Generated quality artifacts for run {result.run_id}.",
        )


@quality_app.command("show")
def quality_show(
    task_id: Annotated[str, typer.Option("--taskid")],
    run_id: Annotated[str, typer.Option("--run-id")],
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    def action() -> dict[str, object]:
        records = FileArtifactStore(FileTaskStore(project)).list(task_id, run_id)
        return {
            "task_id": task_id,
            "run_id": run_id,
            "artifact_refs": [
                {
                    "artifact_id": r.artifact_id,
                    "relative_path": r.relative_path,
                    "sha256": r.sha256,
                }
                for r in records
            ],
        }

    value = _invoke(output, action)
    if value is not None:
        _render_value(output, value, f"Quality artifacts for run {run_id}.")


@quality_app.command("harness")
def quality_harness(
    task_id: Annotated[str, typer.Option("--taskid")],
    run_id: Annotated[str, typer.Option("--run-id")],
    kind: Annotated[str, typer.Option("--kind")],
    profile_revision: Annotated[int | None, typer.Option("--profile-revision")] = None,
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    def action() -> HarnessReport:
        if kind == "srs":
            return SrsHarness(project, task_id, run_id).run()
        if kind == "sdd":
            return SddHarness(
                project,
                task_id,
                run_id,
                profile_revision=profile_revision,
                settings_dir=settings_dir,
            ).run()
        raise DomainError("HARNESS_KIND_INVALID", "harness kind must be srs or sdd")

    report = cast(HarnessReport | None, _invoke(output, action))
    if report is not None:
        _render(output, report, f"{kind} Harness status: {report.status}.")


@quality_app.command("pytest")
def quality_pytest(
    task_id: Annotated[str, typer.Option("--taskid")],
    run_id: Annotated[str | None, typer.Option("--run-id")] = None,
    alias: Annotated[str | None, typer.Option("--alias")] = None,
    prepare: Annotated[bool, typer.Option("--prepare")] = False,
    approve: Annotated[bool, typer.Option("--approve")] = False,
    approval_id: Annotated[str | None, typer.Option("--approval-id")] = None,
    confirmation_token: Annotated[
        str | None, typer.Option("--confirmation-token")
    ] = None,
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds")] = 60,
    request_id: Annotated[str | None, typer.Option("--request-id")] = None,
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    service = QualityArtifactService(project)

    def action() -> dict[str, object]:
        if prepare:
            if alias is None:
                raise DomainError(
                    "PYTEST_ALIAS_REQUIRED",
                    "pytest preparation requires a repository alias",
                )
            return service.prepare_pytest(
                task_id, alias, timeout_seconds=timeout_seconds, request_id=request_id
            )
        if approve:
            if run_id is None or approval_id is None:
                raise DomainError(
                    "APPROVAL_REQUIRED",
                    "pytest approval requires run ID and approval ID",
                )
            return service.approve_pytest(task_id, run_id, approval_id)
        if run_id is None:
            raise DomainError(
                "PYTEST_RUN_REQUIRED", "pytest execution requires a prepared run ID"
            )
        return service.run_pytest(
            task_id,
            run_id,
            approval_id=approval_id,
            confirmation_token=confirmation_token,
        )

    value = _invoke(output, action)
    if value is not None:
        _render_value(
            output,
            value,
            f"Controlled pytest status: {value.get('status', 'prepared')}.",
        )


@code_app.command("generate")
def code_generate(
    task_id: Annotated[str, typer.Option("--taskid")],
    alias: Annotated[str, typer.Option("--alias")],
    instruction: Annotated[str, typer.Option("--instruction")],
    planner: Annotated[str, typer.Option("--planner")],
    request_id: Annotated[str | None, typer.Option("--request-id")] = None,
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    result = _invoke(
        output,
        lambda: CodeService(project).generate(
            task_id, alias, instruction, planner, request_id
        ),
    )
    if result is not None:
        _render_value(
            output,
            result.value(),
            f"Code generation run {result.run_id} is {result.status}.",
        )
        if result.status != "succeeded":
            raise typer.Exit(code=1)


@code_app.command("apply")
def code_apply(
    task_id: Annotated[str, typer.Option("--taskid")],
    run_id: Annotated[str, typer.Option("--run-id")],
    prepare_only: Annotated[bool, typer.Option("--prepare-only")] = False,
    request_id: Annotated[str | None, typer.Option("--request-id")] = None,
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    if not prepare_only:
        _render_value(
            output,
            {
                "code": "PREPARE_ONLY_REQUIRED",
                "message": "code apply only supports --prepare-only",
            },
            "PREPARE_ONLY_REQUIRED: code apply only supports --prepare-only",
        )
        raise typer.Exit(code=1)
    result = _invoke(
        output, lambda: CodeService(project).prepare_only(task_id, run_id, request_id)
    )
    if result is not None:
        _render_value(
            output,
            result.value(),
            f"Prepared handoff for run {result.run_id}; no Git apply was performed.",
        )


@provider_app.command("add")
def provider_add(
    name: Annotated[str, typer.Option("--name")],
    base_url: Annotated[str, typer.Option("--base-url")],
    model: Annotated[str, typer.Option("--model")],
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
    api_key_ref: Annotated[str | None, typer.Option("--api-key-ref")] = None,
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds")] = 60.0,
    output_budget: Annotated[int, typer.Option("--output-budget")] = 4096,
    capabilities: Annotated[str, typer.Option("--capabilities")] = "chat",
    enabled: Annotated[bool, typer.Option("--enabled/--disabled")] = True,
    revision: Annotated[int, typer.Option("--revision")] = 1,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    profile = _settings_invoke(
        output,
        lambda: ProviderService(settings_dir).add(
            name=name,
            provider_type="openai_compatible",
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
            output_budget=output_budget,
            capabilities=tuple(item for item in capabilities.split(",") if item),
            enabled=enabled,
            revision=revision,
            api_key_ref=api_key_ref,
        ),
    )
    if profile is not None:
        _render_value(
            output, profile.public_value(), f"Saved provider profile {profile.name}."
        )


@provider_app.command("list")
def provider_list(
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    value = _settings_invoke(output, lambda: ProviderService(settings_dir).list())
    if value is not None:
        profiles = value["profiles"]
        assert isinstance(profiles, list)
        _render_value(output, value, f"Configured {len(profiles)} provider profile(s).")


@provider_app.command("show")
def provider_show(
    name: Annotated[str, typer.Option("--name")],
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    profile = _settings_invoke(output, lambda: ProviderService(settings_dir).show(name))
    if profile is not None:
        _render_value(
            output, profile.public_value(), f"Provider profile {profile.name}."
        )


@provider_app.command("use")
def provider_use(
    name: Annotated[str, typer.Option("--name")],
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    profile = _settings_invoke(output, lambda: ProviderService(settings_dir).use(name))
    if profile is not None:
        _render_value(
            output, profile.public_value(), f"Using provider profile {profile.name}."
        )


@provider_app.command("set-key")
def provider_set_key(
    name: Annotated[str, typer.Option("--name")],
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    value = typer.prompt("API key", hide_input=True)
    profile = _settings_invoke(
        output, lambda: ProviderService(settings_dir).set_key(name, value)
    )
    if profile is not None:
        _render_value(
            output, profile.public_value(), f"Stored key reference for {profile.name}."
        )


@provider_app.command("test")
def provider_test(
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
    name: Annotated[str | None, typer.Option("--name")] = None,
    request: Annotated[bool, typer.Option("--request")] = False,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    result = _settings_invoke(
        output, lambda: ProviderService(settings_dir).test(name, request=request)
    )
    if result is not None:
        _render_value(
            output,
            result.public_value(),
            f"Provider profile {result.profile.name}: {result.status}.",
        )


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


@run_app.command("watch")
def run_watch(
    run_id: Annotated[str, typer.Option("--run-id")],
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    after_sequence: Annotated[str, typer.Option("--after-sequence")] = "0",
    output: Annotated[str, typer.Option("--output")] = "human",
    follow_timeout_seconds: Annotated[
        str, typer.Option("--follow-timeout-seconds")
    ] = "30",
) -> None:
    """Replay a durable event journal and poll until its terminal event."""
    try:
        cursor = _watch_cursor(after_sequence)
        timeout = _watch_timeout(follow_timeout_seconds)
        renderer = _event_renderer(output)
        store = FileTaskStore(project)
        run = store.find_run(run_id)
        journal = JsonlEventJournal(
            store.event_journal_path(run.run_id),
            task_id=run.task_id,
            run_id=run.run_id,
            stage=run.stage,
        )
        _watch_journal(journal, cursor, timeout, renderer)
    except DomainError as error:
        _render_watch_error(output, error)
        raise typer.Exit(code=1) from error


def _watch_cursor(value: str) -> int:
    try:
        cursor = int(value)
    except ValueError as error:
        raise DomainError(
            "EVENT_CURSOR_INVALID", "event cursor must be an integer"
        ) from error
    if cursor < 0:
        raise DomainError("EVENT_CURSOR_INVALID", "event cursor must be non-negative")
    return cursor


def _watch_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as error:
        raise DomainError(
            "FOLLOW_TIMEOUT_INVALID", "follow timeout must be a positive number"
        ) from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise DomainError(
            "FOLLOW_TIMEOUT_INVALID", "follow timeout must be a positive number"
        )
    return timeout


def _event_renderer(output: str) -> JsonlEventRenderer | HumanEventRenderer:
    if output == "jsonl":
        return JsonlEventRenderer()
    if output == "human":
        return HumanEventRenderer()
    raise DomainError("OUTPUT_INVALID", "output must be either 'human' or 'jsonl'")


def _watch_journal(
    journal: JsonlEventJournal,
    cursor: int,
    timeout: float,
    renderer: JsonlEventRenderer | HumanEventRenderer,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        events = journal.read_after(cursor)
        for event in events:
            renderer.render(event)
            cursor = event.sequence
            if is_terminal_event(event):
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise typer.Exit(code=4)
        time.sleep(min(0.1, remaining))


def _render_watch_error(output: str, error: DomainError) -> None:
    value = json.dumps(
        {"code": error.code, "message": error.message},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if output == "jsonl":
        typer.echo(value, err=True)
    else:
        typer.echo(str(error), err=True)


def _project_service(project: Path) -> ProjectService:
    return ProjectService(project)


def _sync_registered_repository(
    project: Path, task_id: str, alias: str
) -> RepoMetadata:
    service = RepoService(project, GitRunner())
    metadata = service.get_metadata(task_id, alias)
    return service.add_and_sync(task_id, metadata.normalized_url, alias)


def _repo_value(metadata: RepoMetadata) -> dict[str, object]:
    return {
        "alias": metadata.alias,
        "cache_path": str(metadata.cache_path),
        "head_sha": metadata.head_sha,
        "normalized_url": metadata.normalized_url,
        "schema_version": metadata.schema_version,
        "synced_at": metadata.synced_at,
        "task_id": metadata.task_id,
        "url_hash": metadata.url_hash,
    }


def _workspace_value(workspace: PreparedWorkspace) -> dict[str, object]:
    return {
        "base_sha": workspace.base_sha,
        "branch": workspace.branch,
        "created_at": workspace.created_at,
        "path": str(workspace.path),
        "repo_alias": workspace.repo_alias,
        "repo_hash": workspace.repo_hash,
        "run_id": workspace.run_id,
        "schema_version": workspace.schema_version,
        "task_id": workspace.task_id,
    }


def _invoke(output: str, action: Callable[[], Result]) -> Result | None:
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


def _settings_invoke(output: str, action: Callable[[], Result]) -> Result | None:
    _validate_output(output)
    try:
        return action()
    except DomainError as error:
        value = SecretRedactor().redact({"code": error.code, "message": error.message})
        assert isinstance(value, dict)
        if output == "json":
            typer.echo(
                json.dumps(
                    value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                err=True,
            )
        else:
            typer.echo(f"{error.code}: {value['message']}", err=True)
        raise typer.Exit(code=1) from error


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
        raise typer.BadParameter(
            "must be either 'human' or 'json'", param_hint="--output"
        )
