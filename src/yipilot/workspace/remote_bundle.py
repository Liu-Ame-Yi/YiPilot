"""Bounded bundles and reconstruction from frozen input, never live workspace bytes."""

import hashlib
import os
import shutil
import stat
import struct
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from yipilot.domain.remote_execution import (
    IMAGE,
    MAX_BUNDLE_BYTES,
    MAX_LOG_BYTES,
    PROFILE,
    JobRequest,
    JobResult,
    Profile,
    ScriptPolicy,
    canonical,
    manifest_hash,
)
from yipilot.domain.sandbox import SourceFile
from yipilot.security.paths import require_plain_path
from yipilot.workspace.sandbox_sources import scan_source_tree


@contextmanager
def bounded_zip(path: Path) -> Iterator[zipfile.ZipFile]:
    require_plain_path(path)
    size = path.stat().st_size
    if size > MAX_BUNDLE_BYTES:
        raise ValueError("Bundle exceeds transport budget")
    with path.open("rb") as source:
        source.seek(max(0, size - 65557))
        tail = source.read()
        end = tail.rfind(b"PK\x05\x06")
        if end < 0 or len(tail) - end < 22:
            raise ValueError("Missing bounded ZIP directory")
        _, disk, directory_disk, count, total, length, offset, comment = struct.unpack_from("<4s4H2LH", tail, end)
        if (disk or directory_disk or count != total or total > 10003 or length > 16 * 1024**2
                or offset + length != size - len(tail) + end or len(tail) - end != 22 + comment):
            raise ValueError("Unsupported or oversized ZIP directory")
        source.seek(offset)
        directory = source.read(length)
    cursor = entries = 0
    while cursor < len(directory):
        if directory[cursor:cursor + 4] != b"PK\x01\x02" or len(directory) - cursor < 46:
            raise ValueError("Invalid ZIP directory")
        name, extra, note = struct.unpack_from("<3H", directory, cursor + 28)
        entries += 1
        if entries > 10003 or name > 1031:
            raise ValueError("Oversized ZIP directory entries")
        cursor += 46 + name + extra + note
    if entries != total or cursor != len(directory):
        raise ValueError("ZIP directory count differs")
    with zipfile.ZipFile(path) as bundle:
        yield bundle


