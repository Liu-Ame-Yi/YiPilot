"""Atomic schema-2 model settings, with lossless schema-1 profile migration."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from platformdirs import user_config_dir
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from yipilot.domain.errors import DomainError
from yipilot.domain.provider import ModelEntry, ProviderConnection, ProviderProfile
from yipilot.infrastructure.filesystem import atomic_write_bytes, atomic_write_json


class _SchemaOneSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    default_profile: str | None = None
    profiles: tuple[ProviderProfile, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def validate_profiles(self) -> "_SchemaOneSettings":
        names = [profile.name for profile in self.profiles]
        if len(names) != len(set(names)) or (self.default_profile and self.default_profile not in names):
            raise ValueError("provider names and default must be valid")
        return self


class ProviderSettings(BaseModel):
    """User-local public metadata; credentials are references only."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[2] = 2
    default_model_selector: str | None = None
    connections: tuple[ProviderConnection, ...] = Field(default_factory=tuple)
    models: tuple[ModelEntry, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def validate_references(self) -> "ProviderSettings":
        ids = [connection.connection_id for connection in self.connections]
        model_keys = [(model.connection_id, model.model_id) for model in self.models]
        if len(ids) != len(set(ids)) or len(model_keys) != len(set(model_keys)):
            raise ValueError("connections and models must be unique")
        if any(model.connection_id not in ids for model in self.models):
            raise ValueError("each model must have a connection")
        if self.default_model_selector is not None:
            from yipilot.domain.provider import ModelSelector

            selector = ModelSelector.parse(self.default_model_selector)
            exact = next(
                (model for model in self.models if (model.connection_id, model.model_id) == (selector.connection_id, selector.model_id)),
                None,
            )
            model_id, separator, variant = selector.model_id.rpartition(":")
            variant_model = next(
                (model for model in self.models if (model.connection_id, model.model_id) == (selector.connection_id, model_id)),
                None,
            ) if separator else None
            if exact is None and (
                variant_model is None
                or not variant
                or variant not in {item.name for item in variant_model.variants}
            ):
                raise ValueError("default model must exist")
        return self


def settings_directory(override: Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    configured = os.environ.get("YIPILOT_SETTINGS_DIR")
    return Path(configured) if configured else Path(user_config_dir("yipilot", "Yipilot"))


class FileProviderStore:
    """Store schema-2 connections/models outside project-local runtime state."""

    def __init__(self, settings_dir: Path | None = None) -> None:
        self._path = settings_directory(settings_dir) / "providers.json"

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> ProviderSettings:
        if not self._path.exists():
            return ProviderSettings()
        try:
            raw = self._path.read_bytes()
            value = json.loads(raw)
            if value.get("schema_version") == 1:
                return self._migrate_schema_one(raw)
            return ProviderSettings.model_validate(value)
        except (OSError, ValidationError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise DomainError("PROVIDER_CONFIG_INVALID", "provider settings file is invalid") from error

    def _migrate_schema_one(self, raw: bytes) -> ProviderSettings:
        """Validate all old data before preserving it and atomically replacing it."""
        try:
            legacy = _SchemaOneSettings.model_validate_json(raw)
            connections = tuple(ProviderConnection(
                connection_id=item.name, type=item.provider_type, base_url=item.base_url,
                capabilities=item.capabilities, enabled=item.enabled,
                timeout_seconds=item.timeout_seconds, credential_ref=item.api_key_ref,
                revision=item.revision,
            ) for item in legacy.profiles)
            models = tuple(ModelEntry(
                connection_id=item.name, model_id=item.model, display_name=item.model,
                output_limit=item.output_budget, capabilities=item.capabilities,
                enabled=item.enabled,
            ) for item in legacy.profiles)
            default = next((f"{item.name}/{item.model}" for item in legacy.profiles if item.name == legacy.default_profile), None)
            migrated = ProviderSettings(connections=connections, models=models, default_model_selector=default)
        except (ValidationError, ValueError) as error:
            raise DomainError("PROVIDER_CONFIG_INVALID", "provider settings file is invalid") from error
        backup = self._path.with_name(f"{self._path.name}.schema-1-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}.bak")
        try:
            atomic_write_bytes(backup, raw)
            atomic_write_json(self._path, migrated.model_dump(mode="json"), ProviderSettings.model_validate)
        except (OSError, DomainError) as error:
            raise DomainError("PROVIDER_MIGRATION_FAILED", "provider settings migration failed; original was preserved") from error
        return migrated

    def save(self, settings: ProviderSettings) -> ProviderSettings:
        atomic_write_json(self._path, settings.model_dump(mode="json"), ProviderSettings.model_validate)
        return self.load()

    def get_connection(self, connection_id: str) -> ProviderConnection:
        for connection in self.load().connections:
            if connection.connection_id == connection_id:
                return connection
        raise DomainError("PROVIDER_NOT_FOUND", "provider connection was not found")

    def get_model(self, connection_id: str, model_id: str) -> ModelEntry:
        for model in self.load().models:
            if (model.connection_id, model.model_id) == (connection_id, model_id):
                return model
        raise DomainError("MODEL_NOT_FOUND", "model was not found")

    def add_connection(self, connection: ProviderConnection) -> ProviderConnection:
        settings = self.load()
        current = next((item for item in settings.connections if item.connection_id == connection.connection_id), None)
        if current is not None and connection.revision <= current.revision:
            raise DomainError("PROVIDER_REVISION_INVALID", "connection revision must increase when it changes")
        values = [item for item in settings.connections if item.connection_id != connection.connection_id] + [connection]
        self.save(settings.model_copy(update={"connections": tuple(sorted(values, key=lambda item: item.connection_id))}))
        return self.get_connection(connection.connection_id)

    def add_model(self, model: ModelEntry) -> ModelEntry:
        settings = self.load()
        if model.connection_id not in {item.connection_id for item in settings.connections}:
            raise DomainError("PROVIDER_NOT_FOUND", "provider connection was not found")
        previous = _find_model(settings.models, model.connection_id, model.model_id)
        values = [item for item in settings.models if (item.connection_id, item.model_id) != (model.connection_id, model.model_id)] + [model]
        updates: dict[str, object] = {"models": tuple(sorted(values, key=lambda item: (item.connection_id, item.model_id)))}
        if previous != model and any(item.connection_id == model.connection_id for item in settings.models):
            updates["connections"] = tuple(
                item.model_copy(update={"revision": item.revision + 1}) if item.connection_id == model.connection_id else item
                for item in settings.connections
            )
        self.save(settings.model_copy(update=updates))
        return self.get_model(model.connection_id, model.model_id)

    def set_default(self, selector: str | None) -> ProviderSettings:
        return self.save(self.load().model_copy(update={"default_model_selector": selector}))

    # M5 compatibility surface. A legacy profile can describe only one model.
    def add(self, profile: ProviderProfile) -> ProviderProfile:
        self.add_connection(ProviderConnection(connection_id=profile.name, type=profile.provider_type,
            base_url=profile.base_url, capabilities=profile.capabilities, enabled=profile.enabled,
            timeout_seconds=profile.timeout_seconds, credential_ref=profile.api_key_ref, revision=profile.revision))
        self.add_model(ModelEntry(connection_id=profile.name, model_id=profile.model,
            display_name=profile.model, output_limit=profile.output_budget,
            capabilities=profile.capabilities, enabled=profile.enabled))
        return self.get(profile.name)

    def get(self, name: str) -> ProviderProfile:
        connection = self.get_connection(name)
        models = [item for item in self.load().models if item.connection_id == name]
        if len(models) != 1:
            raise DomainError("PROVIDER_PROFILE_AMBIGUOUS", "legacy profile does not identify one model")
        model = models[0]
        return ProviderProfile(name=name, provider_type=connection.type, base_url=connection.base_url,
            model=model.model_id, timeout_seconds=connection.timeout_seconds,
            output_budget=model.output_limit, capabilities=connection.capabilities,
            enabled=connection.enabled, revision=connection.revision,
            api_key_ref=connection.credential_ref)

    def list(self) -> list[ProviderProfile]:
        return [self.get(item.connection_id) for item in self.load().connections
                if len([model for model in self.load().models if model.connection_id == item.connection_id]) == 1]

    def use(self, name: str) -> ProviderProfile:
        profile = self.get(name)
        if not profile.enabled:
            raise DomainError("PROVIDER_DISABLED", "provider profile is disabled")
        return profile

    def replace(self, profile: ProviderProfile) -> ProviderProfile:
        return self.add(profile)


def _find_model(models: tuple[ModelEntry, ...], connection_id: str, model_id: str) -> ModelEntry | None:
    return next((item for item in models if (item.connection_id, item.model_id) == (connection_id, model_id)), None)
