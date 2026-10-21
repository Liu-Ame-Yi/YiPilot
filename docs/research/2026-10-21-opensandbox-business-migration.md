# OpenSandbox 第四阶段：业务替换与待验收项

日期：2026-10-21。状态：**业务替换代码已实现；本地完整复测发现并修复锁文件竞态，完整 CI 以对应 GitHub 提交为准；服务器未部署，G4 未通过。** 服务器最后成功同步的版本为 bc189b1；此前镜像/容量证据见[前置记录](2026-10-21-opensandbox-toolchain-validation.md)，不能作为本轮业务迁移的验收结论。

## 实际实现

- ProjectApplication 装配一个惰性 RunnerClient，应用和验证子执行共用。准备审批时只读取公开端点、冻结输入，不读取 key 或调用网络；SecretStore 在网络请求时解析密钥，不传入源码包/任务。
- sandbox configure 保存用户级 runner.json 的 endpoint/credential_ref，sandbox doctor 改为远程只读检查。新增 key/fingerprint 查询；提交响应丢失时不再次 POST。端点只接受 HTTPS 或回环 IPv4 HTTP，拒绝 URL 凭据、路径和控制字符。
- 新业务请求/结果使用 schema 2，绑定 Task/Operation/Run、端点、注册 argv、固定 tools profile、镜像、输入和预算。新业务预算显式为 512 MiB，使用新的 apply/verification policy revision 和审批指纹；旧 1 GiB 请求不缩减，旧审批不复用。
- 新业务采用 python-isolated-script-v2，以 python -I 启动受信入口，防止仓库同名模块替换标准库。历史 python-script-v1、通用 Job 默认值和原 request key 保持原义。
- RunnerSandboxWorker 收拢冻结、提交、查询、取消、下载及可信结果发布；输入/结果 ZIP 复用同一份字节、manifest、delta、Git mode 和日志校验。首次从冻结包重建到新目录，后续消费核验包内容与当前执行树，不能从变化后的用户仓库补文件。
- 新结果保存真实 Job/sandbox UUID、profile、镜像及停止/资源移除事实，不伪造 Docker Desktop backend、network=none 或 Docker inspect。业务日志沿用原脱敏规则，并与已验证原始 Job 日志绑定。
- 远程等待持有 Run 锁。准备与最终 Git 应用在短项目临界区执行，保留等待期限和基线/HEAD/index 重查；同项目两个 Run 的并行等待已用本地真实 HTTP 测试。模型阶段仍保留既有项目所有权，不承诺所有阶段并行。
- run recover 只对账/取消原 Job。停止未知、清理失败或网络不可达时保留 paused；确认终态后保守结束中断 Run，不继续业务、重放命令或绕过输出审批。
- 删除旧 DockerWorker 实时实现和专用本机 Docker 测试；保留可信 Git 的 LocalCommandRunner、源文件/路径校验、diff、审批与 Git 应用。LegacySandboxRequest/LegacySandboxResult 只读 DTO 保留，SourceManifest 的 schema 1 未改变，未重写历史数据。

## 已运行检查

| 检查 | 已观察结果 | 范围 |
| --- | --- | --- |
| 业务集成 | 17 passed | 真实回环 HTTP/SQLite/冻结包/结果重建/审批/Git/repair；仅模型与底层运行时为 fixture |
| 源码、diff 与输出校验 | 110 passed | 字节/路径、审批、漂移与可信输出；纯 collector 测试单独替换远程证据 gate |
| Runner、迁移及交互组合 | 85 passed，1 warning | 旧脚本语义和既有所有权；新增隔离入口 SDK 参数化已由后续首轮全套覆盖 |
| 旧合同、审批持久化与 CLI | 99 passed | 包含 v1 DTO 只读验证 |
| 最终持久化/领域/架构/CLI/客户端组合 | 232 passed | 含配置、只读查询、20 分钟包体传输期限、identity 编码、失败不覆盖已有文件 |
| 静态检查 | Ruff 通过；mypy 81 个源码文件通过 | 修复锁文件竞态后重新运行 src/tests/deploy 与全部 81 个源码文件 |

集合有重叠，不能相加作为全量测试数量。17 项业务测试包含三处审批拒绝、验证失败后的有限修复、提交响应在接受前/后丢失且不重放、同项目两 Run 等待及包/结果/diff 篡改拒绝。并行测试使用 FakeRuntime 和专用测试额度，不是服务器支持两个 512 MiB 实际重负载的证据。另已通过 3 项纯客户端/文件系统测试，验证首次请求被拒绝与已接受后失去授权必须区分、未知提交只查询不 POST；首轮全套已覆盖新增隔离入口 SDK 参数化，完整 CI 按本次提交的 GitHub 运行结果判定。