def bundle_entries(bundle: zipfile.ZipFile, expected: set[str]) -> None:
    entries = bundle.infolist()
    if len(entries) != len(expected) or {e.filename for e in entries} != expected:
        raise ValueError("Bundle has missing, duplicate or unexpected entries")
    for entry in entries:
        mode = entry.external_attr >> 16
        if (entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
            raise ValueError("Only unencrypted regular files are accepted")


def read_entry(bundle: zipfile.ZipFile, name: str, maximum: int) -> bytes:
    if bundle.getinfo(name).file_size > maximum:
        raise ValueError("Bundle entry exceeds budget")
    with bundle.open(name) as handle:
        data = handle.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("Expanded entry exceeds budget")
    return data


def validate_source_bytes(file: SourceFile, data: bytes) -> None:
    if len(data) != file.size_bytes or hashlib.sha256(data).hexdigest() != file.sha256:
        raise ValueError("File size or digest differs")
    data.decode("utf-8")
    if b"\0" in data:
        raise ValueError("Only UTF-8 patchable source is permitted")


def write_source(root: Path, file: SourceFile, data: bytes) -> None:
    validate_source_bytes(file, data)
    path = root / file.path
    require_plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    path.chmod(0o755 if file.git_mode == "100755" else 0o644)


def freeze_bundle(source: Path, destination: Path, *, request_key: str, task_id: str,
                  operation_id: str, run_id: str, script: str,
                  frozen_files: tuple[SourceFile, ...] = (), timeout_seconds: int = 60,
                  memory_bytes: int = 128 * 1024**2, cpu_millis: int = 250,
                  profile: Profile = PROFILE, image: str = IMAGE,
                  stdout_bytes: int = MAX_LOG_BYTES, stderr_bytes: int = MAX_LOG_BYTES,
                  policy_revision: ScriptPolicy = "python-script-v1") -> JobRequest:
    files, contents = scan_source_tree(source, frozen_files)
    request = JobRequest(request_key=request_key, task_id=task_id, operation_id=operation_id,
                         run_id=run_id, script=script, files=files, timeout_seconds=timeout_seconds,
                         memory_bytes=memory_bytes, cpu_millis=cpu_millis, profile=profile, image=image,
                         stdout_bytes=stdout_bytes, stderr_bytes=stderr_bytes, policy_revision=policy_revision)
    require_plain_path(destination)
    with destination.open("xb") as output, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("request.json", canonical(request))
        for file in files:
            bundle.writestr("source/" + file.path, contents[file.path])
    if destination.stat().st_size > MAX_BUNDLE_BYTES:
        destination.unlink()
        raise ValueError("Input bundle exceeds transport budget")
    return request


def unpack_input(path: Path, destination: Path) -> JobRequest:
    require_plain_path(path)
    require_plain_path(destination)
    if path.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError("Input bundle exceeds transport budget")
    with bounded_zip(path) as bundle:
        request = JobRequest.model_validate_json(read_entry(bundle, "request.json", 4 * 1024**2))
        bundle_entries(bundle, {"request.json", *("source/" + f.path for f in request.files)})
        destination.mkdir(mode=0o700)
        for file in request.files:
            write_source(destination, file, read_entry(bundle, "source/" + file.path, file.size_bytes))
    actual, _ = scan_source_tree(destination, request.files)
    if actual != request.files:
        raise ValueError("Input manifest differs")
    return request


def publish_result(path: Path, result: JobResult, contents: dict[str, bytes],
                   stdout: bytes, stderr: bytes) -> None:
    if set(contents) != set(result.changed):
        raise ValueError("Changed source differs")
    temporary = path.with_suffix(".partial")
    with temporary.open("xb") as output:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("result.json", canonical(result))
            bundle.writestr("stdout.log", stdout)
            bundle.writestr("stderr.log", stderr)
            for name, data in contents.items():
                bundle.writestr("source/" + name, data)
        output.flush()
        os.fsync(output.fileno())
    if temporary.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError("Output bundle exceeds transport budget")
    os.replace(temporary, path)



def result_metadata(bundle: zipfile.ZipFile, request: JobRequest, job_id: str) -> JobResult:
    result = JobResult.model_validate_json(read_entry(bundle, "result.json", 4 * 1024**2))
    if (result.job_id != job_id or result.request_fingerprint != request.fingerprint
            or result.profile != request.profile or result.image != request.image
            or result.input_manifest_hash != manifest_hash(request.files)):
        raise ValueError("Result belongs to a different input or Job")
    old, new = {f.path: f for f in request.files}, {f.path: f for f in result.files}
    changed = tuple(sorted(p for p in new if new[p] != old.get(p)))
    deleted = tuple(sorted(old.keys() - new.keys()))
    if result.changed != changed or result.deleted != deleted:
        raise ValueError("Result delta differs from complete manifest")
    aliases = {p.casefold(): p for p in old}
    if any(aliases.get(p.casefold(), p) != p for p in new):
        raise ValueError("Cross-tree case alias denied")
    bundle_entries(bundle, {"result.json", "stdout.log", "stderr.log",
                            *("source/" + p for p in changed)})
    for stream in ("stdout", "stderr"):
        data = read_entry(bundle, stream + ".log", getattr(request, stream + "_bytes"))
        if (len(data) != getattr(result, stream + "_bytes")
                or hashlib.sha256(data).hexdigest() != getattr(result, stream + "_sha256")):
            raise ValueError("Log digest differs")
    return result


def verify_input_bytes(path: Path, expected: JobRequest) -> None:
    """Recheck an approved manifest and payload without writing another source tree."""
    with bounded_zip(path) as bundle:
        actual = JobRequest.model_validate_json(read_entry(bundle, "request.json", 4 * 1024**2))
        if actual != expected:
            raise ValueError("Frozen input contract differs")
        bundle_entries(bundle, {"request.json", *("source/" + f.path for f in expected.files)})
        for file in expected.files:
            validate_source_bytes(file, read_entry(bundle, "source/" + file.path, file.size_bytes))


def reconstruct_result(input_bundle: Path, output_bundle: Path, destination: Path,
                       *, job_id: str) -> JobResult:
    require_plain_path(destination)
    if destination.exists() or output_bundle.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError("Use a new result directory and a bounded bundle")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent, prefix=".remote-result-") as temporary:
        tree = Path(temporary) / "tree"
        request = unpack_input(input_bundle, tree)
        with bounded_zip(output_bundle) as bundle:
            result = result_metadata(bundle, request, job_id)
            new = {f.path: f for f in result.files}
            changed, deleted = result.changed, result.deleted
            for name in (*deleted, *changed):
                path = tree / name
                if not path.is_dir():
                    path.unlink(missing_ok=True)
            for directory in sorted(tree.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
            for name in changed:
                write_source(tree, new[name], read_entry(bundle, "source/" + name, new[name].size_bytes))
        actual, _ = scan_source_tree(tree, result.files)
        if actual != result.files:
            raise ValueError("Reconstructed output differs")
        shutil.move(str(tree), str(destination))
    return result
