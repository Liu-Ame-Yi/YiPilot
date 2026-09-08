> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# Yipilot Docker Sandbox Worker 调研与决策依据

**日期：** 2026-09-05\
**结论：** Yipilot 在 Windows 上将 Docker Desktop 的 Linux container 作为 Sandbox Worker 的执行边界。Python 保持为 Control Plane；不新增 Rust/C++/C# runner。

## 结论

Python Agent 的成熟实现通常不自行重写 Windows token/ACL sandbox，而是把不可信命令交给容器或远程运行时：OpenHands 使用 Docker Runtime；SWE-agent 将目标仓库复制进 Docker 环境；OpenAI Agents SDK 的 Python 文档建议 Windows 使用 Docker 或 hosted sandbox；LangChain Deep Agents 将 sandbox 作为可替换的 backend。[OpenHands runtime](https://docs.openhands.dev/openhands/usage/architecture/runtime) [SWE-agent environment](https://github.com/SWE-agent/SWE-agent/blob/main/docs/usage/cl_tutorial.md) [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/sandbox/guide/) [Deep Agents](https://docs.langchain.com/oss/python/deepagents/sandboxes)

这与 Yipilot 的 Python 架构匹配：Control Plane 保留 Provider key、Task/Run、Approval、Git 和 evidence；Docker container 只接收一份本次 Run 的无 `.git` 源码副本、固定的资源与网络政策、以及已经登记的 `argv`。它不是向 container 挂载原 checkout，更不允许 container 获得 Docker socket。

## Windows 与 Docker Desktop 的实际边界

Docker Desktop 的 Linux containers 运行在定制 Linux VM 中；container 只能访问 Docker Desktop 配置共享且被显式 bind mount 的宿主机路径。若启用 Enhanced Container Isolation，container 还会得到独立 Linux user namespace。但 Docker 也明确指出：Docker daemon 是高权限控制面，只有可信的 Control Plane 能调用它；如果把 Docker socket 或任意宿主目录交给 agent，container 可以据此获得宿主能力。[Docker Desktop container isolation](https://docs.docker.com/security/faqs/containers/) [Docker daemon security](https://docs.docker.com/engine/security/)

因此 Yipilot 的安全结论不是“Docker 自动安全”，而是以下组合同时成立：

1. Linux container mode；不能接受 Windows process container 作为默认 Worker。
2. Control Plane 只 bind mount `runs/<run-id>/execution`，不能 mount 项目根、用户目录、原 checkout、`.yipilot` 控制目录、`.git` 或 Docker socket/named pipe。
3. 执行 workspace 是独立副本；未挂载的 `baseline` 保留在 Control Plane 一侧，用来重算 diff。
4. `network=none`；OpenAI Agents SDK 的 Docker client 同样将此作为明确的无网络配置，而不是删除代理环境变量。[OpenAI Docker sandbox networking](https://openai.github.io/openai-agents-python/sandbox/clients/)
5. 运行时 root filesystem read-only；只给 workspace bind mount 和 private tmpfs 写能力；删除 Linux capabilities，设置 no-new-privileges，不发布端口，不接设备，不使用 privileged/pid-host/ipc-host。
6. 用 Docker 的 CPU、memory、PID 限制和 Control Plane watchdog 管住资源；超时或取消以 container id 执行 kill，再验证 container 已退出。Docker 将 cgroup 作为资源限制机制，但它不替代文件和网络隔离。[Docker Engine security](https://docs.docker.com/engine/security/)

Docker Desktop 在 Windows 默认可以使用 WSL 2。WSL 2 能提供 Linux kernel 与良好兼容性，但 Docker 说明 WSL 2 仍与 Windows 高度互操作；更高部署隔离可选择 Hyper-V backend 或 Enhanced Container Isolation。Yipilot 必须将 backend/ECI 状态写进 evidence，不能把普通 Docker Desktop 宣称为不可攻破的 VM。[Docker Desktop WSL 2 security](https://docs.docker.com/desktop/features/wsl/) [ECI limitations](https://docs.docker.com/enterprise/security/hardened-desktop/enhanced-container-isolation/limitations/)

## 运行合同

```text
Control Plane
  base commit -> baseline copy (not mounted)
              -> execution copy (the only bind mount)
  approved model patch / registered argv
              -> DockerSandboxWorker

DockerSandboxWorker
  image pinned by digest
  /workspace = execution copy, read-write
  /tmp       = private tmpfs
  rootfs     = read-only
  network    = none
  credentials / Docker socket / .git = absent
              -> stdout/stderr, exit status, resource facts

Control Plane
  baseline vs execution copy -> validated patch + manifest
  validation passed -> task worktree / Change Handoff
```

An image tag is mutable. The Control Plane may let an operator choose a local image tag during preparation, but before a Run it must resolve and persist the immutable image digest. Runtime dependency installation is not a Worker action: network-disabled runs use a prepared image with the required Python/Git/test tools already present. A missing image, daemon, digest, preflight, or policy check is `SANDBOX_UNAVAILABLE`, not a reason to use `HOST_TRUSTED`.

## Product implications

- PRD/SRS/SDD generation, provider calls, Approval, Git delivery and Jenkins/PR integration remain outside the container. They are control-plane actions, not arbitrary-code execution.
- Model-created code, repository hooks, test imports, shell tools and build commands run only in the container.
- A model may request registered `argv` tools during an Agent loop, but it never receives Docker CLI access or a shell command running on the host.
- The container output is untrusted. The Control Plane rejects special files, reparse/link escapes, paths outside the execution tree, manifest/hash drift and any diff that cannot be represented as approved regular-file changes.

## Current-machine preflight observation

On 2026-09-05 the local `docker` CLI reported version `29.2.1`, but the daemon endpoint `npipe:////./pipe/docker_engine` was unavailable to the current process. This does not change the design: a real implementation must call `docker version`/`docker info` during preflight and fail closed until the Linux-container daemon is reachable. No Docker state was changed during this observation.
