import json

import pytest

from yipilot.application.provider_service import ProviderService
from yipilot.domain.errors import DomainError
from yipilot.domain.provider import ProviderProfile
from yipilot.infrastructure.provider_store import FileProviderStore
from yipilot.infrastructure.secret_store import FakeSecretBackend, SecretStore
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
    assert persisted["profiles"][0]["revision"] == 2
    assert "YIPILOT_TEST_KEY" not in str(tmp_path / ".yipilot")


def test_fake_keyring_and_fake_env_read_without_system_credentials(tmp_path) -> None:
    backend = FakeSecretBackend()
    secrets = SecretStore(backend, {"YIPILOT_TEST_KEY": "unit-secret"})
    assert secrets.read("env:YIPILOT_TEST_KEY") == "unit-secret"
    assert secrets.set_keyring("local", "unit-secret") == "keyring:local"
    assert secrets.read("keyring:local") == "unit-secret"
    service = ProviderService(tmp_path, secrets=secrets)
    service.add(**_profile().model_dump())
    assert service.test().name == "local"


def test_redactor_hides_known_values_headers_and_url_credentials() -> None:
    value = SecretRedactor(["unit-secret"]).redact_text(
        "Authorization: Bearer unit-secret https://user:password@example.test/v1"
    )
    assert "unit-secret" not in value
    assert "password" not in value
    assert "Bearer [REDACTED]" in value
