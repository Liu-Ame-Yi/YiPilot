# OpenSandbox 第四阶段前置：工具镜像与业务预算校准

日期：2026-10-21。状态：**Implemented 工具 profile 与受信样例；G4 全部业务迁移尚未完成。** 本次推进第四阶段需要的工具镜像和资源判断，未替换现有业务调用、项目锁或历史 Docker 结果合同。

## 已完成与证据

机器仍为华为云 2 vCPU、实际约 1771 MiB 内存、无 swap。原 8080 服务保持运行，候选 8081 profile 与 8090 Runner 用于本轮受信样例，验收后停止且不自启。原配置摘要不变，未开公网管理端口或增加机器费用。

[结构化证据](evidence/2026-10-21-opensandbox-toolchain.json)记录镜像、构建输入、安装源码摘要、固定补丁、任务 ID、工具日志、资源采样和数据库重启核对。[构建收据](../../deploy/toolchain/image.json)绑定完整本地 image ID。镜像约 125.2 MiB，包含 Git、Python 3.12.15、pytest 8.4.2、Ruff 0.16.1、mypy 1.20.2。Python 包从现有 uv.lock 按 hash 安装，未增加项目依赖。

新增 `yipilot-tools-v3`，原 `yipilot-python-v2` 保持原镜像、字段默认值和指纹。Server 与 Runner 都校验 image/profile 对应关系；发布结果使用原请求的对应关系，本地重建再独立核对，不能用另一合法 profile 的结果冒充本 Job。历史负载不需要重写或清空。

新镜像的 18 项隔离探针全部通过：包括 UID/GID、rootfs、控制端口、管理 API、跨 Job/宿主/metadata 网络、受保护控制进程及退出后写入者清理。工具增加不替代这些边界。Windows 控制端经实际 SSH 隧道的原 v2 五类任务回归也通过，包括非零退出、超时、取消、输入拒绝和冻结输入重建。

最终受影响离线测试为 143 passed、2 skipped（本机未安装 Server wheel/patch 工具的构建测试）、1 条重复 ZIP 条目测试的预期 warning；Ruff 检查 src/tests/deploy 通过，mypy 的 81 个源码文件通过。完整 CI 的最终提交结果由服务器发布收据绑定，不能用本轮子集结果代替整个业务 G4 验收。

## 实际资源结果

| 负载 | 实际同时运行 | 主机最低可用内存 | 主容器观测最大值 | 总耗时 | OOM |
| --- | --- | --- | --- | --- | --- |
| 一个申请 512 MiB 的工具任务 + 一个申请 128 MiB 的轻任务 | 2 | 722.0 MiB | 工具 97.4 MiB，轻任务 58.8 MiB | 12.41 秒 | 0 |
| 四个申请 128 MiB 的轻任务 | 4 | 570.6 MiB | 每个约 58.3–59.5 MiB | 13.75 秒 | 0 |

工具任务使用自有最小 Python 工程，先应用 42→43 的补丁，再运行固定 pytest/Ruff/mypy 命令，并核对结果文件和删除项。轻任务各分配 48 MiB 并等待 8 秒。重叠既有工作目录标记采样，也用完整回传的起止区间核对；不是把 queued 数量当并发。主容器数值是 cgroup 采样观测值，不能视为所有瞬时峰值或大型项目的保证。

两种负载都使用总计 768 MiB 的资源申请，包含每任务 64 MiB sidecar；混合负载申请 1450m CPU，四轻任务申请 1400m，均在 1500m 总预算内。原 1 GiB 请求返回 `409 BUDGET_UNAVAILABLE`，未执行，也未被暗中降为 512 MiB。

两组结束后均无容器/卷残留；最终检查还覆盖工作挂载和本 Runner 的 loop 设备。SQLite v2 完整性检查通过；排空后重启 Runner，逐行核对记录不变，历史请求 key 和过期 tombstone 仍保留。数据库备份位于 `/srv/yipilot-runner/backups/g3-20261021T022218Z-317c94.db`；新工具镜像另有私有归档及摘要。

## 后续业务接入的明确安排

1. 在 ProjectApplication 集中装配 RunnerClient 和本机 endpoint/SecretStore 引用；源码不保存密钥。远程等待按 Run/Job 持有所有权，最终 Git 应用保留短锁与基线重查。
2. 应用补丁和验证使用新的远程业务请求/结果合同与策略 revision，审批明确展示工具 profile、固定镜像和资源预算。基于本轮小样例，可先验证一个申请 512 MiB 的新业务策略；这不是自动修改现有 1 GiB 请求的许可，也不是所有项目的容量承诺。预算耗尽须明确失败，不自动降额重跑。
3. 收拢 Job 提交、状态查询、取消和结果重建；保留丢失响应后的同 key 对账、结果未知不重放、结果过期和清理未完成的失败语义。统一远程证据检查后接通 apply、verify、repair、recovery、doctor 和 CLI。
4. 历史 v1 只读；新执行不伪造旧 Docker Desktop backend、容器 inspect 或 `network=none`。更新 fixtures/架构约束后删除旧实时 Worker；以完整业务演练和 G4 验收决定正式切换。

本轮未完成：业务切换、审批展示与合同替换、项目锁缩短、用户项目额外依赖、大型验证/构建、K8s 镜像分发。镜像目前按 Docker 本地内容 ID 部署，迁机需传递并核验已验收归档，不能直接从 GitHub 拉源码后假定能拉取该 ID。[部署说明](../../deploy/toolchain/README.md)。

Docker 对 image ID 内容寻址与仓库拉取 digest 的说明见[官方镜像文档](https://docs.docker.com/reference/cli/docker/image/pull/)；本轮实际调用行为以固定 Server 1.1.1 源码与上述真实验收为准。
