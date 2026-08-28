"""Compatibility facade for the Runtime ModelPort consumer."""

from yipilot.domain.execution import ToolRequest
from yipilot.domain.model import ModelPort, ModelRequest
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.runtime_evidence import FileRuntimeEvidence
from yipilot.runtime.model_planner import ModelPlanner as _ModelPlanner
from yipilot.runtime.ports import RuntimeEvidencePort
from yipilot.security.redaction import SecretRedactor


class ModelPlanner(_ModelPlanner):
    """Accept existing Artifact/Event adapters while Runtime sees one evidence seam."""

    def __init__(
        self,
        port: ModelPort,
        request: ModelRequest,
        base_request: ToolRequest,
        evidence: RuntimeEvidencePort | FileArtifactStore,
        journal: JsonlEventJournal | None = None,
        *,
        redactor: SecretRedactor | None = None,
    ) -> None:
        runtime_evidence: RuntimeEvidencePort
        if journal is not None:
            if not isinstance(evidence, FileArtifactStore):
                raise TypeError("legacy ModelPlanner requires a FileArtifactStore")
            runtime_evidence = FileRuntimeEvidence(evidence, journal)
        else:
            if isinstance(evidence, FileArtifactStore):
                raise TypeError("ModelPlanner requires RuntimeEvidencePort without a journal")
            runtime_evidence = evidence
        super().__init__(port, request, base_request, runtime_evidence, redactor=redactor)


__all__ = ["ModelPlanner"]
