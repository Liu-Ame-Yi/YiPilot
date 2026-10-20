# OpenSandbox 第二阶段：最小 Runner 闭环

日期：2026-10-20（Asia/Shanghai）。状态：**Implemented 独立远程 Job 闭环，尚未切换正式业务。** 在 [v2 固定隔离 profile](2026-10-20-opensandbox-control-isolation.md)上新增耐久 Runner、本地 HTTP 客户端和冻结输入/输出包，用自有 Python 样例完成真实服务器验收。[部署及接口说明](../../deploy/runner/README.md)。

本文件保留第二阶段的当时记录。创建归属、磁盘上限、保留期、故障和四并发已有[第三阶段更新](2026-10-20-opensandbox-reliability-validation.md)；下文首版限制和默认预算不代表当前候选。

## 实际修改与整合

| 位置 | 职责 |
| --- | --- |
| `domain/remote_execution.py` | 版本 2 请求、状态和结果；绑定 Task/Run、请求 key、输入清单、固定工具/profile/镜像及预算 |
| `runner/store.py`、`service.py` | 独立 SQLite、接受前校验、请求去重、基本预算准入、取消、崩溃后不重放、停止后可信发布 |
| `runner/runtime.py` | 官方 SDK 创建/命令/销毁；持久登记 sandbox ID，宿主核查资源及 cgroup 写入者停止 |
| `runner/http.py` | 回环认证 HTTP，独立 key；请求连接与执行生命周期分开 |
| `workspace/remote_bundle.py`、`runner_client.py` | 有界 ZIP、摘要/完整清单/增改删除/Git mode 校验，从原始冻结输入重建结果 |
| `deploy/runner`、依赖锁和 CI | 独立环境、配置/unit、自有样例、可选 SDK、Linux Runner 验证 |

复用现有 `SourceFile`、便携路径校验、`scan_source_tree` 和 SecretStore，没有复制一套源码排除规则、宿主 Git 执行器或业务工作流。新的执行合同独立于旧 `SandboxAssociation`，未改写旧 SourceManifest，也未伪装 Windows backend 或 `network=none`。

Runner 只登记一次执行 attempt；输入完整发布并提交 SQLite 后才准入。HTTP 断线不拥有任务生死，重复 key/内容返回原 Job，内容不同冲突。运行中崩溃结局记为 uncertain；清理失败或创建结局缺失时保留预算并停止新准入。输入目录发布和数据库提交分步完成，间隙崩溃可能留下未接受目录，首版不自动删除无法证明归属的目录。

## 真实验收范围

本地 Windows 经 SSH 回环转发到服务器 8090，再由官方 SDK 1.1.1 调用 8081 的固定 Server。Python 3.11.15、Runner 虚拟环境和安装包独立于系统 Python 3.10 和原 OpenSandbox 环境。依赖来自 uv.lock 导出的固定摘要清单，镜像下载仍核对官方 artifact 摘要。

| 用例 | 验收事实 |
| --- | --- |
| 认证 | 无凭据 401；独立 Runner key 可访问；控制 key 不进入任务 |
| 文件变化 | 修改/新增/删除正确，POSIX 可执行位转为 `100755` 并往返 |
| 冻结与重建 | 提交后改变本地原目录，取回结果仍包含冻结版本的未修改字节 |
| 请求去重 | 每类任务重复提交返回同一 Job，SQLite 仅登记一条对应请求 |
| 非零退出 | Python 退出码 7 原样返回，状态为 failed，仍可取回可信结果 |
| 输出限额 | 500000 字符输出在 128 KiB 截断并停止，reason 为 output_limit |
| 超时 | 2 秒任务包含脱离会话的子写入者；销毁后宿主才扫描结果 |
| 取消 | 登记取消意图，最终 reason 为 cancelled；不把请求收到视为已停止 |
| 输入拒绝 | 缺少源码条目、摘要不符和越界路径拒绝，未进入执行 |

结构化记录见[验收证据](evidence/2026-10-20-opensandbox-runner.json)。它仅包含自有样例、Job ID、摘要及宿主检查，不包含凭据或用户源码。候选源码和安装包摘要与收据核对；验收后 Runner/profile 停止且 disabled，原 8080 保持健康，容器和卷无残留。

本地受影响检查为 124 passed、1 skipped；跳过项由现有 Linux profile CI 执行真实补丁构建。Ruff 通过，mypy 80 个源文件通过。新增 Linux Runner CI 安装锁定可选 SDK 并运行真实 SQLite、HTTP 和 SDK 接口测试；完整 Windows/Linux 结论以本提交 CI 为准。

## 当前限制与后续任务

首版只登记 `python-script-v1`，当前镜像没有完整 pytest/Ruff/mypy/Git 工具链，也没有把模型输出接入执行。正式 `/apply`、`/verify`、doctor 和审批仍走原业务实现，需要第四阶段统一替换；当前本地客户端不会把结果自动写入用户工作区。

实现最多 4 个槽位，按每个主容器加 64 MiB/100m sidecar 计算总量；默认 1280 MiB 和 1500m CPU，不降低既有业务单次 1 GiB 预算。**本批次任务顺序验收，不代表四并发可用。** 内存实测与保留量、硬磁盘配额、保留期 GC、完整断线/崩溃/销毁失败矩阵尚未完成；最多保留 32 Job，达到后拒绝新任务。创建响应完全丢失时可能无法识别仅创建到一半的 sidecar，首版保守阻断等待对账。

下一任务为既定第三阶段：先补保留期/资源归属和故障对账，再测 1、2、4 并发的真实内存峰值、OOM、耗时和清理残留；按实测决定这台服务器能启用的档位。最终业务切换和删除旧 DockerWorker 留到完整验收后。[总体安排](2026-10-19-opensandbox-implementation-plan.md)。
