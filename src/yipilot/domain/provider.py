"""Versioned, non-secret provider profile contracts."""

import re
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_PROVIDER_TIMEOUT_SECONDS = 600.0
MAX_PROVIDER_OUTPUT_BUDGET = 128_000
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_REFERENCE = re.compile(r"^(?:(?P<env>env):(?P<env_name>[A-Za-z_][A-Za-z0-9_]*)|(?P<keyring>keyring):(?P<keyring_name>[A-Za-z0-9][A-Za-z0-9_.-]{0,63}))$")


def parse_api_key_ref(value: str) -> tuple[str, str]:
    """Validate one safe secret reference and return its kind and name."""
    match = _REFERENCE.fullmatch(value)
    if match is None:
        raise ValueError("api key must be an env: or keyring: reference")
    if match.group("env") == "env":
        return "env", match.group("env_name")
    return "keyring", match.group("keyring_name")


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

def _is_loopback(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False
