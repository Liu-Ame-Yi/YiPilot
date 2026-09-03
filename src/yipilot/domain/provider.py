"""Versioned, non-secret provider profile contracts."""

import re
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

MAX_PROVIDER_TIMEOUT_SECONDS = 600.0
MAX_PROVIDER_OUTPUT_BUDGET = 128_000
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_REFERENCE = re.compile(r"^(?:(?P<env>env):(?P<env_name>[A-Za-z_][A-Za-z0-9_]*)|(?P<keyring>keyring):(?P<keyring_name>[A-Za-z0-9][A-Za-z0-9_.-]{0,63})|(?P<file>file):(?P<file_name>[A-Za-z0-9][A-Za-z0-9_.-]{0,63}))$")
ALLOWED_MODEL_REQUEST_OPTIONS = frozenset(
    {"frequency_penalty", "presence_penalty", "reasoning_effort", "seed", "stop", "temperature", "top_p"}
)


def parse_api_key_ref(value: str) -> tuple[str, str]:
    """Validate one safe secret reference and return its kind and name."""
    match = _REFERENCE.fullmatch(value)
    if match is None:
        raise ValueError("api key must be an env:, keyring:, or file: reference")
    if match.group("env") == "env":
        return "env", match.group("env_name")
    if match.group("keyring") == "keyring":
        return "keyring", match.group("keyring_name")
    return "file", match.group("file_name")


class ProviderProfile(BaseModel):
    """One user-local model configuration; it intentionally contains no secret."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=64)
    provider_type: Literal["openai_compatible"] = "openai_compatible"
    base_url: str
    model: str = Field(min_length=1, max_length=128)
    timeout_seconds: float = Field(gt=0, le=MAX_PROVIDER_TIMEOUT_SECONDS, allow_inf_nan=False)
    output_budget: int = Field(gt=0, le=MAX_PROVIDER_OUTPUT_BUDGET)
    capabilities: tuple[str, ...] = ("chat",)
    enabled: bool = True
    revision: int = Field(ge=1)
    api_key_ref: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if _NAME.fullmatch(value) is None:
            raise ValueError("profile name must be a safe identifier")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in value)
        ):
            raise ValueError("base URL must be an http(s) URL without credentials")
        if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
            raise ValueError("HTTP base URLs are limited to loopback hosts")
        return value.rstrip("/")

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if _MODEL.fullmatch(value) is None:
            raise ValueError("model must be a safe model identifier")
        return value

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value) or any(_CAPABILITY.fullmatch(item) is None for item in value):
            raise ValueError("capabilities must be unique safe identifiers")
        return value

    @field_validator("api_key_ref")
    @classmethod
    def validate_api_key_ref(cls, value: str | None) -> str | None:
        if value is not None:
            parse_api_key_ref(value)
        return value

    def public_value(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class ProviderConnection(BaseModel):
    """Non-secret endpoint configuration shared by one or more models."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: str = Field(min_length=1, max_length=64)
    type: Literal["openai_compatible"] = "openai_compatible"
    base_url: str
    capabilities: tuple[str, ...] = ("chat",)
    enabled: bool = True
    timeout_seconds: float = Field(gt=0, le=MAX_PROVIDER_TIMEOUT_SECONDS, allow_inf_nan=False)
    credential_ref: str | None = None
    revision: int = Field(ge=1)

    _name = field_validator("connection_id")(ProviderProfile.validate_name.__func__)  # type: ignore[attr-defined]
    _url = field_validator("base_url")(ProviderProfile.validate_url.__func__)  # type: ignore[attr-defined]
    _capabilities = field_validator("capabilities")(ProviderProfile.validate_capabilities.__func__)  # type: ignore[attr-defined]
    _credential = field_validator("credential_ref")(ProviderProfile.validate_api_key_ref.__func__)  # type: ignore[attr-defined]


