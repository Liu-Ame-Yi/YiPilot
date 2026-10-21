# OpenSandbox 本机迁移、代码整合与精简：详细执行计划

Historical reference：本文保留当日设计；旧实时 Worker 已从当前工作树删除，旧源码链接固定到迁移前提交。当前实现与尚未通过的 G4 见[业务迁移记录](2026-10-21-opensandbox-business-migration.md)。

> **Historical reference / Superseded（实施路线）：** 后续已取得华为云节点，并将本地业务、远端 Runner 作为推荐接入方式。当前专项实施安排见 [2026-10-19 远程迁移计划](2026-10-19-opensandbox-implementation-plan.md)。本文件保留早期本机 SDK/collector 与补丁候选背景，不再按以下工作包直接续做。

> **日期：2026-10-14（Asia/Shanghai）。状态：Proposed，详细安排；尚未实施迁移。**
>
> 用户已明确的方向：先在本机模拟，之后可能接独立服务器/K8s；迁移同时整合、精简代码。本文件把这些要求落到具体措施、工作包、先后关系、验收与回切。当前旧 Docker 流程继续保持。
>
> 本文件细化本次专项请求，不覆盖[产品 roadmap](../product/roadmap.md)，不表示任何门槛已经通过。技术依据见[原调研及措施补查](2026-10-13-opensandbox-migration.md)。第 10 节的新证据优先于早期“只换 SDK/只改配置即可”的假设。

## 1. 推荐方案与现在能确定的决定

