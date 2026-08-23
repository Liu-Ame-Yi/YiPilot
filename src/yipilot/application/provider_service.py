"""Small use-cases for user-local provider setup without network access."""

from pathlib import Path

from pydantic import ValidationError

from yipilot.domain.errors import DomainError
from yipilot.domain.provider import ProviderProfile
from yipilot.infrastructure.provider_store import FileProviderStore
from yipilot.infrastructure.secret_store import SecretStore


class ProviderService:
    def __init__(
        self,
        settings_dir: Path | None = None,
        *,
        secrets: SecretStore | None = None,
    ) -> None:
        self._store = FileProviderStore(settings_dir)
        self._secrets = secrets or SecretStore()

    @property
    def path(self) -> Path:
        return self._store.path

    def add(self, **values: object) -> ProviderProfile:
        try:
            profile = ProviderProfile.model_validate(values)
        except ValidationError as error:
            raise DomainError("PROVIDER_PROFILE_INVALID", "provider profile is invalid") from error
        return self._store.add(profile)

    def list(self) -> dict[str, object]:
        settings = self._store.load()
        return {
            "default_profile": settings.default_profile,
            "profiles": [profile.public_value() for profile in settings.profiles],
        }

    def show(self, name: str) -> ProviderProfile:
        return self._store.get(name)

    def use(self, name: str) -> ProviderProfile:
        return self._store.use(name)

    def set_key(self, name: str, value: str) -> ProviderProfile:
        profile = self._store.get(name)
        reference = self._secrets.set_keyring(profile.name, value)
        return self._store.replace(
            profile.model_copy(
                update={"api_key_ref": reference, "revision": profile.revision + 1}
            )
        )

    def test(self, name: str | None = None, *, request: bool = False) -> ProviderProfile:
        if request:
            raise DomainError(
                "LIVE_PROVIDER_TEST_NOT_IMPLEMENTED",
                "live provider requests are unavailable before Task 16",
            )
        settings = self._store.load()
        selected = name or settings.default_profile
        if selected is None:
            raise DomainError("PROVIDER_NOT_FOUND", "no provider profile is selected")
        profile = self._store.get(selected)
        if profile.api_key_ref is None:
            raise DomainError("SECRET_REF_MISSING", "provider profile has no secret reference")
        self._secrets.read(profile.api_key_ref)
        return profile
