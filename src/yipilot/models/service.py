"""Small use-cases for user-local provider setup with an explicit live probe."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import ValidationError

from yipilot.domain.errors import DomainError
from yipilot.domain.model import ModelPort
from yipilot.domain.provider import (
    ModelEntry,
    ProviderConnection,
    ProviderProfile,
    ResolvedModel,
)
from yipilot.models.secrets import SecretStore
from yipilot.models.selection import ModelSelectionService
from yipilot.models.store import FileProviderStore


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
        port_factory: Callable[[ResolvedModel, SecretStore, str], ModelPort] | None = None,
    ) -> None:
        self._store = FileProviderStore(settings_dir)
        self._secrets = secrets or SecretStore(settings_dir=settings_dir)
        self._selection = ModelSelectionService(
            settings_dir,
            secrets=self._secrets,
            port_factory=port_factory,
        )

    @property
    def path(self) -> Path:
        return self._store.path

    def add(self, **values: object) -> ProviderProfile:
        try:
            profile = ProviderProfile.model_validate(values)
        except ValidationError as error:
            raise DomainError(
                "PROVIDER_PROFILE_INVALID", "provider profile is invalid"
            ) from error
        self._selection.add(
            ProviderConnection(connection_id=profile.name, type=profile.provider_type, base_url=profile.base_url, capabilities=profile.capabilities, enabled=profile.enabled, timeout_seconds=profile.timeout_seconds, credential_ref=profile.api_key_ref, revision=profile.revision),
            ModelEntry(connection_id=profile.name, model_id=profile.model, display_name=profile.model, output_limit=profile.output_budget, capabilities=profile.capabilities, enabled=profile.enabled),
        )
        return self._store.get(profile.name)

    def list(self) -> dict[str, object]:
        settings = self._store.load()
        return {
            "default_profile": (
                settings.default_model_selector.split("/", 1)[0]
                if settings.default_model_selector else None
            ),
            "profiles": [profile.public_value() for profile in self._store.list()],
        }

    def show(self, name: str) -> ProviderProfile:
        return self._store.get(name)

    def use(self, name: str) -> ProviderProfile:
        return self._store.use(name)

    def set_key(self, name: str, value: str) -> ProviderProfile:
        connection = self._store.get_connection(name)
        reference = self._secrets.set_file(name, value)
        self._store.add_connection(connection.model_copy(update={"credential_ref": reference, "revision": connection.revision + 1}))
        return self._store.get(name)

    @property
    def model_selection(self) -> ModelSelectionService:
        return self._selection

    def selector_for_profile(self, name: str) -> str:
        profile = self.show(name)
        return f"{profile.name}/{profile.model}"

    def test(
        self, name: str | None = None, *, request: bool = False
    ) -> ProviderTestResult:
        settings = self._store.load()
        selected = name or (
            settings.default_model_selector.split("/", 1)[0]
            if settings.default_model_selector else None
        )
        if selected is None:
            raise DomainError("PROVIDER_NOT_FOUND", "no provider profile is selected")
        profile = self._store.get(selected)
        result = self._selection.test(self.selector_for_profile(selected), request=request)
        return ProviderTestResult(
            profile,
            cast(Literal["local_validation_passed", "request_passed"], result["status"]),
            "minimal provider probe completed" if request else "local profile and secret validation passed",
        )
