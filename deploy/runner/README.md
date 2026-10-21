# Runner 最小远程 Job 闭环

状态：**完整 CI 与六 Job 的真实服务器业务主链路已通过；候选安装后停止且 disabled，常驻与用户项目切换尚未完成。** 本地通过 SSH 隧道访问服务器回环 8090，Runner 再通过官方 SDK 1.1.1 调用回环 8081 的固定 OpenSandbox profile。管理端口不公开。

后续[真实项目实测](../../docs/research/2026-10-21-opensandbox-real-project-validation.md)在输出收集阶段失败。[独立失败诊断与缓存隔离修复](../../docs/research/2026-10-21-opensandbox-diagnostics-validation.md)现已部署并通过针对性复测，Ruff/mypy通过，完整pytest仍未通过；不能沿用样例成功放行真实项目。

Runner 使用独立 Python 3.11 环境和 `yipilot[runner]` 可选依赖。普通本地安装不需要 OpenSandbox SDK；本地 HTTP 客户端复用已有 httpx，凭据由 SecretStore 提供。源码、依赖和配置均由可信宿主部署，任务只看到自己的 work。

## 调用与数据

| 接口 | 行为 |
| --- | --- |
| `GET /health` | 认证并检查调度线程；线程不可用返回 503 |
| `GET /v1/doctor` | 只读核对调度、OpenSandbox 健康与固定镜像缓存，不创建沙箱 |
| `GET /v1/requests/{key}/{fingerprint}` | 只读对账提交结果；缺失不视为重新提交授权 |
| `POST /v1/jobs` | 有界 ZIP 输入包；完整校验、发布不可变 input 并提交 SQLite 后返回 202 |
| `GET /v1/jobs/{id}` | 查询 durable Job 状态及关联 sandbox ID |
| `POST /v1/jobs/{id}/cancel` | 取消排队任务，或向执行中的 Job 登记取消意图；仍需查询停止结果 |
| `GET /v1/jobs/{id}/result` | 只下载已可信发布的结果 ZIP，内含完整清单、增改字节、删除项及日志 |
| `GET /v1/jobs/{id}/diagnostics` | 只读取得已完成且输出无效 Job 的有界、脱敏诊断；不是成功结果或源码清单 |

所有接口要求 `Authorization: Bearer <独立 Runner key>`，与 OpenSandbox key 分开。请求只接受版本 2 合同、固定 profile/镜像、已登记的 `python-script-v1` 或 `python-isolated-script-v2` 入口及其输入脚本；不接收 shell、宿主路径、额外 Docker 参数或模型配置。

已登记的对应关系包括原 `yipilot-python-v2` 基础镜像和新增 `yipilot-tools-v3` [工具镜像](../toolchain/README.md)。冻结输入时显式传入对应的 profile 和 image；结果也必须与原输入对应，客户端拒绝跨 profile 替换。通用 Job 的默认值仍是原 profile，不改变历史指纹。新业务请求使用 v2 合同、工具 profile、512 MiB 预算和隔离 Python 入口，必须重新审批；不解释或缩减旧 1 GiB 请求。

输入包为 `request.json` 加 `source/<路径>`。校验 ZIP 中央目录数量/长度、普通文件类型、完整条目集合、便携路径、UTF-8、摘要、文件数和展开大小，不使用 `extractall`。复用现有源码扫描，拒绝链接、特殊文件、凭据目录和 `.env` 等排除项。单文件最多 16 MiB、全树最多 128 MiB、10000 文件，传输包最多 132 MiB。

相同 request key 和请求摘要返回同一 Job；内容不同返回冲突。接受前不执行；进入 running 后发生崩溃，不自动重放。重启先对账 running 和 cleanup_failed，再把已停止而结局不确定的执行记为 uncertain。不能承诺外部副作用 exactly-once。

Runner 在创建之前持久登记确定的 sandbox UUID 和 root 私有创建租约。Server 在实际 Docker 创建线程中持有同一文件锁，租约仅消费一次，并在所有创建操作返回后登记 settled。回收先撤销租约，阻止迟到请求；API 不可用或只留下 sidecar 时，宿主仅按名称、UUID 和标签均核验过的 ID 回收。缺少身份或创建完成证明时保留 cleanup_failed 和准入额度；Server 在创建中崩溃不能凭一次“未看到容器”判断安全释放。

执行结束先销毁，再从宿主核查容器/sidecar/runtime volume 和 cgroup 写入者停止，最后扫描完整 work。结果在任务不可访问的 artifacts 中原子发布。客户端用原始冻结输入重建结果，校验完整清单、增改删除、Git mode 和日志摘要，验证后才发布到新的本地目录。用户的当前工作区不会作为未修改文件的补充来源，也不会被这个样例自动应用。

若源码扫描或结果发布失败，Job 保持 failed / OUTPUT_INVALID；已有执行原因、退出码和 stdout/stderr 单独原子保存为 diagnostics.json。每个日志遵守原请求预算且不超过 128 KiB，整个诊断文档另有约 1.52 MiB 传输上限，绑定 Job、请求指纹和 sandbox 身份，并随原 Job 负载过期。诊断中不包含源码或成功/清理回执。结果接口仍拒绝该 Job；客户端将其记录为 REMOTE_OUTPUT_INVALID，日志不可用时仍保留已执行失败的事实，不称为提交前拒绝，也不自动重放。旧任务没有保存过的日志无法补回。

