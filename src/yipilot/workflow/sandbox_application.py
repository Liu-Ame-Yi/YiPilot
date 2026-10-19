"""Run preparation and evidence loading for the interactive sandbox workflow."""

import hashlib
import shutil
from pathlib import Path
from uuid import uuid4

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    ArgvRule,
    ExecutionPolicy,
    ToolKind,
    ToolPolicy,
    fingerprint_model,
)
from yipilot.domain.patch_proposal import PatchProposal
from yipilot.domain.sandbox import (
    SandboxBudgets,
    SandboxDiffEvidence,
    SandboxMode,
    SandboxRequest,
    SandboxResult,
    SourceManifest,
)
from yipilot.persistence.approval_store import FileApprovalStore
from yipilot.persistence.artifact_store import FileArtifactStore
from yipilot.persistence.filesystem import atomic_write_bytes
from yipilot.persistence.ownership import exclusive_execution
from yipilot.persistence.task_store import FileTaskStore
from yipilot.security.paths import read_evidence, require_plain_path
from yipilot.workspace.docker_worker import DockerSandboxWorker
from yipilot.workspace.git_runner import GitRunner
from yipilot.workspace.sandbox_apply import SandboxApplyService
from yipilot.workspace.sandbox_output import SandboxOutputService
from yipilot.workspace.sandbox_sources import (
    PreparedSources,
    SandboxSourceService,
    scan_source_tree,
)

# A controller-authored, registered argv. The proposal is data, never Python/shell.
# The payload disappears before output collection and cannot collide with source.
_PAYLOAD = "yipilot-approved-patch.diff"
_ARGV = (
    "python",
    "-I",
    "-B",
    "-c",
    (
        "import pathlib,subprocess; "
        "p=pathlib.Path('yipilot-approved-patch.diff'); "
        "r=subprocess.run(['git','-c','core.hooksPath=/dev/null','apply','--no-index',"
        "'--whitespace=nowarn','--','yipilot-approved-patch.diff'],"
        "env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/tmp',"
        "'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'}); "
        "p.unlink(); raise SystemExit(r.returncode)"
    ),
)


def apply_policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        revision="sandbox-cli-apply-v1",
        tools=(
            ToolPolicy(
                tool_id="sandbox-apply-patch",
                kind=ToolKind.WORKSPACE_WRITE,
                executable=_ARGV[0],
                argv_schema=tuple(ArgvRule(literal=value) for value in _ARGV[1:]),
                max_output_bytes=128 * 1024,
                max_timeout_seconds=60,
            ),
        ),
    )


