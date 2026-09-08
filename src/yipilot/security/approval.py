"""Application-level approval checks for high-risk controlled tools."""

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    ApprovalRecord,
    ExecutionTrustLevel,
    ToolKind,
    ToolPolicy,
    ToolRequest,
)
from yipilot.persistence.approval_store import FileApprovalStore


def approval_action_type(tool: ToolPolicy, trust_level: ExecutionTrustLevel) -> str:
    """Keep the action label deterministic and bound to tool risk semantics."""
    return f"tool:{tool.kind.value}:{trust_level.value}"


class ApprovalGate:
    """Require a persisted, exact and explicitly confirmed approval."""

    def __init__(self, store: FileApprovalStore) -> None:
        self._store = store

    def require_approved(
        self,
        request: ToolRequest,
        tool: ToolPolicy,
        trust_level: ExecutionTrustLevel,
        *,
        approval_id: str | None,
        confirm_token: str | None,
        policy_revision: str,
    ) -> ApprovalRecord:
        if approval_id is None:
            raise DomainError("APPROVAL_REQUIRED", "high-risk tool execution requires approval")
        try:
            approval = self._store.get(request.task_id, request.run_id, approval_id)
        except DomainError as error:
            if error.code == "APPROVAL_NOT_FOUND":
                raise DomainError(
                    "APPROVAL_REQUIRED", "high-risk tool execution requires approval"
                ) from error
            raise
        if confirm_token != approval.target_fingerprint:
            raise DomainError(
                "APPROVAL_CONFIRMATION_INVALID",
                "confirmation token does not match approval target",
            )
        return approval.require_fresh(
            task_id=request.task_id,
            operation_id=request.operation_id,
            run_id=request.run_id,
            action_type=approval_action_type(tool, trust_level),
            target_fingerprint=request.fingerprint,
            policy_revision=policy_revision,
        )


def requires_approval(tool: ToolPolicy, trust_level: ExecutionTrustLevel) -> bool:
    return (
        trust_level is ExecutionTrustLevel.HOST_TRUSTED
        or tool.kind in {ToolKind.WORKSPACE_WRITE, ToolKind.TEST}
    )
