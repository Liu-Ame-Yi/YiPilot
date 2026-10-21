"""Temporary disks share the work-disk ownership checks and fail closed."""

import json
from uuid import UUID, uuid5

import pytest

from yipilot.runner import disk


@pytest.fixture
def temporary_disk(tmp_path, monkeypatch):
    job_id = "a" * 32
    work = tmp_path / job_id / "tmp"
    work.mkdir(parents=True)
    image = work.with_suffix(".fs")
    with image.open("wb") as stream:
        stream.truncate(disk.TEMP_BYTES)
    facts = {"target": str(work), "source": "/dev/loop7", "uuid": str(uuid5(UUID(job_id), "tmp")),
             "fstype": "ext4", "options": "rw,nodev,nosuid,noexec"}
    devices = [{"name": "/dev/loop7", "back-file": str(image)}]
    monkeypatch.setattr(disk, "mounts", lambda *args: [] if "--source" in args else [facts])
    calls = []

    def command(*args):
        calls.append(args)
        return json.dumps({"loopdevices": devices}).encode() if args[0] == "losetup" and args[1] == "--json" else b""

    monkeypatch.setattr(disk, "command", command)
    return job_id, work, facts, devices, calls


def test_temporary_disk_is_distinct_and_removed_after_release(temporary_disk):
    job_id, work, facts, _, calls = temporary_disk
    assert facts["uuid"] != str(UUID(job_id))
    assert disk.owned_mount(job_id, work) == facts
    disk.release(job_id, work)
    assert ("umount", str(work)) in calls
    assert ("losetup", "--detach", "/dev/loop7") in calls
    assert not work.exists() and not work.with_suffix(".fs").exists()


@pytest.mark.parametrize("change", [
    {"uuid": str(UUID("a" * 32))}, {"options": "rw,nodev,nosuid"},
    {"fstype": "tmpfs"}, {"source": "/dev/loop8"}, {"target": "/other-job/tmp"},
])
def test_foreign_or_executable_temporary_mount_is_preserved(temporary_disk, change):
    job_id, work, facts, _, calls = temporary_disk
    facts.update(change)
    with pytest.raises(ValueError, match="ownership"):
        disk.release(job_id, work)
    assert work.with_suffix(".fs").exists()
    assert not any(c[0] == "umount" for c in calls)


def test_still_mounted_loop_is_preserved(temporary_disk, monkeypatch):
    job_id, work, facts, _, calls = temporary_disk
    monkeypatch.setattr(disk, "mounts", lambda *args: [facts])
    with pytest.raises(ValueError, match="still mounted"):
        disk.release(job_id, work)
    assert work.with_suffix(".fs").exists()
    assert not any(c[:2] == ("losetup", "--detach") for c in calls)


def test_partial_unmounted_temporary_disk_can_be_collected(temporary_disk, monkeypatch):
    job_id, work, _, devices, _ = temporary_disk
    devices.clear()
    monkeypatch.setattr(disk, "mounts", lambda *args: [])
    disk.release(job_id, work)
    disk.release(job_id, work)
    assert not work.with_suffix(".fs").exists()