class ModelVariant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    output_limit: int | None = Field(default=None, gt=0, le=MAX_PROVIDER_OUTPUT_BUDGET)
    context_limit: int | None = Field(default=None, gt=0)
    request_options: dict[str, str | int | float | bool] = Field(default_factory=dict)
    enabled: bool = True

    _name = field_validator("name")(ProviderProfile.validate_name.__func__)  # type: ignore[attr-defined]

    @field_validator("request_options")
    @classmethod
    def validate_request_options(
        cls, value: dict[str, str | int | float | bool]
    ) -> dict[str, str | int | float | bool]:
        if not set(value) <= ALLOWED_MODEL_REQUEST_OPTIONS:
            raise ValueError("model variant request options are not supported")
        return value


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    context_limit: int | None = Field(default=None, gt=0)
    output_limit: int = Field(gt=0, le=MAX_PROVIDER_OUTPUT_BUDGET)
    capabilities: tuple[str, ...] = ("chat",)
    enabled: bool = True
    variants: tuple[ModelVariant, ...] = ()

    _connection = field_validator("connection_id")(ProviderProfile.validate_name.__func__)  # type: ignore[attr-defined]
    _model = field_validator("model_id")(ProviderProfile.validate_model.__func__)  # type: ignore[attr-defined]
    _capabilities = field_validator("capabilities")(ProviderProfile.validate_capabilities.__func__)  # type: ignore[attr-defined]

    @field_validator("variants")
    @classmethod
    def validate_variants(cls, value: tuple[ModelVariant, ...]) -> tuple[ModelVariant, ...]:
        if len({variant.name for variant in value}) != len(value):
            raise ValueError("model variant names must be unique")
        return value


class ModelSelector(BaseModel):
    """Canonical, exact model identity; display indexes are never identities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: str
    model_id: str
    variant: str | None = None

    _connection = field_validator("connection_id")(ProviderProfile.validate_name.__func__)  # type: ignore[attr-defined]
    _model = field_validator("model_id")(ProviderProfile.validate_model.__func__)  # type: ignore[attr-defined]
    @field_validator("variant")
    @classmethod
    def validate_variant(cls, value: str | None) -> str | None:
        if value is not None:
            ProviderProfile.validate_name(value)
        return value

    @classmethod
    def parse(cls, value: str) -> "ModelSelector":
        if "/" not in value or not value or any(character.isspace() for character in value):
            raise ValueError("model selector must be connection_id/model_id[:variant]")
        connection_id, model_part = value.split("/", 1)
        if not connection_id or not model_part:
            raise ValueError("model selector must be connection_id/model_id[:variant]")
        # A colon is part of a valid model ID.  Only the control plane, which
        # has the configured model list, can safely decide whether it is a variant.
        return cls(connection_id=connection_id, model_id=model_part)

    def __str__(self) -> str:
        return f"{self.connection_id}/{self.model_id}" + (f":{self.variant}" if self.variant else "")


class ResolvedModel(BaseModel):
    """Redacted immutable live-model snapshot, safe to persist with a Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    selector: str
    source: Literal["explicit", "session", "global_default"]
    connection_id: str
    provider_type: Literal["openai_compatible"]
    base_url: str
    connection_revision: int = Field(ge=1)
    model_id: str
    display_name: str
    capabilities: tuple[str, ...]
    context_limit: int | None = Field(default=None, gt=0)
    output_limit: int = Field(gt=0, le=MAX_PROVIDER_OUTPUT_BUDGET)
    timeout_seconds: float = Field(gt=0, le=MAX_PROVIDER_TIMEOUT_SECONDS)
    variant: str | None = None
    request_options: dict[str, str | int | float | bool] = Field(default_factory=dict)
    _credential_ref: str | None = PrivateAttr(default=None)

    def with_credential_reference(self, value: str) -> "ResolvedModel":
        object.__setattr__(self, "_credential_ref", value)
        return self

    def credential_reference(self) -> str:
        if self._credential_ref is None:
            raise ValueError("resolved model credential is unavailable")
        return self._credential_ref

def _is_loopback(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False
