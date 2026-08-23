"""Atomic user-local storage for provider profile metadata only."""

import os
from pathlib import Path
from typing import Literal

from platformdirs import user_config_dir
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from yipilot.domain.errors import DomainError
from yipilot.domain.provider import ProviderProfile
from yipilot.infrastructure.filesystem import atomic_write_json


class ProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    default_profile: str | None = None
    profiles: tuple[ProviderProfile, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def validate_profiles(self) -> "ProviderSettings":
        names = [profile.name for profile in self.profiles]
        if len(names) != len(set(names)):
            raise ValueError("provider names must be unique")
        if self.default_profile is not None and self.default_profile not in names:
            raise ValueError("default provider must exist")
        return self


def settings_directory(override: Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    configured = os.environ.get("YIPILOT_SETTINGS_DIR")
    return Path(configured) if configured else Path(user_config_dir("yipilot", "Yipilot"))


class FileProviderStore:
    """Store profiles outside project-local `.yipilot` task state."""

    def __init__(self, settings_dir: Path | None = None) -> None:
        self._path = settings_directory(settings_dir) / "providers.json"

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> ProviderSettings:
        if not self._path.exists():
            return ProviderSettings()
        try:
            return ProviderSettings.model_validate_json(self._path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as error:
            raise DomainError(
                "PROVIDER_CONFIG_INVALID", "provider settings file is invalid"
            ) from error

    def save(self, settings: ProviderSettings) -> ProviderSettings:
        atomic_write_json(
            self._path, settings.model_dump(mode="json"), ProviderSettings.model_validate
        )
        return self.load()

    def add(self, profile: ProviderProfile) -> ProviderProfile:
        settings = self.load()
        existing = next((item for item in settings.profiles if item.name == profile.name), None)
        if existing is not None and profile.revision <= existing.revision:
            raise DomainError(
                "PROVIDER_REVISION_INVALID", "profile revision must increase when it changes"
            )
        profiles = [item for item in settings.profiles if item.name != profile.name]
        profiles.append(profile)
        self.save(settings.model_copy(update={"profiles": tuple(sorted(profiles, key=lambda item: item.name))}))
        return self.get(profile.name)

    def get(self, name: str) -> ProviderProfile:
        for profile in self.load().profiles:
            if profile.name == name:
                return profile
        raise DomainError("PROVIDER_NOT_FOUND", "provider profile was not found")

    def list(self) -> list[ProviderProfile]:
        return list(self.load().profiles)

    def use(self, name: str) -> ProviderProfile:
        profile = self.get(name)
        if not profile.enabled:
            raise DomainError("PROVIDER_DISABLED", "provider profile is disabled")
        self.save(self.load().model_copy(update={"default_profile": name}))
        return profile

    def replace(self, profile: ProviderProfile) -> ProviderProfile:
        return self.add(profile)
