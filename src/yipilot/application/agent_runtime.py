"""Control-plane compatibility facade for the Agent Runtime Module."""

from dataclasses import replace
from pathlib import Path
from typing import Any

from yipilot.application.runtime_adapters import RegistryToolGateway
from yipilot.domain.errors import DomainError
from yipilot.domain.models import RunStatus
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.runtime_evidence import FileRuntimeEvidence
from yipilot.runtime.agent_runtime import AgentRuntime as _AgentRuntime
from yipilot.runtime.agent_runtime import AgentRuntimeResult, ApprovalInfo
from yipilot.runtime.ports import RuntimeEvidencePort, ToolExecutionPort


class AgentRuntime:
    """Start and terminate Runs in the control plane around the pure Runtime loop."""

    _legacy_store: Any
    _legacy_planner: Any
    _legacy_tools: ToolExecutionPort | None
    _store: Any
    _evidence: RuntimeEvidencePort
    _runtime: _AgentRuntime | None

    def __init__(self, *args: Any) -> None:
        legacy = len(args) == 4 and isinstance(args[0], (Path, str))
        if legacy:
            project_root, store, planner, registry = args
            tools = RegistryToolGateway(project_root, registry)
            self._legacy_store = store
            self._legacy_planner = planner
            self._legacy_tools = tools
            self._runtime = None
            return
        store, planner, tools, evidence = args
        self._legacy_store = None
        self._legacy_planner = None
        self._legacy_tools = None
        self._store = store
        self._evidence = evidence
        self._runtime = _AgentRuntime(store, planner, tools, evidence)

    def execute(self, *args: Any, **kwargs: Any) -> AgentRuntimeResult:
        if self._runtime is None:
            assert self._legacy_store is not None
            assert self._legacy_planner is not None
            assert self._legacy_tools is not None
            task_id, _operation_id, run_id, *_ = args
            run = self._legacy_store.get_run(task_id, run_id)
            evidence = FileRuntimeEvidence(
                FileArtifactStore(self._legacy_store),
                JsonlEventJournal(
                    self._legacy_store.event_journal_path(run.run_id),
                    task_id=run.task_id,
                    run_id=run.run_id,
                    stage=run.stage,
                ),
            )
            runtime = _AgentRuntime(
                self._legacy_store, self._legacy_planner, self._legacy_tools, evidence
            )
            return self._execute_and_finish(runtime, self._legacy_store, evidence, *args, **kwargs)
        return self._execute_and_finish(self._runtime, self._store, self._evidence, *args, **kwargs)

    @staticmethod
    def _execute_and_finish(
        runtime: _AgentRuntime,
        store: Any,
        evidence: RuntimeEvidencePort,
        *args: Any,
        **kwargs: Any,
    ) -> AgentRuntimeResult:
        task_id, _operation_id, run_id, *_ = args
        run = store.get_run(task_id, run_id)
        if run.status is not RunStatus.PENDING:
            raise DomainError("RUN_NOT_PENDING", "agent runtime only starts pending runs")
        store.set_run_status(run_id, RunStatus.RUNNING)
        evidence.append_event("run.started", {})
        result = runtime.execute(*args, **kwargs)
        completed = store.set_run_status(
            run_id, result.terminal_status, error_code=result.error_code
        )
        payload = {"error_code": result.error_code} if result.error_code else {}
        evidence.append_event(f"run.{result.terminal_status.value}", payload)
        return replace(result, run=completed)


__all__ = ["AgentRuntime", "AgentRuntimeResult", "ApprovalInfo"]
