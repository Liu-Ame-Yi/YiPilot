import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from yipilot.harness.models import HarnessStatus
from yipilot.harness.quality import SddHarness, SrsHarness
from yipilot.harness.report_store import (
    CODE_HARNESS_REPORT_NAME,
    SDD_HARNESS_REPORT_NAME,
    SRS_HARNESS_REPORT_NAME,
    HarnessReportStore,
)
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.task_store import FileTaskStore
from yipilot.infrastructure.tool_run_store import FileToolRunStore
from yipilot.transport.cli.app import app

runner = CliRunner()


def _git(cwd: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=cwd, text=True).strip()


def _snapshot(source: Path, remote: Path) -> tuple[str, str, str, str]:
    return (
        _git(source, "rev-parse", "HEAD"),
        _git(source, "status", "--porcelain"),
        _git(source, "config", "--get-regexp", r"^remote\."),
        _git(remote, "for-each-ref", "--format=%(refname) %(objectname)"),
    )


def _invoke(*arguments: str) -> dict[str, object]:
    result = runner.invoke(app, list(arguments))
    assert result.exit_code == 0, (result.stdout, result.exception)
    return json.loads(result.stdout)


def _assert_run_evidence(store: FileTaskStore, task_id: str, run_id: str) -> None:
    run = store.get_run(task_id, run_id)
    events = JsonlEventJournal(store.event_journal_path(run_id)).read_after(0)
    assert events
    assert all(
        (event.task_id, event.run_id, event.stage)
        == (run.task_id, run.run_id, run.stage)
        for event in events
    )
    artifacts = FileArtifactStore(store)
    records = artifacts.list(task_id, run_id)
    assert records
    assert all(
        (record.task_id, record.operation_id, record.run_id)
        == (run.task_id, run.operation_id, run.run_id)
        for record in records
    )
    for record in records:
        assert artifacts.verify(task_id, run_id, record.artifact_id) == record


def test_m4_offline_acceptance_keeps_source_and_remote_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Yipilot Tests")
    _git(source, "config", "user.email", "tests@yipilot.invalid")
    (source / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    _git(source, "add", "tracked.py")
    _git(source, "commit", "-m", "initial")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-u", "origin", "main")
    before = _snapshot(source, remote)

    project = tmp_path / "project"
    project.mkdir()
    _invoke("init", "--project", str(project), "--output", "json")
    _invoke("task", "init", "--project", str(project), "--taskid", "1001", "--output", "json")
    _invoke(
        "task", "repo", "add", "--project", str(project), "--taskid", "1001",
        "--url", str(source), "--alias", "sample", "--sync", "--output", "json",
    )

    generated = _invoke(
        "code", "generate", "--project", str(project), "--taskid", "1001",
        "--alias", "sample", "--instruction", "offline fake diagnostic",
        "--planner", "fake", "--output", "json",
    )
    code_run_id = str(generated["run_id"])
    store = FileTaskStore(project)
    _assert_run_evidence(store, "1001", code_run_id)
    code_run = store.get_run("1001", code_run_id)
    tool_runs = FileToolRunStore(store).list("1001", code_run_id)
    assert len(tool_runs) == 1
    assert all(
        (item.task_id, item.operation_id, item.run_id)
        == (code_run.task_id, code_run.operation_id, code_run.run_id)
        for item in tool_runs
    )
    code_report = HarnessReportStore(store).load(code_run_id, CODE_HARNESS_REPORT_NAME)
    assert code_report.status is HarnessStatus.PASSED

    quality = _invoke(
        "quality", "generate", "--project", str(project), "--taskid", "1001",
        "--prd", "Build a redacted offline quality workflow.", "--model", "replay",
        "--output", "json",
    )
    quality_run_id = str(quality["run_id"])
    _assert_run_evidence(store, "1001", quality_run_id)
    quality_paths = {item["relative_path"] for item in quality["artifact_refs"]}
    assert {"artifacts/srs.md", "artifacts/sdd.md", "artifacts/test-plan.md"} <= quality_paths
    assert SrsHarness(project, "1001", quality_run_id).run().status is HarnessStatus.PASSED
    assert SddHarness(project, "1001", quality_run_id, profile_revision=1).run().status is HarnessStatus.PASSED
    reports = HarnessReportStore(store)
    assert reports.load(quality_run_id, SRS_HARNESS_REPORT_NAME).status is HarnessStatus.PASSED
    assert reports.load(quality_run_id, SDD_HARNESS_REPORT_NAME).status is HarnessStatus.PASSED

    handoff = _invoke(
        "code", "apply", "--project", str(project), "--taskid", "1001",
        "--run-id", code_run_id, "--prepare-only", "--output", "json",
    )
    handoff_run_id = str(handoff["run_id"])
    _assert_run_evidence(store, "1001", handoff_run_id)
    handoff_ref = handoff["handoff_artifact_ref"]
    assert isinstance(handoff_ref, dict)
    handoff_path = store.event_journal_path(handoff_run_id).parent / str(handoff_ref["relative_path"])
    handoff_value = json.loads(handoff_path.read_text(encoding="utf-8"))
    assert handoff_value["prepare_only"] is True
    assert handoff_value["code_harness_report_ref"]["path"].endswith(
        f"runs/{code_run_id}/harness/code-harness-report.json"
    )
    assert _snapshot(source, remote) == before
