"""The one built-in offline replay-model fixture used by the code CLI."""

import json

from yipilot.application.model_planner import ModelPlanner
from yipilot.domain.execution import ToolRequest
from yipilot.domain.model import (
    ModelChunk,
    ModelRequest,
    ModelResponse,
    model_request_fingerprint,
)
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.model_port import RecordedReplayModelPort
from yipilot.infrastructure.task_store import RunRecord

REPLAY_MODEL_INSTRUCTION = "yipilot replay model diagnostic"


def replay_model_planner(
    run: RunRecord,
    instruction: str,
    base_request: ToolRequest,
    artifacts: FileArtifactStore,
    journal: JsonlEventJournal,
) -> ModelPlanner:
    """Return an exact replay fixture or a deterministic no-match port."""
    fingerprint = model_request_fingerprint(instruction)
    request = ModelRequest(
        task_id=run.task_id,
        operation_id=run.operation_id,
        run_id=run.run_id,
        provider_profile_name="offline-replay",
        provider_type="openai_compatible",
        model="recorded-model",
        profile_revision=1,
        timeout_seconds=1,
        output_budget_bytes=1024,
        input_fingerprint=fingerprint,
        user_instruction=instruction,
    )
    response = ModelResponse(
        chunks=(
            ModelChunk(
                content=json.dumps(
                    {
                        "schema_version": 1,
                        "steps": [
                            {"tool_id": "fake-diagnostic", "args": {"scope": "workspace"}}
                        ],
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
        )
    )
    responses = {fingerprint: response} if instruction == REPLAY_MODEL_INSTRUCTION else {}
    return ModelPlanner(
        RecordedReplayModelPort("builtin-model-replay-v1", responses),
        request,
        base_request,
        artifacts,
        journal,
    )
