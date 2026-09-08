> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# Windows Docker Desktop pipe 与受限 Agent 进程

**范围：** 解释本机已由 `liuyi` 成功连接 Docker Desktop/Linux engine、但 Codex 的 `CodexSandboxOffline` 进程被 `npipe:////./pipe/docker_engine` 拒绝的差异；只补充既有 Docker sandbox 调研，不重做运行时设计。

## 已确认的事实

- 普通交互式 PowerShell 已返回 Docker Desktop server `29.2.1`，且 `docker info --format '{{.OSType}}'` 为 `linux`。这证明 daemon 与 Linux mode 正常；不是镜像、WSL 或 Yipilot Python 的故障。
- Codex 受限进程可看见 `docker_engine` pipe，但 Docker CLI 返回 `permission denied`。这是**当前调用者的 Windows token 不获 pipe 授权**，不是“Docker Desktop 没有启动”。
- Docker Desktop 官方说明：Windows Docker CLI 的默认 endpoint 就是该 named pipe；Desktop 创建的非特权 named pipe 只授予启动 Desktop 的用户、`Administrators` 和 `LOCALSYSTEM`。因此，交互用户可用并不蕴含受限/非交互身份也可用。[Docker CLI endpoints](https://docs.docker.com/reference/cli/docker/) [Docker Desktop Windows permissions](https://docs.docker.com/desktop/setup/install/windows-permission-requirements/)
- `docker-users` 保护的是特权 helper 的 `dockerBackendV2` pipe；官方也警告 Docker daemon socket 的访问相当敏感，不能把它当作普通应用权限来放宽。[Docker Desktop Windows permissions](https://docs.docker.com/desktop/setup/install/windows-permission-requirements/) [Docker daemon socket protection](https://docs.docker.com/engine/security/protect-access/)

**推论（基于上述事实和本次实际错误）：** `CodexSandboxOffline` 是 Codex 为工具调用提供的受限身份，不等同于登录的 `liuyi` token。为让它访问 Docker 而修改 pipe ACL、给该身份管理员/`docker-users` 权限，等于把一个能创建特权容器的控制面交给受限执行环境；这会破坏 Codex 的隔离边界，且不是 Yipilot 应要求的安装步骤。

## 正确的本机真实验收路径

在 **`liuyi` 的普通交互 PowerShell** 执行 Yipilot，而不是在 Codex 的受限终端执行。先确认同一 token 的最小前置条件：

```powershell
docker version --format '{{json .Server}}'
docker info --format '{{.OSType}}'
```

两项已在本机通过。接着完成 G0-B 的显式 image preparation（允许单独 build/import；不要把 build/pull 藏进 Agent Run），取得 image ID，再在同一窗口运行 doctor：

```powershell
Set-Location D:\ly112233\agent
docker build --pull=false --tag yipilot-runtime:local .
$digest = docker image inspect yipilot-runtime:local --format '{{.Id}}'
.\.venv\Scripts\yipilot.exe sandbox doctor --image-digest $digest --output json
```

`doctor` 会实际创建并删除一个短生命周期 probe container；它不执行仓库源码，也不会 build/pull。只有返回 0，才能称为该交互环境中的真实 Docker policy/cleanup 已验收。若 Dockerfile build 需要联网，应将其作为可审计的 G0-B image preparation，而非在 sandbox run 中临时联网安装。

不要通过 Docker Desktop 的“Expose daemon on `tcp://localhost:2375` without TLS”、`DOCKER_HOST` 或 raw socket 绕过 pipe。Docker 官方警告未用 TLS 开放远程 API 会暴露宿主访问；并且 Docker Desktop 曾修复 Windows raw endpoint 绕过 Enhanced Container Isolation 的漏洞。[Docker remote access](https://docs.docker.com/engine/daemon/remote-access/) [Docker Desktop release notes](https://docs.docker.com/desktop/release-notes/)

若必须让自动化运行真实容器，使用一个由 `liuyi` 明确启动、拥有 Docker 访问权的受信本地 runner/CI job，最小授权运行 `doctor` 与 integration tests；其输出作为 artifact 回传给 Codex。不要把 Docker daemon 权限扩展给 Codex sandbox 本身。

## Python coding-agent 的已验证处理方式

以下是**来源事实**，不是“它们遇到了完全相同的 Codex token 问题”的断言：

| 项目 | 官方实现/文档事实 | Docker 不可用时的边界 |
| --- | --- | --- |
| OpenAI Agents SDK (Python) | Docker sandbox client 创建真实 Docker session；create 失败会尽力删除已创建的 container/volume 后重新抛出异常。Windows 指南建议 Docker client 或 hosted provider。 | Docker 是一个显式 sandbox provider，不是自动的宿主机 fallback；Docker 不可达时应选择已配置的 hosted provider，或停止执行。[Docker client](https://openai.github.io/openai-agents-python/sandbox/clients/) [create/cleanup source](https://github.com/openai/openai-agents-python/blob/main/src/agents/sandbox/sandboxes/docker.py#L508-L549) [Windows/provider guide](https://openai.github.io/openai-agents-python/sandbox/guide/)
| OpenHands | Docker workspace 先调用 `docker version`；失败抛错，run 非零时保留 stderr，健康检查失败时保留 Docker logs。 | 不自动改跑宿主机；官方排障也要求先以 `docker ps` 验证 client/daemon。[Docker workspace source](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-workspace/openhands/workspace/docker/workspace.py#L1314-L1605) [Troubleshooting](https://docs.openhands.dev/openhands/usage/troubleshooting/troubleshooting#launch-docker-client-failed)
| SWE-agent | Docker 是默认 backend；配置也可明确选择 cloud code evaluation，镜像由 deployment 配置指定。 | Docker 问题以 `docker run hello-world` 等前置检查诊断；替代 backend 必须显式选择，而非静默 host fallback。[Installation](https://swe-agent.com/latest/installation/source/) [Docker troubleshooting](https://swe-agent.com/latest/installation/tips/) [Environments](https://swe-agent.com/latest/config/environments/)

**Yipilot 结论：** 保持目前的 fail-closed：`DOCKER_ACCESS_DENIED` 时不执行 `/apply`、`/verify` 或未来 `/repair`。测试环境可以明确 skip Docker integration，产品 Run 不能偷偷换成 host execution。这和上述项目的共同边界一致：sandbox provider 是可用的、显式授权的执行环境，而不是一个可绕过的开关。
