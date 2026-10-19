"""OS ownership, independent of SQLite write locks and elapsed time."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from threading import local
from typing import Any, TypeVar, cast

from filelock import FileLock, Timeout

from yipilot.domain.errors import DomainError
from yipilot.persistence.filesystem import project_path, safe_path_component
from yipilot.security.paths import require_plain_path

_state = local()
F = TypeVar("F", bound=Callable[..., Any])


@contextmanager
def project_lock(project: Path, name: str, *, timeout: float = 0) -> Iterator[None]:
    safe_path_component(name)
    directory = project_path(project, ".yipilot", "locks")
    directory.mkdir(parents=True, exist_ok=True)
    # FileLock deletes its file on Windows release; resolve the stable parent only.
    path = directory / f"{name}.lock"
    require_plain_path(path)
    locks = getattr(_state, "locks", None)
    if locks is None:
        locks = _state.locks = {}
    key = str(path)
    lock = locks.setdefault(key, FileLock(path))
    try:
        with lock.acquire(timeout=timeout):
            yield
    except Timeout as error:
        raise DomainError("PROJECT_BUSY", "project executor or migration is active") from error


def exclusive_execution(method: F) -> F:
    """Serialize actual service entry points; nesting on the same thread is safe."""
    @wraps(method)
    def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
        store = getattr(self, "_store", None) or getattr(self, "store", None) or getattr(self, "_task_store", None) or self.sandbox.store
        with project_lock(store.db.project, "executor"):
            return method(self, *args, **kwargs)
    return cast(F, guarded)
