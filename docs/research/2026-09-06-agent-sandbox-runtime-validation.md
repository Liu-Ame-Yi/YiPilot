> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# 成熟 Coding Agent 的 Docker 沙箱：运行时就绪与真实验收

**日期：** 2026-09-06\
**范围：** Docker Desktop（Windows 主机 + Linux container mode）下，Yipilot 如何验证沙箱真的可运行；不讨论模型质量或 CI 平台。

## 先给结论

成熟项目把以下四件事分开：

```text
开发机是否有 Docker  ── preflight
可信镜像是否已准备好 ── preparation
实际容器是否按策略启动 ── integration/security test
产品遇到不可用时怎么办 ── fail-closed 或显式 non-sandbox mode
```

它们不会把「单元测试 mock 了 Docker 命令」当成「Docker 沙箱已验证」。OpenHands 的 Docker Runtime 是实际启动 runtime image 后在容器内执行动作的 client-server 架构；SWE-agent 默认也在 Docker container 中运行目标任务；OpenAI Agents SDK 的 `DockerSandboxClient` 创建实际 Docker session，并由 client 删除底层 container。[OpenHands Runtime Architecture](https://docs.openhands.dev/openhands/usage/architecture/runtime) [SWE-agent CLI](https://github.com/SWE-agent/SWE-agent/blob/main/sweagent/run/run_single.py) [OpenAI Docker example](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) [OpenAI session lifecycle](https://openai.github.io/openai-agents-python/ref/testing/sandbox/)

但成熟项目也不会假称 Docker 永远可用。OpenHands 将 Docker client/daemon 不可用显式报错，并要求先用 `docker ps` 成功确认；其 `local` runtime 被明确标为无隔离的开发选项，而不是 Docker 失败后的安全替代品。[OpenHands troubleshooting](https://docs.openhands.dev/openhands/usage/troubleshooting/troubleshooting) [OpenHands runtime source README](https://github.com/OpenHands/OpenHands/blob/main/openhands/runtime/README.md)

**Yipilot 决策：** 对用户触发的 `/apply`、`/verify` 和未来 `/repair`，Docker preflight 或安全策略任一项失败都返回 `SANDBOX_UNAVAILABLE`；只允许测试收集阶段将真实 Docker 测试标记为 skipped。绝不把一次失败降级成 `HOST_TRUSTED` 执行。

## 从项目中学到什么

| 项目 | 真实运行策略 | 可学习点 | 不应照搬之处 |
|---|---|---|---|
| OpenHands | 后端构建/选择 runtime image，启动 Docker container，容器中的 action server 执行 shell、文件和 Python 动作 | Runtime 与控制端分离；有针对 Docker daemon、Docker Desktop socket、挂载权限的故障诊断 | 它支持用户挂载可写宿主目录，并警告 agent 可以修改其挂载内容；Yipilot 不应挂载正式 worktree |
| SWE-agent | 默认 Docker deployment；本地 repo 复制进入 container；推荐将依赖预先做进指定 image | 目标 repo 用 copy-in，而不是依赖宿主运行环境；依赖是 image preparation 的职责 | `post_startup_commands` 可用于实验方便，但离线、安全 Run 不应允许模型在启动后联网装包 |
| OpenAI Agents SDK | Windows 推荐 `DockerSandboxClient` 或 hosted provider；Manifest 定义入口，Docker session 负责真实生命周期 | 路径须 workspace-relative；额外 host path grant 是可信配置；`network_mode="none"` 是显式选项 | SDK 是 beta，且默认网络行为是 Docker 默认值；Yipilot 必须固定为 none，不能依赖默认 |
| Deep Agents | sandbox 是可选 backend，默认可为 none；评测可以选 local Docker 或 hosted sandbox | 控制层与可替换 sandbox backend 分离 | `provider = none` 适合无代码执行的产品；Yipilot 进入执行阶段时不能默认为 none |

来源：[OpenHands runtime](https://docs.openhands.dev/openhands/usage/architecture/runtime) [OpenHands 挂载与安全警告](https://docs.openhands.dev/openhands/usage/v0/runtimes/V0_docker) [SWE-agent 环境配置](https://github.com/SWE-agent/SWE-agent/blob/main/docs/usage/cl_tutorial.md) [OpenAI manifest 与 Windows 建议](https://openai.github.io/openai-agents-python/sandbox/guide/) [OpenAI 无网络 Docker 配置](https://openai.github.io/openai-agents-python/sandbox/clients/) [Deep Agents deploy](https://github.com/langchain-ai/deepagents/blob/main/deepagents-deploy.md)

## Yipilot 的四段运行时流程

```text
0. Operator preparation（允许联网）
   构建或导入 runtime image → 解析并登记 digest

1. Control-plane preflight（不执行仓库代码）
   daemon / Linux mode / image digest / policy → pass or SANDBOX_UNAVAILABLE

2. Sandbox Run（network=none）
   execution copy + registered argv → real Docker container → result

3. Control-plane validation
   inspect 实际 container facts + 重算 baseline/execution diff → evidence / handoff
```

`baseline/` 永远不挂载，只有 `execution/` 可绑定到 `/workspace`。这比 OpenHands 便利优先的可写 host mount 更适合 Yipilot 的受控交付模型；OpenHands 自己也明确说明可写挂载里的文件会被 agent 修改。[OpenHands Docker volumes](https://docs.openhands.dev/openhands/usage/v0/runtimes/V0_docker)

## G0-A：真实 Docker preflight（不改业务代码）

将 preflight 设计成一个**读取 Docker 状态、绝不 build/pull/执行仓库代码**的命令或服务。输出结构化诊断，供 CLI、测试和 evidence 共用。

| 顺序 | 检查 | 失败分类 | 处理 |
|---|---|---|---|
| 1 | `docker version --format ...` 同时取得 Client 与 Server | `DOCKER_CLI_UNAVAILABLE`、`DOCKER_DAEMON_UNAVAILABLE`、`DOCKER_ACCESS_DENIED` | 产品 Run 失败关闭；提示启动 Docker Desktop 或检查当前用户权限 |
| 2 | `docker info --format '{{.OSType}}'` | `DOCKER_NOT_LINUX_MODE` | 拒绝 Windows container mode；本首版只接受 `linux` |
| 3 | `docker image inspect <registered-digest>`，并比对返回 ID/RepoDigest | `SANDBOX_IMAGE_MISSING`、`SANDBOX_IMAGE_DIGEST_MISMATCH` | 不 pull、不以 tag 替代 digest |
| 4 | 在临时无业务数据的 probe container 上验证最小启动 | `SANDBOX_POLICY_UNAVAILABLE` | 检查 image 能用、非 root user、network none 和只读 rootfs 可同时成立 |
| 5 | 清理 probe 并复查 container 不存在 | `SANDBOX_CLEANUP_FAILED` | 不留下未知状态，再允许后续 Run |

OpenHands 采用同样的第一层判断：要求 `docker ps` 在终端成功，否则明确指导启动 Docker Desktop/daemon；这正是 daemon 不存在与“Yipilot Python 代码错误”的分界线。[OpenHands troubleshooting](https://docs.openhands.dev/openhands/usage/troubleshooting/troubleshooting)

在当前机器已见到 named pipe 缺失时，第一步的预期就是 `DOCKER_DAEMON_UNAVAILABLE`。不能直接去修改 Docker ACL、启动服务或构建镜像；先由实际运行 Yipilot 的用户在 Docker Desktop 中启动 Linux containers。启动后才检查是否是第二层 `Access denied`。

## G0-B：镜像准备与登记

把依赖安装与 Agent Run 分离：

```text
operator-approved image build / image import（可联网）
  → local tag 仅作构建别名
  → inspect 解析 sha256 digest
  → control plane 登记 digest + tool versions + Dockerfile source revision
  → ordinary Run 只接受这个 digest，network=none
```

SWE-agent 官方推荐构建包含依赖的 Docker image，再以 image 配置运行；OpenHands 同样从基础 image 构建包含 runtime client 的 runtime image。[SWE-agent custom image](https://github.com/SWE-agent/SWE-agent/blob/main/docs/usage/cl_tutorial.md) [OpenHands image construction](https://docs.openhands.dev/openhands/usage/architecture/runtime)

因此 Yipilot 的 runtime image 至少预装 Python、Git、pytest、ruff、mypy 与项目确定的依赖。普通 Run 中禁止 `pip install`、`apt`、`docker pull`、`docker build`。缺工具是 `SANDBOX_UNAVAILABLE` 或“重新准备镜像”，不是临时开网。

镜像应由**digest**登记，而不是 tag。tag 只说明名字，可能被重新指向；控制面每次 Run 都要记录实际 image ID / RepoDigest。OpenHands 用 lock tag 管理某一基础 image、依赖与自身版本的组合；Yipilot 可以借鉴其“运行时可追溯”的目标，但采用更强的 digest 输入合同。[OpenHands runtime image maintenance](https://docs.openhands.dev/openhands/usage/architecture/runtime)

## G0-C：真实 Docker 集成与安全测试

测试必须分三层，不能由 mock 取代前两层以外的层次：

| 层级 | Docker 是否真实运行 | 该测什么 | 允许 skip 的条件 |
|---|---:|---|---|
| Unit | 否 | argv 白名单、错误映射、manifest/diff/path 校验、Docker 参数构造 | 从不依赖 Docker |
| Integration-security | 是 | daemon→container→inspect→cleanup 的完整 Worker 路径 | 仅在开发机缺 daemon、非 Linux mode 或缺已登记测试 image 时 skip |
| Release/CI acceptance | 是 | 与 Integration 相同的攻击 fixture，加最小代码修改与真实 pytest | CI 必须准备 daemon 与 image；任何 skip 都是失败 |

官方项目的公开样例明确调用真实 Docker client 创建 session，并最终删除 container；它们是实际运行示例，不能以 mock 代替。公开资料未足以证明 OpenHands、SWE-agent 或 OpenAI Agents SDK 的每一项内部 CI 安全测试均为真实 Docker，因此不应把“项目成熟”推论为“其所有 Docker 测试都是真实容器”。[OpenAI Docker runner](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) [OpenAI cleanup contract](https://openai.github.io/openai-agents-python/ref/testing/sandbox/) [OpenHands Docker Runtime](https://docs.openhands.dev/openhands/usage/architecture/runtime)

Yipilot 的 Integration-security 固定采用无害 fixture repository 和预准备 runtime image；不使用模型、不调用 Provider、不运行本仓库业务测试。至少覆盖：

1. **实容器与策略事实：** 从 `docker inspect` 读取 `NetworkMode=none`、只读 rootfs、有效 UID、内存/CPU/PID 限制、cap drop/no-new-privileges；不能只检查传给 Docker SDK 的参数。
2. **网络探针：** 在实容器中访问 DNS/公网必须失败；同时禁止发布端口。OpenAI Docker client 将 `network_mode="none"` 作为唯一支持的显式无网络模式，并拒绝同时暴露端口的组合。[OpenAI Docker network mode](https://openai.github.io/openai-agents-python/sandbox/clients/)
3. **挂载探针：** 在 `/workspace` 创建普通文件后 Control Plane 可见；尝试访问未挂载的 host sentinel、Docker socket、`.git`、Provider settings 均不可行。特别验证 `baseline/` 没有被挂载。
4. **资源与清理：** 一个超时 fixture 启动子进程后超时；Control Plane kill 后 `docker inspect` 证实 container 已退出/删除，且没有遗留 `yipilot-*` container。
5. **可信输出：** execution 变化后由 Control Plane 重新算 diff；故意伪造 Worker 自报 hash 时被拒绝。

Docker 的 mount 与网络只在启动时有效，所以 `docker inspect` 是比 mock 更接近真实安全属性的验证。OpenAI 也将 host-path grants 视为可信配置，不能由模型输出加载；Yipilot 应以此为最低标准，且只给一个 execution 路径。[OpenAI trusted path grants](https://openai.github.io/openai-agents-python/sandbox/guide/)

## 缺 Docker 时：skip 与 fail-closed 的边界

```text
pytest collection on a developer laptop
  Docker daemon/image missing → integration tests skip with明确原因

Yipilot /apply, /verify, /repair
  Docker daemon/image/policy missing → SANDBOX_UNAVAILABLE
  → 不启动 LocalCommandRunner，不写正式 worktree

CI/release gate
  Docker daemon/image/policy missing → FAIL
```

OpenHands 的 `local` runtime 说明了为什么这个边界必要：它可在 Docker 不可用时用于开发，但官方明确它以当前用户权限直接运行、没有隔离。[OpenHands local runtime warning](https://github.com/OpenHands/OpenHands/blob/main/openhands/runtime/README.md) Yipilot 可保留 host-trusted runner 仅给开发者的显式维护命令，但不能把它作为面向用户 Agent 执行的 fallback。

## 建议的后续任务拆分

1. **G0-A：Docker preflight 与诊断。** 只实现上述 1–5 的读取/临时 probe；暂不改 `/apply`、`/verify`。
2. **G0-B：runtime image preparation。** 在 Docker 可用的机器上显式 build/import，登记 digest；不能将 build 藏进 Agent Run。
3. **G0-C：测试基线修复。** 修复重复测试模块名和废弃 `FakeExecutor` 导入；测试替身只留在 `tests/`。
4. **G0-D：真实 Integration-security 验收。** 建立 marker、fixture 与 CI required job；本机可 skip，CI 不可 skip。
5. **M6-SBX：迁移 `/repair`。** 只有 G0-D 通过后，才允许 repair 消费失败的 `sandbox_verification` evidence 并生成下一份真实模型 PatchProposal。

这四个 G0 任务完成前，应将项目状态称为“Docker Sandbox Worker 已实现、未在本机真实验收”，而不能称为“已验证的沙箱执行”。
