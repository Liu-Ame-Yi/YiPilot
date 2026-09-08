"""The sole control-plane boundary for model resolution and live-port creation."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from yipilot.domain.errors import DomainError
from yipilot.domain.model import ModelPort, ModelRequest
from yipilot.domain.provider import (
    ModelEntry,
    ModelSelector,
    ProviderConnection,
    ResolvedModel,
)
from yipilot.models.openai_compatible import OpenAICompatibleModelPort
from yipilot.models.secrets import FakeSecretBackend, SecretStore
from yipilot.models.store import FileProviderStore, ProviderSettings


class ModelSelectionService:
    def __init__(
        self,
        settings_dir: Path | None = None,
        *,
        secrets: SecretStore | None = None,
        port_factory: Callable[[ResolvedModel, SecretStore, str], ModelPort] | None = None,
    ) -> None:
        self._store = FileProviderStore(settings_dir)
        self._secrets = secrets or SecretStore(settings_dir=settings_dir)
        self._port_factory = port_factory or (lambda model, secrets, ref: OpenAICompatibleModelPort(model, secrets, credential_ref=ref))

    @property
    def path(self) -> Path:
        return self._store.path

    def list_public(self) -> dict[str, object]:
        settings = self._store.load()
        return {
            "default_model_selector": settings.default_model_selector,
            "models": [self._public_model(model, settings) for model in settings.models],
        }

    def add(self, connection: ProviderConnection, model: ModelEntry) -> ModelEntry:
        try:
            current = self._store.get_connection(connection.connection_id)
        except DomainError:
            self._store.add_connection(connection)
        else:
            if current != connection:
                self._store.add_connection(connection)
        return self._store.add_model(model)

    def set_default(self, selector: str | None) -> None:
        if selector is not None:
            self.resolve(explicit_selector=selector)
        self._store.set_default(selector)

    def remove(self, selector_value: str) -> dict[str, object]:
        """Remove one model, its empty connection, and its file credential when safe."""
        try:
            requested = ModelSelector.parse(selector_value)
        except ValueError as error:
            raise DomainError("MODEL_SELECTOR_INVALID", "model selector must be connection_id/model_id[:variant]") from error
        settings = self._store.load()
        selector, model = _selection_and_model(settings, requested)
        connection = _connection(settings, selector)
        models = tuple(item for item in settings.models if item != model)
        remove_connection = not any(item.connection_id == connection.connection_id for item in models)
        connections = tuple(item for item in settings.connections if item != connection) if remove_connection else settings.connections
        canonical = f"{connection.connection_id}/{model.model_id}"
        default = settings.default_model_selector
        if default is not None and (default == canonical or default.startswith(f"{canonical}:")):
            default = None
        self._store.save(settings.model_copy(update={
            "connections": connections,
            "models": models,
            "default_model_selector": default,
        }))
        if remove_connection and connection.credential_ref == SecretStore.file_reference(connection.connection_id):
            self._secrets.remove_file(connection.connection_id)
        return {"selector": canonical, "connection_removed": remove_connection}

    def resolve(
        self,
        *,
        explicit_selector: str | None = None,
        session_selector: str | None = None,
    ) -> ResolvedModel:
        source: Literal["explicit", "session", "global_default"]
        selector_value: str | None
        if explicit_selector is not None:
            selector_value, source = explicit_selector, "explicit"
        elif session_selector is not None:
            selector_value, source = session_selector, "session"
        else:
            selector_value = self._store.load().default_model_selector
            source = "global_default"
        if selector_value is None:
            raise DomainError("MODEL_SELECTION_REQUIRED", "select a model with /model or --model")
        try:
            requested = ModelSelector.parse(selector_value)
        except ValueError as error:
            raise DomainError("MODEL_SELECTOR_INVALID", "model selector must be connection_id/model_id[:variant]") from error
        settings = self._store.load()
        connection = _connection(settings, requested)
        selector, model = _selection_and_model(settings, requested)
        variant = next((item for item in model.variants if item.name == selector.variant), None)
        if selector.variant is not None and variant is None:
            raise DomainError("MODEL_VARIANT_NOT_FOUND", "model variant was not found")
        if not connection.enabled:
            raise DomainError("PROVIDER_DISABLED", "provider connection is disabled")
        if not model.enabled:
            raise DomainError("MODEL_DISABLED", "model is disabled")
        if variant is not None and not variant.enabled:
            raise DomainError("MODEL_DISABLED", "model variant is disabled")
        if "chat" not in connection.capabilities or "chat" not in model.capabilities:
            raise DomainError("PROVIDER_CAPABILITY_UNAVAILABLE", "model selection does not support chat")
        output_limit = variant.output_limit if variant and variant.output_limit is not None else model.output_limit
        context_limit = variant.context_limit if variant and variant.context_limit is not None else model.context_limit
        if output_limit <= 0 or connection.timeout_seconds <= 0:
            raise DomainError("MODEL_LIMIT_INVALID", "model selection has invalid limits")
        if connection.credential_ref is None:
            raise DomainError("SECRET_REF_MISSING", "provider connection has no secret reference")
        try:
            self._secrets.read(connection.credential_ref)
        except DomainError as error:
            if error.code in {"SECRET_NOT_FOUND", "SECRET_BACKEND_UNAVAILABLE"}:
                raise DomainError("SECRET_REF_UNREADABLE", "secret reference is not readable") from error
            raise
        return ResolvedModel(
            selector=str(selector), source=source, connection_id=connection.connection_id,
            provider_type=connection.type, base_url=connection.base_url,
            connection_revision=connection.revision, model_id=model.model_id,
            display_name=model.display_name, capabilities=tuple(sorted(set(connection.capabilities) & set(model.capabilities))),
            context_limit=context_limit, output_limit=output_limit,
            timeout_seconds=connection.timeout_seconds, variant=selector.variant,
            request_options=dict(variant.request_options) if variant else {},
        ).with_credential_reference(connection.credential_ref)

    def model_port(self, resolved: ResolvedModel) -> ModelPort:
        """Construct only from the frozen snapshot, never from mutable Settings."""
        try:
            reference = resolved.credential_reference()
        except ValueError as error:
            raise DomainError("MODEL_FROZEN_CREDENTIAL_UNAVAILABLE", "frozen model credential is unavailable") from error
        return self._port_factory(resolved, self._secrets, reference)

    def test(self, selector: str, *, request: bool = False) -> dict[str, object]:
        resolved = self.resolve(explicit_selector=selector)
        if not request:
            return {"selector": resolved.selector, "status": "local_validation_passed"}
        from yipilot.models.openai_compatible import provider_probe_request

        port = self.model_port(resolved)
        try:
            self._probe_port(port, provider_probe_request(resolved))
        finally:
            close = getattr(port, "close", None)
            if callable(close):
                close()
        return {"selector": resolved.selector, "status": "request_passed"}

    def test_draft(self, connection: ProviderConnection, model: ModelEntry, api_key: str) -> None:
        """Probe an unpersisted connection/model with an in-memory credential."""
        if not api_key:
            raise DomainError("SECRET_VALUE_INVALID", "secret value must not be empty")
        if not connection.enabled or not model.enabled:
            raise DomainError("MODEL_DISABLED", "provider connection or model is disabled")
        if "chat" not in connection.capabilities or "chat" not in model.capabilities:
            raise DomainError("PROVIDER_CAPABILITY_UNAVAILABLE", "model selection does not support chat")
        reference = SecretStore.keyring_reference("draft-probe")
        secrets = SecretStore(FakeSecretBackend({("yipilot", "draft-probe"): api_key}))
        resolved = ResolvedModel(
            selector=f"{connection.connection_id}/{model.model_id}", source="explicit",
            connection_id=connection.connection_id, provider_type=connection.type,
            base_url=connection.base_url, connection_revision=connection.revision,
            model_id=model.model_id, display_name=model.display_name,
            capabilities=tuple(sorted(set(connection.capabilities) & set(model.capabilities))),
            context_limit=model.context_limit, output_limit=model.output_limit,
            timeout_seconds=connection.timeout_seconds,
        ).with_credential_reference(reference)
        from yipilot.models.openai_compatible import provider_probe_request

        port = self._port_factory(resolved, secrets, reference)
        try:
            self._probe_port(port, provider_probe_request(resolved))
        finally:
            close = getattr(port, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _probe_port(port: ModelPort, request: ModelRequest) -> None:
        probe = getattr(port, "probe", None)
        if callable(probe):
            probe(request)
        else:
            port.stream(request)

    @staticmethod
    def _public_model(model: ModelEntry, settings: ProviderSettings) -> dict[str, object]:
        connection = next(item for item in settings.connections if item.connection_id == model.connection_id)
        selectors = [f"{model.connection_id}/{model.model_id}"] + [f"{model.connection_id}/{model.model_id}:{variant.name}" for variant in model.variants]
        return {"selectors": selectors, "connection_id": model.connection_id, "model_id": model.model_id,
                "display_name": model.display_name, "capabilities": model.capabilities,
                "enabled": model.enabled and connection.enabled, "output_limit": model.output_limit,
                "base_url": connection.base_url}


def _connection(settings: ProviderSettings, selector: ModelSelector) -> ProviderConnection:
    for item in settings.connections:
        if item.connection_id == selector.connection_id:
            return item
    raise DomainError("PROVIDER_NOT_FOUND", "provider connection was not found")


def _model(settings: ProviderSettings, selector: ModelSelector) -> ModelEntry:
    for item in settings.models:
        if (item.connection_id, item.model_id) == (selector.connection_id, selector.model_id):
            return item
    raise DomainError("MODEL_NOT_FOUND", "model was not found")


def _selection_and_model(
    settings: ProviderSettings, requested: ModelSelector
) -> tuple[ModelSelector, ModelEntry]:
    """Prefer an exact configured model ID before treating a suffix as a variant."""
    exact = _find_model(settings, requested.connection_id, requested.model_id)
    if exact is not None:
        return requested, exact
    model_id, separator, variant = requested.model_id.rpartition(":")
    if not separator or not model_id or not variant:
        raise DomainError("MODEL_NOT_FOUND", "model was not found")
    selector = ModelSelector(
        connection_id=requested.connection_id, model_id=model_id, variant=variant
    )
    return selector, _model(settings, selector)


def _find_model(settings: ProviderSettings, connection_id: str, model_id: str) -> ModelEntry | None:
    return next((item for item in settings.models if (item.connection_id, item.model_id) == (connection_id, model_id)), None)