## 完整复测与锁文件竞态修复

本地全套命令与 Windows CI 一致，临时目录在 Git 仓库外；首轮 **846 passed、8 skipped、1 failed、1 warning**，耗时 1695.76 秒。唯一失败为事件日志双线程追加时路径校验误拒绝 Windows 待删除锁文件；没有日志序号断裂或数据库损坏证据。8 项跳过分别是 2 项真实模型、4 项 Windows 不支持的文件名、1 项 Linux Server wheel 构建、1 项 Linux flock。warning 来自故意构造重复 ZIP 条目的拒绝测试。

最小真实双线程复现捕获 `st_nlink=0` 的普通锁文件。共享路径检查改为拒绝多硬链接（`st_nlink>1`），保留符号链接、重解析点与特殊文件拒绝；无需改变依赖、锁文件生命周期或镜像。新增删除竞态回归在修复前失败、修复后通过，真实硬链接仍被拒绝且目标字节不变。

修复后持久化/迁移 **81 passed**；双线程 **3,000 次实际锁获取通过**，期间捕获 7 次真实零链接状态。Ruff、mypy 81 个源码文件及 diff 检查通过。本地首轮全套的失败不能改写为全量通过；最终整套结果由本次同步触发的 GitHub Windows/Linux Runner/profile 三项 CI 分别记录。相关本地日志位于未入库的 `tmp/full-suite-tests.log`、`tmp/full-suite-results.xml` 与 `tmp/full-suite-lock-regression.log`。

## 首次 CI 与启动锁修复

[提交 cebe113 的首次 CI](https://github.com/Liu-Ame-Yi/YiPilot/actions/runs/39551852798)中，Linux Runner **65 passed**、profile 构建 **95 passed**；Windows **844 passed、12 skipped、1 failed**，失败为并发初始化解析 `.bootstrap.lock` 时误报路径越界。此前事件日志并发用例已通过，不能把这轮 CI 写成全部通过。

无数据库的并发最小复现也在 `exists()` 与 `resolve()` 之间遇到启动锁删除。初始化现在复用已解析的稳定 runtime 父目录，单独对原 `.bootstrap.lock` 做普通路径校验，再交给 FileLock；原锁路径和互斥机制保持原义，未放宽项目越界、符号链接或硬链接拒绝。

新增两类瞬态解析回归与真实硬链接拒绝回归均在修复前失败。修复后持久化/迁移 **84 passed**；8 个线程共 **80 次真实初始化通过**，数据库只保留一条 Project。最后单独复测并发初始化与三个新回归 **4 passed**，Ruff、mypy 81 个源码文件通过。完整三项 CI 随后按修复提交重新执行，结果须核对该提交的 GitHub 运行，不能沿用首次 CI 的成功子项替代。

## 工具通道

首次执行时，服务器备份、只读 SSH 和新增套接字测试命令曾在 CreateProcess 前遇到自动权限审批审查超时，GitHub 连接器亦曾传输失败。这些是此前未推送/部署的原因，不能视为动作不安全或已成功执行。

本轮复测权限审查与 GitHub 连接器均已恢复；Git 原 Schannel 握手失败，单次命令使用 OpenSSL 后可读取远端，仍保留证书校验。同步授权私有 YiPilot 仓库并等待完整 CI，服务器不因本次测试自动升级或启用。

## 剩余操作顺序

1. 确认本次准确提交的 Windows、Linux Runner、OpenSandbox profile CI 均通过；保存执行链接、测试数量与源码摘要。
2. 服务器拉取通过提交，排空候选并备份 Runner SQLite；新 wheel 安装到独立发布目录，保留配置/密钥/历史 Job，检查旧 DockerWorker 未残留安装。
3. 运行 deploy/runner/business_acceptance.py：仅模型使用受信 fixture，服务器真实执行应用、一次 pytest 失败、有限修复、新应用及 pytest/Ruff/mypy 交付。预期六个 Job、一次非零退出，用户源仓库不变，只修改新的任务 worktree。
4. 对账历史记录、未知结果、取消/恢复、资源和清理，保存输入/审批/Job/结果/handoff 关联。通过后再配置本机 SecretStore/隧道并启用正式服务。

服务器业务演练、部署/启用和用户本机凭据/隧道仍未完成。大型用户项目、额外依赖、重构建、K8s 以及恢复原业务工作流均未验证。尚未升级的服务器不能执行新策略；不得回退旧 Docker 或重放未知 Job。
