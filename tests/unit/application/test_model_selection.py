import json

import pytest

from yipilot.application.model_selection_service import ModelSelectionService
from yipilot.domain.errors import DomainError
from yipilot.domain.provider import ModelEntry, ModelSelector, ProviderConnection
from yipilot.infrastructure.secret_store import SecretStore


def _service(tmp_path):
    service = ModelSelectionService(tmp_path, secrets=SecretStore(environ={"KEY": "secret"}))
    service.add(ProviderConnection(connection_id="local", base_url="https://example.test/v1", timeout_seconds=10, credential_ref="env:KEY", revision=2), ModelEntry(connection_id="local", model_id="coder", display_name="Coder", output_limit=100))
    return service


def test_schema_one_migrates_to_schema_two_with_backup(tmp_path) -> None:
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"schema_version": 1, "default_profile": "local", "profiles": [{"schema_version": 1, "name": "local", "provider_type": "openai_compatible", "base_url": "https://example.test/v1", "model": "coder", "timeout_seconds": 10, "output_budget": 100, "capabilities": ["chat"], "enabled": True, "revision": 2, "api_key_ref": "env:KEY"}]}), encoding="utf-8")
    assert ModelSelectionService(tmp_path, secrets=SecretStore(environ={"KEY": "secret"})).resolve().selector == "local/coder"
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2
    assert list(tmp_path.glob("providers.json.schema-1-*.bak"))


def test_exact_priority_and_no_default(tmp_path) -> None:
    with pytest.raises(DomainError, match="MODEL_SELECTION_REQUIRED"):
        ModelSelectionService(tmp_path).resolve()
    service = _service(tmp_path)
    service.set_default("local/coder")
    assert service.resolve(session_selector="local/coder").source == "session"
    assert service.resolve(explicit_selector="local/coder", session_selector="wrong/nope").source == "explicit"
    with pytest.raises(ValueError):
        ModelSelector.parse("coder")


def test_model_id_with_slash_or_colon_stays_exact_and_model_edit_bumps_revision(tmp_path) -> None:
    service = ModelSelectionService(tmp_path, secrets=SecretStore(environ={"KEY": "secret"}))
    connection = ProviderConnection(connection_id="local", base_url="https://example.test/v1", timeout_seconds=10, credential_ref="env:KEY", revision=1)
    service.add(connection, ModelEntry(connection_id="local", model_id="org/coder:v1", display_name="Coder", output_limit=100))
    service.set_default("local/org/coder:v1")
    assert service.resolve().model_id == "org/coder:v1"
    service.add(connection, ModelEntry(connection_id="local", model_id="org/coder:v1", display_name="Coder", output_limit=99))
    assert service.resolve().connection_revision == 2


def test_remove_last_model_clears_default_connection_and_file_key(tmp_path) -> None:
    secrets = SecretStore(settings_dir=tmp_path)
    service = ModelSelectionService(tmp_path, secrets=secrets)
    secrets.set_file("local", "unit-secret")
    service.add(ProviderConnection(connection_id="local", base_url="https://example.test/v1", timeout_seconds=10, credential_ref="file:local", revision=1), ModelEntry(connection_id="local", model_id="coder", display_name="Coder", output_limit=100))
    service.set_default("local/coder")
    assert service.remove("local/coder") == {"selector": "local/coder", "connection_removed": True}
    assert service.list_public() == {"default_model_selector": None, "models": []}
    assert not (tmp_path / "auth.json").exists()


def test_request_probe_uses_adapter_probe_when_available(tmp_path) -> None:
    calls: list[str] = []

    class ProbePort:
        def probe(self, request) -> tuple[object, ...]:
            calls.append("probe")
            return ()

        def stream(self, request) -> tuple[object, ...]:
            calls.append("stream")
            return ()

        def close(self) -> None:
            pass

    service = ModelSelectionService(
        tmp_path,
        secrets=SecretStore(environ={"KEY": "secret"}),
        port_factory=lambda *_: ProbePort(),
    )
    service.add(ProviderConnection(connection_id="local", base_url="https://example.test/v1", timeout_seconds=10, credential_ref="env:KEY", revision=1), ModelEntry(connection_id="local", model_id="coder", display_name="Coder", output_limit=100))
    assert service.test("local/coder", request=True)["status"] == "request_passed"
    assert calls == ["probe"]
