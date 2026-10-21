# YiPilot 沙箱迁移到 OpenSandbox：调研、代码整合与精简方案

Historical reference：本文保留当日设计；旧实时 Worker 已从当前工作树删除，旧源码链接固定到迁移前提交。当前实现与尚未通过的 G4 见[业务迁移记录](2026-10-21-opensandbox-business-migration.md)。

> **调研日期：2026-10-13（Asia/Shanghai）。状态：Proposed / Research。** 本文没有表示迁移已实现或真实沙箱已验收。用户的部署顺序是：**先在本机模拟，后续可能接独立服务器或 Kubernetes**。建议“模拟”包括真实 OpenSandbox + Docker 的最小实验；mock 只负责验证错误处理。
>
> 仓库依据：当前工作树，HEAD `f7b6ec282e453a14c60811afa36ce5326444fd2a`；存在未提交修改，分析以实际文件为准。本轮只维护调研与规划文档，不调整现有运行代码或产品状态。代码整合、精简与删除顺序见第 9 节。
>
> **2026-10-14 补充：** 第 10 节新增管理初始化、环境继承和清理措施核查；可执行的工作包、关口、回切及文件安排以[详细执行计划](2026-10-14-opensandbox-execution-plan.md)为准。原版 1.1.1 先限无敏感 fixture，正式接入需先通过新管理面门槛。

## 1. 建议与可行性

**可以迁移，但应替换执行实现及其证据合同，保留 YiPilot 的审批、源码过滤、可信 diff、验证和交接流程。** 首先用本机实验判断安全策略是否能成立，再接入业务。不要直接把 `DockerSandboxWorker.execute()` 改成 SDK 调用后就宣布迁移完成。

