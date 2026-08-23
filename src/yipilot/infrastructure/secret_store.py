"""User-local secret references backed by Windows Credential Locker or env."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

import keyring

from yipilot.domain.errors import DomainError

_REFERENCE = re.compile(r"^(?:(?P<env>env):(?P<env_name>[A-Za-z_][A-Za-z0-9_]*)|(?P<keyring>keyring):(?P<keyring_name>[A-Za-z0-9][A-Za-z0-9_.-]{0,63}))$")
_SERVICE_NAME = "yipilot"


@dataclass
class FakeSecretBackend:
    """In-memory keyring substitute for deterministic tests."""

    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password


class SecretStore:
    """Resolve only references; callers never receive a value for presentation."""

    def __init__(
        self,
        backend: object | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._backend = backend if backend is not None else keyring
        self._environ = environ if environ is not None else os.environ

    @staticmethod
    def validate_reference(reference: str) -> str:
        if _REFERENCE.fullmatch(reference) is None:
            raise DomainError("SECRET_REF_INVALID", "secret reference is invalid")
        return reference

    @staticmethod
    def keyring_reference(profile_name: str) -> str:
        return f"keyring:{profile_name}"

    def read(self, reference: str) -> str:
        match = _REFERENCE.fullmatch(reference)
        if match is None:
            raise DomainError("SECRET_REF_INVALID", "secret reference is invalid")
        name = match.group("env_name") or match.group("keyring_name")
        if match.group("env") == "env":
            value = self._environ.get(name)
        else:
            try:
                value = self._backend.get_password(_SERVICE_NAME, name)  # type: ignore[attr-defined]
            except Exception as error:
                raise DomainError(
                    "SECRET_BACKEND_UNAVAILABLE", "secret storage is unavailable"
                ) from error
        if not value:
            raise DomainError("SECRET_NOT_FOUND", "secret reference is not readable")
        return value

    def set_keyring(self, profile_name: str, value: str) -> str:
        if not value:
            raise DomainError("SECRET_VALUE_INVALID", "secret value must not be empty")
        try:
            self._backend.set_password(_SERVICE_NAME, profile_name, value)  # type: ignore[attr-defined]
        except Exception as error:
            raise DomainError(
                "SECRET_BACKEND_UNAVAILABLE", "secret storage is unavailable"
            ) from error
        return self.keyring_reference(profile_name)
