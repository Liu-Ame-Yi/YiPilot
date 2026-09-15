"""Opt-in real model acceptance, using a disposable repository and public entry."""

import os
import subprocess

import pytest

from yipilot.app import ProjectApplication
from yipilot.domain.provider import ModelVariant
from yipilot.models.secrets import SecretStore
from yipilot.models.selection import ModelSelectionService
from yipilot.models.store import FileProviderStore


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("YIPILOT_LIVE_SPECIFICATION") != "1", reason="explicit live opt-in required")
def test_real_specification(tmp_path):
    source = tmp_path / "source"
    project = tmp_path / "project"
    source.mkdir()
    project.mkdir()
    (source / "value.py").write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    (source / "test_value.py").write_text("from value import value\n\ndef test_value():\n    assert value() == 1\n", encoding="utf-8")
    for args in (["init"], ["add", "value.py", "test_value.py"],
                 ["-c", "user.name=Yipilot Acceptance", "-c", "user.email=acceptance@yipilot.invalid", "commit", "-m", "sample baseline"]):
        subprocess.run(["git", *args], cwd=source, capture_output=True, check=True)
    models = ModelSelectionService()
    if effort := os.environ.get("YIPILOT_SPEC_REASONING_EFFORT"):
        # Explicit acceptance setting: copy public metadata only; never copy credentials.
        source_settings = FileProviderStore()
        settings = source_settings.load()
        entries = tuple(entry.model_copy(update={"variants": (*entry.variants,
            ModelVariant(name="spec-acceptance", request_options={"reasoning_effort": effort}))})
                        for entry in settings.models)
        target_settings = FileProviderStore(tmp_path / "settings")
        target_settings.save(settings.model_copy(update={"models": entries,
            "default_model_selector": f"{settings.default_model_selector}:spec-acceptance"}))
        models = ModelSelectionService(target_settings.path.parent,
                                       secrets=SecretStore(settings_dir=source_settings.path.parent))
    application = ProjectApplication(project, models=models)
    application.tasks.initialize_project()
    application.tasks.initialize_task("1")
    application.repos.add_and_sync("1", str(source), "sample")
    state = application.start_specification("1", alias="sample", context_files=["value.py", "test_value.py"],
        instruction=("Change the existing value() function to return integer 2 instead of 1. "
                     "Keep its name, module, zero-argument interface and int annotation. "
                     "Update the existing test to assert 2. No new dependencies, persistence, CLI, or other behavior. "
                     "Acceptance: importing value and calling value() returns integer 2 and the updated test passes. "
                     "Produce concise English SRS and SDD only; do not implement code or run tests on the sample."),
        request_id="live-acceptance")
    (tmp_path / "specification-result.json").write_text(state.model_dump_json(indent=2), encoding="utf-8")
    print(f"\nREAL_SPECIFICATION_EVIDENCE={tmp_path}")
    print(f"status={state.status}; reason={state.reason}; revisions={state.revisions}")
    assert state.status == "completed", state.model_dump_json()
    runs_before = len(application.sandbox.store.list_runs("1"))
    restarted = ProjectApplication(project, models=models)
    assert restarted.resume_specification("1") == state
    assert len(restarted.sandbox.store.list_runs("1")) == runs_before
    assert (source / "value.py").read_text() == "def value() -> int:\n    return 1\n"
