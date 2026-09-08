"""Apply verified Docker output to a fresh, controller-owned Git worktree."""

import hashlib
import json
import os
from pathlib import Path

from filelock import FileLock

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ExecutionPolicy
from yipilot.domain.sandbox import SandboxDiffEvidence, SandboxRequest, SandboxResult
from yipilot.persistence.filesystem import atomic_write_bytes, project_path
from yipilot.persistence.task_store import FileTaskStore
from yipilot.security.paths import require_plain_path
from yipilot.workspace.docker_worker import DockerSandboxWorker
from yipilot.workspace.git_runner import GitRunner
from yipilot.workspace.repo_service import RepoService
from yipilot.workspace.sandbox_output import SandboxOutputService
from yipilot.workspace.sandbox_sources import (
    PreparedSources,
    scan_source_tree,
)
from yipilot.workspace.workspace_service import PreparedWorkspace, WorkspaceService


class SandboxApplyService:
    """One fresh destination per Run; never runs repository tools or creates a Handoff."""

    def __init__(self, project_root: Path, git_runner: GitRunner) -> None:
        self._root = project_root.resolve(strict=True)
        self._git = git_runner
        self._store = FileTaskStore(self._root)
        self._output = SandboxOutputService(self._root, git_runner)

    def apply(
        self,
        prepared: PreparedSources,
        request: SandboxRequest,
        policy: ExecutionPolicy,
        evidence: SandboxDiffEvidence,
    ) -> PreparedWorkspace:
        """Recheck persisted approval/evidence, then apply the exact controller diff."""
        patch = self._output.verify(prepared, request, policy, evidence)
        root = self._store.event_journal_path(request.run_id).parent
        self._require_stopped_worker(root, request)
        source = prepared.manifest
        metadata = RepoService(self._root, self._git).get_metadata(
            request.task_id, source.repository_alias
        )
        # Fail closed for filtered bases: materializing omitted tracked credentials
        # would defeat the source-copy contract. Support sparse delivery separately.
        names = self._run(
            metadata.cache_path, "ls-tree", "-rz", "--name-only", source.base_sha
        )
        if set(names.rstrip(b"\0").split(b"\0")) - {b""} != {
            f.path.encode() for f in source.files
        }:
            raise DomainError(
                "SANDBOX_SOURCE_DENIED", "base contains excluded tracked files"
            )
        destination = project_path(root, "sandbox-apply")
        lock = project_path(root, ".sandbox-apply.lock")
        require_plain_path(lock)
        with FileLock(str(lock)):
            require_plain_path(destination)
            target = project_path(
                self._root,
                ".yipilot",
                "tasks",
                request.task_id,
                "repos",
                source.repository_alias,
                "workspaces",
                request.run_id,
            )
            require_plain_path(target)
            manifest_path = target.parent / f"{request.run_id}.json"
            require_plain_path(manifest_path)
            if destination.exists() or target.exists() or manifest_path.exists():
                raise DomainError(
                    "SANDBOX_CONFLICT", "apply attempt already exists; use a new Run"
                )
            destination.mkdir()
            try:
                atomic_write_bytes(
                    destination / "input.json", evidence.model_dump_json().encode()
                )
                atomic_write_bytes(destination / "patch.diff", patch)
                workspace = WorkspaceService(self._root, self._git).prepare(
                    request.task_id,
                    source.repository_alias,
                    request.run_id,
                    base_sha=source.base_sha,
                    checkout=False,
                )
                pointer = workspace.path / ".git"
                require_plain_path(pointer)
                git_pointer = pointer.read_bytes()
                if (
                    not git_pointer.startswith(b"gitdir: ")
                    or scan_source_tree(
                        workspace.path, source.files, git_pointer=git_pointer
                    )[0]
                ):
                    raise DomainError("SANDBOX_CONFLICT", "new worktree is not empty")
                self._run(workspace.path, "read-tree", source.base_sha)
                tree = self._run(
                    workspace.path, "rev-parse", f"{source.base_sha}^{{tree}}"
                ).strip()
                self._check_git(workspace.path, source.base_sha, tree)
                if self._output.verify(prepared, request, policy, evidence) != patch:
                    raise DomainError("SANDBOX_OUTPUT_DRIFT", "approved patch changed")
                if (destination / "patch.diff").read_bytes() != patch:
                    raise DomainError("SANDBOX_EVIDENCE_FAILED", "staged patch changed")
                index = destination / "index"
                self._run(workspace.path, "read-tree", source.base_sha, index=index)
                if patch:
                    self._run(
                        workspace.path,
                        "apply",
                        "--cached",
                        "--check",
                        "--whitespace=nowarn",
                        "--",
                        str(destination / "patch.diff"),
                        index=index,
                    )
                    self._run(
                        workspace.path,
                        "apply",
                        "--cached",
                        "--whitespace=nowarn",
                        "--",
                        str(destination / "patch.diff"),
                        index=index,
                    )
                # --cached reads Git blobs, never checkout/clean/smudge filters.
                # Match its complete index against independently hashed output bytes.
                files, contents = scan_source_tree(prepared.execution, source.files)
                entries = self._run(
                    workspace.path, "ls-files", "--stage", "-z", index=index
                )
                expected_entries = set()
                for file in files:
                    data = contents[file.path]
                    oid = hashlib.sha1(
                        b"blob " + str(len(data)).encode() + b"\0" + data
                    ).hexdigest()
                    expected_entries.add(
                        f"{file.git_mode} {oid} 0\t{file.path}".encode()
                    )
                if set(entries.rstrip(b"\0").split(b"\0")) - {b""} != expected_entries:
                    raise DomainError(
                        "SANDBOX_OUTPUT_DRIFT",
                        "Git patch tree differs from approved output",
                    )
                for file in files:
                    path = project_path(workspace.path, file.path)
                    require_plain_path(path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("xb") as handle:
                        handle.write(contents[file.path])
                    path.chmod(0o755 if file.git_mode == "100755" else 0o644)
                actual = scan_source_tree(
                    workspace.path, source.files, git_pointer=git_pointer
                )[0]
                expected = self._output.inspect(prepared, request, policy)
                if (
                    actual != expected.manifest.files
                    or expected.manifest.fingerprint != evidence.output_manifest_hash
                ):
                    raise DomainError(
                        "SANDBOX_OUTPUT_DRIFT",
                        "applied files differ from approved output",
                    )
                self._check_git(workspace.path, source.base_sha, tree)
                self._output.verify(prepared, request, policy, evidence)
                if (
                    scan_source_tree(
                        workspace.path, source.files, git_pointer=git_pointer
                    )[0]
                    != actual
                ):
                    raise DomainError(
                        "SANDBOX_OUTPUT_DRIFT", "worktree changed before publication"
                    )
                atomic_write_bytes(
                    destination / "result.json",
                    json.dumps(
                        {
                            "schema_version": 1,
                            "task_id": request.task_id,
                            "operation_id": request.operation_id,
                            "run_id": request.run_id,
                            "request_fingerprint": request.fingerprint,
                            "approval_id": evidence.approval_id,
                            "patch_sha256": evidence.patch_sha256,
                            "output_manifest_hash": evidence.output_manifest_hash,
                            "base_sha": source.base_sha,
                            "index_tree": tree.decode(),
                            "git_pointer_sha256": hashlib.sha256(
                                git_pointer
                            ).hexdigest(),
                            "workspace": str(workspace.path),
                            "handoff_created": False,
                        },
                        sort_keys=True,
                    ).encode(),
                )
                return workspace
            except BaseException as error:
                # Preserve partial state for audit; the exclusive attempt directory
                # prevents reuse. Never reset/delete a potentially modified worktree.
                atomic_write_bytes(
                    destination / "failure.json",
                    json.dumps(
                        {
                            "error_code": error.code
                            if isinstance(error, DomainError)
                            else "SANDBOX_EVIDENCE_FAILED",
                            "handoff_created": False,
                        }
                    ).encode(),
                )
                if isinstance(error, (OSError, ValueError)):
                    raise DomainError(
                        "SANDBOX_EVIDENCE_FAILED", "trusted apply failed"
                    ) from error
                raise

    def verify(
        self,
        prepared: PreparedSources,
        request: SandboxRequest,
        policy: ExecutionPolicy,
        evidence: SandboxDiffEvidence,
    ) -> PreparedWorkspace:
        """Revalidate the receipt and live worktree before verification or delivery."""
        self._output.verify(prepared, request, policy, evidence)
        root = self._store.event_journal_path(request.run_id).parent
        self._require_stopped_worker(root, request)
        try:
            receipt = root / "sandbox-apply/result.json"
            require_plain_path(receipt)
            value = json.loads(receipt.read_bytes())
            source = prepared.manifest
            target = project_path(
                self._root,
                ".yipilot",
                "tasks",
                request.task_id,
                "repos",
                source.repository_alias,
                "workspaces",
                request.run_id,
            )
            for path in (target / ".git", target.parent / f"{request.run_id}.json"):
                require_plain_path(path)
                if not path.is_file():
                    raise ValueError("worktree missing")
            workspace = WorkspaceService(self._root, self._git).prepare(
                request.task_id,
                source.repository_alias,
                request.run_id,
                base_sha=source.base_sha,
            )
            if (
                workspace.path != target
                or workspace.base_sha != source.base_sha
                or value
                != {
                    "schema_version": 1,
                    "task_id": request.task_id,
                    "operation_id": request.operation_id,
                    "run_id": request.run_id,
                    "request_fingerprint": request.fingerprint,
                    "approval_id": evidence.approval_id,
                    "patch_sha256": evidence.patch_sha256,
                    "output_manifest_hash": evidence.output_manifest_hash,
                    "base_sha": source.base_sha,
                    "index_tree": self._run(
                        target, "rev-parse", f"{source.base_sha}^{{tree}}"
                    )
                    .decode()
                    .strip(),
                    "git_pointer_sha256": hashlib.sha256(
                        (target / ".git").read_bytes()
                    ).hexdigest(),
                    "workspace": str(target),
                    "handoff_created": False,
                }
            ):
                raise ValueError("apply receipt changed")
            self._check_git(target, source.base_sha, value["index_tree"].encode())
            if (
                self._run(target, "symbolic-ref", "--short", "HEAD").decode().strip()
                != workspace.branch
            ):
                raise ValueError("worktree branch changed")
            output = self._output.inspect(prepared, request, policy)
            if (
                scan_source_tree(
                    target, source.files, git_pointer=(target / ".git").read_bytes()
                )[0]
                != output.manifest.files
            ):
                raise ValueError("applied worktree changed")
            return workspace
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise DomainError(
                "SANDBOX_EVIDENCE_FAILED", "apply evidence is stale"
            ) from error

    def _check_git(self, workspace: Path, base: str, tree: bytes) -> None:
        if (
            self._run(workspace, "rev-parse", "HEAD").strip() != base.encode()
            or self._run(workspace, "write-tree").strip() != tree
        ):
            raise DomainError("SANDBOX_OUTPUT_DRIFT", "HEAD or index changed")

    def _run(self, cwd: Path, *args: str, index: Path | None = None) -> bytes:
        return self._git.run_bytes(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.autocrlf=false",
                *args,
            ],
            cwd=cwd,
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_COUNT": "0",
                "GIT_CONFIG_PARAMETERS": None,
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_CEILING_DIRECTORIES": str(cwd.parent),
                "GIT_INDEX_FILE": str(index) if index is not None else None,
            },
        ).stdout

    @staticmethod
    def _require_stopped_worker(root: Path, request: SandboxRequest) -> None:
        try:
            records = {}
            for name in ("result.json", "cleanup.json", "container-after.json"):
                path = root / "docker-worker" / name
                require_plain_path(path)
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError("oversized Worker record")
                records[name] = json.loads(path.read_bytes())
            result = SandboxResult.model_validate_json(
                json.dumps(records["result.json"])
            )
            accepted = SandboxResult.model_validate_json(
                (root / "sandbox-output/result.json").read_bytes()
            )
            facts = records["container-after.json"]
            if (
                result != accepted
                or records["cleanup.json"] != {"container_removed": True}
                or facts["State"]["Running"]
                or facts["State"]["Status"] != "exited"
                or facts["State"]["ExitCode"] != 0
                or facts["State"].get("OOMKilled")
                or facts["State"].get("Error")
                or facts["Id"] != result.container_id
            ):
                raise ValueError("Worker exit evidence differs")
            DockerSandboxWorker.check_container(
                facts, request, Path(request.execution_directory), request.argv
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise DomainError(
                "SANDBOX_EVIDENCE_FAILED", "confirmed Docker cleanup evidence required"
            ) from error
