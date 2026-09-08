> **Historical reference / Superseded — 文档整理：2026-09-07。** 本文保留旧阶段设计与实施记录，不再作为当前待办或产品范围。当前目标见 [产品定义](../../product/vision.md)，技术设计见 [V2](../specs/2026-09-07-yipilot-v2-architecture.md)，实际能力见 [状态页](../../product/status.md)。下文原状态、强制要求及命令仅在原历史范围内成立。

# Yipilot Docker Sandbox Worker：完成基线

**状态：** accepted design，尚未实现\
**日期：** 2026-09-05\
**范围：** Windows 上的 Docker Desktop Linux containers；以完整安全合同和验收结果为完成标准，不按版本编号交付。

## 决策

Yipilot 的不可信代码执行使用 **Docker Sandbox Worker**。Yipilot 主体继续是 Python Control Plane；不实现原生 Windows Token/ACL runner，也不把普通 host `subprocess` 标记为 sandbox。

```text
Python Control Plane (trusted)
  Provider key, documents, Task/Run, Approval, Git, evidence
  -> create baseline and execution source copies
  -> start a policy-fixed Docker container

Docker Sandbox Worker (untrusted execution)
  execution copy only, no .git, no host credentials, no Docker socket
  -> apply patch / use registered tools / run tests
  -> return bounded logs and execution facts

Python Control Plane (trusted)
  -> recompute and validate diff
  -> update task worktree only with verified patch
  -> create Change Handoff
```

The design draws on the Python-agent Docker/remote-runtime pattern and retains the fail-closed policy and per-call evidence lessons from DeepSeek Harness. See [Docker research](../../research/2026-09-05-docker-sandbox-worker.md), [Agent sandbox comparison](../../research/2026-09-04-agent-sandbox-comparison.md), and the [sandbox ADR](../../adr/2026-08-30-sandboxed-agent-execution.md).

## Why the worker receives a copy, not a worktree

The existing Git worktree belongs to the trusted Control Plane. Its `.git` pointer can lead to Yipilot-managed Git metadata, so it must never be bind-mounted into a Worker.

For every execution attempt the Control Plane creates two disjoint directories from the frozen base commit:

```text
runs/<run-id>/baseline/   # not mounted; immutable comparison source
runs/<run-id>/execution/  # the only host path mounted at /workspace
```

The export includes tracked source files only. It does not include `.git`, ignored files, `.env`, a virtual environment, Yipilot configuration, evidence, or user-local credentials. Before container launch the Control Plane rejects reparse points, symbolic links and non-regular output files that it cannot safely represent in a patch.

The Worker may alter `execution`; it cannot see `baseline`. After the container exits, the Control Plane calculates the diff from `baseline` to `execution`, validates it, and only then applies that exact diff to the task-owned worktree with fixed Git arguments.

## SandboxWorker Interface

Expose one deep `SandboxWorker` module to the application layer:

```python
SandboxWorker.execute(request: SandboxRequest) -> SandboxResult
```

`SandboxRequest` is immutable and Run-bound. It contains only:

- Task, Operation, Run and policy revision identifiers;
- canonical execution directory, baseline manifest hash and image digest;
- one registered `argv`, not a host shell string;
- wall-clock, CPU, memory, PID and stdout/stderr budgets;
- the fixed mode `offline_workspace_write`.

`SandboxResult` contains:

- container id/image digest and Docker backend facts;
- `network=none`, effective user, resource limits and outcome;
- exit/cancel/timeout reason and bounded stdout/stderr artifacts;
- output manifest hash and a statement that no fallback occurred.

The module owns Docker argument construction, lifecycle, cancellation, output caps and cleanup. Callers do not construct `docker run` arguments. There is only one implementation now, so no public backend factory is introduced.

## Non-negotiable Docker policy

The Control Plane creates an equivalent fixed invocation; values marked Run-bound come only from `SandboxRequest` after validation.

```text
docker run --rm --name <run-bound-name>
  --network none
  --read-only
  --tmpfs /tmp:rw,noexec,nosuid,size=<budget>
  --cap-drop ALL
  --security-opt no-new-privileges=true
  --pids-limit <budget> --memory <budget> --cpus <budget>
  --user <non-root-runtime-user>
  --workdir /workspace
  --mount type=bind,src=<execution-copy>,dst=/workspace,rw
  --label yipilot.run=<run-id>
  <image@immutable-digest> <registered-argv>
```

