import json
from datetime import UTC, datetime

import pytest

from yipilot.domain.errors import DomainError
from yipilot.domain.provider import ProviderProfile
from yipilot.harness.models import HarnessCheck, HarnessReport, HarnessStatus
from yipilot.harness.report_store import HarnessReportStore
from yipilot.models.secrets import FakeSecretBackend, SecretStore
from yipilot.models.service import ProviderService
from yipilot.models.store import FileProviderStore
from yipilot.persistence.artifact_store import FileArtifactStore
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.task_store import FileTaskStore
from yipilot.security.redaction import SecretRedactor


def _profile(revision: int = 1) -> ProviderProfile:
    return ProviderProfile(
        name="local", base_url="https://example.test/v1", model="test-model",
        timeout_seconds=30, output_budget=1024, revision=revision, api_key_ref="env:YIPILOT_TEST_KEY",
    )


def test_provider_store_is_user_local_and_requires_new_revision(tmp_path) -> None:
    store = FileProviderStore(tmp_path)
    store.add(_profile())
    with pytest.raises(DomainError, match="PROVIDER_REVISION_INVALID"):
        store.add(_profile())
    store.add(_profile(2))
    persisted = json.loads(store.path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 2
    assert persisted["connections"][0]["revision"] == 2
    assert "YIPILOT_TEST_KEY" not in str(tmp_path / ".yipilot")


def test_fake_keyring_and_fake_env_read_without_system_credentials(tmp_path) -> None:
    backend = FakeSecretBackend()
    secrets = SecretStore(backend, {"YIPILOT_TEST_KEY": "unit-secret"})
    assert secrets.read("env:YIPILOT_TEST_KEY") == "unit-secret"
    assert secrets.set_keyring("local", "unit-secret") == "keyring:local"
    assert secrets.read("keyring:local") == "unit-secret"
    service = ProviderService(tmp_path, secrets=secrets)
    service.add(**_profile().model_dump())
    assert service.test("local").profile.name == "local"


def test_auth_file_round_trip_is_separate_from_provider_settings(tmp_path) -> None:
    secrets = SecretStore(settings_dir=tmp_path)
    assert secrets.set_file("deepseek-1", "unit-file-secret") == "file:deepseek-1"
    assert secrets.read("file:deepseek-1") == "unit-file-secret"
    auth = json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))
    assert auth == {"keys": {"deepseek-1": "unit-file-secret"}, "schema_version": 1}


def test_redactor_hides_known_values_headers_and_url_credentials() -> None:
    value = SecretRedactor(["unit-secret"]).redact_text(
        "Authorization: Bearer unit-secret https://user:password@example.test/v1"
    )
    assert "unit-secret" not in value
    assert "password" not in value
    assert "Authorization: [REDACTED]" in value


def test_event_artifact_and_harness_persistence_redact_credentials(tmp_path) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    operation = store.create_operation("1001", "test", "provider-security", {})
    run = store.create_run("1001", operation.operation_id, "test")
    text = "Authorization: Bearer provider-test-secret https://user:password@example.test"
    journal = JsonlEventJournal(store.event_journal_path(run.run_id))
    journal.append("stage.progress", {"x-api-key": "provider-test-secret", "message": text})
    artifact = FileArtifactStore(store).write_bytes(
        run.task_id, run.run_id, producer="test", relative_path="artifacts/output.txt",
        content=text.encode(), input_fingerprint="input", operation_id=run.operation_id,
    )
    now = datetime.now(UTC)
    report = HarnessReport(
        report_id="provider-security", task_id=run.task_id, operation_id=run.operation_id,
        run_id=run.run_id, stage=run.stage, harness_name="test", harness_version="1",
        started_at=now, ended_at=now, input_fingerprint="input", inputs=[],
        status=HarnessStatus.PASSED,
        checks=[HarnessCheck(id="TEST", status=HarnessStatus.PASSED, message=text)], summary=text,
    )
    HarnessReportStore(store).save(report)
    persisted = "".join(path.read_text(encoding="utf-8") for path in [
        store.event_journal_path(run.run_id),
        store.event_journal_path(run.run_id).parent / artifact.relative_path,
        HarnessReportStore(store).report_path(run.run_id),
    ])
    assert "provider-test-secret" not in persisted
    assert "password" not in persisted
