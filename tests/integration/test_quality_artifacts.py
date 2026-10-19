import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.helpers.sqlite_records import event_text, read_metadata, write_metadata
from yipilot.domain.errors import DomainError
from yipilot.domain.model import ModelChunk, ModelResponse
from yipilot.domain.provider import ProviderProfile
from yipilot.harness.models import HarnessStatus
from yipilot.harness.quality import SddHarness, SrsHarness
from yipilot.models.store import FileProviderStore
from yipilot.persistence.artifact_store import FileArtifactStore
from yipilot.persistence.task_store import FileTaskStore
from yipilot.workflow.quality_artifacts import QualityArtifactService


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
        SddHarness(project, "1001", result.run_id, profile_revision=1).run().status
        is HarnessStatus.PASSED
    )
    events = event_text(root / "events.jsonl")
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
    sdd_report = SddHarness(project, "1001", result.run_id, profile_revision=1).verify()
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


def _check(report: object, check_id: str) -> HarnessStatus:
    return next(check.status for check in report.checks if check.id == check_id)


def test_sdd_harness_rechecks_prd_fingerprint_and_upstream_artifact(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    service = QualityArtifactService(project)
    local = service.generate(
        "1001", prd_text="Build beta capability.", model_mode="fake"
    )
    assert (
        _check(
            SddHarness(project, "1001", local.run_id, profile_revision=1).verify(),
            "SDD-PROFILE-REVISION-001",
        )
        is HarnessStatus.PASSED
    )
    local_prd = project / ".yipilot/tasks/1001/runs" / local.run_id / "artifacts/prd.md"
    local_prd.write_text("tampered", encoding="utf-8")
    local_report = SddHarness(
        project, "1001", local.run_id, profile_revision=1
    ).verify()
    assert local_report.status is HarnessStatus.FAILED
    assert _check(local_report, "SDD-PRD-ARTIFACT-001") is HarnessStatus.FAILED

    source = service.generate(
        "1001",
        prd_text="Build alpha capability.",
        model_mode="fake",
        request_id="source",
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
        request_id="downstream",
    )
    (project / ".yipilot/tasks/1001/runs" / source.run_id / "artifacts/prd.md").unlink()
    upstream_report = SddHarness(
        project, "1001", downstream.run_id, profile_revision=1
    ).verify()
    assert upstream_report.status is HarnessStatus.FAILED
    assert _check(upstream_report, "SDD-UPSTREAM-001") is HarnessStatus.FAILED


def test_sdd_harness_requires_independent_offline_revision_and_blocks_semantic_review(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    result = QualityArtifactService(project).generate(
        "1001", prd_text="Build beta capability.", model_mode="fake"
    )
    missing = SddHarness(project, "1001", result.run_id).verify()
    assert missing.status is HarnessStatus.FAILED
    assert _check(missing, "SDD-PROFILE-REVISION-001") is HarnessStatus.FAILED

    enabled = SddHarness(
        project,
        "1001",
        result.run_id,
        profile_revision=1,
        semantic_review=True,
    ).verify()
    assert enabled.status is HarnessStatus.FAILED
    assert _check(enabled, "SDD-SEMANTIC-REVIEW-001") is HarnessStatus.FAILED


def test_sdd_harness_blocks_invalid_metadata_fingerprint_and_missing_profile(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    settings = tmp_path / "settings"
    provider = ProviderProfile(
        name="current-profile",
        base_url="https://example.invalid",
        model="fixture-model",
        timeout_seconds=30,
        output_budget=4096,
        revision=1,
    )
    FileProviderStore(settings).add(provider)
    result = QualityArtifactService(project, settings_dir=settings).generate(
        "1001",
        prd_text="Build beta capability.",
        model_mode="fake",
        provider_profile_name="current-profile",
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
    report = SddHarness(project, "1001", result.run_id, settings_dir=settings).verify()
    assert report.status is HarnessStatus.FAILED
    assert _check(report, "QUALITY-METADATA-001") is HarnessStatus.FAILED

    fresh = QualityArtifactService(project, settings_dir=settings).generate(
        "1001",
        prd_text="Build alpha capability.",
        model_mode="fake",
        provider_profile_name="current-profile",
        request_id="missing-profile",
    )
    missing = SddHarness(
        project, "1001", fresh.run_id, settings_dir=tmp_path / "empty"
    ).verify()
    assert missing.status is HarnessStatus.FAILED
    assert _check(missing, "SDD-PROFILE-REVISION-001") is HarnessStatus.FAILED


def _replace_registered_metadata(
    project: Path,
    run_id: str,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    store = FileTaskStore(project)
    artifacts = FileArtifactStore(store)
    record = next(
        item
        for item in artifacts.list("1001", run_id)
        if item.relative_path == "artifacts/quality-metadata.json"
    )
    path = store.event_journal_path(run_id).parent / record.relative_path
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    mutate(value)
    content = json.dumps(value, sort_keys=True).encode()
    path.write_bytes(content)
    record_path = (
        store.event_journal_path(run_id).parent
        / "artifacts/records"
        / f"{record.artifact_id}.json"
    )
    record_value = json.loads(read_metadata(record_path))
    record_value["sha256"] = hashlib.sha256(content).hexdigest()
    write_metadata(record_path, json.dumps(record_value))


def test_sdd_harness_blocks_coherent_fingerprint_and_upstream_ref_mismatch(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    service = QualityArtifactService(project)
    local = service.generate(
        "1001", prd_text="Build beta capability.", model_mode="fake"
    )
    _replace_registered_metadata(
        project,
        local.run_id,
        lambda value: value.update({"input_fingerprint": "0" * 64}),
    )
    fingerprint = SddHarness(project, "1001", local.run_id, profile_revision=1).verify()
    assert fingerprint.status is HarnessStatus.FAILED
    assert _check(fingerprint, "SDD-FINGERPRINT-001") is HarnessStatus.FAILED

    source = service.generate(
        "1001",
        prd_text="Build alpha capability.",
        model_mode="fake",
        request_id="ref-source",
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
        request_id="ref-downstream",
    )

    def corrupt_ref(value: dict[str, object]) -> None:
        refs = value["upstream_artifact_refs"]
        assert isinstance(refs, list)
        source_ref = next(
            ref
            for ref in refs
            if isinstance(ref, dict) and ref.get("run_id") == source.run_id
        )
        assert isinstance(source_ref, dict)
        source_ref["sha256"] = "0" * 64

    _replace_registered_metadata(project, downstream.run_id, corrupt_ref)
    mismatch = SddHarness(
        project, "1001", downstream.run_id, profile_revision=1
    ).verify()
    assert mismatch.status is HarnessStatus.FAILED
    assert _check(mismatch, "SDD-UPSTREAM-001") is HarnessStatus.FAILED

@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        ("SRS Artifact: {SRS_ARTIFACT}", "SRS Artifact: forged-srs"),
        ("SDD Artifact: {SDD_ARTIFACT}", "SDD Artifact: forged-sdd"),
        ("SRS Artifact: {SRS_ARTIFACT}", "SRS Artifact:"),
        ("SDD Artifact: {SDD_ARTIFACT}", "SDD Artifact:"),
    ),
)
def test_srs_harness_blocks_forged_or_missing_test_plan_references(
    tmp_path: Path, original: str, replacement: str
) -> None:
    project = _project(tmp_path)
    prd = f"Test plan reference {replacement}."
    srs, sdd, plan = _valid_documents()
    fixtures = {
        hashlib.sha256(prd.encode()).hexdigest(): _fixture(
            srs=srs,
            sdd=sdd,
            test_plan=plan.replace(original, replacement),
        )
    }
    result = QualityArtifactService(project, fixture_responses=fixtures).generate(
        "1001", prd_text=prd, model_mode="fake"
    )
    report = SrsHarness(project, "1001", result.run_id).verify()
    assert report.status is HarnessStatus.FAILED
    assert _check(report, "TEST-PLAN-REFERENCES-001") is HarnessStatus.FAILED


@pytest.mark.parametrize("old_path", ("artifacts/srs.md", "artifacts/prd.md"))
def test_srs_harness_blocks_old_or_wrong_type_test_plan_reference(
    tmp_path: Path, old_path: str
) -> None:
    project = _project(tmp_path)
    old = QualityArtifactService(project).generate(
        "1001", prd_text="Build alpha capability.", model_mode="fake"
    )
    old_reference = next(
        ref["artifact_id"]
        for ref in old.artifact_refs
        if ref["relative_path"] == old_path
    )
    prd = "Old test plan reference."
    srs, sdd, plan = _valid_documents()
    fixtures = {
        hashlib.sha256(prd.encode()).hexdigest(): _fixture(
            srs=srs,
            sdd=sdd,
            test_plan=plan.replace("{SRS_ARTIFACT}", old_reference),
        )
    }
    result = QualityArtifactService(project, fixture_responses=fixtures).generate(
        "1001", prd_text=prd, model_mode="fake"
    )
    report = SrsHarness(project, "1001", result.run_id).verify()
    assert report.status is HarnessStatus.FAILED
    assert _check(report, "TEST-PLAN-REFERENCES-001") is HarnessStatus.FAILED

@pytest.mark.parametrize("relative_path", ("artifacts/srs.md", "artifacts/sdd.md"))
def test_srs_harness_blocks_test_plan_when_referenced_artifact_is_stale(
    tmp_path: Path, relative_path: str
) -> None:
    project = _project(tmp_path)
    result = QualityArtifactService(project).generate(
        "1001", prd_text="Build beta capability.", model_mode="fake"
    )
    path = project / ".yipilot/tasks/1001/runs" / result.run_id / relative_path
    path.write_text("tampered", encoding="utf-8")
    report = SrsHarness(project, "1001", result.run_id).verify()
    assert report.status is HarnessStatus.FAILED
    assert _check(report, "TEST-PLAN-REFERENCES-001") is HarnessStatus.FAILED


def test_quality_generation_rejects_non_prd_artifacts_without_side_effects(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    service = QualityArtifactService(project)
    source = service.generate(
        "1001", prd_text="Build beta capability.", model_mode="fake"
    )
    store = FileTaskStore(project)
    artifacts = FileArtifactStore(store)
    extras = [
        artifacts.write_bytes(
            "1001",
            source.run_id,
            producer="test",
            relative_path=relative_path,
            content=b"not a PRD",
            input_fingerprint="test",
            operation_id=source.operation_id,
        )
        for relative_path in (
            "artifacts/notes.md",
            "artifacts/pytest-stdout.txt",
            "artifacts/pytest-stderr.txt",
        )
    ]
    invalid_ids = [
        ref["artifact_id"]
        for ref in source.artifact_refs
        if ref["relative_path"] != "artifacts/prd.md"
    ] + [record.artifact_id for record in extras]
    before_runs = [run.run_id for run in store.list_runs("1001")]
    for artifact_id in invalid_ids:
        with pytest.raises(DomainError) as error:
            service.generate(
                "1001",
                source_run_id=source.run_id,
                prd_artifact_id=artifact_id,
                model_mode="replay",
            )
        assert error.value.code == "PRD_ARTIFACT_INVALID"
        assert [run.run_id for run in store.list_runs("1001")] == before_runs


def test_quality_generation_rejects_stale_missing_or_mismatched_prd_artifact(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    service = QualityArtifactService(project)
    first = service.generate(
        "1001", prd_text="Build alpha capability.", model_mode="fake"
    )
    second = service.generate(
        "1001", prd_text="Build beta capability.", model_mode="fake"
    )
    first_prd = next(
        ref["artifact_id"]
        for ref in first.artifact_refs
        if ref["relative_path"] == "artifacts/prd.md"
    )
    second_prd = next(
        ref["artifact_id"]
        for ref in second.artifact_refs
        if ref["relative_path"] == "artifacts/prd.md"
    )
    store = FileTaskStore(project)
    before_runs = [run.run_id for run in store.list_runs("1001")]
    for source_run_id, artifact_id in ((first.run_id, second_prd),):
        with pytest.raises(DomainError) as error:
            service.generate(
                "1001",
                source_run_id=source_run_id,
                prd_artifact_id=artifact_id,
                model_mode="replay",
            )
        assert error.value.code == "PRD_ARTIFACT_INVALID"
    first_path = project / ".yipilot/tasks/1001/runs" / first.run_id / "artifacts/prd.md"
    first_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(DomainError) as error:
        service.generate(
            "1001",
            source_run_id=first.run_id,
            prd_artifact_id=first_prd,
            model_mode="replay",
        )
    assert error.value.code == "PRD_ARTIFACT_INVALID"
    second_path = project / ".yipilot/tasks/1001/runs" / second.run_id / "artifacts/prd.md"
    second_path.unlink()
    with pytest.raises(DomainError) as error:
        service.generate(
            "1001",
            source_run_id=second.run_id,
            prd_artifact_id=second_prd,
            model_mode="replay",
        )
    assert error.value.code == "PRD_ARTIFACT_INVALID"
    assert [run.run_id for run in store.list_runs("1001")] == before_runs
