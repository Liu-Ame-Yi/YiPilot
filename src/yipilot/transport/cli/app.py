import json
import math
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, TypeVar, cast

import typer
from pydantic import BaseModel, ValidationError

from yipilot import __version__
from yipilot.app import ProjectApplication
from yipilot.domain.errors import DomainError
from yipilot.domain.provider import ProviderProfile
from yipilot.harness.models import HarnessReport
from yipilot.harness.quality import SddHarness, SrsHarness
from yipilot.models.secrets import SecretStore
from yipilot.models.selection import ModelSelectionService
from yipilot.models.service import ProviderService
from yipilot.persistence.artifact_store import FileArtifactStore
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.task_store import FileTaskStore
from yipilot.security.redaction import SecretRedactor
from yipilot.transport.cli.events import (
    HumanEventRenderer,
    JsonlEventRenderer,
    is_terminal_event,
)
from yipilot.transport.cli.interactive import InteractiveSession
from yipilot.transport.cli.specification import app as specification_app
from yipilot.workflow.project_service import ProjectService
from yipilot.workflow.quality_artifacts import QualityArtifactService
from yipilot.workflow.sandbox_application import inspect_sandbox, register_sandbox_image
from yipilot.workspace.git_runner import GitRunner
from yipilot.workspace.repo_service import RepoMetadata, RepoService
from yipilot.workspace.workspace_service import PreparedWorkspace, WorkspaceService

app = typer.Typer(no_args_is_help=False, add_completion=False, invoke_without_command=True)
task_app = typer.Typer(no_args_is_help=True)
run_app = typer.Typer(no_args_is_help=True)
repo_app = typer.Typer(no_args_is_help=True)
workspace_app = typer.Typer(no_args_is_help=True)
code_app = typer.Typer(no_args_is_help=True)
quality_app = typer.Typer(no_args_is_help=True)
settings_app = typer.Typer(no_args_is_help=True)
provider_app = typer.Typer(no_args_is_help=True)
model_app = typer.Typer(no_args_is_help=True)
sandbox_app = typer.Typer(no_args_is_help=True)
app.add_typer(task_app, name="task")
app.add_typer(run_app, name="run")
app.add_typer(code_app, name="code")
app.add_typer(quality_app, name="quality")
app.add_typer(specification_app, name="specification")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(settings_app, name="settings")
task_app.add_typer(repo_app, name="repo")
task_app.add_typer(workspace_app, name="workspace")
settings_app.add_typer(provider_app, name="provider")
settings_app.add_typer(model_app, name="model")

Result = TypeVar("Result")
_DEFAULT_PROJECT = Path(".")


@provider_app.callback()
def provider_deprecation() -> None:
    # Keep machine-readable --output json streams single-object compatible.
    if sys.stderr.isatty():
        typer.echo("DEPRECATION: settings provider is supported through M5 only; use settings model.", err=True)


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version"),
    output: str = typer.Option("human", "--output"),
    interactive: bool = typer.Option(False, "--interactive"),
    project: Annotated[Path | None, typer.Option("--project")] = None,
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
) -> None:
    if not version and ctx.invoked_subcommand is not None:
        return
    if version and output == "json":
        typer.echo(
            json.dumps(
                {"name": "yipilot", "version": __version__}, separators=(",", ":")
            )
        )
    elif version and output == "human":
        typer.echo(f"yipilot {__version__}")
    elif version:
        raise typer.BadParameter(
            "must be either 'human' or 'json'", param_hint="--output"
        )

    if version:
        raise typer.Exit()
    if output != "human":
        raise typer.BadParameter("interactive session requires --output human", param_hint="--output")
    if not interactive and not sys.stdin.isatty():
        typer.echo("Interactive session requires a TTY; use --interactive with scripted stdin.")
        return
    InteractiveSession(settings_dir=settings_dir, project=project).run()


