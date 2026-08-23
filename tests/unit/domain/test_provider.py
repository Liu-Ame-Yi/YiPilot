import pytest
from pydantic import ValidationError

from yipilot.domain.provider import ProviderProfile


@pytest.mark.parametrize(
    "change",
    [
        {"api_key_ref": "sk-secret-value"},
        {"api_key_ref": "env:NOT-VALID!"},
        {"base_url": "https://user:password@example.test/v1"},
        {"base_url": "file:///tmp/provider"},
        {"timeout_seconds": 0},
        {"output_budget": 128_001},
    ],
)
def test_provider_profile_rejects_unsafe_values(change: dict[str, object]) -> None:
    values: dict[str, object] = {
        "name": "local",
        "base_url": "https://example.test/v1",
        "model": "test-model",
        "timeout_seconds": 30,
        "output_budget": 1024,
        "revision": 1,
    }
    values.update(change)
    with pytest.raises(ValidationError):
        ProviderProfile.model_validate(values)


def test_provider_revision_is_a_visible_versioned_field() -> None:
    base = ProviderProfile(
        name="local", base_url="https://example.test/v1", model="test-model",
        timeout_seconds=30, output_budget=1024, revision=1,
    )
    revised = base.model_copy(update={"revision": 2})
    assert revised.revision == 2
    assert revised.model_dump(mode="json") != base.model_dump(mode="json")