class SandboxCliService:
    def __init__(self, project: Path) -> None:
        self.root = project.resolve(strict=True)
        self.store = FileTaskStore(self.root)
        self.git = GitRunner()

    @exclusive_execution
    def prepare(
        self,
        task_id: str,
        alias: str,
        base_sha: str,
        proposal: PatchProposal,
        image_digest: str,
        *,
        proposal_run_id: str | None = None,
        root_proposal_run_id: str | None = None,
        request_id: str | None = None,
    ) -> SandboxRequest:
        """Freeze reviewable input without executing Docker or repository commands."""
        operation = self.store.create_operation(
            task_id,
            "sandbox_apply",
            f"sandbox-apply:{task_id}:{alias}:{request_id or uuid4()}",
            {
                "alias": alias,
                "base_sha": base_sha,
                "image_digest": image_digest,
                "proposal_sha256": fingerprint_model(proposal),
                **({"proposal_run_id": proposal_run_id} if proposal_run_id else {}),
                **({"root_proposal_run_id": root_proposal_run_id} if root_proposal_run_id else {}),
            },
        )
        run, created = self.store.get_or_create_run(task_id, operation.operation_id, "sandbox_apply")
        if not created:
            return self.load(task_id, run.run_id)[1]
        copies = None
        try:
            copies = SandboxSourceService(self.root, self.git).prepare(
                self.store.get_task(task_id),
                alias,
                run,
                base_sha,
            )
            if any(f.path.casefold() == _PAYLOAD for f in copies.manifest.files) or any(
                p.path.casefold() == _PAYLOAD for p in proposal.patches
            ):
                raise DomainError(
                    "SANDBOX_SOURCE_DENIED", "reserved patch payload path"
                )
            patch = "".join(
                p.unified_diff.rstrip("\n") + "\n" for p in proposal.patches
            )
            atomic_write_bytes(copies.execution / _PAYLOAD, patch.encode())
            files, _ = scan_source_tree(copies.execution, copies.manifest.files)
            seed = SourceManifest.model_validate_json(
                copies.manifest.model_copy(update={"files": files}).model_dump_json()
            )
            root = copies.execution.parent
            atomic_write_bytes(
                root / "input-manifest.json", seed.model_dump_json().encode()
            )
            request = SandboxRequest.model_validate(
                {
                    "task_id": task_id,
                    "operation_id": run.operation_id,
                    "run_id": run.run_id,
                    "policy_revision": apply_policy().revision,
                    "mode": SandboxMode.OFFLINE_WORKSPACE_WRITE,
                    "execution_directory": str(copies.execution),
                    "baseline_manifest_hash": copies.manifest_hash,
                    "input_manifest_hash": fingerprint_model(seed),
                    "image_digest": image_digest,
                    "tool_id": "sandbox-apply-patch",
                    "argv": _ARGV,
                    "budgets": SandboxBudgets(
                        wall_clock_seconds=60,
                        cpu_count=1,
                        memory_bytes=1024**3,
                        pid_limit=128,
                        stdout_bytes=128 * 1024,
                        stderr_bytes=128 * 1024,
                    ),
                },
                context={"policy": apply_policy()},
            )
            atomic_write_bytes(
                root / "sandbox-cli-request.json", request.model_dump_json().encode()
            )
            return request
        except BaseException:
            self.cancel(task_id, run.run_id)
            if copies is not None:
                root = self.store.event_journal_path(run.run_id).parent
                for name in (
                    "execution",
                    "baseline",
                    "source-manifest.json",
                    "input-manifest.json",
                    "sandbox-cli-request.json",
                ):
                    path = root / name
                    require_plain_path(path)
                    path.resolve().relative_to(root.resolve(strict=True))
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink(missing_ok=True)
            raise

    def load(self, task_id: str, run_id: str) -> tuple[PreparedSources, SandboxRequest]:
        run = self.store.get_run(task_id, run_id)
        if run.stage != "sandbox_apply":
            raise DomainError("SANDBOX_ASSOCIATION_INVALID", "not a sandbox apply Run")
        root = self.store.event_journal_path(run_id).parent
        try:
            request = SandboxRequest.model_validate_json(
                read_evidence(root / "sandbox-cli-request.json"),
                context={"policy": apply_policy()},
            )
            manifest = SourceManifest.model_validate_json(
                read_evidence(root / "source-manifest.json")
            )
            if (request.task_id, request.operation_id, request.run_id) != (
                task_id,
                run.operation_id,
                run_id,
            ) or Path(request.execution_directory) != root / "execution":
                raise ValueError("request association changed")
            copies = PreparedSources(
                root / "baseline",
                root / "execution",
                root / "source-manifest.json",
                hashlib.sha256(read_evidence(root / "source-manifest.json")).hexdigest(),
                manifest,
            )
            SandboxSourceService(self.root, self.git).validate(copies)
            return copies, request
        except (OSError, ValueError) as error:
            raise DomainError(
                "SANDBOX_EVIDENCE_FAILED", "invalid apply inputs"
            ) from error

    @exclusive_execution
    def execute(self, request: SandboxRequest, approval_id: str) -> SandboxResult:
        copies, current = self.load(request.task_id, request.run_id)
        if current != request:
            raise DomainError("SANDBOX_OUTPUT_DRIFT", "apply request changed")
        approval = FileApprovalStore(self.store).get(
            request.task_id, request.run_id, approval_id
        )
        approval.require_fresh(
            task_id=request.task_id,
            operation_id=request.operation_id,
            run_id=request.run_id,
            action_type="sandbox_execution",
            target_fingerprint=request.fingerprint,
            policy_revision=request.policy_revision,
        )
        if approval.simulation_only:
            raise DomainError("SANDBOX_OUTPUT_UNAPPROVED", "human approval required")
        if self.store.get_run(request.task_id, request.run_id).status.value != "pending":
            raise DomainError("EXTERNAL_RESULT_UNCERTAIN", "existing execution must be reconciled; do not restart")
        self.store.set_run_status(request.run_id, "running")
        try:
            seed = SourceManifest.model_validate_json(
                read_evidence(copies.execution.parent / "input-manifest.json")
            )
            result = DockerSandboxWorker(
                self.root, apply_policy(), copies, input_manifest=seed
            ).execute(request)
            if result.exit_code != 0 or result.reason != "exited":
                raise DomainError("SANDBOX_OUTPUT_INVALID", "patch Worker failed")
            return result
        except BaseException as error:
            self._fail(request, error)
            raise

    def accepted(
        self, task_id: str, run_id: str
    ) -> tuple[PreparedSources, SandboxRequest, SandboxDiffEvidence]:
        copies, request = self.load(task_id, run_id)
        run = self.store.get_run(task_id, run_id)
        if run.legacy:
            raise DomainError("LEGACY_EVIDENCE", "legacy execution is historical only")
        artifacts = FileArtifactStore(self.store)
        for identifier in run.result_artifact_ids:
            artifacts.verify(task_id, run_id, identifier)
        try:
            evidence = SandboxDiffEvidence.model_validate_json(
                read_evidence(copies.execution.parent / "sandbox-output/evidence.json")
            )
        except (OSError, ValueError) as error:
            raise DomainError(
                "SANDBOX_EVIDENCE_FAILED", "accepted output missing"
            ) from error
        SandboxApplyService(self.root, self.git).verify(
            copies, request, apply_policy(), evidence
        )
        return copies, request, evidence

    @exclusive_execution
    def apply(
        self,
        request: SandboxRequest,
        result: SandboxResult,
        patch_hash: str,
        approval_id: str,
    ) -> None:
        copies, current = self.load(request.task_id, request.run_id)
        if current != request:
            raise DomainError("SANDBOX_OUTPUT_DRIFT", "apply request changed")
        try:
            evidence = SandboxOutputService(self.root, self.git).collect(
                copies,
                request,
                apply_policy(),
                result,
                approved_patch_sha256=patch_hash,
                approval_id=approval_id,
            )
            SandboxApplyService(self.root, self.git).apply(
                copies, request, apply_policy(), evidence
            )
            artifacts = FileArtifactStore(self.store)
            for relative in ("docker-worker/result.json", "docker-worker/container-after.json", "docker-worker/cleanup.json",
                             "sandbox-output/evidence.json", "sandbox-output/patch.diff", "sandbox-apply/result.json"):
                artifacts.register(request.task_id, request.run_id, producer="sandbox_apply",
                    relative_path=relative, input_fingerprint=request.fingerprint)
            self.store.set_run_status(request.run_id, "succeeded")
        except BaseException as error:
            self._fail(request, error)
            raise

    def _fail(self, request: SandboxRequest, error: BaseException) -> None:
        if isinstance(error, KeyboardInterrupt):
            self.cancel(request.task_id, request.run_id)
        elif (
            self.store.get_run(request.task_id, request.run_id).status.value
            == "running"
        ):
            self.store.set_run_status(
                request.run_id,
                "failed",
                error_code=error.code
                if isinstance(error, DomainError)
                else "SANDBOX_EVIDENCE_FAILED",
            )

    def cancel(self, task_id: str, run_id: str) -> None:
        run = self.store.get_run(task_id, run_id)
        if run.status.value in {"pending", "running"}:
            self.store.set_run_status(
                run_id, "cancelled", error_code="SANDBOX_CANCELLED"
            )


def inspect_sandbox(project: Path | None, image_digest: str | None) -> dict[str, object]:
    """Resolve configured image before the workspace preflight; no host fallback."""
    if image_digest is None and project is not None:
        image_digest = FileTaskStore(project).get_project().sandbox_image_digest
    if image_digest is None:
        raise DomainError("SANDBOX_IMAGE_NOT_REGISTERED",
            "register an immutable image with `yipilot sandbox register --project <path> --image-digest <sha256:...>`")
    return DockerSandboxWorker.doctor(image_digest)


def register_sandbox_image(project: Path, image_digest: str) -> dict[str, str]:
    store = FileTaskStore(project)
    store.initialize_project()
    return {"image_digest": store.set_sandbox_image_digest(image_digest).sandbox_image_digest or ""}
