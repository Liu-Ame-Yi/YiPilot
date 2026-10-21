import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from yipilot.domain.errors import DomainError
from yipilot.persistence.filesystem import (
    atomic_write_json,
    atomic_write_yaml,
    project_path,
)
from yipilot.persistence.ownership import project_lock


def test_project_path_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(DomainError, match="PATH_DENIED"):
        project_path(tmp_path, "..", "outside.json")


def test_project_path_rejects_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(DomainError, match="PATH_DENIED"):
        project_path(tmp_path, str((tmp_path.parent / "outside.json").resolve()))


def test_project_path_rejects_existing_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(DomainError, match="PATH_DENIED"):
        project_path(tmp_path, "link", "evidence.json")


def test_lock_accepts_a_delete_pending_file_observed_during_validation(tmp_path, monkeypatch):
    directory = tmp_path / ".yipilot/locks"
    directory.mkdir(parents=True)
    path = directory / "maintenance.lock"
    path.touch()
    metadata = path.lstat()
    path.unlink()
    original = Path.lstat
    observed = False

    def pending(candidate, *args, **kwargs):
        nonlocal observed
        if candidate == path and not observed:
            observed = True
            return SimpleNamespace(st_mode=metadata.st_mode, st_nlink=0,
                                   st_file_attributes=getattr(metadata, "st_file_attributes", 0))
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", pending)
    with project_lock(tmp_path, "maintenance"):
        assert observed


def test_lock_rejects_a_hard_link_without_changing_its_target(tmp_path):
    directory = tmp_path / ".yipilot/locks"
    directory.mkdir(parents=True)
    target = tmp_path / "original.txt"
    target.write_bytes(b"preserve")
    os.link(target, directory / "maintenance.lock")
    with pytest.raises(DomainError, match="PATH_DENIED"), project_lock(tmp_path, "maintenance"):
        pytest.fail("hard-linked lock was acquired")
    assert target.read_bytes() == b"preserve"


@given(st.lists(st.sampled_from([".", "..", "safe", "nested"]), min_size=1, max_size=6))
def test_project_path_never_returns_a_path_outside_root(
    parts: list[str],
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        if ".." in parts:
            with pytest.raises(DomainError, match="PATH_DENIED"):
                project_path(root, *parts)
        else:
            path = project_path(root, *parts)
            assert path == root.resolve() or root.resolve() in path.parents


def test_atomic_writes_round_trip_and_leave_no_temporary_files(tmp_path: Path) -> None:
    json_path = tmp_path / "records" / "record.json"
    yaml_path = tmp_path / "records" / "record.yaml"

    atomic_write_json(json_path, {"schema_version": 1, "name": "项目"})
    atomic_write_yaml(yaml_path, {"schema_version": 1, "name": "项目"})

    assert json.loads(json_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "name": "项目",
    }
    assert "schema_version: 1" in yaml_path.read_text(encoding="utf-8")
    assert not list(tmp_path.rglob("*.tmp"))
