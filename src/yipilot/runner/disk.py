"""A host-owned ext4 image bounds task writes without consuming host tmpfs RAM."""

import json
import os
import subprocess
from pathlib import Path
from uuid import UUID

from yipilot.security.paths import require_plain_path

WORK_BYTES = 256 * 1024**2


def command(*arguments: str) -> bytes:
    return subprocess.run(arguments, check=True, capture_output=True, timeout=30).stdout


def mounts(*arguments: str) -> list[dict]:
    result = subprocess.run(["findmnt", "--json", *arguments, "-o", "TARGET,SOURCE,FSTYPE,OPTIONS,UUID"],
                            check=False, capture_output=True, timeout=15)
    if result.returncode == 1 and not result.stdout:
        return []
    result.check_returncode()
    return json.loads(result.stdout)["filesystems"]


def owned_mount(job_id: str, work: Path) -> dict | None:
    require_plain_path(work)
    found = mounts("--mountpoint", str(work))
    if not found:
        return None
    facts = found[0]
    devices = json.loads(command("losetup", "--json", "--list", "--output", "NAME,BACK-FILE"))["loopdevices"]
    image = work.parent / "work.fs"
    require_plain_path(image)
    if (len(found) != 1 or facts["target"] != str(work) or facts["uuid"] != str(UUID(job_id))
            or facts["fstype"] != "ext4" or not {"nodev", "nosuid"} <= set(facts["options"].split(","))
            or not any(d["name"] == facts["source"] and d["back-file"] == str(image) for d in devices)):
        raise ValueError("Job mount ownership differs")
    return facts


def prepare(job_id: str, work: Path) -> None:
    require_plain_path(work)
    image = work.parent / "work.fs"
    with image.open("xb") as output:
        output.truncate(WORK_BYTES)
        os.fsync(output.fileno())
    image.chmod(0o600)
    command("mkfs.ext4", "-q", "-F", "-m", "0", "-N", "32768", "-U", str(UUID(job_id)), str(image))
    work.mkdir(mode=0o700)
    command("mount", "-o", "loop,nodev,nosuid,noatime", str(image), str(work))
    if owned_mount(job_id, work) is None:
        raise ValueError("Job filesystem was not mounted")
    (work / "lost+found").rmdir()


def release(job_id: str, work: Path) -> None:
    if owned_mount(job_id, work) is not None:
        command("umount", str(work))
    image = work.parent / "work.fs"
    if not image.exists():
        return
    require_plain_path(image)
    devices = json.loads(command("losetup", "--json", "--list", "--output", "NAME,BACK-FILE"))["loopdevices"]
    for device in devices:
        if device["back-file"] == str(image):
            if mounts("--source", device["name"]):
                raise ValueError("Owned device is still mounted")
            command("losetup", "--detach", device["name"])