@model_app.command("list")
def model_list(
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    value = _settings_invoke(output, lambda: ModelSelectionService(settings_dir).list_public())
    if value is not None:
        models = cast(list[object], value["models"])
        _render_value(output, value, f"Configured {len(models)} model(s).")


@model_app.command("select")
def model_select(
    selector: str,
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    def action() -> dict[str, object]:
        ModelSelectionService(settings_dir).set_default(selector)
        return {"default_model_selector": selector}
    value = _settings_invoke(output, action)
    if value is not None:
        _render_value(output, value, f"Global default model is {selector}.")


@model_app.command("test")
def model_test(
    selector: str,
    request: Annotated[bool, typer.Option("--request")] = False,
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    value = _settings_invoke(output, lambda: ModelSelectionService(settings_dir).test(selector, request=request))
    if value is not None:
        _render_value(output, value, f"Model {selector}: {value['status']}.")


@sandbox_app.command("doctor")
def sandbox_doctor(
    image_digest: Annotated[str | None, typer.Option("--image-digest")] = None,
    project: Annotated[Path | None, typer.Option("--project")] = None,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    """Check Docker Desktop and a prepared image without pulling or building."""
    result = _settings_invoke(
        output,
        lambda: inspect_sandbox(project, image_digest),
    )
    if result is not None:
        _render_value(output, result, f"Sandbox preflight passed for {result['image_digest']}.")


@sandbox_app.command("register")
def sandbox_register(
    project: Annotated[Path, typer.Option("--project")],
    image_digest: Annotated[str, typer.Option("--image-digest")],
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    """Register the immutable local image used by interactive sandbox Runs."""
    result = _settings_invoke(output, lambda: register_sandbox_image(project, image_digest))
    if result is not None:
        _render_value(output, result, f"Registered sandbox image for {project}.")


@model_app.command("configure")
def model_configure(
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
) -> None:
    """Configure one OpenAI-compatible connection and model."""
    from yipilot.domain.provider import ModelEntry, ProviderConnection

    service = ModelSelectionService(settings_dir)
    connection_id = typer.prompt("Connection ID")
    base_url = typer.prompt("OpenAI-compatible URL")
    model_id = typer.prompt("Model ID")
    use_auth_file = typer.confirm("Store API key in Yipilot auth.json", default=True)
    reference = f"file:{connection_id}" if use_auth_file else f"env:{typer.prompt('Environment variable name')}"
    key = typer.prompt("API key", hide_input=True) if use_auth_file else None
    connection = ProviderConnection(connection_id=connection_id, base_url=base_url, timeout_seconds=60, credential_ref=reference, revision=1)
    model = ModelEntry(connection_id=connection_id, model_id=model_id, display_name=model_id, output_limit=4096)
    if key is not None:
        SecretStore(settings_dir=settings_dir).set_file(connection_id, key)
    if _settings_invoke("human", lambda: service.add(connection, model)) is None:
        return
    selector = f"{connection_id}/{model_id}"
    if _settings_invoke("human", lambda: service.resolve(explicit_selector=selector)) is None:
        return
    if typer.confirm("Use this model as the global default", default=True):
        _settings_invoke("human", lambda: service.set_default(selector))
    typer.echo(f"Configured model {selector}.")


@app.command("init")
def init_project(
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    record = _invoke(output, lambda: _project_service(project).initialize_project())
    if record is not None:
        _render_value(
            output,
            {"schema_version": record.schema_version},
            "Initialized Yipilot project.",
        )


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
    result = _quality_invoke(
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
    semantic_review: Annotated[bool, typer.Option("--semantic-review")] = False,
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    def action() -> HarnessReport:
        if kind == "srs":
            if semantic_review:
                raise DomainError(
                    "SEMANTIC_REVIEW_KIND_INVALID",
                    "semantic review is only available for the SDD Harness",
                )
            return SrsHarness(project, task_id, run_id).run()
        if kind == "sdd":
            return SddHarness(
                project,
                task_id,
                run_id,
                profile_revision=profile_revision,
                settings_dir=settings_dir,
                semantic_review=semantic_review,
            ).run()
        raise DomainError("HARNESS_KIND_INVALID", "harness kind must be srs or sdd")

    report = cast(HarnessReport | None, _quality_invoke(output, action))
    if report is not None:
        _render(output, report, f"{kind} Harness status: {report.status}.")


@quality_app.command("pytest")
def quality_pytest(
    task_id: Annotated[str, typer.Option("--taskid")],
    run_id: Annotated[str | None, typer.Option("--run-id")] = None,
    alias: Annotated[str | None, typer.Option("--alias")] = None,
    prepare: Annotated[bool, typer.Option("--prepare")] = False,
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
            if (
                alias is None
                or run_id is not None
                or approval_id is not None
                or confirmation_token is not None
            ):
                raise DomainError(
                    "PYTEST_MODE_INVALID",
                    "pytest preparation accepts only a repository alias",
                )
            return service.prepare_pytest(
                task_id, alias, timeout_seconds=timeout_seconds, request_id=request_id
            )
        if alias is not None or request_id is not None:
            raise DomainError(
                "PYTEST_MODE_INVALID", "pytest execution requires only a prepared run"
            )
        return service.run_pytest(
            task_id,
            run_id or "missing",
            approval_id=approval_id,
            confirmation_token=confirmation_token,
        )

    value = _quality_invoke(output, action)
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
    model: Annotated[str | None, typer.Option("--model")] = None,
    context_file: Annotated[list[str] | None, typer.Option("--context-file")] = None,
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
    request_id: Annotated[str | None, typer.Option("--request-id")] = None,
    project: Annotated[Path, typer.Option("--project")] = _DEFAULT_PROJECT,
    output: Annotated[str, typer.Option("--output")] = "human",
) -> None:
    result = _invoke(
        output,
        lambda: ProjectApplication(project, settings_dir=settings_dir).generate(
            task_id,
            alias,
            instruction,
            request_id=request_id,
            model_selector=model,
            context_files=context_file or (),
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


@provider_app.command("configure")
def provider_configure(
    settings_dir: Annotated[Path | None, typer.Option("--settings-dir")] = None,
) -> None:
    """Interactively create one Provider Profile and optionally verify it."""
    service = ProviderService(settings_dir)
    name = typer.prompt("Profile name")
    base_url = typer.prompt("OpenAI-compatible base URL")
    model = typer.prompt("Model ID")
    use_auth_file = typer.confirm(
        "Store API key in Yipilot auth.json", default=True
    )
    api_key_ref = None
    if not use_auth_file:
        api_key_ref = f"env:{typer.prompt('Environment variable name')}"
    profile = _settings_invoke(
        "human",
        lambda: service.add(
            name=name,
            provider_type="openai_compatible",
            base_url=base_url,
            model=model,
            timeout_seconds=60.0,
            output_budget=4096,
            capabilities=("chat",),
            enabled=True,
            revision=1,
            api_key_ref=api_key_ref,
        ),
    )
    if profile is None:
        return
    if use_auth_file:
        key = typer.prompt("API key", hide_input=True)
        profile = _settings_invoke("human", lambda: service.set_key(name, key))
        if profile is None:
            return
    if typer.confirm("Use this profile as the default", default=True):
        profile = _settings_invoke("human", lambda: service.use(name))
        if profile is None:
            return
        def set_default() -> bool:
            service.model_selection.set_default(service.selector_for_profile(name))
            return True

        if _settings_invoke("human", set_default) is None:
            return
    checked = _settings_invoke("human", lambda: service.test(name))
    if checked is None:
        return
    typer.echo(f"Provider profile {profile.name} is configured and locally validated.")
    if typer.confirm("Send a small live provider probe now", default=False):
        probed = _settings_invoke("human", lambda: service.test(name, request=True))
        if probed is not None:
            typer.echo(f"Provider profile {profile.name}: {probed.status}.")


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
    def action() -> ProviderProfile:
        service = ProviderService(settings_dir)
        profile = service.use(name)
        service.model_selection.set_default(service.selector_for_profile(name))
        return profile

    profile = _settings_invoke(output, action)
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


def _quality_invoke(output: str, action: Callable[[], Result]) -> Result | None:
    _validate_output(output)
    try:
        return action()
    except (DomainError, ValidationError) as error:
        code, message = (
            (error.code, error.message)
            if isinstance(error, DomainError)
            else ("VALIDATION_ERROR", "input validation failed")
        )
        value = SecretRedactor().redact({"code": code, "message": message})
        assert isinstance(value, dict)
        safe_code, safe_message = value["code"], value["message"]
        assert isinstance(safe_code, str)
        assert isinstance(safe_message, str)
        _render_value(
            output,
            {"code": safe_code, "message": safe_message},
            f"{safe_code}: {safe_message}",
        )
        raise typer.Exit(code=2 if code == "INVALID_TASK_ID" else 1) from error


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
