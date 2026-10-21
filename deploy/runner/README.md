# Runner 最小远程 Job 闭环

状态：**已实现并完成自有样例验收，尚未接入 YiPilot 正式业务。** 本地通过 SSH 隧道访问服务器回环 8090，Runner 再通过官方 SDK 1.1.1 调用回环 8081 的固定 OpenSandbox profile。管理端口不公开。

Runner 使用独立 Python 3.11 环境和 `yipilot[runner]` 可选依赖。普通本地安装不需要 OpenSandbox SDK；本地 HTTP 客户端复用已有 httpx，凭据由 SecretStore 提供。源码、依赖和配置均由可信宿主部署，任务只看到自己的 work。

## 调用与数据

| 接口 | 行为 |
| --- | --- |
| `GET /health` | 认证并检查调度线程；线程不可用返回 503 |
| `POST /v1/jobs` | 有界 ZIP 输入包；完整校验、发布不可变 input 并提交 SQLite 后返回 202 |
| `GET /v1/jobs/{id}` | 查询 durable Job 状态及关联 sandbox ID |
| `POST /v1/jobs/{id}/cancel` | 取消排队任务，或向执行中的 Job 登记取消意图；仍需查询停止结果 |
| `GET /v1/jobs/{id}/result` | 只下载已可信发布的结果 ZIP，内含完整清单、增改字节、删除项及日志 |

所有接口要求 `Authorization: Bearer <独立 Runner key>`，与 OpenSandbox key 分开。请求只接受版本 2 合同、固定 profile/镜像、已登记的 `python-script-v1` 工具及其输入脚本；不接收 shell、宿主路径、额外 Docker 参数或模型配置。

已登记的对应关系包括原 `yipilot-python-v2` 基础镜像和新增 `yipilot-tools-v3` [工具镜像](../toolchain/README.md)。冻结输入时显式传入对应的 profile 和 image；结果也必须与原输入对应，客户端拒绝跨 profile 替换。默认值仍是原 profile，不改变历史指纹或现有业务预算。

输入包为 `request.json` 加 `source/<路径>`。校验 ZIP 中央目录数量/长度、普通文件类型、完整条目集合、便携路径、UTF-8、摘要、文件数和展开大小，不使用 `extractall`。复用现有源码扫描，拒绝链接、特殊文件、凭据目录和 `.env` 等排除项。单文件最多 16 MiB、全树最多 128 MiB、10000 文件，传输包最多 132 MiB。

相同 request key 和请求摘要返回同一 Job；内容不同返回冲突。接受前不执行；进入 running 后发生崩溃，不自动重放。重启先对账 running 和 cleanup_failed，再把已停止而结局不确定的执行记为 uncertain。不能承诺外部副作用 exactly-once。

Runner 在创建之前持久登记确定的 sandbox UUID 和 root 私有创建租约。Server 在实际 Docker 创建线程中持有同一文件锁，租约仅消费一次，并在所有创建操作返回后登记 settled。回收先撤销租约，阻止迟到请求；API 不可用或只留下 sidecar 时，宿主仅按名称、UUID 和标签均核验过的 ID 回收。缺少身份或创建完成证明时保留 cleanup_failed 和准入额度；Server 在创建中崩溃不能凭一次“未看到容器”判断安全释放。

执行结束先销毁，再从宿主核查容器/sidecar/runtime volume 和 cgroup 写入者停止，最后扫描完整 work。结果在任务不可访问的 artifacts 中原子发布。客户端用原始冻结输入重建结果，校验完整清单、增改删除、Git mode 和日志摘要，验证后才发布到新的本地目录。用户的当前工作区不会作为未修改文件的补充来源，也不会被这个样例自动应用。

## 运行与验收

服务器布局：

```text
/opt/yipilot-python/                    独立 CPython 3.11
/opt/yipilot-runner/venv/              锁定依赖及安装的 YiPilot wheel
/opt/yipilot-runner/releases/          候选源码与摘要收据
/etc/yipilot-runner.toml               root:root 0600，两种独立密钥
/srv/yipilot-runner/jobs.db            Runner 独立 SQLite
/srv/yipilot-runner/jobs/<id>/
  input/                              root 私有，文件只读
  runtime.json / runtime.lock          root 私有单次创建租约
  work.fs                             root 私有 256 MiB 稀疏 ext4 镜像
  work/                               执行时挂载，任务 UID/GID 10001
  artifacts/result.zip                任务不可访问
```

