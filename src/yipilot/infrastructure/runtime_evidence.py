"""Filesystem adapter for Runtime Event and Artifact evidence."""

import json
from collections.abc import Mapping

from yipilot.domain.execution import AgentPlan, ArtifactRef
from yipilot.domain.model import ModelCallRecord, ModelRequest
from yipilot.domain.patch_proposal import PatchProposal
from yipilot.infrastructure.artifact_store import ArtifactRecord, FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal


class FileRuntimeEvidence:
    """Keep Runtime file persistence behind its evidence Interface."""

    def __init__(self, artifacts: FileArtifactStore, journal: JsonlEventJournal) -> None:
        self._artifacts = artifacts
        self._journal = journal

    def append_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        self._journal.append(event_type, payload)

    def write_plan(self, plan: AgentPlan) -> ArtifactRef:
        return _artifact_ref(
            self._artifacts.write_bytes(
                plan.task_id,
                plan.run_id,
                producer="agent_runtime",
                relative_path="artifacts/agent-plan.json",
                content=json.dumps(
                    plan.model_dump(mode="json"),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
                input_fingerprint=plan.instruction_fingerprint,
                operation_id=plan.operation_id,
            )
        )

    def write_model_response(self, request: ModelRequest, content: str) -> ArtifactRef:
        return _artifact_ref(
            self._artifacts.write_bytes(
                request.task_id,
                request.run_id,
                producer="model_port",
                relative_path="artifacts/model-response.json",
                content=json.dumps(
                    {"schema_version": 1, "content": content},
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
                input_fingerprint=request.input_fingerprint,
                operation_id=request.operation_id,
            )
        )

    def write_model_call(self, record: ModelCallRecord) -> None:
        self._artifacts.write_bytes(
            record.task_id,
            record.run_id,
            producer="model_port",
            relative_path="artifacts/model-call.json",
            content=record.model_dump_json(exclude_none=True).encode("utf-8"),
            input_fingerprint=record.input_fingerprint,
            operation_id=record.operation_id,
        )

    def write_patch_proposal(
        self, request: ModelRequest, proposal: PatchProposal
    ) -> ArtifactRef:
        return _artifact_ref(
            self._artifacts.write_bytes(
                request.task_id,
                request.run_id,
                producer="agent_runtime",
                relative_path="artifacts/patch-proposal.json",
                content=proposal.model_dump_json().encode("utf-8"),
                input_fingerprint=request.input_fingerprint,
                operation_id=request.operation_id,
            )
        )

    def verify_input_artifact(self, reference: ArtifactRef) -> ArtifactRef:
        return _artifact_ref(
            self._artifacts.verify(reference.task_id, reference.run_id, reference.artifact_id)
        )


def _artifact_ref(record: ArtifactRecord) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=record.artifact_id,
        task_id=record.task_id,
        operation_id=record.operation_id,
        run_id=record.run_id,
        relative_path=record.relative_path,
        sha256=record.sha256,
    )
