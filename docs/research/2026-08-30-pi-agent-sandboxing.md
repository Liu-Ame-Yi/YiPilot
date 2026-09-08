> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# Pi Agent 的 sandbox 与权限模型调研

**标签：** Proposed（调研结论，不构成实现承诺）
**日期：** 2026-08-30
**范围：** 仅比较 Pi Agent 公开的一方文档/源码与 Yipilot 当前 M5 要求；不建议在运行时嵌入或依赖 Pi Agent。

## 结论

Yipilot 应把 **sandbox 置于真实模型写入之前**，而不采用 M5 的
`host_trusted` 写入预览作为默认路径。这是一个尚待 ADR 接受的产品/技术
决定，不是当前已实现能力。

Pi 的做法提供了两个很有价值的教训：安全边界必须由操作系统或虚拟化层
执行；应用层仍需要逐工具、逐路径、逐网络目标的权限判断。Pi 本身也明确
指出，项目信任和进程内权限检查不是 sandbox。Yipilot 应借鉴其边界分层和
“拒绝优先”思想，但不能把 Pi 的可选扩展称为完整隔离，也不能照搬其默认
的当前工作目录写入授权。

## Pi 的实际模型

### 1. Pi 核心默认没有 sandbox

Pi 的官方安全文档说明：内置 `read`、`write`、`edit`、shell 等工具，以及
TypeScript 扩展，均以启动 Pi 的用户权限运行；项目 `trust` 只决定是否加载
项目设置、资源和扩展，**不**限制模型随后请求工具做什么。官方建议对不受
信任、无人值守或不准备持续监看的工作，将整个 Pi 进程放入容器、VM、
micro-VM、远程 sandbox 或受策略约束的 sandbox。

