"""Small use-cases for user-local provider setup with an explicit live probe."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from yipilot.domain.errors import DomainError
from yipilot.domain.model import ModelPort
from yipilot.domain.provider import ProviderProfile
from yipilot.infrastructure.openai_compatible import (
    OpenAICompatibleModelPort,
    provider_probe_request,
)
from yipilot.infrastructure.provider_store import FileProviderStore
from yipilot.infrastructure.secret_store import SecretStore


@dataclass(frozen=True)
class ProviderTestResult:
    profile: ProviderProfile
    status: Literal["local_validation_passed", "request_passed"]
    summary: str

    def public_value(self) -> dict[str, object]:
        return {
            "model": self.profile.model,
            "name": self.profile.name,
            "secret": "configured",
            "status": self.status,
            "summary": self.summary,
        }


class ProviderService:
    def __init__(
        self,
        settings_dir: Path | None = None,
        *,
        secrets: SecretStore | None = None,
        port_factory: Callable[[ProviderProfile, SecretStore], ModelPort] | None = None,
    ) -> None:
        self._store = FileProviderStore(settings_dir)
        self._secrets = secrets or SecretStore()
        self._port_factory = port_factory or OpenAICompatibleModelPort

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

    def test(self, name: str | None = None, *, request: bool = False) -> ProviderTestResult:
        settings = self._store.load()
        selected = name or settings.default_profile
        if selected is None:
            raise DomainError("PROVIDER_NOT_FOUND", "no provider profile is selected")
        profile = self._store.get(selected)
        if profile.api_key_ref is None:
            raise DomainError("SECRET_REF_MISSING", "provider profile has no secret reference")
        self._secrets.read(profile.api_key_ref)
        if not request:
            return ProviderTestResult(
                profile, "local_validation_passed", "local profile and secret validation passed"
            )
        port = self._port_factory(profile, self._secrets)
        try:
            port.stream(provider_probe_request(profile))
        finally:
            close = getattr(port, "close", None)
            if callable(close):
                close()
        return ProviderTestResult(profile, "request_passed", "minimal provider probe completed")
