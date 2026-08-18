"""Deterministic planners for the offline built-in Agent runtime."""

from collections.abc import Mapping
from typing import Protocol

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import AgentPlan


class PlannerPort(Protocol):
    """Return a pre-structured plan for one already-fingerprinted instruction."""

    def plan(self, *, instruction_fingerprint: str) -> AgentPlan: ...


class FakePlanner:
    """Return the exact plan supplied by a test; it never invents a plan."""

    def __init__(self, plan: AgentPlan) -> None:
        self._plan = plan

    def plan(self, *, instruction_fingerprint: str) -> AgentPlan:
        del instruction_fingerprint
        return self._plan


class RecordedReplayPlanner:
    """Replay versioned, explicit plans keyed by instruction fingerprint."""

    def __init__(self, version: str, plans: Mapping[str, AgentPlan]) -> None:
        if not version:
            raise ValueError("replay version is required")
        self.version = version
        self._plans = dict(plans)

    def plan(self, *, instruction_fingerprint: str) -> AgentPlan:
        try:
            return self._plans[instruction_fingerprint]
        except KeyError as error:
            raise DomainError("REPLAY_PLAN_NOT_FOUND", "no recorded plan matches this input") from error
