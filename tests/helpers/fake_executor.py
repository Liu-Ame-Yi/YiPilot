"""Deterministic executor used only by tests; production has no fake path."""

from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

from yipilot.domain.execution import (
    ArtifactRef,
    ExecutionTrustLevel,
    ToolChangeSummary,
    ToolPolicy,
    ToolRequest,
    ToolResult,
    ToolRunStatus,
)
from yipilot.persistence.artifact_store import FileArtifactStore
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.tool_run_store import FileToolRunStore


class FakeExecutor:
    """Return test-provided tool output without starting a subprocess."""

    trust_level = ExecutionTrustLevel.FAKE

    def __init__(
        self,
        artifact_store: FileArtifactStore,
        journal: JsonlEventJournal,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int | None = 0,
        status: ToolRunStatus | None = None,
    ) -> None:
        self._artifacts = artifact_store
        self._journal = journal
        self._stdout = stdout
        self._stderr = stderr
        self._exit_code = exit_code
        self._status = status

    def execute(
        self,
        request: ToolRequest,
        tool: ToolPolicy,
        *,
        cwd: Path,
        cancel_event: Event | None = None,
        **_: object,
    ) -> ToolResult:
        del tool, cwd
        run_id = str(uuid4())
        started_at = datetime.now(UTC)
        self._journal.append("tool.started", {"tool_run_id": run_id, "tool_id": request.tool_id})
        stdout, stderr = self._stdout, self._stderr
        status = self._status or (
            ToolRunStatus.CANCELLED
            if cancel_event is not None and cancel_event.is_set()
            else ToolRunStatus.SUCCEEDED
            if self._exit_code == 0
            else ToolRunStatus.FAILED
        )
        if len(stdout) + len(stderr) > request.output_budget_bytes:
            stdout = stdout[: request.output_budget_bytes]
            stderr = stderr[: max(request.output_budget_bytes - len(stdout), 0)]
            status = ToolRunStatus.FAILED
            stderr += b"YIPILOT_OUTPUT_LIMIT_EXCEEDED\n"
        result = ToolResult(
            tool_run_id=run_id,
            task_id=request.task_id,
            operation_id=request.operation_id,
            run_id=request.run_id,
            request_fingerprint=request.fingerprint,
            argv=request.argv,
            status=status,
            started_at=started_at,
            ended_at=datetime.now(UTC),
            exit_code=self._exit_code,
            stdout_artifact_ref=self._write(request, run_id, "stdout", stdout),
            stderr_artifact_ref=self._write(request, run_id, "stderr", stderr),
            change_summary=ToolChangeSummary(changed_file_count=0),
            trust_level=self.trust_level,
        )
        FileToolRunStore(self._artifacts.task_store).save(result)
        self._journal.append(
            "tool.completed",
            {
                "tool_run_id": result.tool_run_id,
                "tool_id": request.tool_id,
                "status": result.status.value,
                "exit_code": result.exit_code,
                "stdout_artifact_ref": result.stdout_artifact_ref.model_dump(mode="json"),
                "stderr_artifact_ref": result.stderr_artifact_ref.model_dump(mode="json"),
            },
        )
        return result

    def _write(
        self, request: ToolRequest, tool_run_id: str, stream: str, content: bytes
    ) -> ArtifactRef:
        record = self._artifacts.write_bytes(
            request.task_id,
            request.run_id,
            producer="test_fake_executor",
            relative_path=f"artifacts/tool-runs/{tool_run_id}/{stream}.bin",
            content=content,
            input_fingerprint=request.fingerprint,
            operation_id=request.operation_id,
        )
        return ArtifactRef(
            artifact_id=record.artifact_id,
            task_id=record.task_id,
            operation_id=record.operation_id,
            run_id=record.run_id,
            relative_path=record.relative_path,
            sha256=record.sha256,
        )
