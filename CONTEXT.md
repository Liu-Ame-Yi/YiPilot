# Zpilot-Py Domain Context

## Purpose

Zpilot-Py is a local-first, auditable controller for AI-assisted software-delivery work. It coordinates work; it is not itself an unrestricted coding agent.

## Ubiquitous language

| Term | Meaning |
| --- | --- |
| Project | A local directory governed by one `.zpilot/config.yaml` file. |
| Task | A long-lived, user-visible development unit identified by a decimal `task_id`. |
| Stage | A stable business capability, such as `srs_generate` or `code_generate`. |
| Operation | A durable record of one user intent to perform work. It may coordinate one or more runs. |
| Run | One concrete attempt to execute a stage. It has a lifecycle, events and optional checkpoint. |
| Artifact | A versioned output used as business input or evidence, such as an SRS, SDD, patch or report. |
| Evidence | An immutable or append-only record that explains an observed action or result. |
| Harness | Deterministic validation that produces an auditable quality-gate report for a stage; it is not an LLM self-review. |
| Approval | An explicit, durable human decision needed before a high-risk side effect. |
| Workspace | A task-owned Git worktree where an executor may modify code. |
| Adapter | A replaceable implementation of an external-system port. It may be production, fake, recorded-replay or disabled. |
