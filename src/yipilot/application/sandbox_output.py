"""Trusted diff export only. Call after the Control Plane has stopped the Worker."""

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp

from filelock import FileLock
from pydantic import TypeAdapter, ValidationError

from yipilot.application.sandbox_sources import (
    PreparedSources,
    SandboxSourceService,
    _plain_path,
    scan_source_tree,
)
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ExecutionPolicy, Sha256, _fingerprint_model
from yipilot.domain.patch_proposal import MAX_PATCH_DIFF_BYTES, MAX_PATCH_FILES
from yipilot.domain.sandbox import (
    SandboxDiffEvidence,
    SandboxOutputManifest,
    SandboxRequest,
    SandboxResult,
    source_path,
)
from yipilot.domain.sandbox import SandboxErrorCode as Code
from yipilot.infrastructure.approval_store import FileApprovalStore
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.filesystem import atomic_write_bytes, project_path
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.task_store import FileTaskStore


def output_approval_fingerprint(request: SandboxRequest, patch_sha256: str) -> str:
    """Bind an existing human approval to this exact request and approved patch."""
    TypeAdapter(Sha256).validate_python(patch_sha256, strict=True)
    return hashlib.sha256(
        json.dumps(
            {"request_fingerprint": request.fingerprint, "patch_sha256": patch_sha256},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class InspectedSandboxOutput:
    """Candidate bytes recomputed by the controller, not an approved Handoff."""

    manifest: SandboxOutputManifest
    patch: bytes

    @property
    def patch_sha256(self) -> str:
        return hashlib.sha256(self.patch).hexdigest()


class SandboxOutputService:
    def __init__(self, project_root: Path, git_runner: GitRunner) -> None:
        self._sources = SandboxSourceService(project_root, git_runner)
        self._root = project_root.resolve(strict=True)
        self._store = FileTaskStore(self._root)
        self._artifacts = FileArtifactStore(self._store)
        self._approvals = FileApprovalStore(self._store)
        self._git = git_runner

    def inspect(
        self,
        prepared: PreparedSources,
        request: SandboxRequest,
        policy: ExecutionPolicy,
    ) -> InspectedSandboxOutput:
        """Recompute a candidate diff from checked snapshots; run no repository code."""
        try:
            request = SandboxRequest.model_validate_json(
                request.model_dump_json(), context={"policy": policy}
            )
            manifest = prepared.manifest
            run = self._store.get_run(request.task_id, request.run_id)
            self._store.get_operation(request.task_id, request.operation_id)
            if (
                (manifest.task_id, manifest.operation_id, manifest.run_id)
                != (request.task_id, request.operation_id, request.run_id)
                or run.operation_id != request.operation_id
                or str(prepared.execution) != request.execution_directory
                or prepared.manifest_hash != request.baseline_manifest_hash
            ):
                raise DomainError(
                    Code.ASSOCIATION_INVALID, "output does not belong to request"
                )
            self._sources.validate(prepared)
            baseline_files, before = scan_source_tree(prepared.baseline, manifest.files)
            after_files, after = scan_source_tree(prepared.execution, manifest.files)
            if baseline_files != manifest.files:
                raise DomainError(
                    Code.OUTPUT_DRIFT, "baseline changed during collection"
                )
            old = {f.path: f for f in baseline_files}
            new = {f.path: f for f in after_files}
            aliases = {p.casefold(): p for p in old}
            if any(aliases.get(p.casefold(), p) != p for p in new):
                raise DomainError(Code.PATH_DENIED, "cross-tree case alias denied")
            changed = {p for p in old.keys() | new.keys() if old.get(p) != new.get(p)}
            # ponytail: cap changed-file input at 64 KiB; stream bounded Git output
            # if large-file edits become necessary.
            if (
                len(changed) > MAX_PATCH_FILES
                or sum(
                    len(before.get(p, b"")) + len(after.get(p, b"")) for p in changed
                )
                > MAX_PATCH_DIFF_BYTES
            ):
                raise DomainError(
                    Code.OUTPUT_INVALID, "changed source exceeds patch budget"
                )
            output_manifest = SandboxOutputManifest(
                task_id=request.task_id,
                operation_id=request.operation_id,
                run_id=request.run_id,
                request_fingerprint=request.fingerprint,
                baseline_manifest_hash=request.baseline_manifest_hash,
                files=after_files,
            )
            # Snapshots contain the bytes just hashed. Git never reads the live execution tree.
            with TemporaryDirectory(
                prefix=".diff-", dir=prepared.execution.parent
            ) as directory:
                staging = Path(directory)
                for name, files, contents in (
                    ("a", baseline_files, before),
                    ("b", after_files, after),
                ):
                    (staging / name).mkdir()
                    for file in files:
                        path = staging / name / file.path
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(contents[file.path])
                        path.chmod(0o755 if file.git_mode == "100755" else 0o644)
                patch = self._git.run_bytes(
                    [
                        "git",
                        "-c",
                        "core.autocrlf=false",
                        "-c",
                        "core.quotePath=true",
                        "-c",
                        f"core.attributesFile={os.devnull}",
                        "diff",
                        "--no-index",
                        "--no-prefix",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--",
                        "a",
                        "b",
                    ],
                    cwd=staging,
                    env={
                        "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_GLOBAL": os.devnull,
                        "GIT_CONFIG_COUNT": "0",
                        "GIT_CONFIG_PARAMETERS": None,
                        "GIT_CEILING_DIRECTORIES": str(staging.parent),
                        "GIT_EXTERNAL_DIFF": None,
                        "GIT_DIFF_OPTS": None,
                    },
                ).stdout
            if len(patch) > MAX_PATCH_DIFF_BYTES or any(
                line.startswith(b"Binary files ") or line == b"GIT binary patch"
                for line in patch.splitlines()
            ):
                raise DomainError(
                    Code.OUTPUT_INVALID, "output is not a bounded ordinary patch"
                )
            self._sources.validate(prepared)
            if scan_source_tree(prepared.execution, manifest.files)[0] != after_files:
                raise DomainError(
                    Code.OUTPUT_DRIFT, "execution changed during collection"
                )
            return InspectedSandboxOutput(output_manifest, patch)
        except (OSError, ValueError) as error:
            raise DomainError(
                Code.OUTPUT_INVALID, "cannot inspect sandbox output"
            ) from error

    def collect(
        self,
        prepared: PreparedSources,
        request: SandboxRequest,
        policy: ExecutionPolicy,
        result: SandboxResult,
        *,
        approved_patch_sha256: str,
        approval_id: str,
    ) -> SandboxDiffEvidence:
        """Publish a complete evidence bundle only after every independent check passes."""
        root = self._store.event_journal_path(request.run_id).parent
        _plain_path(root)
        destination = project_path(root, "sandbox-output")
        lock = project_path(root, ".sandbox-output.lock")
        _plain_path(lock)
        with FileLock(str(lock)):
            _plain_path(destination)
            if destination.exists():
                raise DomainError(Code.CONFLICT, "output evidence already exists")
            result = self._validate_result(root, request, result)
            self._require_approval(root, request, approved_patch_sha256, approval_id)
            output = self.inspect(prepared, request, policy)
            if output.patch_sha256 != approved_patch_sha256:
                raise DomainError(
                    Code.OUTPUT_UNAPPROVED,
                    "controller diff differs from approved patch",
                )
            if output.manifest.fingerprint != result.output_manifest_hash:
                raise DomainError(
                    Code.OUTPUT_DRIFT,
                    "reported output manifest differs from controller",
                )
            evidence = SandboxDiffEvidence(
                task_id=request.task_id,
                operation_id=request.operation_id,
                run_id=request.run_id,
                request_fingerprint=request.fingerprint,
                result_fingerprint=_fingerprint_model(result),
                policy_revision=request.policy_revision,
                baseline_manifest_hash=prepared.manifest_hash,
                output_manifest_hash=output.manifest.fingerprint,
                patch_sha256=output.patch_sha256,
                approval_id=approval_id,
                created_at=datetime.now(UTC),
            )
            staging = Path(mkdtemp(prefix=".sandbox-output-", suffix=".tmp", dir=root))
            try:
                for name, content in {
                    "patch.diff": output.patch,
                    "manifest.json": output.manifest.canonical_bytes,
                    "request.json": request.model_dump_json().encode(),
                    "result.json": result.model_dump_json().encode(),
                    "policy.json": policy.model_dump_json().encode(),
                    "evidence.json": evidence.model_dump_json().encode(),
                }.items():
                    # Do not redact exact patch bytes: changing them invalidates approval.
                    atomic_write_bytes(staging / name, content)
                    if (staging / name).read_bytes() != content:
                        raise DomainError(
                            Code.EVIDENCE_FAILED, "persisted output bytes changed"
                        )
                self._validate_result(root, request, result)
                self._require_approval(
                    root, request, approved_patch_sha256, approval_id
                )
                current = self.inspect(prepared, request, policy)
                if current != output:
                    raise DomainError(
                        Code.OUTPUT_DRIFT, "output changed before publication"
                    )
                staging.rename(destination)
            except BaseException as error:
                _plain_path(staging)
                shutil.rmtree(staging)
                if isinstance(error, OSError):
                    raise DomainError(
                        Code.EVIDENCE_FAILED, "cannot persist output evidence"
                    ) from error
                raise
            return evidence

    def verify(
        self,
        prepared: PreparedSources,
        request: SandboxRequest,
        policy: ExecutionPolicy,
        evidence: SandboxDiffEvidence,
    ) -> bytes:
        """Revalidate evidence and live tree hashes before a future trusted consumer uses bytes."""
        root = self._store.event_journal_path(request.run_id).parent
        bundle = project_path(root, "sandbox-output")
        try:
            contents = {}
            for name in (
                "evidence.json",
                "request.json",
                "result.json",
                "policy.json",
                "manifest.json",
                "patch.diff",
            ):
                path = bundle / name
                _plain_path(path)
                if path.stat().st_size > 16 * 1024**2:
                    raise ValueError("oversized evidence")
                contents[name] = path.read_bytes()
            if (
                SandboxDiffEvidence.model_validate_json(contents["evidence.json"])
                != evidence
                or (evidence.task_id, evidence.operation_id, evidence.run_id)
                != (request.task_id, request.operation_id, request.run_id)
                or evidence.policy_revision != request.policy_revision
                or contents["request.json"] != request.model_dump_json().encode()
                or contents["policy.json"] != policy.model_dump_json().encode()
            ):
                raise ValueError("evidence binding changed")
            result = SandboxResult.model_validate_json(contents["result.json"])
            self._validate_result(root, request, result)
            if _fingerprint_model(result) != evidence.result_fingerprint:
                raise ValueError("result evidence changed")
            output = self.inspect(prepared, request, policy)
            if (
                contents["manifest.json"] != output.manifest.canonical_bytes
                or contents["patch.diff"] != output.patch
                or evidence.request_fingerprint != request.fingerprint
                or evidence.baseline_manifest_hash != request.baseline_manifest_hash
                or evidence.output_manifest_hash != output.manifest.fingerprint
                or evidence.patch_sha256 != output.patch_sha256
                or result.output_manifest_hash != output.manifest.fingerprint
            ):
                raise ValueError("output evidence changed")
            self._require_approval(
                root, request, output.patch_sha256, evidence.approval_id
            )
            return output.patch
        except (OSError, ValueError) as error:
            raise DomainError(
                Code.EVIDENCE_FAILED, "output evidence is stale or incomplete"
            ) from error

    def _require_approval(
        self, root: Path, request: SandboxRequest, patch_sha256: str, approval_id: str
    ) -> None:
        try:
            source_path(approval_id)
            fingerprint = output_approval_fingerprint(request, patch_sha256)
        except ValueError as error:
            raise DomainError(
                Code.OUTPUT_UNAPPROVED, "invalid approval binding"
            ) from error
        if "/" in approval_id:
            raise DomainError(Code.OUTPUT_UNAPPROVED, "invalid approval identifier")
        _plain_path(root / "approvals" / f"{approval_id}.json")
        approval = self._approvals.get(request.task_id, request.run_id, approval_id)
        approval.require_fresh(
            task_id=request.task_id,
            operation_id=request.operation_id,
            run_id=request.run_id,
            action_type="sandbox_output",
            policy_revision=request.policy_revision,
            target_fingerprint=fingerprint,
        )
        if approval.simulation_only:
            raise DomainError(
                Code.OUTPUT_UNAPPROVED, "simulation approval cannot authorize output"
            )

    def _validate_result(
        self, root: Path, request: SandboxRequest, result: SandboxResult
    ) -> SandboxResult:
        try:
            result = SandboxResult.model_validate_json(result.model_dump_json())
        except ValidationError as error:
            raise DomainError(Code.OUTPUT_INVALID, "invalid result contract") from error
        if (
            (result.task_id, result.operation_id, result.run_id)
            != (request.task_id, request.operation_id, request.run_id)
            or result.request_fingerprint != request.fingerprint
            or result.image_digest != request.image_digest
            or result.resource_limits != request.budgets
            or result.reason != "exited"
            or result.exit_code != 0
        ):
            raise DomainError(
                Code.OUTPUT_INVALID, "failed result or execution policy drift"
            )
        run = self._store.get_run(request.task_id, request.run_id)
        if run.status.value in {"failed", "cancelled"}:
            raise DomainError(Code.OUTPUT_INVALID, "Run cannot produce accepted output")
        for ref, size in (
            (result.stdout_artifact_ref, result.stdout_bytes),
            (result.stderr_artifact_ref, result.stderr_bytes),
        ):
            source_path(ref.artifact_id)
            source_path(ref.relative_path)
            if "/" in ref.artifact_id or ref.relative_path.split("/")[0] not in {
                "artifacts",
                "tool-runs",
            }:
                raise DomainError(
                    Code.OUTPUT_INVALID, "stream evidence is not controller-owned"
                )
            _plain_path(root / "artifacts" / "records" / f"{ref.artifact_id}.json")
            path = project_path(root, ref.relative_path)
            _plain_path(path)
            if not path.is_file() or path.stat().st_size != size:
                raise DomainError(Code.OUTPUT_INVALID, "stream artifact size differs")
            record = self._artifacts.verify(
                request.task_id, request.run_id, ref.artifact_id
            )
            if (
                record.sha256 != ref.sha256
                or record.relative_path != ref.relative_path
                or record.input_fingerprint != request.fingerprint
            ):
                raise DomainError(
                    Code.OUTPUT_INVALID, "stream artifact binding differs"
                )
        return result
