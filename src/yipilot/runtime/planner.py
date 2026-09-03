"""Restricted plan sources for the Agent Runtime."""

from typing import Protocol

from yipilot.domain.execution import AgentPlan


class PlannerPort(Protocol):
    """Return a pre-structured plan for one already-fingerprinted instruction."""

    def plan(self, *, instruction_fingerprint: str) -> AgentPlan: ...