- [Pi Security: Project Trust and No Built-in Sandbox](https://pi.dev/docs/latest/security)
- [Pi Settings: available built-in tools](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/settings.md#tools)

这意味着 Pi core 不能作为“本地代码 Agent 默认已经安全隔离”的参考实现。
它的核心工具面同时包括读、写、编辑和 shell/PowerShell；关闭或限制工具
只是能力选择，不能取代 OS 安全边界。

### 2. `pi-sandbox` 是可选的双层机制，并非所有操作都由 OS 拦截

Pi 的官方 `pi-sandbox` 包对 Bash 子进程使用 macOS 的 `sandbox-exec` 或 Linux
的 `bubblewrap`，以 OS 层限制文件系统和网络。`read`、`write`、`edit` 由于
直接运行在 Node.js 进程内，只能由扩展拦截并按相同策略检查；它们不是由该
子进程 sandbox 强制执行。官方示例源码也明确以 `SandboxManager.initialize()`
初始化该运行时，并在会话关闭时 reset。

- [pi-sandbox: what it does](https://pi.dev/packages/pi-sandbox)
- [官方示例扩展源码：运行时初始化与默认策略](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/examples/extensions/sandbox/index.ts)

因此更精确的评估是：**对 Bash 的文件与网络限制是 OS 级（限 macOS/Linux）；
对 Pi 文件工具则是应用级策略强制。整体不是一个覆盖 Pi 进程、扩展和全部
工具的完整 OS sandbox。** 这并非缺陷掩盖；Pi 官方文档正是以“完整隔离要靠
OS 或虚拟化边界”的方式描述这一点。

### 3. 权限、文件与网络规则

`pi-sandbox` 可由全局 `~/.pi/agent/sandbox.json` 和项目
`.pi/sandbox.json` 配置。路径/域名数组会合并，项目可以增加条目；触发限制时
用户可拒绝、仅本会话允许、写入项目配置允许，或写入全局配置允许。仅会话
授权保存在内存，Pi/agent 不能读取或修改，超时默认选择拒绝。

其关键优先级为：

- 未在 `allowRead` / `allowWrite` 中的读取会提示；`denyRead` 只是默认拒绝，
  用户允许后可被 `allowRead` 覆盖。
- 未在 `allowWrite` 中的写入会提示；`denyWrite` 优先级最高，永远硬拒绝。
- 域名不在 `allowedDomains` 时提示；`deniedDomains` 在 OS 层硬拒绝。
- `allowWrite` 使用前缀匹配，`.` 即当前整个工作目录；`"*"` 网络域名会移除
  逐域名提示，官方会警告。

来源：[pi-sandbox 配置、默认值及优先级](https://pi.dev/packages/pi-sandbox)。

### 4. worktree 与结果交接

本次查阅的 Pi 官方安全、设置和 `pi-sandbox` 资料未定义 Yipilot 所需的
Task / Run、任务专属 Git worktree、Change Handoff、与 commit SHA 绑定的审批，
或 PR/Jenkins 证据链。Pi 的官方安全建议反而强调：若要更强地防止非预期写入，
应使用只读挂载，或把文件复制进/出隔离环境，并在回传到受信任系统前审查
diff 与输出；读写绑定挂载仍会修改宿主工作区。

- [Pi Security: Running Untrusted or Unmonitored Work](https://pi.dev/docs/latest/security#running-untrusted-or-unmonitored-work)

所以“独立 worktree + 已验证 evidence/handoff + 人类 Approval”仍是 Yipilot
自己的控制平面责任，不能委托给 Pi 风格的会话权限提示。

## 与 Yipilot M5 的差距和建议

Yipilot 当前 M5 规格已要求：模型不能执行任意 shell；只能提交类型化动作；
写入只能发生在 task-owned worktree；每次写入必须可追溯到 Run、policy、
Approval、diff 和 Git 证据。它也承认完整 sandbox 尚未设计，`host_trusted`
只是可选预览。[M5--M8 规格](../superpowers/specs/2026-08-29-yipilot-ai-code-delivery.md)

建议把 M5 的开放决策 1 收敛为：**首个真实代码写入必须在 sandbox 中执行；
不交付 `host_trusted` 自动写入模式。** 这可避免把“执行用户拥有完整主机权限”
误包装成受控写入。

在进入代码实现前，应新增一份 sandbox ADR/规格，至少锁定：

1. **隔离单位与最小挂载。** 每个 Run 使用 task worktree 的隔离副本或受限
   挂载；源 checkout、`.git` 管理目录、其他 worktree、用户目录、SSH/云凭据
   都不挂载或明确只读。将已验证 diff/工件通过单向收集返回，而不是把主
   checkout 读写挂载给 Agent。
2. **强制执行层。** 文件写入和命令子进程都必须由 OS/容器/VM 强制，而不仅是
   Python 的路径检查；针对首个支持的平台验证路径解析、符号链接/重解析点、
   子进程、网络和逃逸尝试。Pi 的 `sandbox-exec`/`bubblewrap` 模式不能直接
   覆盖当前 Windows 环境，Windows 的实现必须单独选型和验收。
3. **固定命令与网络最小化。** 仍只允许 M6 规定的 argv；模型 API 由控制面
   请求，不将长期 Provider 密钥暴露给执行环境。验证阶段默认无网络；若确有
   依赖下载需求，按精确域名、短期凭据和单独 Approval 开放。
4. **不可覆盖的保护规则。** `.git`、密钥文件、配置、系统目录和 Yipilot
   控制/证据目录应是硬拒绝，不能由模型、项目文件或普通会话提示覆盖；所有
   用户授权必须绑定 Task、Run、policy revision、路径/命令集合、预算和有效期。
5. **失败关闭与可审计。** sandbox 初始化、策略载入、证据持久化、路径解析或
   进程启动任一失败，Run 必须停在非成功状态；证据记录 sandbox 镜像/策略
   哈希、挂载、网络规则、实际命令、退出码和输出哈希。

## 可借鉴与不应照搬

| 类别 | 结论 | 对 Yipilot 的含义 |
| --- | --- | --- |
| 可借鉴 | OS/虚拟化层才是可信执行边界 | 把 Agent 执行环境与控制面、宿主凭据分开。 |
| 可借鉴 | 子进程 OS sandbox + 文件工具策略检查是不同层 | 同时实施内核/容器限制与 Runtime 类型化动作校验。 |
| 可借鉴 | `denyWrite` 永远高于允许、超时默认拒绝、会话授权不持久化 | 用不可覆盖的硬拒绝和 Run-bound、到期即失效的 Approval。 |
| 可借鉴 | 域名 allowlist 和最小凭据/挂载 | 模型调用、依赖获取和 CI 导入分别设计最小网络与密钥边界。 |
| 不应照搬 | Pi core 以启动用户全部权限运行 | 不允许真实 Yipilot 写入以宿主完整权限直接执行。 |
| 不应照搬 | `allowWrite: ["."]` 的宽工作目录默认值 | 改为每个 Run 的 worktree/隔离副本及精确可写路径；保护 `.git` 和控制数据。 |
| 不应照搬 | `denyRead` 可由交互授权覆盖 | Yipilot 的密钥、宿主身份数据和其他工作区须为不可覆盖的硬拒绝。 |
| 不应照搬 | 项目配置可与全局数组合并、并可持久增加权限 | 仓库内容不得扩张 Yipilot 权限；策略只能由受控配置和明确、留证的人类决策改变。 |
| 不应照搬 | 将文件工具的进程内拦截描述为完整 OS 隔离 | 文档必须区分“应用策略检查”与“OS 强制”，对覆盖范围和平台如实声明。 |

## 后续动作（不构成承诺）

在真实 Provider 写入功能开始前，先完成 sandbox 专项 ADR 和验证夹具：合法
worktree 写入、`..`/符号链接/重解析点逃逸、`.git` 与密钥访问、任意命令注入、
未授权网络、子进程树、初始化失败、证据失败与回收失败。通过后，才实现
“真实模型 -> 类型化计划 -> sandbox 内 patch -> 受控 pytest -> Change Handoff”。