`config.toml.example` 只能作为模板，两个 key 必须从可信宿主生成/读取。systemd unit 依赖固定 profile，源码包通过独立虚拟环境安装。普通安装可用 `uv sync --extra runner --no-dev --frozen --no-editable`；若使用镜像，先从同一 uv.lock 导出无项目依赖清单，再使用 `uv pip sync --require-hashes` 校验完整摘要，最后从已核对摘要的源码安装项目，不能跳过依赖验证。

服务器启动入口是 `python -m yipilot.runner.http /etc/yipilot-runner.toml`，生产配置校验要求 Linux root 和私有配置。先排空并停止服务再替换安装包，不原地修改运行代码。本轮服务仅用于受信验收，验收后停止并保持 disabled。

本轮将 Runner 数据库从 user_version 1 事务迁移到 2，先使用 SQLite backup 备份。原请求身份和终态保留；新旧数据库不能直接交给旧程序混用。profile 创建租约和 Runner 必须在排空后一起更新。

本地先建立 SSH 回环转发至服务器 8090，再由 SecretStore 解析独立凭据。`acceptance.py BASE_URL SECRET_REF NEW_OUTPUT_DIRECTORY` 运行五类自有样例，输出 JSON 验收记录；在已安装本项目的 Python 环境调用，不在用户项目目录执行样例代码。不要把 key 直接写入命令、示例或仓库。

## 磁盘、保留和恢复

每个 work 使用宿主独立 256 MiB ext4 loop 文件系统，挂载 nodev/nosuid，无空间预留给 root；有效数据容量约 227 MiB，另有限 inode。主容器的只读 rootfs、受限 tmpfs 仍由固定 profile 提供。宿主校验文件系统 UUID、源 loop 设备和镜像路径，停止所有写入者后扫描和发布，再正常卸载；卸载失败仍占额度，禁止 lazy umount 和全局 prune。需要宿主 mkfs.ext4/mount/umount/findmnt/losetup；当前 Ubuntu 主机已验证，K8s 不适用此存储实现。

默认保留 24 小时、最多 32 份 Job 负载。达到数量上限时先释放最老的已完成任务；running、cleanup_failed 和正在下载的结果不清理。过期先持久记录 result_expired，再删除本 Runner 已登记的目录，重启继续未完成删除。状态和原 request key/摘要保留，重复提交仍返回原 Job，结果下载返回 410；去重记录随总请求数增长。未登记目录不自动删除。下载完整性或结果过期不能作为重跑授权。

删除请求单独使用 5 秒 HTTP 超时并关闭 SDK 重试，外层停止和关闭也有期限；API 故障后继续按宿主证据回收。输入校验和结果扫描/打包共用串行临界区，控制同时驻留的大块源码字节。

## 已测容量和剩余门槛

最多 4 个运行槽位，同时按主容器加每个 sidecar 64 MiB/100m CPU 的总预算准入。默认总量已校准为 768 MiB、1500m CPU，保留 4 GiB 空盘。四个 128 MiB/250m 请求连同 sidecar 正好占 768 MiB/1400m；超出总量的单个请求直接拒绝。现有业务的 1 GiB 请求未被降低，在此配置下会明确返回 BUDGET_UNAVAILABLE。命令最长 600 秒，stdout/stderr 分别最多 128 KiB。

这台标称 2 GiB 主机已通过 **1/2/4 个真实活跃轻任务**，每个样例分配 48 MiB 并保持 12 秒。四并发最低可用内存仍超过 550 MiB，无 OOM、资源残留；这是轻量内存与等待任务的校准，未证明重构建或 1 GiB 验证可用四并发。[第三阶段数据与故障范围](../../docs/research/2026-10-20-opensandbox-reliability-validation.md)。

`reliability.py` 仅供已排空的专用验收环境，以 Linux root 运行；它会创建自有任务、暂停候选 API、重启/强制终止 Runner 并注入卸载失败。通过断言后保存无凭据 JSON 收据，不在用户项目中运行。

本轮不调用真实模型，不应用到用户工作树。正式业务调用、审批、项目锁和工具镜像的全面迁移仍属于第四阶段；下一步统一替换这些调用并删除旧 Docker 执行实现。[迁移安排](../../docs/research/2026-10-19-opensandbox-implementation-plan.md)。
