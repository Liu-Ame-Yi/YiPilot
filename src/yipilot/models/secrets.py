"""User-local secret references backed by auth.json, Windows Credential Locker, or env."""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile

import keyring

from yipilot.domain.errors import DomainError
from yipilot.domain.provider import parse_api_key_ref
from yipilot.models.store import settings_directory

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
        *,
        settings_dir: Path | None = None,
    ) -> None:
        self._backend = backend if backend is not None else keyring
        self._environ = environ if environ is not None else os.environ
        self._auth_path = settings_directory(settings_dir) / "auth.json"

    @staticmethod
    def validate_reference(reference: str) -> str:
        try:
            parse_api_key_ref(reference)
        except ValueError as error:
            raise DomainError("SECRET_REF_INVALID", "secret reference is invalid") from error
        return reference

    @staticmethod
    def keyring_reference(profile_name: str) -> str:
        return f"keyring:{profile_name}"

    @staticmethod
    def file_reference(profile_name: str) -> str:
        return f"file:{profile_name}"

    def read(self, reference: str) -> str:
        try:
            kind, name = parse_api_key_ref(reference)
        except ValueError as error:
            raise DomainError("SECRET_REF_INVALID", "secret reference is invalid") from error
        if kind == "env":
            value = self._environ.get(name)
        elif kind == "file":
            value = self._read_file_value(name)
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

    def set_file(self, profile_name: str, value: str) -> str:
        if not value:
            raise DomainError("SECRET_VALUE_INVALID", "secret value must not be empty")
        values = self._read_file_values()
        values[profile_name] = value
        self._write_file_values(values)
        return self.file_reference(profile_name)

    def remove_file(self, profile_name: str) -> bool:
        values = self._read_file_values()
        if profile_name not in values:
            return False
        del values[profile_name]
        if values:
            self._write_file_values(values)
        else:
            try:
                self._auth_path.unlink(missing_ok=True)
            except OSError as error:
                raise DomainError("SECRET_BACKEND_UNAVAILABLE", "auth.json cannot be updated") from error
        return True

    def _read_file_value(self, name: str) -> str | None:
        return self._read_file_values().get(name)

    def _read_file_values(self) -> dict[str, str]:
        if not self._auth_path.exists():
            return {}
        try:
            value = json.loads(self._auth_path.read_text(encoding="utf-8"))
            keys = value["keys"]
            if value.get("schema_version") != 1 or not isinstance(keys, dict):
                raise ValueError("invalid auth file")
            if any(not isinstance(name, str) or not isinstance(key, str) for name, key in keys.items()):
                raise ValueError("invalid auth file")
            return dict(keys)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise DomainError("SECRET_BACKEND_UNAVAILABLE", "auth.json is unavailable") from error

    def _write_file_values(self, values: dict[str, str]) -> None:
        self._auth_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps({"schema_version": 1, "keys": values}, ensure_ascii=False, sort_keys=True) + "\n"
        temporary: Path | None = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", dir=self._auth_path.parent,
                                    prefix=f".{self._auth_path.name}.", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._auth_path)
            os.chmod(self._auth_path, 0o600)
            temporary = None
        except OSError as error:
            raise DomainError("SECRET_BACKEND_UNAVAILABLE", "auth.json cannot be saved") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
