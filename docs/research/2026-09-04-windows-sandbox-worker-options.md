> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# Yipilot Windows 首版 Sandbox Worker：实现路线与边界

> **实施决策更新（2026-09-05）：** 本文保留为 Windows 原生
> token/ACL、Firewall 与 Job Object 的比较研究；其中“推荐本机 Codex 式
> Worker”的结论已不再是 Yipilot 的实施路线。Yipilot 现选择 Docker Desktop
> Linux container 作为 Sandbox Worker。请以
> [Docker Sandbox Worker 调研](2026-09-05-docker-sandbox-worker.md) 和
> [Docker Sandbox Worker 实施基线](../superpowers/specs/2026-09-04-yipilot-windows-sandbox-worker.md)
> 为准。

**标签：** Research / architecture decision input（不是已实现能力）\
**日期：** 2026-09-04\
**结论等级：** Windows 首版的设计依据；所有外部事实均链接到一手资料。

## 结论先行

是的，Windows、Linux、macOS 的沙箱实现会明显不同。这不是“换一个命令”的差别，而是三个 OS 提供的强制边界不同：Linux 常用 namespace + mount + seccomp/Landlock，macOS 有 Seatbelt policy；Windows 没有一个同时适合任意开发工具、任意路径读写、按子进程树断网的单一原语。OpenAI 为 Codex Windows 最终组合了独立 sandbox 用户、受限 token、NTFS ACL、防火墙和独立 command runner；其公开说明也明确说 Windows 没有开箱即用的同类能力。[OpenAI：Codex Windows sandbox](https://openai.com/index/building-codex-windows-sandbox/)

**Yipilot 的 Windows 首版建议采用“Codex 式本机 Worker”，而不是把 worktree、`subprocess`、环境变量或 DeepSeek Harness 的 ACL 后端称作完整沙箱。** 这条路线需要一次管理员初始化和一个小型原生 runner，但能保持 `yipilot` 的本地 CLI 体验、可复用既有 Python 环境，并实现默认离线、无用户凭据、受限写入、可取消的真实执行。

在该 runner 完成前，Yipilot 应维持现在诚实的 `HOST_TRUSTED` 标识：真实 `/apply`、`/verify` 可以作为本地受控操作存在，但**不得**显示为“沙箱执行”。真正的 `sandboxed` 模式必须是 fail-closed：初始化、策略校验或 runner 启动任一失败，命令不执行。

## 1. 先统一：Yipilot 所说的沙箱到底保护什么

### 1.1 威胁模型

要防的是模型生成的补丁、测试、构建脚本或依赖安装脚本在执行时：

1. 读取 `~/.ssh`、浏览器 token、云 CLI 凭据、Yipilot provider JSON/API key；
2. 修改原 checkout、其他任务 worktree、`.git` 元数据、Yipilot 的证据或设置；
3. 联网下载恶意依赖、上传本地资料、访问内网；
4. 派生进程、无限运行、耗尽 CPU/RAM/磁盘或刷爆日志；
5. 通过 `..`、junction/reparse point、symlink、hard link 绕过“路径前缀检查”。

不把“攻击者已拥有管理员权限”“Windows/Hyper-V 内核 0-day”承诺为首版可防范围；那需要 VM/远程执行等更强边界。模型 API 请求由控制面完成，**key 永远不是 Worker 的环境变量、文件或映射目录的一部分**。

### 1.2 不是沙箱的东西

| 现有/常见机制 | 它解决什么 | 它不解决什么 |
| --- | --- | --- |
| Git worktree | 任务并行、受控 diff | 凭据读取、联网、宿主进程执行 |
| 固定 argv、`shell=False` | 少一层 shell 注入 | Python/pytest 本身可执行任意仓库代码 |
| Approval | 人工决定某次扩权 | 模型绕过、已批准命令的破坏能力 |
| Python 路径白名单 | 误操作预防 | junction、hard link、native 子进程、TOCTOU |
| Job Object | 管理一棵进程树与资源 | 文件/网络/凭据隔离 |

DeepSeek Harness 的定义尤其值得学习：它把“每次调用的 sandbox policy”和“具体平台 runner”分开，并报告 `full` 或 `partial` enforcement；没有 runner 时拒绝运行。它同时明确其进程 sandbox 词汇只治理**文件效果**，网络、凭据、syscall 和资源不在其中。[DSH sandbox contract](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox/README.md)

## 2. Windows 原语：每个能做什么，不能做什么

### 2.1 AppContainer：强，但不适合首版任意开发工作流

AppContainer 是 capability-based OS boundary。子进程只能访问明确授予的资源；它能隔离凭据、文件/注册表、网络、进程和窗口。[Microsoft：AppContainer isolation](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation) 这很像我们想要的最小权限。

问题在于它要求预先声明能力和可访问资源。OpenAI 评估后认为它适合“事先知道所需资源”的应用，却不适合会调用 shell、Git、Python、包管理器和未知二进制的开放式 coding agent；DeepSeek Harness 的 Windows 设计记录也指出它不能做任意路径读取。 [OpenAI：为何不用 AppContainer](https://openai.com/index/building-codex-windows-sandbox/) [DSH Windows ACL backend](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox-windows-acl/README.md)

**学习结论：** AppContainer 适合将来把 Yipilot 自身打包成受限桌面程序，或运行一个依赖完全固定的 worker；不适合首版直接承载用户任意 Python 项目的测试命令。

### 2.2 Restricted token + NTFS ACL：可控写入，但单独不够

`CreateRestrictedToken` 可以移除 token privileges、把 SID 设为 deny-only，并要求普通 token 与 restricting SID 的两次访问检查都通过。[Microsoft：Restricted Tokens](https://learn.microsoft.com/en-us/windows/win32/secauthz/restricted-tokens) Codex 使用 synthetic SID 加 workspace ACL，让受限 token 只能写当前工作区及指定 writable roots；写成功必须同时通过真实用户和 restricted SID 的检查。[OpenAI：写受限 token 的两次检查](https://openai.com/index/building-codex-windows-sandbox/)

它**能**做：把“可写目录”压缩为单个 Run workspace + 私有 temp，并让所有子进程在同一受限 token 下继承写约束。它**不能单独做**：

- 阻止读取调用者本来有权读取的任何文件；
- 阻止 socket/network；
- 绝对防住 ACL 的 `Everyone` 留存和 NTFS hard link 的 file-object 别名；
- 自动限制 CPU/RAM/进程数。

DeepSeek Harness 的实现是很好的反例教材：它用 `WRITE_RESTRICTED` token 和 workspace/private-temp capability SID，所有 Win32 调用失败即抛错，避免 POC 失败后以完整 token 启动；但它仍明确报告 `partial`，因为 `Everyone` 需要保留以让 DLL 初始化工作，hard link 也可让同一 file object 从外部路径访问。更重要的是它承认受限子进程仍可读取 caller-readable 文件、仍可开 socket。 [DSH Windows backend：机制与限制](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox-windows-acl/README.md)

**学习结论：** DSH 的 `full/partial` 回报、每 session 私有 temp SID、路径 canonicalization 和 fail-closed 必须借鉴；但不能把它单独作为 Yipilot 的“秘密不可读、默认断网”承诺。

### 2.3 独立 Windows 用户 + 防火墙：解决 token 单独解决不了的网络与凭据问题

Windows Firewall 无法只按 restricted token 中的 synthetic SID 匹配，也不能用“某个 `python.exe`”规则精确限制某一次 agent 调用；环境变量代理更会被直接 socket 绕过。OpenAI 的结论是：要把防火墙规则绑定到**独立 principal**，而非真实登录用户。[OpenAI：为何 proxy/普通 firewall 规则不够](https://openai.com/index/building-codex-windows-sandbox/)

因此 Codex 建立 online/offline 两个本地 sandbox 用户；offline 用户被 firewall 阻断出网。控制面用 `CreateProcessWithLogonW` 先以 sandbox user 启动 runner，再由 runner 用自己的 token 创建 restricted token 和最终 child；OpenAI 之所以拆为 setup binary、command runner 和 child，正是为跨 user/token 的权限边界与可靠启动。[OpenAI：最终 Windows 架构](https://openai.com/index/building-codex-windows-sandbox/)

**学习结论：** 这是 Yipilot 首版的关键组合：

- sandbox user 没有真实用户的 profile、Credential Manager、SSH/Git 凭据或 provider JSON；
- Offline user 的 Firewall 出站 deny 是按用户身份生效，不依赖程序是否尊重代理环境变量；
- Worker 再用 restricted token + ACL，将该用户本来可写的位置缩到本 Run workspace 与私有 temp；
- 需要联网的依赖 bootstrap 是一个**单独的、明确批准的 online 操作**，不能偷偷把默认 Worker 切成 online。

### 2.4 Job Object：必备的进程树和资源治理，不是隔离边界

Job Object 能把一组进程作为整体管理，可计量资源、施加工作集/优先级/时间等限制；`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 会在 runner 断开、取消或崩溃时杀掉 job 内全部进程。[Microsoft：Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) Windows 8+ 支持嵌套 job，但 child 如果被允许 breakaway 就不能完全被父 job 管住。[Microsoft：Job process tree](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)

首版 Worker 必须把 child 放入 `KILL_ON_JOB_CLOSE` job，并至少设置：wall-clock watchdog、per-job CPU time、active-process limit、process memory/working-set 上限和 stdout/stderr byte cap。注意：Job Object 只能约束被成功纳入 job 的进程树；它不等于 network/filesystem sandbox，也不应允许 `BREAKAWAY_OK`。

### 2.5 Windows Sandbox / Hyper-V container：更强 VM 边界，但有产品代价

Windows Sandbox 是一次性、Hyper-V 内核隔离的轻量 VM；关闭即丢弃状态。它可以关网络、禁 clipboard、关 vGPU，映射 host 文件夹并指定只读；但默认网络和 clipboard 是开启的，任何可写 host mapping 的修改会持久回到 host。它只支持 Pro/Enterprise/Education，且一次只能开一个实例。[Microsoft：Windows Sandbox](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/) [Microsoft：`.wsb` mapping/network 配置](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file)

Windows Hyper-V-isolated container 也提供每容器独立 kernel 的硬件级边界；微软明确称 process-isolated Windows/Linux container 共享 host kernel，不适合 hostile workload，而 Hyper-V isolation 才是 robust boundary。[Microsoft：Windows container security](https://learn.microsoft.com/en-us/virtualization/windowscontainers/manage-containers/container-security)

**学习结论：** VM/Hyper-V worker 对抗不受信任代码更强，却需要 guest image、依赖镜像、启动和输出回传设计。尤其绝不能把原 checkout writable bind/mapping 进去：这会重新把 host 的重要目录交给不受信任代码。

### 2.6 junction、symlink、hard link：为什么“`Path.startsWith`”不是防线

- **`..`/junction/reparse point/symlink：** 在控制面创建 workspace、生成 allowlist 前，必须以 Windows 真实 filesystem 语义 canonicalize 路径；然后在 OS token/ACL 或 VM mapping 中实行边界，不能只在 Python 里做字符串前缀比较。DSH 也把 workspace root 以 filesystem semantics canonicalize 后才下发 policy。[DSH subsystem](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/sandbox.md)
- **hard link：** 其本质是同一 NTFS file object 的另一个目录项；对 workspace 路径添加可继承 ACL，可能改变该 object 的安全描述符并通过外部 alias 写到同一文件。DSH 因此不将 Windows ACL 后端标作 full enforcement。[DSH Windows hard-link limitation](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox-windows-acl/README.md)
- **可执行策略：** Run workspace 必须是控制面从受信任 checkout 创建的独立复制，不接受 agent 指定的 root；复制后在开始前拒绝 workspace 内的 junction/reparse point 和 multi-link regular file，或采用 VM/container 内的干净 filesystem。这是防 hard-link/junction 的实际解决，而不是对 agent 输入多做一次字符串校验。

## 3. 三条可实现路线

| 路线 | 安全边界 | 用户成本 | 首版适用性 | 结论 |
| --- | --- | --- | --- | --- |
| A. 本机 Codex 式 Worker | 独立 user + firewall + restricted token + ACL + Job | 初次需管理员 setup；需 native runner | 高：可用本地工具链、体验接近 `yipilot` | **历史推荐；已由 Docker 决策取代** |
| B. Windows Sandbox VM Worker | Hyper-V guest kernel；无网络、无 host writable checkout | 仅 Pro/企业/教育；单实例；需 guest bootstrap | 中：适合高安全可选模式/验证原型 | 严格可选，不作默认 |
| C. Hyper-V Windows container | 独立 kernel + `ContainerUser` + no network | Docker/Windows image 很重、版本兼容与镜像维护 | 中低：适合受控企业环境/以后 CI | 后续部署路线 |

### 路线 A（推荐）：本机、默认离线的 Codex 式 Sandbox Worker

这不是“在 Python 里套一层 subprocess”。它应是控制面与 OS worker 之间明确的 RPC/argv contract：

```text
Yipilot 控制面（真实用户；可访问 Provider/API Key）
  ├─ 创建并冻结 Run manifest、独立 source copy、允许命令 argv
  ├─ 不把 key / HOME / APPDATA / USERPROFILE / SSH_AUTH_SOCK 传给 worker
  ├─ 调用 Windows runner（只传 run id、workspace、private temp、固定 argv）
  │
  └─ Windows runner（sandbox user）
       ├─ 校验 canonical workspace 与 manifest / 拒绝 reparse point、hard link
       ├─ 创建 WRITE_RESTRICTED token；workspace + temp capability SID
       ├─ 挂入 KILL_ON_JOB_CLOSE Job 和资源/输出限额
       ├─ Offline user 防火墙：所有出站 deny
       ├─ CreateProcessAsUserW 启动固定 argv
       └─ 回传 exit code、截断日志、资源计量、output manifest

控制面（真实用户）
  ├─ 重新扫描 workspace、计算 diff 与变更 manifest
  ├─ 验证输出只能来自本 Run workspace
  └─ 仅将 verified diff 交给现有 Apply/Handoff 流程
```

#### A 的一次性管理员 setup

1. 建立 `YipilotSandboxOffline` 本地普通用户；不放入 Administrators，不复用登录用户。
2. 建立只针对该 user 的 outbound deny firewall 规则；控制面要先用一个实际 socket probe 验证其真的断网。
3. 安装/注册带签名或可校验版本的 runner；runner 是极小 native program，唯一职责是 token、ACL、Job 和 child launch。不要让 Python 控制面自行 `ctypes` 拼安全关键 API。
4. 让控制面在运行时创建 `runs/<run-id>/workspace` 与 disjoint `runs/<run-id>/tmp`；授予 sandbox user 读取/执行所需的 runtime，另授予 restricted SID 仅此两个目录写入。
5. 对 `.git`、Yipilot 配置、证据目录、原 checkout、用户 profile、其他 run 显式不授予访问；不要依赖“在 child 环境中删了变量”。

#### A 的执行模式

首版只实现一个可安全命名的模式：`offline-workspace-write`。命令允许写入**一个 Run copy**和该 Run private temp，默认无法联网。没有 `danger-full-access` 自动降级；若用户明确批准“联网准备依赖”，控制面在独立 Run/步骤使用 Online user，并把网络事件、允许原因和最终依赖 lock evidence 写入记录。真正的模型编码与 pytest 验证仍都在 Offline Worker。

#### A 的优点与剩余风险

它具有最好的本地体验，也和 Codex 现行 Windows 思路一致；可限制写、网络、用户凭据和进程树。但它是同宿主 OS 边界，首版仍应把受限 token/ACL 的 `Everyone`、hard-link 等已知限制报告为 `enforcement=partial`，而不是宣称完整 hostile-code VM 安全。需要“绝对不可读 host secrets”的场景，转路线 B/C。

### 路线 B：Windows Sandbox 的可选严格模式

适用于希望把测试代码放到 disposable VM 的用户，不应当把它作为无感默认模式。

1. 控制面构建 guest input：Yipilot worker bundle、固定 Python runtime/依赖 wheelhouse、Run source copy、冻结 manifest；不使用原 checkout。
2. 生成 `.wsb`：`Networking=Disable`、`ClipboardRedirection=Disable`、`AudioInput=Disable`、`vGPU=Disable`；只将 input map 为 read-only。
3. 给 guest 一个**单独、最小、仅用于导出**的 host output folder；或让 guest 生成 patch/manifest 后由受控 channel 拉取。绝不将 source checkout 或用户目录 writable map 入 guest。
4. guest 运行固定 argv，输出 patch、日志、resources manifest；控制面只接受与冻结 manifest 匹配的产物，关闭 VM 后才计算/apply diff。

这条路线把 credentials 和 host filesystem 留在 guest 外；但 Windows Sandbox 默认网络/clipboard 开启、可写 mapping 会回写 host，且需要支持版本并只能单实例，因此配置检查必须 fail-closed。[Microsoft：Windows Sandbox 的默认值及 mapping 风险](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file)

### 路线 C：Hyper-V-isolated Windows container

在企业开发机/CI 上，可以维护预构建的 Windows worker image，以 `ContainerUser`、Hyper-V isolation、默认无网络运行。source 先复制进 container filesystem，产物只以 patch/manifest 回传；不要 mount Docker named pipe、host user directory、SSH directory 或 writable project volume。微软将 Hyper-V isolation 视为强边界，而 shared-kernel process container 不视为 hostile workload 的强安全边界。[Microsoft：Hyper-V container security boundary](https://learn.microsoft.com/en-us/virtualization/windowscontainers/manage-containers/container-security)

其成本是 Docker/Hyper-V、巨大 Windows base image、OS/image compatibility 和 image supply-chain；对现在以本地 Python 项目为核心的 Yipilot 来说，先做 A，只有需要更强环境可复现性或企业 CI 时再做 C。

## 4. 推荐实施顺序：把“大沙箱”拆成可验收任务

### 历史任务建议：冻结边界与术语（先做，纯 Python/文档）

- 定义 `SandboxMode`：首版仅 `OFFLINE_WORKSPACE_WRITE` 与 `HOST_TRUSTED`；后者不能被 `/run` 默认选择。
- 定义 `SandboxEnforcement`：`FULL`、`PARTIAL`、`UNAVAILABLE`，以及 Run evidence 必填字段：runner version、policy revision、workspace canonical path、network mode、token/user identity（不含 secret）、Job 限额、资源结果。
- 提取 `SandboxWorker` 端口，但**不要**给 `HostTrusted` 伪装成实现；`SandboxUnavailable` 必须中止 apply/verify。
- 建立一个 Run workspace copy builder：控制面所有 diff/manifest 都以此副本为输入，禁止把原 checkout 交给 worker。

验收：在没有 Windows backend 时，真实 `/apply` 与 `/verify` 返回结构化 `SANDBOX_UNAVAILABLE`，不启动 pytest、不写任何文件；现有 host 模式仅能经显式开发者 override 使用，并在 evidence 中标红。

### 历史任务建议：Windows native runner 的可行性 spike（必须先于业务接线）

- 新增最小 native runner（Rust/C++/C# 三选一，按现有 Windows toolchain 决定；不要用 Python 重实现 token API）。
- runner 只接受 JSON manifest 文件或固定 argv，不接受 shell string；验证 workspace/temp 是 canonical、disjoint、controller-owned。
- 用 restricted token + ACL 实现只写 workspace/private temp；用 Job Object 实现 cancel、time、active-process、memory/output 上限。
- 建立 isolated local sandbox user 与 offline firewall 规则；以 guest identity 运行 runner；若 setup、token、ACL、firewall probe、Job attach 任一失败，则退出 `SANDBOX_UNAVAILABLE`。

验收：允许的 workspace 写入成功；外部目录写失败；取消会杀死 child tree；直接 socket、`git ls-remote`、`pip install` 失败；runner 失败不会以真实用户运行 child。

### 历史任务建议：控制面接线（替换而非并列“假沙箱”）

- `/apply` 与 `/verify` 仅向 worker 发送允许的固定 argv 和 Run manifest；不传 provider key；worker stdout/stderr 设 byte cap，控制面保存 hash/截断说明。
- worker 退出后控制面重算 diff，拒绝 reparse/hard-link/manifest 不一致和 workspace 外产物；仅成功并达策略要求的 result 可进入 Handoff。
- 对 `PARTIAL` enforcement：默认拒绝 real apply/verify，或只让用户经显式高级设置启用并将 run 标记为 `sandbox_partial`；不能静默降级。

验收：将 `.env`、provider settings、`.git`、其他 worktree 和用户 profile 放进攻击样例；它们在 child 中不可读/不可写。任何 evidence 错误、runner 异常、manifest 不一致都没有 Handoff。

### 历史任务建议：严格 worker（Windows Sandbox 或 Hyper-V container，后续）

为“模型生成的不受信任代码”“公司凭据机器”“执行第三方 test suite”提供可选 `VM_STRICT`。采用路线 B 或 C 的单独 capability，而不是在 RestrictedToken worker 上不断叠临时例外。验收目标是 VM kernel boundary、无 credentials、无 network、可重复构建 image/bundle 与补丁回传。

## 5. 绝对不能省略的安全测试矩阵

| 类别 | 攻击/故障样例 | 期待结果 |
| --- | --- | --- |
| 路径 | `..`、absolute path、junction/symlink 指向 user profile | OS/guest 拒绝；控制面也拒绝异常 workspace |
| NTFS | workspace 内 hard link 指向外部文件 | copy builder/strict guest 拒绝；若本机 ACL 无法绝对保证，报告 `PARTIAL` 并拒绝默认执行 |
| 秘密 | 读取 `%USERPROFILE%\\.ssh`、Credential Manager、Yipilot settings、环境中的 API key | sandbox user/guest 无法读取；key 不存在于环境 |
| 文件 | 写原 checkout、`.git`、其他 Run、证据目录 | 拒绝；host diff 不变 |
| 网络 | raw socket、DNS、Git HTTP/SSH、pip/npm download、proxy bypass | Offline worker 全部失败；不能只测代理变量 |
| 进程 | child/grandchild 常驻、取消时继续运行、breakaway | Job 关闭/取消后树被杀；不允许 breakaway |
| 资源 | fork bomb、CPU busy loop、内存膨胀、数 GB stdout | Job/watchdog/byte cap 终止；evidence 说明哪条限额触发 |
| fail closed | runner 缺失、ACL 设置失败、防火墙 probe 失败、manifest 被篡改 | 不运行 child；Run 记录 `SANDBOX_UNAVAILABLE`/`POLICY_ERROR` |
| 出口 | child 伪造“成功”、输出 workspace 外 patch | 控制面只接受重新计算并验证过的 workspace diff/manifest |

## 6. 需要学习的知识与源码阅读顺序

1. **Windows access token / ACL / SID。** 先读 [Microsoft Restricted Tokens](https://learn.microsoft.com/en-us/windows/win32/secauthz/restricted-tokens)，再读 [OpenAI 对两次 write access check 的实际解释](https://openai.com/index/building-codex-windows-sandbox/)。目标是理解：ACL 是对象权限，token 是调用者身份，restricted SID 是附加交集，而不是目录字符串规则。
2. **Windows process containment。** 读 [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)：重点是 `KILL_ON_JOB_CLOSE`、time/resource limits、breakaway 与 nested job。
3. **Codex Windows 设计全文。** 重点不是复制代码，而是理解为何它拒绝 AppContainer/Windows Sandbox 作为主路径、为何网络必须换 principal、为何有 setup/runner/child 三层。[OpenAI：Codex Windows sandbox](https://openai.com/index/building-codex-windows-sandbox/)
4. **DeepSeek Harness 的接口设计。** 读 [sandbox subsystem](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/sandbox.md)、[Windows ACL backend](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox-windows-acl/README.md)。重点学习 `policy`、`enforcement`、private temp、fail-closed、hard-link/Everyone 限制的诚实报告；不要误读为完整系统隔离。
5. **VM/container 的边界。** 读 [Windows Sandbox configuration](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file) 和 [Windows container security](https://learn.microsoft.com/en-us/virtualization/windowscontainers/manage-containers/container-security)。重点是：网络/clipboard/mapping 默认值和 bind mount 都是主动打孔；process container 不等于 VM boundary。

## 最终决策建议

本文形成时的路线曾是：先完成边界合同，再证明 Windows native runner，最后接入真实 `/apply`/`/verify`。该路线已被 Docker Sandbox Worker 决策替代；请使用文首链接的当前实施基线。

它有意识地没有试图在首版同时支持 macOS/Linux：先把 Windows 的真实边界、证据和 fail-closed 做对，比写三个表面相同、实际能力不同的“sandbox”更安全。日后跨平台时保留同一个 `SandboxWorker` contract，而换掉底层 runner：Linux 用 bwrap/Landlock，macOS 用 Seatbelt，Windows 保持该 user/token/firewall/Job 组合；不能把 Windows ACL 的能力宣称为 Linux/macOS 的等价物。