1. **迁移对象：完整沙箱执行及证据合同。** 保留 ProjectApplication、审批、源码过滤、控制端 diff、可信 worktree 写入、固定 pytest/Ruff/mypy 和有限 repair，不新增第二套业务入口。
2. **版本：固定 release-1.1.1 与源码 commit 531700ca18a7fa5fab6a2f25d6dc68a76eddab12。** Server、SDK、execd、egress、任务镜像分别登记精确版本/digest；升级是独立变更，不跟随 main/latest。[官方版本](https://github.com/opensandbox-group/OpenSandbox/releases/tag/release-1.1.1)
3. **原版范围：仅无敏感 fixture POC。** 新发现的管理初始化问题、环境继承和尽力执行的 sidecar 清理，必须先处理、实测后才可正式接入；本计划不建议直接把原版服务改为正式默认。
4. **代码：首版 YiPilot 生产新增至多两个沙箱模块。** 同一 Adapter 支持不同部署；上层同步调用，内部优先 async SDK + asyncio 有界期限与独立 cleanup。实际恢复还需要一项 reconcile 高层操作，接口不会为了维持“三个方法”而遗漏恢复能力。
5. **文件：本机先验证 Run 专属 bind + 停止后可信扫描。** baseline 与正式 worktree 不挂入任务。服务端和可信扫描器路径一致，Windows 控制端取得精确文件后重新验证；这个措施需明确的可信导出能力，SDK files 本身不提供保证。
6. **替换策略：迁移期双实现，完成后删除旧实时执行。** 旧 JSON/hash 只读审计；不把旧审批升级，不自动切后端、不自动重放未知操作。
7. **暂不建设：** 沙箱池、预热、对象存储、Firecracker、通用插件/DI 框架、全仓库改名或多套 Local/Remote/K8s Workflow。上游安全补丁有实际需要时单独计量，不能把代码搬到依赖后就称整体变简单。

## 2. 本次只读环境核查

| 项目 | 2026-10-14 取得的事实 | 对安排的影响 |
|---|---|---|
| 工作树 | HEAD 为 f7b6ec282e453a14c60811afa36ce5326444fd2a，已有多项 SQLite/业务/文档修改 | W00 必须保留既有修改，不能 reset/整体提交/按旧路径重建 |
| Docker CLI | 已安装，Client 29.8.1 | CLI 存在不等于 daemon 或镜像就绪 |
| Docker endpoint | default 的 docker_engine 与当前 desktop-linux 的 dockerDesktopLinuxEngine named pipe 均不存在/无法连接 | 本次未取得 Server OS/版本/资源；W01 是环境前置，不把空值或 0 当实测结果 |
| WSL | wsl.exe 存在；status/list 在当前执行环境返回 Wsl/E_ACCESS_DENIED | 不能认定未安装或发行版损坏；需在实际运行身份核查，不自动提权/改 ACL |
| 项目工具 | .venv 中 Python/pytest/Ruff/mypy 可执行文件存在；未找到 opensandbox 包目录 | 后续安装 SDK 是显式依赖变更；本次不安装 |
| 当前 CI | 现有 workflow 是 windows-latest 的 offline job | 本机真实验收与 Linux required sandbox job 需要新增安排；既有 CI 通过不能证明沙箱可用 |

本次没有启动 Docker/WSL、安装包、拉镜像、创建容器、读取 Provider 密钥、迁移数据库或运行业务测试。环境诊断失败不影响本轮完成文档规划。[现有 CI](../../.github/workflows/ci.yml)

## 3. 本机部署与路径安排

**首选待测拓扑：** Windows YiPilot → 本机受保护 HTTP 入口 → 指定 WSL2 Linux 发行版中的 Server → Docker Desktop Linux engine。只使用一套 daemon；任务 image/platform 固定 Linux，Windows controller 不采用 Windows guest profile。

先让 fixture controller、Server、可信导出/扫描动作使用同一 WSL 路径视图，验证服务端路径与 daemon bind 的内容一致；随后验证真正 Windows controller 的输入/输出往返。不要把 WSL fixture 成功写成 Windows 端到端成功，也不能直接传 Windows D: 路径给 Linux host volume。[本机拓扑补查](2026-10-13-opensandbox-migration.md)

- Server 使用独立 venv/部署目录，不把 opensandbox-server 放进 YiPilot 运行依赖。
- 生命周期入口、execd/egress 发布地址只允许必要的本机/容器访问。bridge、host_ip、publish_host、proxy resolution 均显式配置；本机仅在实际可达时用 loopback。远端必须配 TLS 与访问控制。
- 挂载只允许 Run 专属 execution。配置 prefix allowlist 不能代替每次请求的 Run/path/无链接验证；禁止 baseline、home、.git、正式 worktree、控制证据和 Docker socket。
- Compose 只作为首选不可行后的明确备选；它新增容器网络与路径映射，重新验收后才能采用，不能自动降级拓扑。
- Server 的 Docker 权限属于可信管理侧；不进入模型工具列表或任务环境。可信导出和 daemon 核查若必须经 WSL helper 实现，应只提供 Run 范围的有界操作，不提供任意 shell/host path 接口。
- 服务端路径映射与导出机制在 W07 固定下来，未证明 Windows 原始 bytes/删除/模式可回收前不开始正式接入。

## 4. 措施清单：配置、补丁与实测分开

| 问题 | 具体措施 | 需要改哪里 | 通过证据 |
|---|---|---|---|
| 原版 execd 初始化可重绑 | Docker 经典 profile 禁用不需要的 /internal/init route/handler；初始化身份建立后不可变；未来需要 init 时必须改用任务不可达且经过鉴权的管理通道 | Execd fork：router、init controller/runtime binding | 从任务侧尝试重绑被拒绝，sandbox/command 关联不变；不能只改 startup flag |
| 控制认证/凭据泄露 | 所有有副作用的管理入口鉴权；任务 env 不继承 daemon 控制变量；控制原始 token 不放任务可读的 env/argv/.env/proc；若同容器认证无法保护，需管理通道隔离 | Execd env/command/FS/session 及 bootstrap；受控镜像 | env、proc、.env 展开与本地 API 对抗失败；token 设置本身不算通过 |
| UID 与权限扩大 | task/execd 首先验证固定非 root 10001:10001、caps ALL drop、no-new-privileges；command/files 不允许请求其他 UID；非 root 不是管理面隔离的充分措施 | image USER + Server 强制 user + Execd 身份限制 | 实际 UID/caps 与跨 UID 拒绝；合法 same-UID 命令/文件读写仍能运行 |
| 根文件系统、swap、日志 | 管理员 profile 强制 read_only、memswap_limit、tmpfs、log_config；预置/校验不可变 execd/bootstrap，兼容只读启动与受控 EXECD_ENVS | Server config/container_ops/runtime；任务 image | 可信 inspect 与 probe 一致；不能只添加 read_only 然后继续 put_archive |
| 任意网络出站 | bridge + 每次创建显式 deny network policy + dns+nft，保留 IPv6 禁用；禁止 task 修改 egress policy | Server/egress 固定配置与 probe | DNS、IP、IPv6、host/内网/metadata/其他 Run 实际拒绝；不填旧 network=none |
| 普通 Run 拉取镜像 | 单独 preparation 预加载全部组件；Docker provider 镜像缺失/平台不匹配时强制失败，取消普通 Run 自动 pull | Server _ensure_image_available 的硬化 profile | 删除 fixture 镜像后 Run 不联网 pull；报 SANDBOX_IMAGE_MISSING |
| 命令/SDK 无总期限 | Adapter 内采用 async SDK、asyncio.timeout；callback 限输出；finally 在总期限之外使用独立 cleanup deadline | 新 OpenSandboxWorker | 缺 SSE 终态、长静默、cancel、callback 异常都能有界返回并清理 |
| destroy 成功但资源残留 | 主容器删除后由可信 daemon 核查同 sandbox/attempt 的 sidecar、临时运行时资源及端口；保留 output bind，不能误删共享组件缓存 | Adapter/可信管理侧 cleanup receipt | 主对象与 sidecar 全部不存在；GET 404/destroy return 单独不足 |
| 输出仍在变化/格式不可信 | 停止所有写入者→确认清理→可信扫描/导出→本地安全 staging→重算 manifest/diff | Run bind/export、sandbox_sources/output | 完整树/删除/原始 byte 一致，链接/特殊文件/大小写/超额拒绝 |
| 创建响应丢失 | create 前提交 attempt nonce/metadata；按可信 Server 身份与 run/attempt 分页对账；多匹配、空列表/失败不能证明 exactly-once | Adapter reconcile、RecoveryService、原有 journal/artifacts | 不重试有副作用 create/command；只清理匹配且确认所有权的资源 |

**硬门槛依据：** 固定 Router 对 /internal/init 免 token，Init Apply 可替换 binding；这是源码事实。任务利用它重绑管理身份是待实测的安全推导，不是本次已复现的漏洞。[Router](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/web/router.go)、[Init Apply](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/web/controller/init_runtime.go)

补丁必须形成独立、固定 revision 的 Server/Execd 制品及升级回归。以上是实施候选，不是上游已提供的配置选项。尤其不能为了 caps ALL、只读程序或跨 UID 切换失败而改成 privileged/SYS_ADMIN/unconfined。若需要大规模改造才能保护管理通道，应停在 G2 评估维护成本，不先重构 YiPilot 证明投入合理。[措施与补丁位置详情](2026-10-13-opensandbox-migration.md)

**W04 的优先实现方向已选定，仍需实测：** 为本机经典 Docker profile 移除不需要的 runtime-init/PTY/session 入口，固定启动身份；控制端保留随机管理 token，execd 仅接收不可变的 token hash/身份配置，不能把原值放入它的 env/argv。鉴权保护所有有副作用的端点；正常命令从明确白名单构建 env，受控 env 文件不可被任务改写或展开控制变量。Execd 的内存/proc 访问另以 non-dumpable、ptrace/process_vm 限制等 Linux 措施核验，防止同 UID 任务读取瞬时请求凭据。hash 初始化及进程保护是拟新增的补丁，不能当现成功能；T02/T03 无法通过时改用真正隔离的管理通道或保持旧 Worker，不降低门槛。

## 5. 请求、配置与证据合同

这些是待实现的字段安排；名称可按项目习惯定稿，含义不能省略。

| 合同 | 必须绑定的内容 | 约束与兼容 |
|---|---|---|
| Project runtime config | provider、runtime_id、安全 endpoint 身份、key_ref、image_ref、platform、runtime policy revision/hash | 使用现有 SQLite 项目记录；key_ref 复用 SecretStore，禁止 plaintext token；更新 setter 不丢其他配置 |
| SandboxRequest v2 | 既有 task/operation/run、tool/argv、controller execution path、baseline/seed hash、预算；增加已冻结 runtime spec | 每次变更改变 fingerprint；不同 provider/endpoint/image/policy/input 不能复用审批 |
| 场景与网络事实 | 场景用 isolated_workspace_write 的新合同；网络明确 bridge/管理入口/任务出站 deny | v1 的 offline/none 只用于历史，不能全局修改 SandboxAssociation 影响所有 SourceManifest |
| 镜像 identity | repository@manifest digest、平台与实际 image/config identity；组件 artifact revision | 旧 Docker Id 的裸 sha256 不是 registry manifest digest；缺事实/不匹配失败 |
| SandboxResult v2 | 请求绑定、sandbox/command ID、终态/reason/exit、UTC 时刻、observed policy/resources、日志 ArtifactRef、输出 hash、cleanup receipt ref、fallback=false | 资源请求 echo 不等于实际事实；字段无值不能补 0/unknown 后放行 |
| attempt record | create 前写 run/attempt/provider/request hash，create/on_init 后追加对象关联 | 使用现有 artifact/event 存储；不增加并行的元数据真相源，不覆盖已经发布的旧文件 |
| runtime evidence | controller result、receipt、组件版本、可信 daemon facts、cleanup 事实与原始证据引用 | 在运行时所有者处写/注册；workflow 只用类型与 ArtifactRef，不直接读取 Docker JSON |
| logs | stdout/stderr 分开、UTF-8 SSE 拼接/预算/截断、脱敏后 artifact bytes/hash | 不声称是任意原始 subprocess bytes；hash/size 取实际持久化数据，禁止凭据进入诊断 |
| source/output | 保留 UTF-8/NUL/链接/路径/大小/Git mode 规则；完整输出树与删除 | 源文件 byte 不脱敏、不换行转换；精确文件写入用既有 atomic bytes + 注册，不套日志脱敏路径 |
| legacy reader | v1 请求/结果/路径按原 bytes/hash 审计 | 不改写旧证据，不升级旧批准；新执行不能借旧记录自动启动 |

SourceManifest 与现有通用 Run/Artifact/Event 的 schema 不必为此次全部升级；仅升级需要改变语义的记录。未来执行阶段进度复用 stage.progress 的 audit payload；不新增第二套 Run 状态机或无限 append 的原始 SSE 事件。长网络动作之外提交 SQLite 状态，绝不在整个 create/run/download 期间持有写事务。

## 6. 运行顺序、资源和停止预算

**一个固定命令一个独立沙箱/子 Run。** pytest、Ruff、mypy 不共享可变 session；本机并发先为 1。

1. 读取冻结配置→验证输入/批准/Run 状态→写 attempt 并提交。
2. 有界 create/readiness→立即落盘 sandbox ID 与可信创建事实→上传/映射核验输入。
3. 用原生 argv、固定 /workspace 与 UID/GID 执行；on_init 记录 command ID；有界收集两路 SSE。
4. 正常或异常均进入独立 cleanup；命令终态缺失先标未知，不能再执行一次求结果。
5. 清理后确认全部 task/sidecar 写入者已消失→可信扫描/导出保留 output→控制端 hash/manifest/diff。
6. 写完整结果与 receipt。只有所有必要事实齐备，才进入输出批准/可信 apply/新副本验证/Handoff。
7. 重启时只 reconcile 已关联对象；过期无对象或多匹配保留未知证据，不自动 replay。基础设施失败不触发模型 code repair。

**初始工程预算（建议，须在 fixture 实测校准）：**

| 预算 | 初始值/来源 | 说明 |
|---|---|---|
| CPU、memory、PID | 1 CPU、1 GiB、128 PID | 与现有普通 apply/verify 相同；Server 全局 PID 设置与请求一致 |
| command | apply 60 秒；每个固定 verify 600 秒 | 保留当前上限，不用默认 TTL 替代 |
| create/ready | 合计 90 秒 | 组件镜像预加载，缺镜像失败；90 秒不足则诊断，不自动放大 |
| 每方向文件传输/映射核验 | 45 秒 | 单文件 16 MiB、树 128 MiB、10,000 文件；超时失败 |
| cleanup | 正常停止/核查 15 秒；后续 reconcile 单次 30 秒 | 到期记 CLEANUP_FAILED/未知，不把超时当成功 |
| sandbox TTL | 明确 15 分钟 | 90+45+600+15+45=795 秒，留余量；不要设置无限寿命/访问自动续期 |
| 单沙箱完整 deadline | 主流程 795 秒上限；异常收尾最多再 15 秒，总上限 810 秒 | 正常五段预算合计 795 秒；finally cleanup 不重复已确认的清理，TTL 900 秒仍有余量 |
| verification parent | 45 分钟上限 | 覆盖三个串行工具和父级核验；不改 repair 的既有 15 分钟链上限 |
| stdout/stderr | 各 128 KiB | skip_accumulation=true；持久化文件亦受限，输出洪泛触发停止 |
| /tmp | 初版保留 64 MiB、有界且不提升权限 | bootstrap 与实际工具若不兼容，记录校准依据，不悄悄换成无限写空间 |

同步 execute 内部使用 asyncio.run；当前同步调用路径可用，已有 event loop 的宿主不能嵌套 asyncio.run，需要明确调用约束，不能在实现中悄悄再建线程运行。最后一段 cleanup 使用新的/独立控制客户端与自己的 timeout，不能靠关闭正在读取的同步 client 代替安全取消。[Python timeout](https://docs.python.org/3/library/asyncio-task.html#timeouts)、[SDK 取消与清理核查](2026-10-13-opensandbox-migration.md)

TTL 仅是断电/崩溃兜底，不替代进程、sidecar、volume 的可信清理证明；已有 TTL/auto-renew 行为也须纳入升级回归。

## 7. 具体工作包与依赖

所有工作包目前为“待实施”。环境和补丁实测前不承诺固定完成日期；以下依赖与关口是明确的安排，不以时间到期自动放行。

| 编号 | 前置 | 要做的动作 | 文件/职责范围 | 必须产出的证据 | 关口 |
|---|---|---|---|---|---|
| W00 | 无 | 冻结工作树/依赖/受影响测试基线 | 当前源码、tests、uv.lock；只读核对 | 列出已有修改和既有失败，后续每批有独立差异 | G0 |
| W01 | W00 | 恢复 Docker Linux engine 可达并核对 WSL 路径 | 本机 Docker Desktop/WSL/daemon endpoint | Server OS/版本可读；指定 WSL integration 可用；不切 context 代替证明 | G1 |
| W02 | W01 | 部署隔离 fixture Server、固定版本与 API/数据面路由 | 独立 Server venv/配置；不修改项目 runtime | lifecycle、execd、egress 端点可达且仅在所需接口开放 | G1 |
| W03 | W02 | 准备并登记工具链、execd、egress 镜像和平台 digest | 独立镜像构建；现有 Dockerfile 的工具链可复用 | 形成镜像清单；普通 Run 镜像缺失不自动 pull | G1 |
| W04 | W03 | 补强 Execd 管理初始化、认证、固定身份与任务环境 | 上游 fork 的 router/init/env/command/FS；重建 execd artifact | 任务不能重绑身份/取得管理凭据/启动管理动作或切 UID；配置单独不足 | G2 |
| W05 | W03 | 补强 Server readonly rootfs、bootstrap、swap/tmp/log/user | 上游 fork 的 config/container_ops/runtime 和受控镜像 | 可信 daemon inspect + fixture 实测相符；不使用 privileged/isolation 便捷开关 | G2 |
| W06 | W04, W05 | 验证出站、管理面和跨 Run 隔离 | dns+nft、IPv6、loopback 管理面、host/metadata/内网探针 | 所有禁止访问实际失败；管理关联和策略保持不变 | G2 |
| W07 | W04, W05 | 验证停止、sidecar/volume 清理和稳定输出收回 | Run bind、可信 daemon 核验/扫描、Windows 字节回传 | 后台写入全停，完整文件树稳定，恶意路径拒绝，资源清理可确认 | G3 |
| W08 | W00 | 合并已有输入/receipt/log 规则，迁出测试 marker writer | sandbox_sources/output/apply、workflow、tests/helpers | 现有 Docker 行为保持；不提前新建只服务单 backend 的框架 | G0 |
| W09 | W06, W07, W08 | 落地 v2 请求/结果/运行配置及审批 hash/只读旧 schema | domain/sandbox.py、task_store.py、相关合同/持久化测试 | 策略变化使批准失效；旧 bytes/hash 可审计；迁移不丢项目配置 | G4 |
| W10 | W09 | 实现完整 OpenSandbox Adapter 与小 Interface | 拟新增 sandbox_runtime.py、opensandbox_worker.py；pyproject/uv.lock | async deadline、原生 argv、有界日志、attempt/cleanup receipt、无 SDK 对象泄漏 | G4 |
| W11 | W10 | 统一装配并切换 apply/verify/可信 apply/repair 的调用 | app.py、sandbox_application/verification、sandbox_apply/output、repair | 非交互调用闭环；原始容器 JSON/SDK 生命周期不进入 workflow | G4 |
| W12 | W10 | 恢复对账、清理与未知结果不重放 | recovery_service.py、runtime reconcile、现有事件/工件存储 | 断连/重启/多匹配/清理失败不重复动作，不清理别的 Run | G4 |
| W13 | W11, W12 | 完成 Windows 离线回归、本机实测、Linux required sandbox job | 相关 tests、pyproject markers、现有 .github/workflows/ci.yml | 明确区分 offline 与真实 sandbox；required job 无 skip 放行 | G5 |
| W14 | W13 | CLI 注册/doctor/审批展示及文档同步，试点切换新任务 | transport/cli、README/guides/status/V2，项目配置 | 新参数 help 可用；旧 CLI 行为明确；回切只影响未来 Run | G5 |
| W15 | W14 | 对账旧任务，删除 Docker 实时执行与迁移包装 | docker_worker.py、旧内部 fixture/文案、纯历史 reader | 旧记录可审计；没有旧正式执行调用；保留各安全场景并计量净变化 | G6 |
| W16 | W15 | 接独立服务器：可信导出、TLS/认证、资源配额/证据 | OpenSandbox Implementation/部署配置/测试 | 同一控制端合同通过远端文件、资源、对账验收 | G7 |
| W17 | W16 | 接 Kubernetes：选择 provider、RBAC/CNI/RuntimeClass/PVC/终态 | 部署清单与同一 Adapter 的必要差异 | 跨租户、删除/存储/管理初始化措施在目标 runtime 重新验证 | G7 |

W08 可与本机可行性准备独立推进，但只合并既有规则；正式合同接入 W09 必须等管理面、硬化、稳定输出关口通过。首轮实施开始于 W00/W01；当前环境不就绪时仍能完成基线核对、去重设计与依赖补丁设计，但不能把 mock 当成 W04–W07 的通过证据。

## 8. 文件级修改、整合与删除

| 文件/模块 | 明确改动 | 本批同步调用方与测试 |
|---|---|---|
| [domain/sandbox.py](../../src/yipilot/domain/sandbox.py) | 新 runtime spec/request/result/receipt 语义；v1 只读；严格关联及版本指纹 | [domain tests](../../tests/unit/domain/test_sandbox.py)、所有 workflow/fixture 的结果构造 |
| [task_store.py](../../src/yipilot/persistence/task_store.py) | Project 配置兼容读取/更新，保留旧 image 字段，新增字段用显式迁移/默认策略 | [持久化/迁移测试](../../tests/integration/test_sqlite_migration.py)、register/app 配置读取 |
| 拟新增 sandbox_runtime.py | 小 Interface：doctor、execute、verify_evidence；实际恢复需要 reconcile；历史纯校验及有限分派 | 三个现有直接 Docker 引用点改用 Interface；所有证据消费处 |
| 拟新增 opensandbox_worker.py | async SDK、输入/映射/版本事实、deadline、日志、attempt、清理/核验与 reconcile | 新 Adapter 合同/故障/真实测试；仅该 Implementation 引 SDK |
| [sandbox_sources.py](../../src/yipilot/workspace/sandbox_sources.py) | 合并 load/_copies/冻结 seed 的共用规则；可信 export 输入再核验 | [源码/输出攻击 fixture](../../tests/integration/test_sandbox_sources.py)；保留各阶段重新检查 |
| [sandbox_output.py](../../src/yipilot/workspace/sandbox_output.py) | 结果/log 绑定规则只有一份；成功与代码失败消费条件分开；继续控制端 diff | 输出 drift、日志 tamper、非零代码失败与 infra 失败分流 |
| [sandbox_apply.py](../../src/yipilot/workspace/sandbox_apply.py) | 将重复 Docker cleanup 规则移入 receipt owner；保留 accepted-result/worktree/index/tree/Git filters 校验 | trusted apply/Handoff fixture；不可减少重验 |
| [sandbox_application.py](../../src/yipilot/workflow/sandbox_application.py)、[sandbox_verification.py](../../src/yipilot/workflow/sandbox_verification.py) | 依赖注入、结果/ArtifactRef 消费、阶段状态/审批保留，删除路径和 JSON 列表 | [ProjectApplication 组合测试](../../tests/integration/test_project_application.py)、CLI 与真实检查 |
| [app.py](../../src/yipilot/app.py)、[repair_service.py](../../src/yipilot/workflow/repair_service.py)、[recovery_service.py](../../src/yipilot/workflow/recovery_service.py) | 唯一装配；只从已核验代码失败触发最多两次修复；恢复只对账不重放 | 批准拒绝/重复请求/repair limit/旧状态/new schema/restart |
| [CLI](../../src/yipilot/transport/cli/app.py)、[interactive](../../src/yipilot/transport/cli/interactive.py) | configure/register/doctor 显式运行时与镜像身份，批准界面展示真实策略 | [CLI tests](../../tests/unit/transport/test_cli_sandbox.py)、实际 help；新参数未实现前只在本计划中描述 |
| [executors.py](../../src/yipilot/workspace/executors.py) / [test helpers](../../tests/helpers/fake_executor.py) | 迁出只被 test_code_harness 使用的 ControlledWorkspaceWriter；保留有实际调用的 LocalCommandRunner | [code harness](../../tests/integration/test_code_harness.py) 保留批准/cwd/写入保护场景 |
| [pyproject/uv.lock](../../pyproject.toml)、[Dockerfile](../../Dockerfile)、[CI](../../.github/workflows/ci.yml) | SDK 固定；镜像与补丁制品独立；加严格 sandbox marker/required job | Windows offline 保留，Linux真实 job 无 skipped 放行，锁文件一致 |
| [旧 DockerWorker](https://github.com/Liu-Ame-Yi/YiPilot/blob/bc189b11b08ed1ddd9cf96aea5f12c9982ed5a73/src/yipilot/workspace/docker_worker.py) | 对账/切换后删实时 CLI/probe/lifecycle；移出历史纯检查；删无调用包装 | 删除仅旧内部实现测试，迁移旧安全/业务场景；旧 JSON/hash 仍可审计 |

`configure` 为候选的新入口，尚未实现；不得把拟定参数写入当前用户指南。CLI schema 变化与 metadata/config 变化一起落地，不先给不存在的命令造文档。旧 register 的 image-digest 若保留，明确仅适用于迁移期 Docker；新 image-ref 使用 repository@digest，不静默重新解释旧值。

所有相关 imports/exports、构造器、fixtures 同批更新。不建通用 Repository/Session/Manager/ServiceContainer，不统一与审批 hash 相关的全部 JSON 序列化，不重写模型 RunExecutor。

## 9. 验收矩阵与关口

| 测试组 | 具体对抗/行为 | 通过标准 |
|---|---|---|
| T01 环境与路由 | 缺 daemon/image、错误 platform/endpoint/token、管理 proxy 路径 | 清晰失败关闭，不创建未知任务、不拉取、不 host fallback |
| T02 管理面 | 同容器 /internal/init、command/files UID 改写、PTY/session、egress policy | 禁止动作拒绝；身份与出站策略不可被任务改变 |
| T03 控制秘密 | env/proc/argv/.env 展开、日志、endpoint headers | 不暴露可用控制/Provider 凭据，原始 token 不落产物 |
| T04 硬化 | rootfs、runtime program、non-root/caps/nnp、PID/memory/swap/CPU/tmp | 实际 facts 与已批准 profile 一致，程序不可修改 |
| T05 网络 | DNS、公网 IP、IPv6、host/metadata/内网/其他 Run | 禁止访问实际失败；不是只看 policy readback |
| T06 文件 | 新增/修改/删除/空文件/Unicode/模式、完整树传输 | 原始 bytes/hash 和预期 Git tree 一致 |
| T07 恶意输出 | 路径穿越/链接/设备/大小写/重复/超额/传输中改写 | 本地发布前拒绝，worktree 不变，不产 Handoff |
| T08 时限与输出 | 静默 SSE、无 complete/exit、洪泛、cancel、子孙/脱组进程 | 有界退出、输出受限、工作负载全停；异常不当成功 |
| T09 清理 | destroy 后 sidecar/volume 残留、删除 warning、查不到状态 | 独立核查未通过就 CLEANUP_FAILED/未知，不发布 |
| T10 lost-create/重启 | 请求成功响应丢失、metadata 多匹配、分页/查询失败、TTL对象消失 | 不自动重发 create/command，不误删他人，不声称 exactly-once |
| T11 指纹/历史 | 修改 provider/image/policy/seed/预算、跨 Run receipt、v1读取 | 旧批准失效，旧证据不改写，错误/未知 schema 拒绝 |
| T12 业务与回切 | apply→approve→verify→Handoff，拒绝批准，非零代码失败 repair，config回切 | 所有真实校验后放行；repair 保留次数/时间限制；不改进行中 Run 后端 |
| T13 结构与精简 | 依赖规则、测试 helper、无旧执行调用、来源/规则数量 | 上层无 SDK/原始容器 JSON，核心新增 ≤2 文件，安全场景不减少 |

| 关口 | 放行条件 | 当前状态/失败动作 |
|---|---|---|
| G0 基线 | 工作树和测试基线明确，去重行为不变 | 代码已读；未运行相关业务基线测试 |
| G1 环境 | W01–W03 的 Linux daemon/路径/组件制品就绪 | 当前 daemon 不可连接；先恢复环境再测试 |
| G2 管理与硬化 | W04–W06、T02–T05 全部实测通过 | 待补丁/实测；失败保留 Docker，停止正式接入 |
| G3 输出与清理 | W07、T06–T09 实测通过 | 待测；不能先接业务再补稳定快照 |
| G4 Adapter/合同 | W09–W12 的离线合同、故障和完整调用方通过 | 待实现；错误/未知结果不重放 |
| G5 正式切换 | W13–W14 的本机真实闭环、required CI、回切演练通过 | 待实施；试点失败只回切未来任务 |
| G6 删除 | 无旧在途/未知执行，历史阅读兼容，旧正式调用方为零 | 待实施；历史问题未解决不删必要 reader |
| G7 远端 | 服务器/K8s 对目标部署重新通过同一验收 | 后续范围；本地成功不自动放行远端 |

相关 offline 验证沿用现有项目工具；真实 sandbox 验证另设 marker 和准备好的固定 image。开发机可以明确 skip 环境缺失的真实测试；required/release job 要在未准备、skip、失败时失败，不能只检查 pytest exit code。

## 10. 切换、回切和维护成本

- schema/config 变更前备份并核验目标项目数据；未来有新 Run 后不能直接恢复旧 state.db 丢掉新证据。不是本次已执行备份。
- 切换只影响新建 Run；审批和请求冻结 provider，不在运行中改 config 来恢复任务。
- 迁移期保留一个**兼容 v2 阅读的双实现版本**作回切点，记录其版本、旧 Docker image/config、Server/Execd 制品 digest。不要回退到完全不认识 v2 数据的原始旧二进制。
- 故障时停止接收新 OpenSandbox Run→确认或隔离在途资源→保留日志/attempt/清理失败证据→配置选择已验收 Docker 供未来新 Run。未知 Run 不自动再应用 patch；需要新 request/Run/批准。
- 删除旧 Worker 后保留可恢复的迁移版本与只读历史校验；回切不靠临时重写旧 executor，不破坏审核历史。
- 上游补丁至少增加 Execd 管理初始化/环境/身份测试与 Server 只读 bootstrap/资源/无 pull 测试。每次升级重跑 T02–T05、T08–T11；维护量与 YiPilot 净代码变化分开报告。
- 同一新版 SDK 支持 Docker/K8s 不表示 API/快照/删除/租户事实等价；远端 profile、可信导出与管理认证必须重新完成 G7。

## 11. 本轮规划交付与下一步

本轮交付：具体方案、配置与补丁措施、18 个工作包、13 组验收、8 个关口、文件与调用方清单、历史兼容和回切安排；并记录只读环境检查结果。安排可以据此分批实施，源码与测试尚未修改。

下一步明确为 **W00 基线 + W01 环境就绪**，随后在无敏感 fixture 上做 W02–W07。G2/G3 失败时优先解决具体边界或评估依赖维护成本，不进入正式业务切换。代码精简 W08 可独立进行，但不删除执行前/发布前的重验；最终删旧执行发生在 W15。

本轮不会安装部署、发布、删除旧 Worker、修改数据库或启动真实任务。规划检查只证明安排自洽、来源与本地链接有效，不能证明 Windows/WSL/Docker、管理面隔离或稳定输出已经通过。
