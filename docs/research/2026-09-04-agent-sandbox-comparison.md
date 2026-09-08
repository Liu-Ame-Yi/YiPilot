> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# Codex、Pi 与 DeepSeek Harness 的沙箱实现对比

**标签：** Research（外部实现调研，不构成 Yipilot 已实现能力）\
**日期：** 2026-09-04\
**范围：** 仅使用 OpenAI、Pi 源码和 `deepseek-ai/deepseek-harness` 的一手公开资料。这里的“沙箱”必须区分：操作系统/容器强制边界、应用层工具策略、Git worktree 和人工审批；后三者都不能单独等同于 OS 沙箱。

## 结论先行

三者都没有把“worktree”当作沙箱：它只隔离 Git 工作副本，不能阻止进程读取宿主机凭据、开网络或启动子进程。真正能约束不受信任测试代码的是执行进程继承的 OS/容器边界。

- **Codex** 是本次资料中最完整的参照：本地命令从启动起处于 OS 强制边界，网络默认关闭；云端则是每个 chat 的隔离容器，且把 setup（可拿 secrets）与 agent（默认离线）分开。[Codex sandbox 与 approval](https://learn.chatgpt.com/docs/agent-approvals-security) [Codex 云环境](https://learn.chatgpt.com/docs/environments/cloud-environment)
- **Pi core** 不自带这个边界。官方仓库提供的是一个可选的 *example extension*：它仅替换 Bash 执行器，在 macOS/Linux 用 Anthropic Sandbox Runtime 包装命令；Windows 不支持，初始化失败后会退回本地 Bash。因此不能把 Pi 的可选示例当作 fail-closed 的全 Agent sandbox。[Pi 示例源码](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/examples/extensions/sandbox/index.ts)
- **DeepSeek Harness（DSH）** 有正式的同宿主进程沙箱服务：Linux 为 bwrap→Landlock，macOS 为 Seatbelt，Windows 为 ACL restricted token；不可用时 fail-closed。但它明确只治理**文件效果**，不提供网络、凭据、syscall 或资源额度边界；Windows 后端更明确标为 `partial`。它是可借鉴的“统一 per-call policy + enforcement evidence”设计，而不是 Yipilot 所需完整隔离环境。[DSH sandbox seam](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox/README.md) [本地后端](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox-local/README.md)

对 Yipilot 的直接结论：在 Windows 上继续把 `LocalCommandRunner` 标记为 `HOST_TRUSTED` 是诚实的；但 M6-B 前不能把它包装成安全执行。应优先实现一个 **fail-closed Sandbox Worker**，并将模型 API key 留在控制面，不能注入执行环境。

## 横向比较

| 维度 | Codex | Pi / Pi Agent | DeepSeek Harness |
| --- | --- | --- | --- |
| 默认定位 | 本地 OS sandbox；云端隔离容器 | core 无内建 OS sandbox；官方有可选示例扩展 | 同宿主进程的可组合文件效果 sandbox |
| OS/容器实现 | macOS Seatbelt；Linux seccomp + Landlock；Windows 专用 sandbox；云端容器 | 示例：macOS `sandbox-exec`、Linux bubblewrap | Linux bwrap，失败时 Landlock；macOS Seatbelt；Windows ACL restricted token |
| 文件边界 | 默认近乎任意读、仅写当前 workspace；可用细粒度 deny | 示例默认可写 cwd、`/tmp`；对 Bash 生效 | `read-only` / `workspace-write` / `danger-full-access`，每调用解析 workspace root |
| 网络 | 默认关闭；可按项目/域名启用 | 示例可配置 domain allow/deny | 当前 seam 不治理网络 |
| 凭据隔离 | 云端 secrets 仅 setup 可用、agent 前移除；本地需显式 deny/隔离敏感路径 | 示例只列出少数默认 deny-read 路径；并非完整进程隔离 | 不是 credential boundary；同宿主文件/进程需另行保护 |
| 审批 | sandbox policy 与 approval policy 分离；越界/联网可请求批准 | 示例可用 `--no-sandbox` 关闭，非支持平台/初始化失败会退回本地 Bash | 拒绝后可请求一次更宽模式，需人类批准；无 runner 则 fail-closed |
| 进程/资源 | 子进程继承边界；公开资料未给本地 CPU/RAM 数字配额 | 子进程树支持超时、终止；未见 CPU/RAM 强制额度声明 | 每命令 timeout/输出预算；公开 sandbox seam 未定义 CPU/RAM/cgroup 限额 |
| Git worktree | Git 并发隔离，不是安全边界 | 本次一手资料未见 core 强制 task worktree | SDK 接受 caller 的 cwd/workspace，不把 worktree 作为 sandbox |

## 1. Codex：把执行权限、审批和环境隔离拆开

### 本地执行

Codex 本地默认允许广泛读取、只在当前 workspace 写入、且未指定时不能联网；操作系统以降低后的权限启动命令，子孙进程继承同一边界。[Windows sandbox 设计说明](https://openai.com/index/building-codex-windows-sandbox/)

不同平台的强制机制不同：macOS 使用 Seatbelt，Linux 使用 seccomp 与 Landlock，Windows 可使用原生 sandbox 或 WSL 的 Linux sandbox。[Codex system card](https://deploymentsafety.openai.com/gpt-5-3-codex/cybersecurity) 目前的权限配置还支持 read/write/deny 规则；`deny` 能在较宽 workspace 权限内挖出不可访问的路径，例如 `.env` 类文件。[Codex permissions](https://learn.chatgpt.com/docs/permissions)

Windows 的当前方案尤其值得借鉴：它不是只改环境变量，而是使用单独的 online/offline sandbox 用户、restricted token、workspace ACL 和防火墙规则；离线用户的所有出网由防火墙禁止。OpenAI 同时明确承认本地 sandbox 为兼容开发流程而保留了接近真实用户的读能力，因此“只限制写”并不自动保护可读取的密钥。[Windows sandbox 设计说明](https://openai.com/index/building-codex-windows-sandbox/)

### 网络、审批和云端 secrets

在 `workspace-write` 中网络默认关闭。启用网络还需要由 network proxy 实际执行域名规则；网络命令与越过文件边界的命令可经 approval policy 请求授权。审批决定“何时暂停让人确认”，sandbox 决定“即使模型或命令恶意也能做什么”，二者不是替代关系。[Codex approval/security](https://learn.chatgpt.com/docs/agent-approvals-security) [Codex permissions](https://learn.chatgpt.com/docs/permissions)

Codex cloud 使用每 chat 的隔离容器：setup phase 可联网并可解密配置的 secrets，agent phase 默认离线且在开始前移除这些 secrets；普通 environment variables 不属于这种 secret 隔离。公开文档没有给出可作为产品承诺的 CPU、内存或磁盘数字配额，因此 Yipilot 不应臆造一个“与 Codex 相同”的资源限制。[Codex 云环境](https://learn.chatgpt.com/docs/environments/cloud-environment)

### worktree 的正确定位

Codex 的 Git worktree 用于多个任务并行：每个 worktree 有独立 checkout、共享 Git 元数据，默认 detached HEAD。它解决并发编辑，不是 OS 安全隔离。[Codex Git worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees)

## 2. Pi：核心默认信任宿主，可选示例只覆盖 Bash

Pi 官方仓库的 sandbox 实现位于 `examples/extensions`，源码自己注明它是替换 built-in Bash tool 的示例。它使用 `@anthropic-ai/sandbox-runtime`：macOS 用 `sandbox-exec`，Linux 用 bubblewrap；全局和项目 JSON 配置可设置 domain allow/deny、deny-read、allow-write、deny-write。[Pi sandbox 示例源码](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/examples/extensions/sandbox/index.ts)

默认配置允许 Bash 在当前目录和 `/tmp` 写入，默认拒绝读取 `~/.ssh`、`~/.aws`、`~/.gnupg`，默认拒绝写 `.env`、PEM 与 key 文件；同时预置 npm、PyPI、GitHub 等联网域名。这是“方便开发”的默认值，不是最小权限的 agent worker 配置。[Pi sandbox 示例默认策略](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/examples/extensions/sandbox/index.ts)

关键限制有三个：

1. 该扩展只包装 Bash；源码没有把 Pi 进程、任意 TypeScript extension 或其他进程内工具放进同一 OS sandbox。
2. macOS/Linux 以外直接显示不支持；`SandboxManager.initialize()` 异常时将 `sandboxEnabled` 置为 false，随后 Bash 调用本地执行器。因此它是 **fail-open**，不适合作为 Yipilot 真实写入/验证的安全前置条件。
3. 它确实为单次 Bash 设置 timeout，并在取消/超时时尝试杀死 detached process group；但源码未设置 CPU、内存或进程数的硬限额。[Pi sandbox 示例执行器](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/examples/extensions/sandbox/index.ts)

“pi-sandbox”在生态中也可能指多个第三方扩展；它们不能代表 Pi core。这里仅以 Pi 官方仓库可核验的示例为结论基础。

## 3. DeepSeek Harness：结构成熟，但边界有意较窄

DSH 将 sandbox policy 集中在 `ctx.sandboxPolicy`：每次调用把 session 的不可变 cwd 解析为 workspace root，模式为 `read-only`、`workspace-write` 或 `danger-full-access`。被拒绝的调用可请求一次更宽权限，人类批准后只重试该调用；没有可用 runner 时抛 `SANDBOX_UNAVAILABLE`，不会静默不受限运行。[DSH sandbox policy](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox-policy/README.md) [DSH sandbox seam](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox/README.md)

平台实现为 Linux 的 bwrap→Landlock、macOS Seatbelt、Windows ACL restricted token。bwrap profile 使用只读 host root、私有 `/proc`/PID namespace；`workspace-write` 额外给 workspace bind 与临时目录。Windows 的 workspace + private temp 写权限借助 restricted token/ACL，但官方将其标成 `partial`：`Everyone` 与 NTFS hard-link 边界不能提供绝对保证。[DSH local backend](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox-local/README.md) [DSH Windows ACL backend](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox-windows-acl/README.md)

DSH 的官方 seam 明确说其词汇只覆盖**文件效果**；不表达网络、进程、syscall、设备或 credential restrictions，并且仍与宿主共享 kernel 与 filesystem。容器、microVM 或 remote executor 必须替换整套 capability，而不是给这个 seam 再加一个开关。[DSH sandbox limitations](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox/README.md) 因此其 Windows ACL 后端特别不适合单独承担“秘密不可读、默认断网”的承诺；官方 README 也建议遇到读/网络隔离需求时改用其他机制或补充 capability token。[DSH Windows ACL backend](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox-windows-acl/README.md)

DSH 的 SDK `minimal` profile 更直白地提醒：它使用 `danger-full-access`，应运行在 disposable checkout 或 container 中，因为 Bash/editor 能修改 runtime process 允许的任何路径。[DSH Python SDK guide](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/python-sdk.md)

## 对 Yipilot 的设计决策

### 应借鉴

1. **Codex 的两层模型。** `SandboxPolicy` 是机器强制的文件/网络/进程边界；`Approval` 是一次具体扩权的人工决定。两者都要写入 Run evidence，不能以一次“确认 y”替代 OS 隔离。
2. **DSH 的 policy 解析与 fail-closed 语义。** 每个执行请求携带冻结后的 mode、workspace root、policy revision 和 enforcement result；sandbox 初始化或 runner 失败时，M5-B/M6 直接失败。
3. **Codex 的 secret 分相。** Provider key 只在控制面发出模型请求；Sandbox Worker 不获得 key、控制面配置目录、用户 profile、`.git` 管理目录或其他 Task worktree。

### 不应照搬

1. 不采用 Pi 示例的“初始化失败就本地执行”语义；Yipilot 的真实 apply/verify 必须 fail-closed。
2. 不把 DSH Windows ACL `partial` 写限制当成完整安全沙箱；它既不能承诺读隔离，也不治理网络。
3. 不把 Codex/Pi 的工作目录宽写默认值直接搬来；Yipilot 应只给单个 Run 的 disposable worktree copy 写权限，并把 diff/日志以受控出口带回。

### 推荐的 S0 Sandbox Worker 最小合同

第一版应只支持当前 Windows，并将实现分为 control plane 与 worker：

```text
Yipilot control plane（真实用户）
  ├─ 保存 API Key、Approval、Task/Run/Event/Artifact
  ├─ 创建 disposable Run workspace（不是原 checkout）
  ├─ 固定 argv：apply / pytest / ruff / mypy
  └─ 启动 fail-closed Sandbox Worker
       ├─ 仅挂载本 Run workspace 为可写
       ├─ `.git`、Yipilot settings/evidence、用户目录与其他 worktree 不可见
       ├─ 默认无网络；依赖安装另开任务与精确 allowlist
       ├─ 传递清洗后的最小环境，不传 Provider key
       ├─ 限制 wall-clock、CPU、内存、进程数与输出
       └─ 只返回退出码、截断日志、manifest 与 diff
```

这比“先挑一个 Windows API”更重要：必须先验收它确实能挡住 `..`、junction/reparse point、hard link、`.git`、用户 profile、环境变量、子进程树和网络访问。若无法报告 `enforcement=full`，M5-B/M6 的真实写入和验证应停止，而不是降级为 `HOST_TRUSTED` 却显示“已沙箱化”。

## 建议的验收矩阵

| 用例 | 预期 |
| --- | --- |
| 在 Run workspace 写一个允许文件 | 成功，manifest 记录 changed path/diff |
| 写原 checkout、其他 Task、`.git`、Yipilot settings/evidence | OS 层拒绝，Run 失败且留证 |
| 读取用户 profile、SSH/云凭据、Provider auth file | 不可见或拒绝 |
| 直接 socket、Git/PyPI/npm 下载、DNS/代理绕过 | 默认拒绝；允许时只能命中显式 allowlist |
| fork bomb、长时间测试、过量 stdout/stderr | worker 终止，记录 timeout/resource/output 原因 |
| sandbox 初始化、策略校验、证据回传失败 | fail-closed；不执行验证/写入 |
| sandbox 输出宣称成功但 host diff/manifest 不匹配 | control plane 拒绝 Handoff |

这些验证通过前，Yipilot 当前的 worktree、Approval、Harness 与固定 argv 只能称为“受控流程”，不能称为“安全沙箱”。
