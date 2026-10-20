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

输入包为 `request.json` 加 `source/<路径>`。校验 ZIP 中央目录数量/长度、普通文件类型、完整条目集合、便携路径、UTF-8、摘要、文件数和展开大小，不使用 `extractall`。复用现有源码扫描，拒绝链接、特殊文件、凭据目录和 `.env` 等排除项。单文件最多 16 MiB、全树最多 128 MiB、10000 文件，传输包最多 132 MiB。

相同 request key 和请求摘要返回同一 Job；内容不同返回冲突。接受前不执行；进入 running 后发生崩溃，不自动重放。重启先清理已登记的运行资源，并把结局不确定的执行记为 uncertain；缺少 sandbox ID 或清理证据时记为 cleanup_failed 并暂停新准入。不能承诺外部副作用 exactly-once。

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
  work/                               任务 UID/GID 10001
  artifacts/result.zip                任务不可访问
```

`config.toml.example` 只能作为模板，两个 key 必须从可信宿主生成/读取。systemd unit 依赖固定 profile，源码包通过独立虚拟环境安装。普通安装可用 `uv sync --extra runner --no-dev --frozen --no-editable`；若使用镜像，先从同一 uv.lock 导出无项目依赖清单，再使用 `uv pip sync --require-hashes` 校验完整摘要，最后从已核对摘要的源码安装项目，不能跳过依赖验证。

服务器启动入口是 `python -m yipilot.runner.http /etc/yipilot-runner.toml`，生产配置校验要求 Linux root 和私有配置。先排空并停止服务再替换安装包，不原地修改运行代码。本轮服务仅用于受信验收，验收后停止并保持 disabled。

本地先建立 SSH 回环转发至服务器 8090，再由 SecretStore 解析独立凭据。`acceptance.py BASE_URL SECRET_REF NEW_OUTPUT_DIRECTORY` 运行五类自有样例，输出 JSON 验收记录；在已安装本项目的 Python 环境调用，不在用户项目目录执行样例代码。不要把 key 直接写入命令、示例或仓库。

## 当前容量和剩余门槛

配置最多 4 个运行槽位，同时按主容器加每个 sidecar 64 MiB/100m CPU 的总预算准入。默认总量是 1280 MiB、1500m CPU，保留 4 GiB 空盘；超出总量的单个请求直接拒绝，不偷偷缩小预算。命令最长 600 秒，stdout/stderr 分别最多 128 KiB，回调不累积超额日志。现有业务的 1 GiB 验证预算仍未改变。

**4 个槽位是实现上限，尚未证明这台 2 GiB 服务器能稳定跑四并发。** 尚未完成物理容量校准、完整断线/崩溃/销毁失败矩阵、每任务文件系统硬配额和自动保留期 GC；首版最多保留 32 个已接受 Job，达到后拒绝新任务。全局低磁盘检测不等于文件系统硬配额。创建响应丢失且没有 sandbox ID 的任务会保守隔离等待对账，不删除无法证明归属的资源。

本轮不调用真实模型，不应用到用户工作树，旧业务执行调用尚未替换。下一阶段先完成故障与 1/2/4 容量验收，再统一迁移全部调用、审批与结果合同并删除旧 Docker 执行实现。[迁移安排](../../docs/research/2026-10-19-opensandbox-implementation-plan.md)。
