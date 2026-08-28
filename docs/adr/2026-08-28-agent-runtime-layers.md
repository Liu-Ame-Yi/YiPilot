# ADR: Agent Runtime three-layer seams

- Status: accepted
- Date: 2026-08-28

## Context

The existing Runtime directly constructed file Artifact/Event stores and called a concrete ToolRegistry. That made the offline model loop harder to replace without reaching into filesystem implementations.

## Decision

The control plane continues to create Operations and Runs, choose the already-fixed input and policy, and compose dependencies. The `runtime` Module owns model-stream consumption, restricted plan parsing, typed `ToolRequest` orchestration and terminal Run decisions. It depends only on its `RunStorePort`, `RuntimeEvidencePort` and `ToolExecutionPort` Interfaces plus domain `ModelPort`.

`FileRuntimeEvidence` adapts existing Artifact/Event implementations; `RegistryToolGateway` adapts the existing ToolRegistry and binds the project root. Provider, SecretStore, subprocess, Git and filesystem details remain Adapter concerns. The legacy application import paths remain compatibility facades.

## Consequences

No provider/model selection moves into Runtime. All side effects still traverse ToolRegistry, Approval, Artifact/Event evidence and Harness before a Run can succeed; `prepare-only` is unchanged. New Adapter variants need only satisfy the narrow Interface, and ordinary tests can use fake/replay ModelPort and fake tool adapters without network or real credentials.
