import json
from pathlib import Path

from yipilot.application.quality_artifacts import QualityArtifactService
from yipilot.harness.models import HarnessStatus
from yipilot.harness.quality import SddHarness, SrsHarness
from yipilot.infrastructure.task_store import FileTaskStore


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    store = FileTaskStore(project)
    store.initialize_project()
    store.create_task("1001")
    return project


def test_fake_quality_documents_are_run_scoped_and_harnessed(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = QualityArtifactService(project).generate(
        "1001", prd_text="Build a redacted offline quality workflow.", model_mode="fake"
    )
    root = project / ".yipilot/tasks/1001/runs" / result.run_id
    assert {item["relative_path"] for item in result.artifact_refs} == {
        "artifacts/prd.md", "artifacts/srs.md", "artifacts/sdd.md",
        "artifacts/test-plan.md", "artifacts/quality-metadata.json",
    }
    assert SrsHarness(project, "1001", result.run_id).run().status is HarnessStatus.PASSED
    assert SddHarness(project, "1001", result.run_id).run().status is HarnessStatus.PASSED
    events = (root / "events.jsonl").read_text(encoding="utf-8")
    assert "Build a redacted" not in events
    assert "quality-model-response" not in events


def test_quality_harnesses_block_stale_metadata_and_profile(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = QualityArtifactService(project).generate("1001", prd_text="A PRD", model_mode="replay")
    assert SddHarness(project, "1001", result.run_id, profile_revision=2).verify().status is HarnessStatus.FAILED
    metadata = project / ".yipilot/tasks/1001/runs" / result.run_id / "artifacts/quality-metadata.json"
    value = json.loads(metadata.read_text(encoding="utf-8"))
    value["input_fingerprint"] = "0" * 64
    metadata.write_text(json.dumps(value), encoding="utf-8")
    assert SrsHarness(project, "1001", result.run_id).verify().status is HarnessStatus.FAILED