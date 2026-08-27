import hashlib
import json
from pathlib import Path

import pytest

from yipilot.application.quality_artifacts import QualityArtifactService
from yipilot.domain.errors import DomainError
from yipilot.domain.model import ModelChunk, ModelResponse
from yipilot.domain.provider import ProviderProfile
from yipilot.harness.models import HarnessStatus
from yipilot.harness.quality import SddHarness, SrsHarness
from yipilot.infrastructure.provider_store import FileProviderStore
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
        "artifacts/prd.md",
        "artifacts/srs.md",
        "artifacts/sdd.md",
        "artifacts/test-plan.md",
        "artifacts/quality-metadata.json",
    }
    assert (
        SrsHarness(project, "1001", result.run_id).run().status is HarnessStatus.PASSED
    )
    assert (
        SddHarness(project, "1001", result.run_id).run().status is HarnessStatus.PASSED
    )
    events = (root / "events.jsonl").read_text(encoding="utf-8")
    assert "Build a redacted" not in events
    assert "quality-model-response" not in events


def test_quality_harnesses_block_stale_metadata_and_profile(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = QualityArtifactService(project).generate(
        "1001", prd_text="Build beta capability.", model_mode="replay"
    )
    assert (
        SddHarness(project, "1001", result.run_id, profile_revision=2).verify().status
        is HarnessStatus.FAILED
    )
    metadata = (
        project
        / ".yipilot/tasks/1001/runs"
        / result.run_id
        / "artifacts/quality-metadata.json"
    )
    value = json.loads(metadata.read_text(encoding="utf-8"))
    value["input_fingerprint"] = "0" * 64
    metadata.write_text(json.dumps(value), encoding="utf-8")
    assert (
        SrsHarness(project, "1001", result.run_id).verify().status
        is HarnessStatus.FAILED
    )


def test_model_payloads_follow_distinct_prds_and_real_profile_revision(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    settings = tmp_path / "settings"
    provider = ProviderProfile(
        name="fixture-profile",
        base_url="https://example.invalid",
        model="fixture-model",
        timeout_seconds=30,
        output_budget=4096,
        revision=1,
    )
    providers = FileProviderStore(settings)
    providers.add(provider)
    service = QualityArtifactService(project, settings_dir=settings)
    first = service.generate(
        "1001",
        prd_text="Build alpha capability.",
        model_mode="fake",
        provider_profile_name="fixture-profile",
    )
    second = service.generate(
        "1001",
        prd_text="Build beta capability.",
        model_mode="replay",
        request_id="beta",
        provider_profile_name="fixture-profile",
    )
    root = project / ".yipilot/tasks/1001/runs"
    first_srs = (root / first.run_id / "artifacts/srs.md").read_text(encoding="utf-8")
    second_srs = (root / second.run_id / "artifacts/srs.md").read_text(encoding="utf-8")
    assert first_srs != second_srs
    assert "alpha" in first_srs
    assert "beta" in second_srs
    assert (
        SddHarness(project, "1001", first.run_id, settings_dir=settings).verify().status
        is HarnessStatus.PASSED
    )
    providers.replace(provider.model_copy(update={"revision": 2}))
    stale = SddHarness(project, "1001", first.run_id, settings_dir=settings).verify()
    assert stale.status is HarnessStatus.FAILED
    assert (
        next(
            check for check in stale.checks if check.id == "SDD-PROFILE-REVISION-001"
        ).status
        is HarnessStatus.FAILED
    )


def _fixture(*, srs: str, sdd: str, test_plan: str) -> ModelResponse:
    return ModelResponse(
        chunks=(
            ModelChunk(
                content=json.dumps(
                    {
                        "schema_version": 1,
                        "srs": srs,
                        "sdd": sdd,
                        "test_plan": test_plan,
                    }
                )
            ),
        )
    )


def _valid_documents(requirement: str = "FR-901") -> tuple[str, str, str]:
    return (
        f"# SRS\n\n## Goals and scope\nFixture.\n\n## Functional requirements\n- {requirement}: Fixture.\n\n## Non-functional requirements\n- NFR-901: Bounded.\n\n## Acceptance criteria\n- {requirement} passes.\n\n## Assumptions and constraints\n- Explicit input.\n",
        f"# SDD\n\n## Architecture and components\nFixture.\n\n## Interfaces and data flow\nPRD -> SRS.\n\n## Constraints\nPython only.\n\n## Error handling\nBlock stale.\n\n## Test traceability\n{requirement}.\n\n## SRS reference\nSRS Artifact: {{SRS_ARTIFACT}}.\n",
        f"# Test Plan\n\n## Test scope\n{requirement}.\n\n## Preconditions\nFresh.\n\n## Test cases and commands\n`python -m pytest`\n\n## Expected results\nPass.\n\n## Requirements and design references\n{requirement}; SRS Artifact: {{SRS_ARTIFACT}}; SDD Artifact: {{SDD_ARTIFACT}}.\n",
    )


def test_replay_requires_exact_fixture_and_payloads_are_injected(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    first_prd, second_prd = "Fixture one.", "Fixture two."
    first_docs = _valid_documents("FR-111")
    second_docs = _valid_documents("FR-222")
    fixtures = {
        hashlib.sha256(first_prd.encode()).hexdigest(): _fixture(
            srs=first_docs[0], sdd=first_docs[1], test_plan=first_docs[2]
        ),
        hashlib.sha256(second_prd.encode()).hexdigest(): _fixture(
            srs=second_docs[0], sdd=second_docs[1], test_plan=second_docs[2]
        ),
    }
    service = QualityArtifactService(project, fixture_responses=fixtures)
    first = service.generate("1001", prd_text=first_prd, model_mode="fake")
    second = service.generate("1001", prd_text=second_prd, model_mode="replay")
    root = project / ".yipilot/tasks/1001/runs"
    assert (root / first.run_id / "artifacts/srs.md").read_text(encoding="utf-8") != (
        root / second.run_id / "artifacts/srs.md"
    ).read_text(encoding="utf-8")
    with pytest.raises(DomainError, match="MODEL_REPLAY_NOT_FOUND"):
        service.generate("1001", prd_text="Unknown fixture.", model_mode="replay")


def test_harnesses_block_malformed_srs_sdd_and_test_plan(tmp_path: Path) -> None:
    project = _project(tmp_path)
    prd = "Broken fixture."
    srs, sdd, plan = _valid_documents()
    fixtures = {
        hashlib.sha256(prd.encode()).hexdigest(): _fixture(
            srs=srs.removeprefix("# SRS\n\n"),
            sdd=sdd.replace("SRS Artifact: {SRS_ARTIFACT}", "missing"),
            test_plan=plan.removeprefix("# Test Plan\n\n"),
        )
    }
    result = QualityArtifactService(project, fixture_responses=fixtures).generate(
        "1001", prd_text=prd, model_mode="fake"
    )
    srs_report = SrsHarness(project, "1001", result.run_id).verify()
    sdd_report = SddHarness(project, "1001", result.run_id).verify()
    assert srs_report.status is HarnessStatus.FAILED
    assert sdd_report.status is HarnessStatus.FAILED
    assert (
        next(
            check for check in srs_report.checks if check.id == "SRS-STRUCTURE-001"
        ).status
        is HarnessStatus.FAILED
    )
    assert (
        next(
            check
            for check in srs_report.checks
            if check.id == "TEST-PLAN-STRUCTURE-001"
        ).status
        is HarnessStatus.FAILED
    )
    assert (
        next(
            check for check in sdd_report.checks if check.id == "SDD-SRS-REFERENCE-001"
        ).status
        is HarnessStatus.FAILED
    )


def test_srs_harness_blocks_tampered_upstream_prd_artifact(tmp_path: Path) -> None:
    project = _project(tmp_path)
    service = QualityArtifactService(project)
    source = service.generate(
        "1001", prd_text="Build alpha capability.", model_mode="fake"
    )
    source_prd = next(
        ref
        for ref in source.artifact_refs
        if ref["relative_path"] == "artifacts/prd.md"
    )
    downstream = service.generate(
        "1001",
        source_run_id=source.run_id,
        prd_artifact_id=source_prd["artifact_id"],
        model_mode="replay",
        request_id="upstream",
    )
    source_path = (
        project / ".yipilot/tasks/1001/runs" / source.run_id / "artifacts/prd.md"
    )
    source_path.write_text("tampered", encoding="utf-8")
    report = SrsHarness(project, "1001", downstream.run_id).verify()
    assert report.status is HarnessStatus.FAILED
    assert (
        next(check for check in report.checks if check.id == "SRS-UPSTREAM-001").status
        is HarnessStatus.FAILED
    )


def test_srs_requirement_and_test_plan_reference_checks_block(tmp_path: Path) -> None:
    project = _project(tmp_path)
    prd = "Missing reference fixture."
    srs, sdd, plan = _valid_documents()
    fixtures = {
        hashlib.sha256(prd.encode()).hexdigest(): _fixture(
            srs=srs.replace("FR-901", "requirement"),
            sdd=sdd,
            test_plan=plan.replace("SDD Artifact: {SDD_ARTIFACT}", "missing"),
        )
    }
    result = QualityArtifactService(project, fixture_responses=fixtures).generate(
        "1001", prd_text=prd, model_mode="fake"
    )
    report = SrsHarness(project, "1001", result.run_id).verify()
    assert report.status is HarnessStatus.FAILED
    assert (
        next(
            check for check in report.checks if check.id == "SRS-REQUIREMENTS-001"
        ).status
        is HarnessStatus.FAILED
    )
    assert (
        next(
            check for check in report.checks if check.id == "TEST-PLAN-REFERENCES-001"
        ).status
        is HarnessStatus.FAILED
    )
