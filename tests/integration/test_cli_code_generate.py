import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from yipilot.infrastructure.task_store import FileTaskStore
from yipilot.transport.cli.app import app

runner = CliRunner()


def _git(cwd: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=cwd, text=True).strip()


def _project_with_repository(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Yipilot Tests")
    _git(source, "config", "user.email", "tests@yipilot.invalid")
    (source / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    _git(source, "add", "tracked.py")
    _git(source, "commit", "-m", "initial")
    project = tmp_path / "project"
    project.mkdir()
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    assert runner.invoke(
        app, ["task", "init", "--project", str(project), "--taskid", "1001"]
    ).exit_code == 0
    assert runner.invoke(
        app,
        [
            "task", "repo", "add", "--project", str(project), "--taskid", "1001",
            "--url", str(source), "--alias", "sample", "--sync",
        ],
    ).exit_code == 0
    return project, source


def _generate(project: Path, instruction: str = "offline fake diagnostic") -> dict[str, object]:
    result = runner.invoke(
        app,
        [
            "code", "generate", "--project", str(project), "--taskid", "1001",
            "--alias", "sample", "--instruction", instruction, "--planner", "fake",
            "--output", "json",
        ],
    )
    assert result.exit_code == 0, (result.stdout, result.exception)
    value = json.loads(result.stdout)
    assert result.stdout == json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    return value


def test_fake_code_generate_is_auditable_and_prepare_only_leaves_git_unchanged(tmp_path: Path) -> None:
    project, source = _project_with_repository(tmp_path)
    source_head = _git(source, "rev-parse", "HEAD")
    generated = _generate(project, "Authorization: Bearer instruction-secret")
    run_id = str(generated["run_id"])
    store = FileTaskStore(project)
    artifacts = list((store.event_journal_path(run_id).parent / "artifacts" / "records").glob("*.json"))
    tool_runs = list((store.event_journal_path(run_id).parent / "tool-runs").glob("*.json"))
    assert generated["status"] == "succeeded"
    assert generated["plan_artifact_ref"] is not None
    assert artifacts and tool_runs

    watched = runner.invoke(
        app, ["run", "watch", "--project", str(project), "--run-id", run_id, "--output", "jsonl"]
    )
    assert watched.exit_code == 0
    assert [json.loads(line)["type"] for line in watched.stdout.splitlines()] == [
        "run.started", "plan.created", "tool.started", "tool.completed", "harness.completed", "run.succeeded",
    ]

    applied = runner.invoke(
        app,
        [
            "code", "apply", "--project", str(project), "--taskid", "1001", "--run-id", run_id,
            "--prepare-only", "--output", "json",
        ],
    )
    assert applied.exit_code == 0
    value = json.loads(applied.stdout)
    handoff_ref = value["handoff_artifact_ref"]
    handoff_path = store.event_journal_path(run_id).parent / handoff_ref["relative_path"]
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    assert handoff["prepare_only"] is True
    assert handoff["approval_refs"] == [{"approval_id": "fake-read-only-simulation", "simulation_only": True}]
    assert handoff["git_evidence_refs"]
    assert "instruction-secret" not in handoff_path.read_text(encoding="utf-8")
    assert "argv" not in handoff_path.read_text(encoding="utf-8")
    assert _git(source, "rev-parse", "HEAD") == source_head
    assert _git(source, "status", "--porcelain") == ""


def test_replay_requires_exact_recording_without_a_tool_run(tmp_path: Path) -> None:
    project, _source = _project_with_repository(tmp_path)
    replay = runner.invoke(
        app,
        [
            "code", "generate", "--project", str(project), "--taskid", "1001", "--alias", "sample",
            "--instruction", "yipilot replay diagnostic", "--planner", "replay", "--output", "json",
        ],
    )
    assert replay.exit_code == 0, (replay.stdout, replay.exception)
    assert json.loads(replay.stdout)["status"] == "succeeded"
    missed = runner.invoke(
        app,
        [
            "code", "generate", "--project", str(project), "--taskid", "1001", "--alias", "sample",
            "--instruction", "unknown recording", "--planner", "replay", "--output", "json",
        ],
    )
    assert missed.exit_code == 1
    missed_value = json.loads(missed.stdout)
    assert missed_value["error_code"] == "PLANNER_REPLAY_NOT_FOUND"
    assert missed_value["status"] == "failed"
    run_root = FileTaskStore(project).event_journal_path(str(missed_value["run_id"])).parent
    assert not (run_root / "tool-runs").exists()


def test_prepare_only_rejects_missing_approval_tampered_evidence_and_wrong_run(tmp_path: Path) -> None:
    project, _source = _project_with_repository(tmp_path)
    generated = _generate(project)
    run_id = str(generated["run_id"])
    run_root = FileTaskStore(project).event_journal_path(run_id).parent
    approval = run_root / "approvals" / "fake-read-only-simulation.json"
    approval.unlink()
    missing = runner.invoke(
        app,
        [
            "code", "apply", "--project", str(project), "--taskid", "1001", "--run-id", run_id,
            "--prepare-only", "--output", "json",
        ],
    )
    assert missing.exit_code == 1
    assert json.loads(missing.stdout)["code"] == "APPROVAL_REQUIRED"
    assert not (run_root / "artifacts" / "prepare-only-handoff.json").exists()

    store = FileTaskStore(project)
    initial_run = store.list_runs("1001")[0]
    wrong_run = runner.invoke(
        app,
        [
            "code", "apply", "--project", str(project), "--taskid", "1001", "--run-id", initial_run.run_id,
            "--prepare-only", "--output", "json",
        ],
    )
    assert wrong_run.exit_code == 1
    assert json.loads(wrong_run.stdout)["code"] == "CODE_HANDOFF_PRECONDITION_FAILED"


def test_prepare_only_rejects_tampered_tool_evidence_without_a_handoff(tmp_path: Path) -> None:
    project, _source = _project_with_repository(tmp_path)
    generated = _generate(project)
    run_id = str(generated["run_id"])
    store = FileTaskStore(project)
    run_root = store.event_journal_path(run_id).parent
    tool_run = next((run_root / "tool-runs").glob("*.json"))
    stdout_ref = json.loads(tool_run.read_text(encoding="utf-8"))["stdout_artifact_ref"]
    (run_root / stdout_ref["relative_path"]).write_bytes(b"tampered")
    result = runner.invoke(
        app,
        [
            "code", "apply", "--project", str(project), "--taskid", "1001", "--run-id", run_id,
            "--prepare-only", "--output", "json",
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "CODE_HARNESS_FAILED"
    assert not (run_root / "artifacts" / "prepare-only-handoff.json").exists()
