import importlib
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yipilot.application.code_service import CodeService
from yipilot.application.provider_service import ProviderService
from yipilot.application.quality_artifacts import QualityArtifactService
from yipilot.domain.errors import DomainError
from yipilot.domain.model import ModelChunk, ModelRequest
from yipilot.harness.models import HarnessStatus
from yipilot.harness.quality import SddHarness, SrsHarness
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.secret_store import SecretStore
from yipilot.infrastructure.task_store import FileTaskStore
from yipilot.transport.cli.app import app

runner = CliRunner()


def _git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=path, text=True).strip()


def _project(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Yipilot Tests")
    _git(source, "config", "user.email", "tests@yipilot.invalid")
    (source / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    (source / "unselected.py").write_text("private = 2\n", encoding="utf-8")
    _git(source, "add", "tracked.py", "unselected.py")
    _git(source, "commit", "-m", "initial")
    project = tmp_path / "project"
    project.mkdir()
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    assert (
        runner.invoke(
            app, ["task", "init", "--project", str(project), "--taskid", "1001"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            [
                "task",
                "repo",
                "add",
                "--project",
                str(project),
                "--taskid",
                "1001",
                "--url",
                str(source),
                "--alias",
                "sample",
                "--sync",
            ],
        ).exit_code
        == 0
    )
    return project, source


def _chunks(content: str) -> tuple[ModelChunk, ...]:
    return tuple(
        ModelChunk(content=content[index : index + 4096])
        for index in range(0, len(content), 4096)
    )


class _Port:
    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[ModelRequest] = []

    def stream(self, request: ModelRequest) -> tuple[ModelChunk, ...]:
        self.requests.append(request)
        return _chunks(self.content)


def _providers(
    tmp_path: Path,
    port: _Port,
    *,
    name: str = "live",
    enabled: bool = True,
    capabilities: tuple[str, ...] = ("chat",),
    api_key_ref: str | None = "env:LIVE_KEY",
) -> ProviderService:
    service = ProviderService(
        tmp_path / "settings",
        secrets=SecretStore(environ={"LIVE_KEY": "live-secret"}),
        port_factory=lambda _model, _secrets, _reference: port,
    )
    service.add(
        name=name,
        provider_type="openai_compatible",
        base_url="https://example.test/v1",
        model="test-model",
        timeout_seconds=10,
        output_budget=64 * 1024,
        capabilities=capabilities,
        enabled=enabled,
        revision=1,
        api_key_ref=api_key_ref,
    )
    if enabled and capabilities == ("chat",) and api_key_ref is not None:
        service.model_selection.set_default(service.selector_for_profile(name))
    return service


def _proposal(path: str = "tracked.py") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "summary": "Update tracked value.",
            "patches": [
                {
                    "path": path,
                    "unified_diff": f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-value = 1\n+value = 2\n",
                }
            ],
        }
    )


def _approve_quality_documents(project: Path, task_id: str, run_id: str) -> None:
    assert SrsHarness(project, task_id, run_id).run().status is HarnessStatus.PASSED
    assert (
        SddHarness(project, task_id, run_id, profile_revision=1).run().status
        is HarnessStatus.PASSED
    )


def test_live_proposal_persists_only_evidence_and_selected_context(
    tmp_path: Path,
) -> None:
    project, source = _project(tmp_path)
    documents = QualityArtifactService(project).generate(
        "1001",
        prd_text="Build a redacted offline quality workflow.",
        model_mode="fake",
    )
    _approve_quality_documents(project, "1001", documents.run_id)
    port = _Port(_proposal())
    result = CodeService(project, provider_service=_providers(tmp_path, port)).generate(
        "1001",
        "sample",
        "change only tracked",
        "live",
        provider_profile="live",
        context_files=("tracked.py",),
    )

    assert result.status == "succeeded"
    assert result.plan_artifact_ref is None
    assert result.patch_proposal_artifact_ref is not None
    assert _git(source, "status", "--porcelain") == ""
    assert (source / "tracked.py").read_text(encoding="utf-8") == "value = 1\n"
    assert len(port.requests) == 1
    prompt = port.requests[0].user_instruction
    assert "tracked.py" in prompt
    assert "unselected.py" not in prompt
    assert "artifacts/srs.md" in prompt and "artifacts/sdd.md" in prompt
    store = FileTaskStore(project)
    root = store.event_journal_path(result.run_id).parent
    profile = json.loads((root / "artifacts/resolved-model.json").read_text(encoding="utf-8"))
    assert profile["model_id"] == "test-model"
    assert profile["provider_type"] == "openai_compatible"
    assert "credential_ref" not in profile
    assert "live-secret" not in (root / "artifacts/resolved-model.json").read_text(
        encoding="utf-8"
    )
    manifest = (root / "artifacts/live-context.md").read_text(encoding="utf-8")
    assert "tracked.py" in manifest and "unselected.py" not in manifest
    assert "artifacts/srs.md" in manifest and "artifacts/test-plan.md" in manifest
    records = FileArtifactStore(store).list("1001", result.run_id)
    assert {record.relative_path for record in records} >= {
        "artifacts/resolved-model.json",
        "artifacts/live-context.md",
        "artifacts/live-model-response.json",
        "artifacts/live-model-call.json",
        "artifacts/patch-proposal.json",
    }


def test_live_withholds_quality_documents_without_passed_harnesses(
    tmp_path: Path,
) -> None:
    project, _source = _project(tmp_path)
    QualityArtifactService(project).generate(
        "1001",
        prd_text="Build a redacted offline quality workflow.",
        model_mode="fake",
    )
    port = _Port(_proposal())

    result = CodeService(project, provider_service=_providers(tmp_path, port)).generate(
        "1001",
        "sample",
        "only selected context",
        "live",
        provider_profile="live",
        context_files=("tracked.py",),
    )

    assert result.status == "succeeded"
    assert "artifacts/srs.md" not in port.requests[0].user_instruction


def test_live_uses_default_profile_and_cli_accepts_live_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _source = _project(tmp_path)
    port = _Port(_proposal())
    service = CodeService(
        project, provider_service=_providers(tmp_path, port, name="default-live")
    )
    cli_module = importlib.import_module("yipilot.transport.cli.app")
    monkeypatch.setattr(
        cli_module, "CodeService", lambda _project, settings_dir=None: service
    )

    result = runner.invoke(
        app,
        [
            "code",
            "generate",
            "--project",
            str(project),
            "--taskid",
            "1001",
            "--alias",
            "sample",
            "--instruction",
            "default profile",
            "--planner",
            "live",
            "--context-file",
            "tracked.py",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["status"] == "succeeded"
    assert port.requests[0].provider_profile_name == "default-live"


@pytest.mark.parametrize(
    ("enabled", "capabilities", "api_key_ref", "code"),
    [
        (False, ("chat",), "env:LIVE_KEY", "PROVIDER_DISABLED"),
        (True, ("embeddings",), "env:LIVE_KEY", "PROVIDER_CAPABILITY_UNAVAILABLE"),
        (True, ("chat",), None, "SECRET_REF_MISSING"),
    ],
)
def test_live_profile_validation_happens_before_worktree_prepare(
    tmp_path: Path,
    enabled: bool,
    capabilities: tuple[str, ...],
    api_key_ref: str | None,
    code: str,
) -> None:
    project, source = _project(tmp_path)
    service = CodeService(
        project,
        provider_service=_providers(
            tmp_path,
            _Port(_proposal()),
            enabled=enabled,
            capabilities=capabilities,
            api_key_ref=api_key_ref,
        ),
    )

    with pytest.raises(DomainError, match=rf"^{code}:"):
        service.generate(
            "1001", "sample", "must not start", "live", provider_profile="live"
        )

    assert _git(source, "status", "--porcelain") == ""
    workspaces = project / ".yipilot/tasks/1001/repos/sample/workspaces"
    assert not workspaces.exists() or not list(workspaces.iterdir())


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("not json", "PATCH_PROPOSAL_INVALID"),
        (_proposal("/absolute.py"), "PATCH_PROPOSAL_INVALID"),
        (_proposal("../escape.py"), "PATCH_PROPOSAL_INVALID"),
        (
            json.dumps(
                {
                    "schema_version": 1,
                    "summary": "bad diff",
                    "patches": [
                        {
                            "path": "tracked.py",
                            "unified_diff": "--- a/tracked.py\n+++ b/tracked.py\n",
                        }
                    ],
                }
            ),
            "PATCH_PROPOSAL_INVALID",
        ),
        (
            json.dumps(
                {
                    "schema_version": 1,
                    "summary": "malformed hunk",
                    "patches": [
                        {
                            "path": "tracked.py",
                            "unified_diff": "--- a/tracked.py\n+++ b/tracked.py\n@@ nonsense\n",
                        }
                    ],
                }
            ),
            "PATCH_PROPOSAL_INVALID",
        ),
        (
            json.dumps(
                {
                    "schema_version": 1,
                    "summary": "wrong hunk count",
                    "patches": [
                        {
                            "path": "tracked.py",
                            "unified_diff": "--- a/tracked.py\n+++ b/tracked.py\n@@ -1,2 +1 @@\n-value = 1\n+value = 2\n",
                        }
                    ],
                }
            ),
            "PATCH_PROPOSAL_INVALID",
        ),
        (
            json.dumps(
                {
                    "schema_version": 1,
                    "summary": "duplicate",
                    "patches": [
                        json.loads(_proposal())["patches"][0],
                        json.loads(_proposal())["patches"][0],
                    ],
                }
            ),
            "PATCH_PROPOSAL_INVALID",
        ),
        ("x" * (64 * 1024 + 1), "MODEL_OUTPUT_LIMIT_EXCEEDED"),
    ],
    ids=(
        "invalid-json",
        "absolute-path",
        "traversal-path",
        "invalid-diff",
        "malformed-hunk",
        "wrong-hunk-count",
        "duplicate-path",
        "over-output-budget",
    ),
)
def test_invalid_live_outputs_fail_without_source_modification(
    tmp_path: Path, content: str, code: str
) -> None:
    project, source = _project(tmp_path)
    port = _Port(content)
    result = CodeService(project, provider_service=_providers(tmp_path, port)).generate(
        "1001",
        "sample",
        f"invalid {code}",
        "live",
        request_id=code,
        context_files=("tracked.py",),
    )

    assert result.status == "failed"
    assert result.error_code == code
    assert result.patch_proposal_artifact_ref is None
    assert (source / "tracked.py").read_text(encoding="utf-8") == "value = 1\n"
    assert _git(source, "status", "--porcelain") == ""
    root = FileTaskStore(project).event_journal_path(result.run_id).parent
    assert (root / "artifacts/live-model-call.json").is_file()
    assert not (root / "artifacts/patch-proposal.json").exists()