本机阶段 OpenSandbox 仍需要 Docker；它新增生命周期服务、容器内 execd 和网络入口，不能解决 Docker Desktop 本身未启动或不可用的问题。价值主要是为之后的服务器/K8s 执行提供统一接口与运维能力。只有本机需求时，服务与网络的额外复杂度可能超过收益。[官方项目介绍](https://github.com/opensandbox-group/OpenSandbox/tree/release-1.1.1)

调研基线采用 **OpenSandbox 1.1.1**，固定 SDK、Server、execd/egress 版本和镜像 digest；不要把官网跟随 main 的能力默认当成安装包能力。[发布说明](https://github.com/opensandbox-group/OpenSandbox/releases/tag/release-1.1.1)

接入前有三个决定性的条件：

1. **安全策略能被实施和验证。** 旧合同的 `network=none`、非 root、只读根文件系统等，与 OpenSandbox 的管理网络和启动方式不同。若采用“管理通道可达、任务出站拒绝”，必须明确这是新策略及新证据，不能填写旧值伪装等价。
2. **停止任务与收回文件的顺序可靠。** 命令退出不等于沙箱停止；销毁后又可能无法下载文件。需要一种可验证的、不会被后台任务继续改写的输出快照。
3. **未知结果不重复执行。** 创建响应丢失、SSE 中断、客户端退出、删除失败都必须记为不确定或失败，不能自动再应用一次 patch，也不能回退到宿主执行。

上述条件没有通过时，保持当前 Docker 默认路径；OpenSandbox 只作为显式实验路径。

## 2. 当前实现与应保留的流程

当前真实链路是：

```mermaid
flowchart LR
    P["模型产生 PatchProposal"] --> A["控制端冻结源码与审批"]
    A --> W["Worker 执行 Run 副本"]
    W --> E["停止与清理证据"]
    E --> D["控制端重算 manifest 和 diff"]
    D --> H["输出审批与可信 apply"]
    H --> V["新副本运行 pytest / Ruff / mypy"]
    V --> O["再次校验证据与 Change Handoff"]
```

这条流程在 [ProjectApplication](../../src/yipilot/app.py)、[应用协调](../../src/yipilot/workflow/sandbox_application.py)、[验证协调](../../src/yipilot/workflow/sandbox_verification.py) 中可见。模型调用、密钥管理和 Git 交接无需搬进 OpenSandbox。

可复用的控制端逻辑：

- [SandboxSourceService](../../src/yipilot/workspace/sandbox_sources.py)：从冻结 Git 对象导出 `baseline/` 与 `execution/`，过滤 `.git`、`.env*`、凭据和控制目录。
- [SandboxOutputService](../../src/yipilot/workspace/sandbox_output.py)：从控制端持有的文件重新计算 manifest/diff，校验日志 artifact、Run、请求指纹及审批。
- [SandboxApplyService](../../src/yipilot/workspace/sandbox_apply.py)：把已核验的确切输出写到新任务 worktree，拒绝 hooks/filters、证据漂移和越界输出。
- 已有固定 `argv` 白名单、资源预算、失败关闭、显式审批和 Change Handoff 放行条件。

现有源码合同仅接受 UTF-8 普通文本文件，拒绝 NUL、符号链接、硬链接、特殊文件、Windows 别名与大小写冲突；上限为 **单文件 16 MiB、文件树 128 MiB、10,000 文件**。PatchProposal 另有限制：**16 个文件、64 KiB diff**。这是当前项目限制，不应因为 OpenSandbox 文件 API 支持更多类型而扩大。[源码扫描](../../src/yipilot/workspace/sandbox_sources.py)、[PatchProposal](../../src/yipilot/domain/patch_proposal.py)

需要识别的两种执行合同：现有 [ExecutorPort](../../src/yipilot/workspace/tool_registry.py) 面向 `ToolRequest -> ToolResult`；沙箱实际使用 `SandboxRequest -> SandboxResult`。不能只给 ToolRegistry 换一个 executor，就覆盖交互式 `/apply`、`/verify`。

## 3. 代码修改范围

| 位置 | 现有耦合 | 建议最小修改 |
|---|---|---|
| [domain/sandbox.py](../../src/yipilot/domain/sandbox.py) | 64 位 Docker ID、docker_version、wsl2/hyper-v、network=none；请求只有裸 sha256 镜像值 | 新证据版本分开公共结果与运行时事实；记录 provider、sandbox_id、不可变镜像引用、策略版本、命令和清理结果 |
| [workspace/docker_worker.py](https://github.com/Liu-Ame-Yi/YiPilot/blob/bc189b11b08ed1ddd9cf96aea5f12c9982ed5a73/src/yipilot/workspace/docker_worker.py) | Docker CLI、Windows named pipe、Docker inspect、挂载与生命周期 | 保留旧实现供回归；新增 `OpenSandboxWorker` Adapter 封装 SDK、生命周期、传输和证据，不向上层泄漏 SDK 对象 |
| [workflow/sandbox_application.py](../../src/yipilot/workflow/sandbox_application.py) | 直接创建 Docker Worker，doctor 和 artifacts 路径写死 Docker | 注入可信 Worker 选择；冻结运行时策略并用于请求指纹；使用该运行时的证据验证 |
| [workflow/sandbox_verification.py](../../src/yipilot/workflow/sandbox_verification.py) | 直接执行 Docker；读取 container-after/cleanup，调用 check_container | 复用同一选择和验证 Interface；先保留每个固定工具独立副本/子 Run，避免新增跨工具状态 |
| [workspace/sandbox_apply.py](../../src/yipilot/workspace/sandbox_apply.py) | `_require_stopped_worker` 直接要求 Docker 清理收据 | 改成按证据版本/运行时验证“任务已停止、输出被冻结、资源清理已确认”；不能删除这个检查 |
| [workspace/sandbox_output.py](../../src/yipilot/workspace/sandbox_output.py) | 校验旧 SandboxResult、image_digest 和预算 | 接受新证据合同；继续验证本地精确文件、日志 hash 和输出指纹；不信任远端自报 diff |
| [persistence/task_store.py](../../src/yipilot/persistence/task_store.py) | ProjectRecord 只有 sandbox_image_digest，项目元数据存 SQLite | 增加最少的非秘密运行时配置及显式迁移；更新 setter 时保留其他字段；冻结到 Run，不能执行时再读取可变配置 |
| [app.py](../../src/yipilot/app.py) | 组合服务、读取旧镜像配置、审批 Request | 在组合入口统一选择 Adapter；审批展示真实运行时及策略 |
| [workflow/recovery_service.py](../../src/yipilot/workflow/recovery_service.py) | 中断副作用 Run 标记 EXTERNAL_RESULT_UNCERTAIN；未清理远端资源 | 保留不重放；增加按已落盘的沙箱关联查询/清理，记录清理失败，必要时由外部 TTL/reaper 回收 |
| [workflow/repair_service.py](../../src/yipilot/workflow/repair_service.py) | 消费失败验证证据；提示词及诊断写有 Docker | 继续只从核验过的失败 Run 生成 proposal；改正运行时措辞与新结果读取，基础设施失败不能当代码失败 |
| [transport/cli/app.py](../../src/yipilot/transport/cli/app.py)、[interactive.py](../../src/yipilot/transport/cli/interactive.py) | doctor/register/image 参数及界面文案是 Docker 专用 | 展示真实 provider、镜像与限制；保持 terminal 只提交意图，不直接调 SDK |
| [pyproject.toml](../../pyproject.toml)、[uv.lock](../../uv.lock)、[Dockerfile](../../Dockerfile) | 没有 OpenSandbox SDK；现有镜像有自己的 USER/ENTRYPOINT | 接入时只加基础 `opensandbox` SDK；Server 独立安装部署；镜像兼容 execd 的 bootstrap，并预装固定测试依赖 |
| tests、fixtures、产品与指南 | tests monkeypatch Docker、构造旧 inspect/cleanup；文档声称离线 Docker | 加新合同与真实 OpenSandbox 测试，修改真正受影响的 fixture/边界检查；实现后同步 status/guide/V2，保留历史研究 |

特别注意镜像字段：当前 Worker 将 `image_digest` 与 Docker `image inspect` 的 `Id` 比较，即本地 image/config identity；远端可拉取的固定镜像引用通常是 `repository@sha256:<manifest digest>`。两者不能直接互换。新合同应分别记录不可变引用、目标平台与实际 image identity，登记/审批/doctor 一起调整；旧裸 sha256 值不应默认为可从仓库拉取的地址。[当前 readiness](https://github.com/Liu-Ame-Yi/YiPilot/blob/bc189b11b08ed1ddd9cf96aea5f12c9982ed5a73/src/yipilot/workspace/docker_worker.py)、[Docker 按 digest 拉取](https://docs.docker.com/reference/cli/docker/image/pull/)、[OCI manifest/config 分离](https://github.com/opencontainers/image-spec/blob/v1.1.1/manifest.md)

**建议的 Seam：** 位于 `workspace/`，围绕现有 `SandboxRequest` 的完整执行及持久化证据校验。最小 Interface 需要能完成 `execute`、运行前 `doctor`、消费时 `verify_evidence` 这三项职责；名称可以随现有代码习惯调整。创建、命令、上传、停止、删除是 Adapter 的 Implementation。不要把这些操作散到 workflow，也不用增加通用插件注册系统。

迁移期有 Docker/OpenSandbox 两个实际 Adapter，这时小 Interface 才有明确用途。最终目标是 OpenSandbox 承担正式执行，按第 9 节条件删除旧 Docker 执行与临时选择逻辑；旧证据只保留必要的纯阅读校验。旧 Run 依证据版本审计，不自动“升级”为新 Run。

## 4. 本机先做什么，哪些设计须考虑远端

### 4.1 最小本机实验

首轮只用无凭据的临时 fixture 仓库，不使用真实项目数据，不生成 Handoff。

- Docker Desktop 必须是 Linux container mode；独立启动固定版本的 OpenSandbox Server。先让官方最小 SDK 命令/文件往返成功，再测试 YiPilot 约束。
- 本机服务与动态端口只开放给需要访问的本机/容器接口。宿主运行 Server 与 Compose 容器运行 Server 的路由不同，不能机械照搬 `127.0.0.1`；要验证 Windows、WSL、Docker 网络实际可达性。
- 对外保留 YiPilot 的同步流程；为实现整体 deadline，Adapter 内部优先采用 async SDK 与独立有界 cleanup，详见第 10.4 节。调用原生 argv 列表，并固定 `working_directory="/workspace"`、UID/GID、命令 timeout；不要拼接 shell 字符串。
- 设置单 Run/单沙箱、1 CPU、1 GiB 等小预算，限制本机并发为 1。PID 等 Server 级限制与 Run 声明必须匹配。
- 单独准备 execd/egress 与任务镜像；Run 阶段不联网安装依赖、不使用 `latest`。image pull 的行为也要单独验证。
- 做两个文件实验：SDK upload/download，及 Run 专属 bind。前者验证将来远端的传输语义；后者验证本机能否在销毁后保留输出。

这些是**建议实验内容**，本文没有执行部署。具体 API、Windows 前置条件与默认差异见后面的官方核验。

### 4.2 文件与快照

`execution_directory` 当前是 Windows 控制端的绝对路径。它应继续表示控制端收回文件的位置，不应直接冒充 Linux 服务端的路径；沙箱内单独固定为 `/workspace`。

**本机可以优先试验的可靠收尾：** 仅把经验证的 Run execution 目录 bind 到 `/workspace`，不挂 baseline 或正式 worktree；先确认沙箱及后台进程已停止/销毁，再由控制端读取仍保留的 bind 文件重算 manifest/diff。这个方案只有在 Windows/WSL/daemon 路径解析、权限和销毁后的 bind 保留都实测通过时才可采用。它不是远端通用方案，也不能保持旧“恰好只有一个挂载”的检查不变：OpenSandbox 本身需要运行时挂载。

**独立服务器/K8s：** 需要上传输入、取得稳定输出并收回本地 staging。不能在“文件 API 下载完成”之后就推断后台进程已停止，也不能先 kill 再下载容器内已经被删除的文件。优先评估服务端 Run 专属持久目录/volume：停止工作负载后，由可信管理侧导出有界、不可变快照，再传回控制端。快照接口是否可用是部署级实验项；pause、SDK file download、runtime snapshot 并不天然组合成这个能力。

输出回收必须覆盖完整文件树与删除；只拉取远端声明的“变更文件”会遗漏或隐藏输出。归档解包前拒绝路径穿越、链接、设备文件、重复/大小写路径和解压超额。文件 API 若自动跟随链接，把内容下载成普通文件，事后本地扫描不能证明原始远端文件无链接；必须取得可信导出元数据，不能信任任务生成的 manifest。

保留原始文件字节、文件大小、hash、Git 模式和删除事实。不要换行规范化或自动编码转换。输入和输出应各有 transfer manifest 与核验结果；所有发布仍绑定原 `task_id/operation_id/run_id`、base_sha 和请求指纹。

### 4.3 网络、身份与证据

现有 probe 要求只存在 loopback 接口。OpenSandbox 的 execd 管理通道与 egress 管理改变了这个条件。建议定义**新策略版本**：明确管理入站路径、拒绝任务对外出站，并逐项测试 DNS、IP 直连、IPv6、内网/宿主/metadata 与其他 Run。模型不得得到创建/管理 SDK 的权限或服务密钥。

Stock Server 的安全设置不能完整表达旧 Worker 所有条件，见官方核验。若必须保留全部旧条件，可能需要小范围定制 Server/runtime；应该在本机可行性阶段作出判断，避免先重构全仓库后才发现运行时不支持。非 root 命令和 root execd 的权限属于不同事实，应分别记录。尤其要测试任务能否访问/操纵容器内的管理接口。

控制端保存的 receipt 至少绑定：provider、Server 身份及版本、SDK/execd/egress 版本、sandbox_id、command_id、输入 hash、不可变镜像与实际 identity、策略 hash、实际资源事实、命令终态、日志完整性、输出 hash、停止/清理结果、无 fallback。SDK `get_info` 不是旧 Docker inspect 的全面替代；本机应读取可信 Docker 管理事实，远端则需要受保护的管理侧证据。沙箱内的自报 UID、资源和镜像 hash 只能辅助探测。

后端、镜像、预算、策略或输入变化都应改变指纹，旧审批不能复用。API key 存 SecretStore/部署 Secret，Run 只保存引用和安全摘要；返回 endpoint 的认证 header、token 和动态地址不能原样进入日志或审批页面。

### 4.4 超时、日志与恢复

保留每个命令的 wall-clock deadline；另设创建/就绪、传输和整个 Run 的上限。沙箱 TTL 只是兜底回收，不是命令 timeout。当前验证单工具预算已达 600 秒，而 OpenSandbox SDK 默认沙箱 TTL 为 10 分钟，必须显式留出创建、传输、核验与清理余量。

SDK 使用有界流式处理，禁用默认无限累积日志，并对控制端写入的 UTF-8 编码字节计数。SSE 是文本协议，不能声称它保留 subprocess 任意原始二进制输出；新 evidence 应写明编码/截断语义。stdout 与 stderr 各自保留现有预算；单条事件和传输响应也需有界。

取消、超时、日志洪泛后主动停止整个沙箱并验证清理。只关闭 HTTP 连接或只 interrupt 一个命令，不能证明子孙进程已停止。返回成功必须有完整命令终态及明确 exit_code=0，SSE EOF、HTTP 200 或 SDK 对象返回都不够。

创建后立即持久化 sandbox_id/关联 metadata；如果创建已发生但响应丢失，按 Run/请求关联查询，不盲目重试 create。如果无法消除不确定性，保留失败证据并依 TTL/reaper 清理。恢复、清理与重试不得删除其他 Run，失败不能产生交接。

## 5. 按什么顺序行动

| 步骤 | 具体工作与产物 | 继续条件 |
|---|---|---|
| 1：本机可行性实验 | 固定 1.1.1；fixture 上验证原生 argv、身份、网络、文件、后台进程、输出收尾；写真实事实与缺口 | 能说明新策略可实施；取得停止后稳定输出；清理能确认 |
| 2：确定合同 | 小型 ADR/规格更新：新证据 schema、镜像 identity、策略/审批指纹、本机与远端文件语义；旧证据处理 | 明确选择保留哪些旧保证、哪些需调整；不确定结果失败关闭 |
| 3：实现最小 Adapter | 新 OpenSandboxWorker；在组合入口注入；迁移 apply/verify/可信 apply 的运行时证据检查；配置/依赖/fixtures 同步 | 固定命令、预算、日志、停止、传输、清理走一个实现；上层不直接调 SDK |
| 4：真实端到端验收 | fixture 的 proposal→apply→输出审批→verify→Handoff；失败→repair proposal→新 Run；覆盖下表故障 | mock 与真实测试都通过；必需环境的 CI 不得把真实测试 skip 当通过 |
| 5：本机显式切换 | 先让新任务显式选择 OpenSandbox；观察错误和资源残留；之后才改默认 | 新旧 Run/审批不串用；不自动 fallback；回切仅用于未来的新 Run |
| 6：独立服务器，再评估 K8s | 加可信文件导出、TLS/入口认证、配额与管理侧 evidence；K8s 再验证 RBAC/CNI/RuntimeClass/存储/删除终态 | 相同业务合同与安全验收通过；不能仅换 endpoint 就称已迁移 |

**下一步最有价值的工作是步骤 1。** 不先删除 DockerWorker，不先增加 K8s 控制器、沙箱池或多后端配置框架。可行性失败时的选择是：定制必要的 Server 策略、明确调整执行安全合同，或继续使用现有 DockerWorker。不是去掉失败的检查。

本地 bind 可以作为第一版受限实现；如果采用，应在实现处标记 `shortcut: 仅支持同机 Run 专属 bind；接入独立服务器前增加可信快照传输`。文件位置留在控制端合同中，但不要提前实现对象存储、缓存、快照池等需求。

## 6. 接入前后的验收清单

| 场景 | 必须看到的结果 |
|---|---|
| 原生 argv 含空格/引号/美元符号/分号 | 按参数执行，没有 shell 展开；固定工具与 cwd 正确 |
| 身份、caps、rootfs、tmp、PID/CPU/内存 | 实际事实与已批准的新策略一致，不靠 create 参数推测；禁止任务提升管理权限 |
| DNS、公网 IP、IPv6、metadata、宿主/内网/其他 Run | 超出批准范围的访问失败；管理接口自身不可被任务越权利用 |
| 访问 baseline、正式 worktree、控制证据、Provider/Server 密钥 | 不可见；仅 Run execution 可写 |
| 新增/修改/删除、空文件、Unicode、Git 模式 | 输入/收回文件字节和 manifest 一致；控制端 diff 能正确重放 |
| 路径穿越、链接、特殊文件、大小写碰撞、超额归档 | 在本地发布前拒绝；无 worktree 写入和 Handoff |
| 超时、取消、输出洪泛、子孙进程/后台写入 | 停止整个工作负载、输出有界、清理确认；不留下可继续改写输出的任务 |
| HTTP 断开、SSE 缺终态、创建响应丢失 | 不推断成功，不重复副作用，不 fallback |
| Server/客户端重启、TTL 到期、destroy 失败 | 对账并回收；失败/不确定有证据，未清理不能放行 |
| 错误 uid/镜像/策略、伪造 manifest/日志/receipt、跨 Run 返回 | 绑定校验失败；旧审批不可用 |
| pytest 返回非零，或真实 pytest/Ruff/mypy 成功 | 非零保留失败诊断供受控 repair；只有全部验证且输出未漂移才产生 Handoff |
| 旧 Docker Run、新 OpenSandbox Run、配置切换 | 历史审计可读，不改写旧证据；当前 Run 继续用冻结配置 |

复用 [现有 Docker 真实验收](https://github.com/Liu-Ame-Yi/YiPilot/blob/bc189b11b08ed1ddd9cf96aea5f12c9982ed5a73/tests/integration/test_docker_worker.py)、[源码/输出攻击 fixture](../../tests/integration/test_sandbox_sources.py)、[请求与结果合同测试](../../tests/unit/domain/test_sandbox.py)，并增加 OpenSandbox 的网络传输/生命周期故障。更新 [组合 fixture](../../tests/integration/test_project_application.py)、[CLI 测试](../../tests/unit/transport/test_cli_sandbox.py) 与 [职责边界测试](../../tests/unit/architecture/test_module_boundaries.py)。

K8s 阶段另查：controller/CRD 与所选 provider 的兼容矩阵、每租户身份与 Namespace/RBAC、禁止 automount ServiceAccount token、CNI 的真实策略执行、RuntimeClass 与 egress 兼容性、PID/ephemeral-storage/总配额、PVC/volume 生命周期，以及删除 API 返回与 Pod 实际停止之间的差异。不同 runtime 的能力不能通过统一 SDK 名称推断。

## 7. 调研边界

本次核对了当前代码与官方版本资料，给出候选设计与验收门槛；**没有安装 SDK/Server、启动 Docker/WSL/K8s、运行沙箱安全探针、修改执行代码或跑业务测试**。这份报告不能证明当前电脑或 OpenSandbox 满足迁移后的策略。

以下官方核验提供外部接口事实；上面的代码映射、阶段顺序与 Go/No-go 条件是结合 YiPilot 的工程建议。

---

## 8. 官方接口与固定版本源码核验

调研日期：2026-10-13（Asia/Shanghai）。以下逐项区分官方事实、源码推导与仍需实测的事项；基线为 release-1.1.1。未安装或运行 OpenSandbox。

### 8.1. 项目身份和版本基线

- 此处指 Alibaba 发起的 OpenSandbox，现官方仓库为 `opensandbox-group/OpenSandbox`，Python 包为 `opensandbox`，服务端包为 `opensandbox-server`；不是 Kubernetes SIG 的 `agent-sandbox`，后者只是可选 Kubernetes workload provider。[官方仓库](https://github.com/opensandbox-group/OpenSandbox)、[固定版服务端配置](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/configuration.md)
- 截至调研日，最新稳定版为 `release-1.1.1`，2026-10-09 发布，commit `531700ca18a7fa5fab6a2f25d6dc68a76eddab12`。现在使用统一版本线；旧 `server/v*`、`python/sandbox/v*` 标签不再代表当前发布。[1.1.1 release](https://github.com/opensandbox-group/OpenSandbox/releases/tag/release-1.1.1)、[固定 commit](https://github.com/opensandbox-group/OpenSandbox/commit/531700ca18a7fa5fab6a2f25d6dc68a76eddab12)
- 1.1.1 修复了 1.1.0 的服务端 wheel 缺失 gRPC 文件导致的导入失败；不建议用 1.1.0 开始试点。SDK、服务端和组件镜像应按同一固定发布线核查。[1.1.1 release](https://github.com/opensandbox-group/OpenSandbox/releases/tag/release-1.1.1)
- 官网跟随默认分支，可能描述尚未发布功能；本报告对关键兼容性优先引用 1.1.1 源码。Python SDK 要求 Python ≥3.10，直接依赖包括 pydantic 2、httpx、httpcore、httpx-sse、attrs、python-dateutil、opentelemetry-api。[SDK 总览](https://open-sandbox.ai/sdks/)、[固定版 pyproject](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/sdks/sandbox/python/pyproject.toml)

### 8.2. 命令契约：可保留 argv，但不能直接当作 subprocess

| 项目 | 核查结果 | 接入时的含义 |
|---|---|---|
| 同步/异步 | Python 提供 `SandboxSync` / `ConnectionConfigSync` 和异步 `Sandbox` / `ConnectionConfig` | 同步应用可直接用同步 SDK，无需为接入改造整个调用链 |
| argv | 1.1.1 的 `commands.run` 接受 `str \| list[str]`；list 使用 native argv，str 使用 shell | 将现有已验证参数序列转为 list；不要拼接 shell 字符串 |
| cwd、env、uid/gid | `RunCommandOpts` 提供 `working_directory`、`envs`、`uid`、`gid`；gid 要求同时提供 uid | 目录为沙箱内路径；命令身份和文件 API 身份分别配置 |
| 输出 | `Execution.logs.stdout/stderr` 是 `OutputMessage.text` 的列表，文本为 str | 不能假设与原始 subprocess bytes 完全一致 |
| exit status | `Execution.exit_code` 可为 None；background 返回启动确认，最终结果用 status API 查询 | None、缺失完成事件、连接断开都不能当作成功 |
| 独立 deadline | command timeout、HTTP timeout、sandbox TTL、readiness timeout 各自独立 | 明确设置各项预算，而非复用一个含混的 timeout |

同步 SDK 与资源清理示例：[Python SDK](https://open-sandbox.ai/sdks/python)。argv、cwd、身份契约：[固定版 Commands 接口](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/sdks/sandbox/python/src/opensandbox/services/command.py)。输出、可空 exit_code、opts 定义：[固定版 execd models](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/sdks/sandbox/python/src/opensandbox/models/execd.py)。

- 前台执行通过 SSE；固定版 API 明确 stdout/stderr 文本包含原始行结束符，可按 stream 逐个拼接，最后一块可以没有换行。它没有承诺任意非法 UTF-8 字节原样往返；需要原始字节时用产物文件回收。避免使用 `Execution.text` 或 `str(execution)` 生成审计原始输出：源码会规范化换行、去掉结尾换行并混入展示内容。[固定版 Execd API](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/specs/execd-api.yaml)、[固定版 models](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/sdks/sandbox/python/src/opensandbox/models/execd.py)
- `ExecutionHandlers.skip_accumulation=True` 才会让 stdout/stderr 仅送 callback、不堆积到 SDK 日志列表；默认 False。回调仍需实现自己的整体输出上限，不能只截断最后展示文本。[固定版 models](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/sdks/sandbox/python/src/opensandbox/models/execd.py)
- SSE client 的 `read=None`、`pool=None`，并绕过自动 retry transport。`ConnectionConfig.request_timeout` 并不自动限制整个前台执行的墙钟时间。`RunCommandOpts.timeout` 默认 None，不设置就没有服务端命令执行上限。[固定 commit CommandsAdapter](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/sdks/sandbox/python/src/opensandbox/adapters/command_adapter.py)、[固定版 models](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/sdks/sandbox/python/src/opensandbox/models/execd.py)
- SDK 暴露 `interrupt(execution_id)`，但核查的 adapter 没有在客户端 coroutine 取消时自动调用 interrupt。客户端 HTTP 错误或取消不等于远端工作已经停止；需要记录 execution ID、显式终止，并确认停止/销毁。没有核实该版本在所有断连场景中对子孙进程的终止保证。[固定 commit CommandsAdapter](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/sdks/sandbox/python/src/opensandbox/adapters/command_adapter.py)
- background 日志 API 返回混合的 plain text 和 cursor，不能拿它替代要求严格分开 stdout/stderr 的前台契约；完成 metadata/logs 至少保留 24 小时，依赖沙箱仍存在。[固定版 Execd API](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/specs/execd-api.yaml)

### 8.3. 生命周期：清理和结果稳定性必须独立设计

- Python 创建默认 TTL 为 10 分钟；`timeout=None` 表示必须手动清理。默认 entrypoint 为 `tail -f /dev/null`，默认 resource 为 1 CPU / 2Gi。`ready_timeout` 默认 30 秒，负责 endpoint publication 和健康检查。[固定版 Sandbox](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/sdks/sandbox/python/src/opensandbox/sandbox.py)
- `connect(id)` 连接已有运行实例；`resume` 返回新的本地 handle。`close()` 和 context-manager 退出只释放本地客户端；`destroy()` 才先 kill、再 close，而且 kill 失败仍 close 并重新抛出终止错误。[Python SDK](https://open-sandbox.ai/sdks/python)、[固定版 Sandbox](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/sdks/sandbox/python/src/opensandbox/sandbox.py)
- `renew(duration)` 设置 `now + duration`，不是在现有剩余 TTL 上增加；会缩短现有 expiration。服务端最大 timeout 配置只限制创建时请求，不限制续期后的总寿命。[固定版 Sandbox](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/sdks/sandbox/python/src/opensandbox/sandbox.py)
- Docker provider 的 delete 先 kill 主容器，再 `remove(force=True)`，并清理 egress sidecar/自动管理卷；pause 调用 Docker container.pause，主容器中的 execd 也被冻结。[固定版 Docker service](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/services/docker/docker_service.py)
- **据源码推导**：命令完成不等于全部写入者停止；`pause → SDK download` 会冻结提供 download 的 execd；`destroy → SDK download` 会失去容器文件 API。因此不能照搬这两种顺序声称结果已稳定。应先设计有可信边界的冻结/停止和导出机制，再证明取得的是不可变结果。[固定版 Docker service](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/services/docker/docker_service.py)
- **本地试点候选方案，尚未验证**：按 run 建立独立 host bind 工作目录，停止并删除沙箱及 sidecar后，由可信宿主进程扫描仍存在的 run 目录；命令日志提前保留。远程部署需服务端可信导出或保留卷/快照机制。host bind 能保留目录不自动解决并发外部写入者、权限、路径安全和残留卷清理。[固定版 volumes](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/services/docker/volumes.py)

### 8.4. 文件接口和 Windows 本地试点

- SDK 支持文本、bytes、IOBase 写入，以及 `read_bytes` / `read_bytes_stream`；目录、元数据、权限接口存在。`read_file` 的 offset/limit 是行范围，不是字节范围，字节读取用 HTTP range。[固定版 Filesystem 接口](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/sdks/sandbox/python/src/opensandbox/services/filesystem.py)、[固定版文件模型](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/sdks/sandbox/python/src/opensandbox/models/filesystem.py)
- 没有在核查的 public Filesystem 接口找到通用目录 tar 上传/自动解压/目录快照导出入口；可上传 archive 为 bytes，但解压仍是需要自行定义的命令和校验环节。binary 能传输，不代表应放宽现有 source/output 安全规则。[固定版 Filesystem 接口](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/sdks/sandbox/python/src/opensandbox/services/filesystem.py)
- host volume 地址属于服务端/daemon 侧；Docker provider 用 `os.path.realpath`、allowlist 检查，并在服务端 `os.makedirs`。服务端和 daemon 的路径视图应对齐。默认 `allowed_host_paths=[]` 拒绝请求级 host bind。[固定版 volumes](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/services/docker/volumes.py)、[固定版配置](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/configuration.md)
- Docker provider 先向容器写入 `/opt/opensandbox/execd`、bootstrap 和辅助程序，再启动。bootstrap 默认创建/触碰 `/opt/opensandbox/.env`；启用生命周期时还写 `/tmp`。只读 rootfs、非 root UID、运行目录和工作卷必须一起验证，不能直接搬用原镜像而假定可启动。[固定版 runtime](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/services/docker/runtime.py)、[固定版 bootstrap](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/components/execd/bootstrap.sh)
- 普通文件客户端使用默认 daemon identity；1.1.1 的 `files_with_identity(uid,gid)` 选择 Linux 文件访问身份，要求 execd 有权限建立该身份，不会失败后退回默认身份。它不是 tenant boundary。命令 uid/gid 另行设置。[Execd 官方说明](https://open-sandbox.ai/architecture/data-plane/execd)
- **未核实**：Windows Python controller + WSL2 lifecycle server + Docker Desktop daemon 的具体挂载映射、UID/权限、nft 可用性。官方 quick start 要求 Docker/Python，不构成该拓扑的通过证明。建议 Linux 服务端与临时 run 目录同一 WSL/Linux 路径视图，Windows 应用通过 HTTP SDK 连接；这是试点建议，不是已验证兼容保证。[官方 quick start](https://open-sandbox.ai/getting-started/)、[固定版 runtime](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/services/docker/runtime.py)

### 8.5. 网络和认证：deny egress 与 network none 的边界不同

- OpenSandbox commands/files 通过 execd HTTP 服务访问；Docker 通过 host 映射端口/reverse proxy 暴露它。管理面连通性存在，因此不能把 egress deny 声称为容器只有 loopback。[官方单机网络](https://open-sandbox.ai/architecture/network/single-host-network)
- **固定版 schema** 默认 `docker.network_mode="host"`；官方单机说明把 bridge 描述为默认单机形态，二者不一致。必须显式设 bridge，不能依赖缺省值。`publish_host` 默认 0.0.0.0，会发布到所有接口；本机可选 127.0.0.1，但 server 在容器内时须选择它可达的 host 地址。[固定版 DockerConfig](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/config.py)
- Docker 只有提交 `networkPolicy` 时才部署 egress sidecar；network_policy 缺失不等于禁网。固定版 egress.mode 默认 dns，只限制 DNS 名称，不阻止绕过 DNS 的 literal IP；packet 层限制必须用 dns+nft。默认 disable_ipv6=True，配置文档明确 IPv6 覆盖不完整。[固定版配置](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/configuration.md)
- egress 共享主容器网络 namespace，只有 sidecar 保留 NET_ADMIN；平台 always deny/allow overlays 可能影响实际规则。gVisor netstack 不支持此 sidecar 拦截机制；官方建议需要该功能时使用 Kata，不能无条件组合 gVisor 和 dns+nft。[固定版 egress 架构](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/docs/architecture/network/egress.md)
- policy 返回/readback 不是完整安全验收；需要实测公网 IP、DNS、IPv6、云 metadata、宿主/内网、其他 sandbox、execd/egress 管理端点，以及配置失效、sidecar 失败后的行为。上列为根据网络结构提出的验收项，未在本机验证。[固定版 networking](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/services/docker/networking.py)
- lifecycle API key header 为 `OPEN-SANDBOX-API-KEY`；server.api_key 空时可无认证启动，但非交互模式要求显式 insecure acknowledgment。SDK `use_server_proxy=True` 可让 commands/files 请求经过生命周期 server，减少客户端直接访问数据面的需要。[官方配置](https://open-sandbox.ai/getting-started/configuration)、[Python SDK](https://open-sandbox.ai/sdks/python)
- execd 的 `X-EXECD-ACCESS-TOKEN`、egress token、Kubernetes gateway 的 `OpenSandbox-Secure-Access` 不是同一种凭据。Docker 拒绝 `secureAccess=True`；不能因 lifecycle 有 API key 就推断公开的 Docker execd 端口已获保护。[Execd](https://open-sandbox.ai/architecture/data-plane/execd)、[Secure Access](https://open-sandbox.ai/guides/secure-access)、[1.1.1 release](https://github.com/opensandbox-group/OpenSandbox/releases/tag/release-1.1.1)
- SDK protocol 默认 http；需要 HTTPS/TLS 时要部署匹配的可信证书和入口；没有核实 server 原生 TLS 配置。server proxy 到 daemon/execd 的内部路径也需要信任边界设计，不能只校验外层 HTTPS。[Python SDK](https://open-sandbox.ai/sdks/python)

### 8.6. 硬化差异与可验证事实

| 当前 1.1.1 Docker provider | 源码事实 | 迁移含义 |
|---|---|---|
| Capabilities | 默认只 drop 9 个指定 capability，配置可改 drop list | 不等于 cap-drop ALL；ALL 后的启动/文件身份能力必须实测 |
| 提权 | no_new_privileges 默认 True，支持指定 AppArmor/seccomp | 把配置写清并验证实际运行事实 |
| 进程限制 | pids_limit 默认 4096 | 显式选择适合当前任务的上限 |
| CPU/内存 | resource 转换为 nano_cpus/mem_limit | 未看到显式 memswap_limit；不能假定保留禁 swap 语义 |
| 用户/rootfs | create_container 不传 user；base host config 不传 read_only | 镜像 USER 会影响身份，但平台未替接入方强制非 root/只读 rootfs |
| tmpfs/logging | 普通 base host config 无 tmpfs/log_config 参数 | 不等于原有 tmpfs 或关闭 Docker logs 策略 |

配置默认：[固定版 DockerConfig](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/config.py)。创建和资源映射：[固定版 container_ops](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/services/docker/container_ops.py)。

- `bootstrap.execd.isolation=enable` 会添加 SYS_ADMIN、AppArmor/seccomp unconfined，并清除 Docker MaskedPaths/ReadonlyPaths。该扩展不能作为保留现有硬化策略的便捷开关。[固定版 Docker service](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/services/docker/docker_service.py)
- Docker provider 的 `_ensure_image_available` 在镜像缺失或平台不匹配时自动 pull；核查的创建路径没有用请求中的 pull policy 保证 `Never`。因此预加载镜像只是减少拉取，不是严格禁止普通 Run 拉取。若保留旧 Worker 的 `--pull=never`，需要服务端强制失败关闭或独立的部署约束，并测试缓存消失时的行为。[固定版 container_ops](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/services/docker/container_ops.py)
- OpenSandbox 默认强隔离配置为空，即默认 OCI runtime；gVisor/Kata 需要宿主运行时、Kubernetes RuntimeClass 等实际部署。runtime profile 是 server 级配置；不能把换 SDK 描述为已经获得 VM 级隔离。Kata 需要 Linux/KVM 和硬件虚拟化，尚未验证 Docker Desktop 的此种部署。[固定版 secure-runtime 指南](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/docs/guides/secure-container.md)
- SDK 的资源请求、健康状态、policy 或 platform echo 不能替代全部 `docker inspect` 安全事实。当前 lifecycle Sandbox 响应不提供全面 container HostConfig/Image ID 审计；Docker provider 返回的 image URI 甚至可能是首个 tag/short ID。需要可信 server 侧执行前 attestation，或明确承认验证能力减少。[固定版 lifecycle spec](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/specs/sandbox-lifecycle.yml)、[固定版 Docker service](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/services/docker/docker_service.py)

### 8.7. 部署规模与后续 Kubernetes 边界

- 本地 Docker 最少新增 lifecycle server、execd artifact image；启用策略再新增 egress image/sidecar。业务 image 和工具链仍需自己固定版本、预装依赖。服务端持有 Docker daemon 权限，因此是可信控制面，不应把 socket/凭据挂入任务容器。[固定版 runtime](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/services/docker/runtime.py)、[固定版 Docker service](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/services/docker/docker_service.py)
- 官网 Kubernetes 部署流程包含 CRDs/base、controller、ingress gateway、server；Fast Sandbox 是可选组件。官网说明一般 server HA 尚不支持，默认单 active server；PostgreSQL 的多 active 例外仅针对特定 public snapshot 拓扑。[官方 Kubernetes 部署](https://open-sandbox.ai/deployment/)
- Docker bridge 每实例按固定配置文档需 2–3 host ports；CPU/memory 之外还须预算 egress 开销、source/output 大小、Docker Desktop VM 总资源和并发 create。服务端 `limit_concurrency` 不是任务调度并发额度；不要把 HTTP 连接上限当作 sandbox quota。[固定版配置](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/configuration.md)
- Pool/预热会引入隔离与清理新语义，Docker provider 不支持 poolRef；本地替换不需要马上引入 Pool/Redis/snapshot/Firecracker。[固定版 Docker service](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.1.1/server/opensandbox_server/services/docker/docker_service.py)

### 8.8. 尚未验证，作为试点 Go/No-go 条件

1. Windows controller → 本机 Linux OpenSandbox → Docker Desktop 真实端到端运行，API/数据面可达性和 bind 路径一致性。
2. 非 root、cap-drop ALL、只读 rootfs、需要的 runtime/tmpfs、pids/CPU/memory/swap 和 env 策略能否同时满足；不能靠请求参数回显宣布通过。
3. dns+nft deny-all 的实际管理面例外和绕过面，尤其 IPv6、metadata、host、同网段、其他实例和 tokenless data-plane 端口。
4. 超时/取消/断连后命令、派生进程、sandbox、sidecar 都停止；不能根据客户端 exception 宣布远端没有副作用。
5. 有可信且稳定的结果导出顺序；拒绝并发写入、恶意链接、超大 output、非法 byte/path；销毁失败时不得将结果作为已隔离且稳定的成功证据。
6. 执行前实际配置和镜像证据、execution ID、sandbox ID、run 对应关系、未知执行结果和 orphan 回收；没有核实端到端 exactly-once 或 create 幂等保证。

上述条件为接入设计的验收建议，不是官方已经承诺或本次已经测得的安全保证。本次没有部署服务、执行不可信代码或测量性能。

## 9. 代码整合与精简计划

> **补充要求：迁移时同时整合、精简代码。状态：Proposed。** 本节给出实际函数的归属、删除顺序与验收条件，不表示源码已重构。沿用本报告的工作树和外部版本基线；实施前须重新核对调用方与正在进行的修改。
>
> 范围限于沙箱及直接调用方，不同时重构模型、整个 Agent Runtime、SQLite 或全部目录。现有七个核心文件合计 3,320 行，其中 DockerWorker 为 768 行；这是物理行数基线，不是应当删掉的代码量，也不是性能指标。

### 9.1 最终结构与新增代码预算

沿用现有目录和公共入口，首版本生产代码最多新增两个有实际职责的文件：

```text
src/yipilot/
  app.py                             # 唯一组合入口，装配项目依赖与 Worker
  domain/sandbox.py                  # 已冻结请求、结果、版本化运行时事实
  workflow/sandbox_application.py    # 应用阶段、审批与 Run 状态
  workflow/sandbox_verification.py   # 三项验证、失败诊断和 Handoff 条件
  workspace/
    sandbox_runtime.py              # 拟新增：小 Interface 与离线 receipt 验证入口
    opensandbox_worker.py            # 拟新增：SDK、传输、停止、清理与运行时事实
    sandbox_sources.py               # 源码输入的唯一所有者
    sandbox_output.py                # 输出字节、manifest/diff 与输出批准的唯一所有者
    sandbox_apply.py                 # 可信 worktree 写入及重验
    docker_worker.py                 # 迁移期保留；切换收尾后退出执行路径
```

`sandbox_runtime.py` 不再创建一个“通用执行服务”去转发所有调用。Interface 的三项主职责是 doctor、完整 execute、持久化 verify_evidence；实际恢复补一项 reconcile 高层操作，见详细执行计划，避免 workflow 直接调用 SDK。创建/上传/命令/停止/下载/销毁留在具体 Adapter 内部。输入继续使用现有 `PreparedSources`、`ExecutionPolicy`、`SandboxRequest` 和 `SourceManifest`，避免新增含相同字段的 Context/Session/Manager 包装。

新文件中只有一处必要的有限运行时分派；执行选择按已冻结的 Run provider，历史阅读按证据版本。不要同时保留 app、workflow 和 receipt reader 三份后端选择逻辑。所有选择必须校验可信配置与 Run 绑定，不能依赖模型输出。

本机、独立服务器、K8s 都先使用同一 OpenSandbox Adapter。只有生命周期或文件合同实际出现差异时，才在 Adapter 内新增有测试的差异处理；不要提前创建 LocalOpenSandboxWorker、RemoteOpenSandboxWorker、K8sOpenSandboxWorker 三套业务链路。未来的可信远端导出能力需要真实接口与实现，不能用名字相同的 Adapter 掩盖缺失的能力。

### 9.2 重复逻辑具体整合到哪里

| 现有位置 | 整合目的地与做法 | 必须保留的差异 |
|---|---|---|
| [应用 load](../../src/yipilot/workflow/sandbox_application.py)、[验证 _copies](../../src/yipilot/workflow/sandbox_verification.py) 都重建 PreparedSources、读取 manifest/hash | 由现有 SandboxSourceService 提供同一加载与关联核验实现，复用有界 read_evidence | apply 的 stage/current-request 检查留在应用模块；verify 的父子 Run 与批准输入关系留在验证模块 |
| [Docker _validate_sources](https://github.com/Liu-Ame-Yi/YiPilot/blob/bc189b11b08ed1ddd9cf96aea5f12c9982ed5a73/src/yipilot/workspace/docker_worker.py) 的冻结输入/seed 校验 | 把与 Docker 无关的输入规则收进 SandboxSourceService，两个 Worker 使用同一入口 | 执行前确认 pending/running 与冻结输入；输出阶段只检查 baseline 和安全性，不能要求 execution 仍等于输入 |
| [可信 apply _require_stopped_worker](../../src/yipilot/workspace/sandbox_apply.py) 与 [验证 _verify_tool](../../src/yipilot/workflow/sandbox_verification.py) 重复读取终态、清理、运行时配置 | 收拢到 sandbox_runtime 的有界 receipt 验证入口，按版本校验 provider-specific facts；Provider 的 live probe 仍在 Adapter | apply 另核对 accepted result；verify 另要求输出等于已批准 seed；这些业务条件不能丢掉 |
| [输出 validate_result](../../src/yipilot/workspace/sandbox_output.py) 与 [failed_diagnostics](../../src/yipilot/workflow/sandbox_verification.py) 重复核对结果关联、日志 artifact/hash | 在现有 SandboxOutputService 中保留一份结果关联与日志完整性规则，成功/失败路径复用 | 输出发布要求 exit=0；repair 只接受已终止且非零的代码失败，不能接受超时、断连、清理失败等基础设施故障 |
| apply/verify 都列出 result/container-after/cleanup 并 register | 运行时证据由 Adapter/receipt 所有者写入并注册，结果用现有 ArtifactRef 引用；workflow 只登记本阶段产物 | 运行时收据的完整集合、请求指纹与 producer 要一致；失败日志和清理失败证据同样要保留 |
| app 与相关构造函数反复创建 store/git/output/apply | 在 ProjectApplication 与触及的 workflow 内集中装配、复用已有实例 | 不建全局单例或 DI 容器；每个 Run 的 journal/锁/沙箱/客户端仍独立，SQLite 事务提交语义不变 |

**共享的是检查实现；执行前、输出审批前、apply 前、verify/Handoff 前的重新检查仍需保留。** 文件可能在这些时刻发生变化，不能因为先前检查通过，就缓存一个布尔值并跳过后续校验。

源码扫描与 diff 扫描次数的减少应以具体文件事实可安全复用为前提。第一版不缓存跨阶段 manifest，也不引入 `already_verified`、`skip_checks` 等开关。

### 9.3 保留、迁出与删除清单

| 分类 | 具体处理 | 完成门槛 |
|---|---|---|
| 保留职责 | sandbox_sources、sandbox_output、sandbox_apply 分别负责输入、输出证据、可信 Git 写入 | 调用链缩短后，过滤/攻击 fixture 和精确字节重放仍通过 |
| 保留业务流程 | apply/verify/repair 继续用现有 ProjectApplication 和 workflow | 不新增第二个 SandboxApplication/统一工作流入口，不把三段流程合成一个大类 |
| 保留实际使用者 | LocalCommandRunner、ToolRegistry、RegistryToolGateway 当前仍被代码/质量流程调用 | 不因为 Worker 替换而直接删除，也不让它们成为沙箱失败时的 fallback |
| 将测试专用实现迁出 src | ControlledWorkspaceWriter 在 src 只有定义，当前调用方只有 [test_code_harness](../../tests/integration/test_code_harness.py)；替换/迁入已有 [测试 executor helpers](../../tests/helpers/fake_executor.py) | 保留该测试对批准、cwd 与 worktree 的对抗覆盖；使用公开接口，避免将生产私有 helper 暴露成新的公共 API |
| 合并重复 helper | PreparedSources 加载、seed 绑定、receipt 读取、结果/log 关联检查各一份 | 所有调用方切换；合并前后的错误语义和安全条件逐项对照 |
| 删除旧执行代码 | OpenSandbox 接入验收后，移除 Docker CLI/named-pipe、create/start/attach/kill/remove/probe、旧实时 doctor 及只服务旧执行的 fixture | 默认已切换；在途/未知结果已对账；旧 runtime 无正式执行调用方；新路径具备实测证据 |
| 保留最小历史阅读 | 旧 schema/旧路径及 Docker 事实校验只供历史阅读，移出可执行 Worker 的纯验证部分，收在 sandbox_runtime | 不改旧 JSON/hash，不把旧 receipt 转成新结果，不允许历史记录启动执行 |
| 删除迁移包装 | 后端兼容构造、已无调用的 factory、重导出和选择分支 | 仅有 OpenSandbox 执行时删除执行选择；必要的历史 schema 分派可保留 |
| 删除陈旧测试与文案 | 仅删除“已删除实现的内部细节测试”；更新 Docker 专用提示、CLI help、README/guide | 原有业务、安全、审批与故障场景在新 Interface 上仍被覆盖；测试总量减少不能代替场景核验 |

迁出 ControlledWorkspaceWriter 时要保留测试能力，不能简单删除测试让“生产无 fake”检查变绿。它是固定 marker 测试用实现，其现有 SANDBOXED 标签不证明发生了容器隔离。

不要为本轮统一所有 JSON 序列化 helper。现有 [fingerprint_model](../../src/yipilot/domain/execution.py)、manifest canonical_bytes、verification context 的序列化字节各有现存消费者。排序、空格、ASCII 转义或结尾换行的变化都可能改变审批 hash。新 schema 才采用确定的新表示，旧 evidence 按旧字节验证；不能把 `atomic_write_json` 机械替换所有 JSON 写入。

### 9.4 分批实施，精简跟随迁移

以下是本次候选改动的批次，不覆盖 [现行 roadmap](../product/roadmap.md)。每批在完整调用方与 fixture 更新后才进入下一批。

| 批次 | 可审查的工作 | 验收与精简要求 |
|---|---|---|
| A：基线与去重 | 记录当前受影响测试结果；合并 receipt 读取/核验与输入加载；迁出测试 marker writer | 尚不改变真实 Docker 策略；先证明重复实现减少、安全场景保留，避免把已有失败归因于迁移 |
| B：本机可行性 | 单独用 fixture 验证 OpenSandbox 运行、安全、文件收尾 | 第 6 节核心探针通过或明确失败；决定新策略能否承担正式任务，此批不切业务默认 |
| C：合同与 Adapter | 加新 schema、两个必要文件、固定 SDK/config；Worker 内实现有界日志、传输与清理 | 新路径通过合同/故障测试；保留旧结果读取；不返回 SDK 对象给 workflow |
| D：完整接入 | ProjectApplication 统一装配；apply/verify/apply-check/repair/Handoff 都使用同一 evidence Interface | 真实本机闭环和失败闭环通过；workflow/terminal 没有原始容器 JSON 或 SDK 生命周期调用 |
| E：切换与删除 | 切换新任务默认；对账旧任务；删除旧 Docker 实时执行与临时选择包装 | 历史证据仍可审计；新执行无旧 backend 依赖；保留所有安全场景，更新文档与 help |
| F：远端扩展 | 只实现当前部署所需的可信导出、入口认证及管理证据 | 尽量只触及 OpenSandbox Implementation/配置及测试，新增 deployment 不再复制 workflow |

A 中去重不能依赖 B 的沙箱已成功启动，可独立验证现有行为；C/D 的正式接入取决于 B 通过。不要把 A 扩成全仓库格式化、改包名或 RunExecutor 重写；现有 RunExecutor 围绕模型/tool 协议工作，暂不强行用它包裹沙箱流程。

在长期网络执行或文件传输期间不持有 SQLite 写事务。状态/关联写入先提交，外部动作后记录结果；超时/未知结果沿用已有不重放规则，避免整合成一个“大事务”而长期阻塞或伪装外部动作可回滚。

### 9.5 怎样判断“确实精简了”

完成时报告以下数字和证据，而不是先承诺减少一个未经测量的百分比：

- **依赖减少：** 当前生产中直接 import DockerWorker 的位置有 3 个；目标迁移后 workflow、可信 apply、terminal 不直接依赖具体 Worker 或 OpenSandbox SDK。
- **规则收敛：** 输入关联/seed 检查、receipt/清理核验、runtime artifact 注册各有唯一实现；不同阶段仍调用它重新验证。
- **新增受控：** 本机首版生产新增至多 2 个沙箱模块，不加依赖容器、插件框架、多层 factory、模板化工作流或部署占位类。
- **删除有依据：** 列出合并/迁出/删除的函数和所有旧调用方；旧执行删除后区分生产、历史阅读和测试代码行数，记录最终净变化。迁移期双实现暂时增大代码是正常现象。
- **业务证明：** `/apply → 输出批准 → /verify → Handoff`、拒绝批准、代码失败 repair、新 Run、超时/取消/清理失败均通过；不改变正式 worktree 写入时机。
- **保护证明：** 现有源码/路径/链接/日志/审批漂移案例继续通过；不能以删除重验、缩减输出验证、忽略未知结果或清理错误实现代码减少。
- **结构证明：** 更新并通过 [职责边界测试](../../tests/unit/architecture/test_module_boundaries.py)，明确禁止 terminal 调 OpenSandbox，禁止 workspace 编排业务；SDK 只允许进入新的 Adapter。

实施验证使用已有 pytest/Ruff/mypy 配置，并覆盖相关 CLI help；真实沙箱测试单独标记、在所需 CI 环境必须执行。只做文档规划时不运行这些业务测试，也不据此声称整合已经完成。

## 10. 2026-10-14 措施补查与剩余门槛

本节仍固定 1.1.1 commit `531700ca18a7fa5fab6a2f25d6dc68a76eddab12`，通过官方源代码补查；未部署、安装或运行探针。下面明确区分配置、需维护的补丁与待测推导。

### 10.1 本机拓扑与配置可解决项

- **优先 POC 拓扑（建议，待测）：** Windows controller → HTTP → 一个 WSL2 Linux 发行版内的 lifecycle server → Docker Desktop 的 Linux engine；只启用该发行版的 WSL integration，不再装第二个 WSL Docker Engine。任务、execd artifact 都用 Linux 平台；Windows controller 不意味着设置 `platform.os=windows`。官方 Windows profile 是 Linux 容器内 KVM/QEMU Windows guest，需要 /dev/kvm、/dev/net/tun，属于另一路部署。[Docker WSL2](https://docs.docker.com/desktop/features/wsl/)、[固定版 Windows profile](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/docs/guides/windows-sandbox.md)
- 服务端、临时 run 目录和可信扫描器先放同一 Linux 路径视图；Windows 的源文件通过受限上传/复制进入 run 目录。host bind 是 daemon 侧路径，不能直接把 `D:\\...` 当成 Linux server path；只允许独立 fixture/run 根目录。要测“服务端检查的目录、daemon 实际挂载的目录、删除后扫描的目录”确为同一内容，及 UID/GID、大小写、链接规则。[Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)、[固定版 volumes](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/volumes.py)
- **Compose 备选：** 官方示例用 bridge、`host_ip=host.docker.internal`、`proxy.resolve_internal=false`、host-gateway 和 Docker socket；server 容器不能将宿主 loopback 当自己的 loopback，也不能把仅存在于 server 容器内部的目录传给 daemon。示例仍含旧 server/execd/egress tags，不能直接照抄；先固定已解析的 1.1.1 官方组件 digest、API key、宿主发布地址，再验证 endpoint/proxy。Compose 比 WSL 进程多一层网络/路径映射，暂不同时推进。[固定版 Compose](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/docker-compose.example.yaml)
- 可直接明确配置 `drop_capabilities=["ALL"]`、no-new-privileges、pids、CPU/memory、bridge、dns+nft、publish_host、路径 allowlist；execd artifact 经 Docker 临时容器获取并注入任务容器，不依赖 Windows 可执行 execd。控制面 Docker socket 只给可信 server；这些设置仍不是第 10.3 节管理面风险的充分修复。[固定版 config](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/config.py)、[固定版 runtime](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/runtime.py)

### 10.2 身份与只读 rootfs：所需最小 Server 改造

- stock Docker create 不传 user；用受控 workload image 的 `USER 10001:10001` 可使 bootstrap/execd/命令同身份运行。相同 UID/GID 的 command 和 filesystem worker 有免切换路径；跨身份切换需要 CAP_SETUID/CAP_SETGID，cap-drop ALL 下应失败。因此本机先选 execd 与任务同非 root 身份，不靠 root execd 再给单次 command 填 uid 来表示整体非 root。[固定版 credential](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/runtime/command.go#L104)、[固定版 filesystem worker](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/runtime/filesystem_worker_linux.go#L59)
- **补丁位置已确定，兼容性待测：** 在 `DockerConfig` 增加仅管理员可设的固定硬化字段；`DockerContainerOpsMixin._base_host_config_kwargs` 注入 `read_only`、`memswap_limit`、`tmpfs`、`log_config`；`_create_and_start_container` 的 create_container kwargs 强制 `user="10001:10001"`。不要开放任意 request HostConfig 或通过用户 env 控制策略。[固定版 container_ops](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/container_ops.py#L335)
- **不能只加 read_only 参数：** stock `DockerRuntimeMixin._prepare_sandbox_runtime` 使用 put_archive 写 /opt/opensandbox；bootstrap 还 touch EXECD_ENVS、生命周期同步写 /tmp。候选方案是受控镜像预置并校验固定 execd/bootstrap/launcher，硬化 profile 下验证已有 artifact 后跳过运行时覆盖；/opt 程序不可被任务修改，/tmp、HOME、工作目录使用独立且有界的可写挂载，EXECD_ENVS 只放受控非敏感内容。该方案需要 Server 分支/镜像维护和 read-only 启动实测，未证明 stock 注入与只读 rootfs 自动兼容。[固定版 runtime](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/runtime.py#L378)、[固定版 bootstrap](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/bootstrap.sh)
- 1.1.1 已有 opt-in `runtime.execd_run_as_init` 与 `[hardening]` launcher，但层可能报告 disabled/degraded/unsupported，缺能力或 launcher 时继续运行；未设 EXECD_INIT 时 entrypoint 不受全覆盖。不能用“已开启 hardening”代替强制非 root、不可变程序、管理面隔离或执行前事实核验。[固定版 hardening](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/runtime/hardening_linux.go#L324)

### 10.3 正式接入硬门槛：任务访问沙箱内管理面

- **固定源码事实：** Router 将 /internal/init 排除于 token 校验；RuntimeInit=false 时该接口仍接受。main 无条件安装 RuntimeInitManager 并监听所有 IPv4 接口；Apply 支持 preserveRuntimeState 后直接替换 sandboxId/generation/accessTokenHash/env binding。请求校验检查格式，没有核查调用者身份或一次性初始化约束。[Router](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/web/router.go#L29)、[Flags](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/flag/flags.go)、[main](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/main.go#L184)、[Apply](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/web/controller/init_runtime.go#L205)
- **安全推导，未实测：** 能连本机 execd 的任务可能重绑管理 token/关联，即使没有原 token；外部 gateway API key 或隐藏宿主端口不会阻断同容器 localhost。root execd 还可能接受 uid=0 command 与默认/root filesystem 操作。同非 root + cap-drop ALL 仅限制 Linux 权限，不能证明任务无法启动额外 command、操作管理 session 或改变执行证据关联。stock 1.1.1 暂只承担无敏感 fixture POC，正式执行先解决此边界。[Router](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/web/router.go)、[command credential](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/runtime/command.go#L104)
- **最小 Execd 补丁候选：** Docker 经典启动不需要 runtime-init，显式禁用该 route/handler，而不是只取消 startup gate；初始化身份建立后保持不可变。需要 runtime-init 的未来部署应移到任务不可达的管理通道并校验可信调用者、代际/重放，而非共享同一用户可取得的 token。command、filesystem、PTY/session 等入口须统一约束固定执行身份及授权；只在 YiPilot adapter 拒绝 uid=0 不能阻止任务直接调用 execd。需要维护 Execd 分支、重建 artifact、完整对抗回归；Server 参数补丁无法单独修复 Router 问题。[Router](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/web/router.go)、[Init handler](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/web/controller/init_runtime.go#L175)
- **环境源代码事实：** prepareCommand 先继承 daemon Environ 再 overlay；默认关闭 hardening 时 launcher 不 strip。EXECD_ENVS 的未引用/双引用值调用 os.ExpandEnv；bootstrap 经典模式的用户 entrypoint 与 execd 从同一 shell 环境启动。因此不能只给请求一个 env allowlist，就宣称环境不继承；若任务可写 .env，可能将 daemon secret 展开到非黑名单别名（推导待测）。[command argv](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/runtime/command_argv.go#L48)、[env parser](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/runtime/env.go)、[launcher gate](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/runtime/hardening_linux.go#L518)、[bootstrap](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/bootstrap.sh)
- egress token 在核查路径只注入 sidecar env，不能据此声称它必然泄露给主容器；需测 env、/proc/其他 PID、进程参数、EXECD_ENVS/runtime 挂载能否暴露控制凭据，以及 localhost /command、/v1/filesystem/0/0、/internal/init、egress /policy 能否被任务读取/修改。通过条件应是实际拒绝及不可变管理关联，不能只验 HTTP 认证开关。[sidecar env](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/networking.py#L466)、[egress authorize](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/egress/policy_server.go#L889)

### 10.4 有界调用、停止确认与 lost-create 对账

- 同步用 `ExecutionHandlersSync` 的普通 Callable：`on_init(event)` 接收 ExecutionInit 并保存 `event.id`；stdout/stderr 回调接收 OutputMessage.text；`skip_accumulation=True` 跳过日志列表累计。callback 抛错会退出 stream，但不会自动 interrupt；SSE read=None，request_timeout 仅 connect/write，不能据此实现完整 deadline。[sync models](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/sdks/sandbox/python/src/opensandbox/models/execd_sync.py)、[sync dispatcher](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/sdks/sandbox/python/src/opensandbox/sync/adapters/converter/execution_event_dispatcher.py)、[sync adapter](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/sdks/sandbox/python/src/opensandbox/sync/adapters/command_adapter.py)
- **建议保持上层同步，仅 Adapter 内采用 async SDK：** 在私有调用范围运行 event loop，用独立总 deadline 取消 create/upload/run/download；日志 callback 同时计量字节并限额。finally 在主 deadline 之外，用独立有界 cleanup 路径执行 interrupt、destroy、对账；cleanup 取消/失败必须记录未知或未清理。不要让阻塞 sync SSE 只靠同线程 watchdog，也不要未经验证跨线程关闭共享 Client。asyncio.timeout 要求 Python ≥3.11，已有 loop 的调用环境不能嵌套 asyncio.run；这些是实现约束，取消 await 仍不等于停止远端。[async adapter](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/sdks/sandbox/python/src/opensandbox/adapters/command_adapter.py)、[Python cancellation/timeouts](https://docs.python.org/3/library/asyncio-task.html#timeouts)
- interrupt 向 command process group 发 TERM/KILL，但等待属 best effort；已结束 command 会拒绝晚到 interrupt，脱离原 process group 的子进程不能由此证明全停。destroy 主容器 kill+remove 失败会抛错，但 `_cleanup_egress_sidecar` 的 list/stop/remove 失败只 warning；destroy 成功与 getSandbox 404 不能证明 sidecar/runtime 卷已清。cleanup receipt 应由可信 daemon 按 sandbox/sidecar 标签确认全部不存在，再扫描保留的独立 run bind。[interrupt](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/runtime/interrupt.go)、[delete](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/docker_service.py#L1111)、[sidecar cleanup](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/networking.py#L414)
- lost-create 可在请求前持久化 run/attempt 标识并作为 create metadata，随后使用 `SandboxManagerSync.list_sandbox_infos(SandboxFilter(metadata=..., page=..., page_size=...))` 逐页找关联，再 get_sandbox_info/kill_sandbox 对账。Docker list 从实际标签容器发现并按 metadata 筛选；metadata 非唯一键、会修改、list 非永久审计，TTL 到期后对象可能消失。空列表不是 exactly-once 或“从未创建”的证明，重复匹配/查询失败/未确认销毁应进入未知状态并禁止自动重放。[ManagerSync](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/sdks/sandbox/python/src/opensandbox/sync/manager.py#L122)、[SandboxFilter](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/sdks/sandbox/python/src/opensandbox/models/sandboxes.py#L867)、[Docker list](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/docker_service.py#L1028)

本节的 Server/Execd 补丁仅为实施候选，不代表现有 1.1.1 已具备这些保证；每次上游升级需重跑初始化越权、环境/proc、身份、deadline、destroy 与 lost-create 对抗案例。