It must never add `--privileged`, `--pid=host`, `--ipc=host`, `--device`, published ports, a host network, a Docker socket/named pipe, a broad bind mount, an operator home directory, or a Provider key environment variable. The model never controls the image, mount source, Docker flags or host command.

The runtime image is prepared separately and pinned by digest before a Run. It contains the trusted Python/Git/test toolchain and required dependencies. An offline Worker never runs `pip install`, package manager bootstrap, image build, or registry pull. Missing runtime capability fails closed with `SANDBOX_UNAVAILABLE`.

## How this joins the Yipilot delivery flow

```text
Requirement -> PRD/SRS/SDD/test plan -> Task
  -> selected provider generates a bounded PatchProposal
  -> human approves the exact proposal
  -> Control Plane seeds execution copy
  -> Docker Worker applies/inspects/tests the copy
  -> Control Plane validates diff + Harness evidence
  -> Change Handoff
  -> separately approved commit/push -> Jenkins -> PR -> human review -> merge
```

Document generation, provider calls and Git delivery stay in the Control Plane. They do not execute untrusted repository code. The Worker is for code writes, test imports, build scripts and registered Agent tool commands. Provider credentials are never copied into the image, workspace, environment or container metadata.

## Required behaviour before this is complete

### Runtime readiness

- Preflight confirms a reachable Docker Desktop Linux-container daemon, expected image digest, non-root runtime user and required command availability.
- The preflight performs an actual no-network probe and a temporary workspace write probe using the fixed policy.
- Docker Desktop backend and Enhanced Container Isolation state, when available, are recorded as facts. Their absence cannot be hidden; a deployment policy may require them.
- Any preflight failure returns `SANDBOX_UNAVAILABLE` and starts no repository command.

### Run lifecycle

- Create the two clean source copies and freeze a manifest before the Worker starts.
- Persist the Docker policy, image digest, mount target, command fingerprint and budgets as Run evidence before launch.
- Stream stdout/stderr through a byte-limited collector. On cancel, timeout or output overflow, kill the named container and verify that it has exited.
- Recreate no container automatically with broader permissions. A retry is a new Run with a new workspace and explicit evidence.
- Clean up the execution directory only after evidence has been collected; retained material becomes read-only audit output, not future Worker input.

### Trusted exit handling

- Compare the unmounted baseline with the execution copy; never trust a Worker-provided diff alone.
- Reject changed paths outside the tree, special files, link/reparse escapes, malformed manifests, image/policy drift and unapproved diff changes.
- Only a verified regular-file patch can enter the existing task worktree and Change Handoff flow.
- A failed test, failed policy check, container error, incomplete evidence or partial cleanup never produces a Handoff.

## Completion evidence

The design is complete only when automated tests demonstrate all of the following:

| Case | Required result |
| --- | --- |
| normal patch and fixed pytest/Ruff/mypy | succeeds inside container; host task worktree remains unchanged until trusted apply |
| missing daemon, image, digest or policy preflight | `SANDBOX_UNAVAILABLE`; no command and no host write |
| raw socket, DNS, Git HTTP/SSH, PyPI/npm access | fails with `network=none` |
| read `%USERPROFILE%`, `.ssh`, `.env`, Yipilot settings, `.git`, other Run | unavailable because no path/env is mounted or passed |
| attempt to use Docker socket, privileged flags, host network or broad mount | rejected by Control Plane before Docker launch |
| timeout, output flood, child/grandchild process | container is killed; Run records the reason and bounded artifacts |
| symlink/reparse/special-file or manifest/hash drift | patch rejected; no Handoff |
| container reports success but Controller diff differs | patch rejected; no Handoff |
| apply/verify after a valid result | exact verified diff only; subsequent Jenkins/PR remains separately approved |

## Deliberate scope boundaries

- Docker Desktop is the required execution dependency for this capability. `HOST_TRUSTED` remains a clearly labeled developer diagnostic path, not a fallback.
- Default Docker Desktop/WSL2 container isolation is not described as a guarantee against kernel or Docker-daemon compromise. Enhanced Container Isolation or Hyper-V are deployment hardening options recorded in evidence.
- VM/remote sandbox backends may be added later behind the same `SandboxWorker` Interface only when there is a second real implementation; they are not placeholders in this design.
- Networked dependency preparation is a separate explicitly approved control-plane workflow with a pinned resulting image; it is never folded into an ordinary code Run.
