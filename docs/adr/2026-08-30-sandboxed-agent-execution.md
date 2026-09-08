> **历史决策适用说明 — 文档整理：2026-09-07。** 保留当时决策及理由；accepted 不表示所有实现完成，也不覆盖新的产品范围。与 [V2 提案](../superpowers/specs/2026-09-07-yipilot-v2-architecture.md) 不同的实现按迁移方案处理，现状查 [状态页](../product/status.md)。

# ADR: Require Docker-sandboxed execution before real AI worktree writes

- Status: accepted
- Date: 2026-08-30

## Context

M0–M4 establish Yipilot's local control plane, task-owned worktrees, approval
records, evidence, and offline model paths. M5 introduces the first real model
driven write to a Python worktree. A `host_trusted` preview would perform that
write with the launching user's authority. A prompt, a typed plan, or a Python
path check alone cannot protect host credentials, other worktrees, Git
metadata, or the operating system if the model or an invoked process escapes
the intended boundary.

The M5–M8 specification left open whether a sandbox must precede real writes.
The Pi Agent research note confirms that project trust and in-process tool
permissions are not a full sandbox, and its optional macOS/Linux Bash sandbox
does not provide a reusable complete boundary for Yipilot on Windows. See
[Pi Agent sandboxing research](../research/2026-08-30-pi-agent-sandboxing.md).

## Decision

Yipilot must use a sandboxed Sandbox Worker before it ships any real AI-driven
file mutation. It will not ship `host_trusted` autonomous writing as an M5
delivery mode.

The trusted Control Plane remains outside the Sandbox Worker. It owns the
Provider request and long-lived secret, Task/Operation/Run state, policy,
Approval, Git evidence collection, Harness decisions, and all commit, push,
CI, PR, and merge decisions. A Sandbox Worker receives only the Run-bound,
policy-approved inputs necessary for one execution attempt.

On Windows, the Sandbox Worker is a Docker Desktop **Linux container**. The
Control Plane creates a base-commit `baseline` copy that is never mounted and
a separate `execution` copy that is the sole bind mount at `/workspace`. The
container runs with network disabled, a read-only root filesystem, a private
tmpfs, dropped capabilities, no-new-privileges, a non-root runtime user, and
CPU/memory/PID/output budgets. It receives neither the Provider credential nor
the Docker socket/named pipe. The image is resolved to an immutable digest
before execution; image build, package installation and registry access are
not Worker actions. The complete Docker policy and acceptance criteria are in
[Docker Sandbox Worker](../superpowers/specs/2026-09-04-yipilot-windows-sandbox-worker.md).

The first sandbox implementation must enforce these invariants at an operating
system, container, VM, or equivalent isolation layer:

- One Run receives only its task worktree (or an isolated copy of it) and the
  exact declared inputs. The source checkout, other worktrees, user profile,
  SSH and cloud credentials, and unrelated host paths are unavailable.
- `.git` metadata and Yipilot control/evidence state are hard-denied to the
  worker. The worker cannot alter approvals, policies, evidence, or Git refs.
- Network access is denied by default. Provider calls remain in the Control
  Plane; the worker never receives a long-lived Provider credential.
- The worker may perform only registered, typed file actions and fixed
  verification argv. Model output is never executed as an arbitrary shell
  command.
- Path escape through `..`, symbolic links, reparse points, mount points, or
  child processes is denied by the enforcement layer, not only by application
  code.
- Worker output is untrusted. The Control Plane collects and validates the
  patch/diff, stdout/stderr, exit status, hashes, and current Git evidence
  before it can produce a Change Handoff.
- Sandbox setup, policy loading, path resolution, evidence collection, or
  worker startup failure is fail-closed: the Run cannot succeed.

Docker Desktop is a dependency of this execution capability. A missing daemon,
Linux-container mode, image digest, policy preflight, or evidence collection
is `SANDBOX_UNAVAILABLE`; Yipilot must not retry on `HOST_TRUSTED`. Docker
Desktop/WSL2 backend and Enhanced Container Isolation facts are captured as
evidence rather than treated as invisible implementation detail.

## Consequences

Real Provider connectivity can be tested independently now, but a Provider
must not be connected to a real write path until the Docker Sandbox Worker
exists and has passed its security fixtures. The required execution flow is:

```text
Provider request in Control Plane
  -> typed plan validation
  -> Run-scoped Docker Sandbox Worker patch/test
  -> trusted evidence validation
  -> fixed verification
  -> Change Handoff
```

The first sandbox implementation must include adversarial tests for path and
link escapes, `.git` and credential reads, unapproved writes, arbitrary command
injection, network access, child-process escape, initialization failure, and
evidence-collection failure.

This adds Docker Desktop and prepared runtime-image requirements. In return,
Yipilot can make a meaningful safety claim about its real code-writing boundary
without delegating host authority to the model or relying on a third-party
coding agent at runtime.

## Alternatives rejected

- **`host_trusted` preview before sandbox:** rejected as a delivered real-write
  mode because it grants the worker the launching user's authority.
- **Prompt-only or Runtime-only restriction:** rejected because process and
  filesystem escapes are outside an application-level policy's authority.
- **Embedding or depending on Pi Agent's sandbox:** rejected because Pi is not
  a required runtime dependency, its core has no built-in sandbox, and its
  optional backend does not supply the required Windows-wide boundary.
- **Native Windows token/ACL runner as the current execution path:** deferred.
  It is a more complex platform-specific implementation than a Docker Desktop
  worker and is not needed while Docker is the declared execution dependency.