隔离入口 python-isolated-script-v2 的执行环境将 HYPOTHESIS_STORAGE_DIRECTORY 固定到容器私有 /tmp/yipilot-hypothesis；与现有 Ruff、mypy 临时缓存一起避免污染受控源码。旧 python-script-v1 环境语义、冻结脚本字节、镜像与数据库版本保持不变。

## 运行与验收

服务器布局：

```text
/opt/yipilot-python/                    独立 CPython 3.11
/opt/yipilot-runner/venv/              保留的前一运行环境；不能直接读新策略任务
/opt/yipilot-runner/releases/          独立候选源码、venv、wheel 与摘要收据
/etc/yipilot-runner.toml               root:root 0600，两种独立密钥
/srv/yipilot-runner/jobs.db            Runner 独立 SQLite
/srv/yipilot-runner/jobs/<id>/
  input/                              root 私有，文件只读
  runtime.json / runtime.lock          root 私有单次创建租约
  diagnostics.json                    输出收集失败时的独立执行诊断
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

最多 4 个运行槽位，同时按主容器加每个 sidecar 64 MiB/100m CPU 的总预算准入。默认总量已校准为 768 MiB、1500m CPU，保留 4 GiB 空盘。四个 128 MiB/250m 请求连同 sidecar 正好占 768 MiB/1400m；超出总量的单个请求直接拒绝。历史 v1 业务的 1 GiB 请求不被降低，在此配置下明确返回 BUDGET_UNAVAILABLE；新 v2 业务显式申请 512 MiB，本轮只验证顺序执行。命令最长 600 秒，stdout/stderr 分别最多 128 KiB。

这台标称 2 GiB 主机已通过 **1/2/4 个真实活跃轻任务**，每个样例分配 48 MiB 并保持 12 秒。四并发最低可用内存仍超过 550 MiB，无 OOM、资源残留；这是轻量内存与等待任务的校准，未证明重构建或 1 GiB 验证可用四并发。[第三阶段数据与故障范围](../../docs/research/2026-10-20-opensandbox-reliability-validation.md)。

`reliability.py` 仅供已排空的专用验收环境，以 Linux root 运行；它会创建自有任务、暂停候选 API、重启/强制终止 Runner 并注入卸载失败。通过断言后保存无凭据 JSON 收据，不在用户项目中运行。

本轮不调用真实模型，不应用到用户工作树。第四阶段已统一替换业务调用、审批、项目锁并删除旧 Docker 实时实现；真实业务样例通过，正式用户切换仍待完成。[迁移安排](../../docs/research/2026-10-19-opensandbox-implementation-plan.md)。

## 业务接入（受信样例已验收）

ProjectApplication 共享一个惰性 RunnerClient；配置保存到用户 settings-dir/runner.json，密钥通过 SecretStore 引用解析，不传给任务。审批前只读取公共端点并冻结输入，不读取 key 或调用网络。新业务使用独立 v2 请求/结果，历史 v1 DTO 保留只读，旧 DockerWorker 已删除。

远程等待持有 Run 锁；准备与最终本地 Git 应用只在短项目临界区执行，并重新核对快照、HEAD/index 与批准 diff。模型阶段仍保留既有项目所有权，不承诺所有模型请求都并行。输入/结果 ZIP 验证复用同一字节、manifest、delta 和日志规则；首次结果重建到新 staging 后才发布执行快照，后续消费重新验证包和当前文件摘要。原执行快照保留用于审计，不合并到用户源仓库。

提交意图在 HTTP 前持久化。丢失响应时通过 key/fingerprint 查询，绝不自动 POST 重放；无法确认停止或清理时本地保留 paused。run recover 对原 Job 查询/取消，确认终态后保守结束中断 Run，不继续审批或应用。新业务脚本使用 python -I，禁止仓库同名模块污染可信入口；普通历史脚本策略保持原义。上传和下载的包体传输有 20 分钟期限及单次 I/O 超时，结果传输只接受 identity 编码。

业务实测脚本 business_acceptance.py 已通过：仅模型是受信 fixture，真实 Runner 完成应用、一次实际 pytest 失败、有限修复和新的 pytest/Ruff/mypy 交付。本机经实际 SSH 隧道完成六个服务器 Job，结果、重启及清理见[真实业务验收](../../docs/research/2026-10-21-opensandbox-business-validation.md)。FixtureModel 的 FIXTURE_MODEL_KEY 只需非空占位值，实际 Runner 密钥仍由 SecretStore 解析。[当前业务迁移记录](../../docs/research/2026-10-21-opensandbox-business-migration.md)。

当前受测发布的 systemd drop-in `/etc/systemd/system/yipilot-runner.service.d/release.conf` 指向独立 release/venv；以 `systemctl show yipilot-runner --property=ExecStart` 与 runtime-release.json 确认实际路径。回退前必须处理新策略负载并保留去重记录，禁止直接还原旧数据库覆盖新增 Job。
