# OpenSandbox v2：控制通道修复与服务器验收

日期：2026-10-20（Asia/Shanghai）。状态：**Implemented 固定隔离 profile；Runner 和业务替换尚未实现。** 18 项真实 profile 检查全部通过，额外完成隔离安装失败、控制进程死亡及排空后的 Docker 重启验收。候选服务验收后已停止且禁用开机启动。完整证据见[结构化收据](evidence/2026-10-20-opensandbox-control.json)，部署入口见[说明](../../deploy/opensandbox/README.md)。

本次继续[迁移安排](2026-10-19-opensandbox-implementation-plan.md)，修复 [v1 受信实测](2026-10-20-opensandbox-profile-validation.md)中任务调用 `/internal/init` 得到 HTTP 200 并改写 sandbox binding 的问题。新 profile 为 `yipilot-python-v2`；v1 的失败记录保留，不能作为新执行证据或新审批授权。

## 实际设计

保留官方 Server/SDK 1.1.1 和固定摘要的 Python、execd、egress 镜像。没有 fork 或重编译 execd，也没有增加镜像内第二份 execd。补丁包修改固定摘要的五个 Server 文件，附带两个小模块；构建器校验原文件、无 fuzz 应用补丁、编译并记录摘要。原版 site-packages 保持不变。

| 边界 | 实际实现 |
| --- | --- |
| 控制身份 | 容器控制进程 UID/GID 0，仅增加 SETUID、SETGID、NET_BIND_SERVICE；rootfs 只读、nnp、资源/PID/tmpfs 限制保留 |
| 任务身份 | Server 对 argv 命令强制 UID/GID 10001；文件请求转入上游 10001:10001 worker；实测任务 capabilities 为零 |
| 控制端口 | 官方 `--port` 参数改为容器内 HTTP 443；任务 namespace 的 `ip_unprivileged_port_start` 必须为 1024，控制进程死亡后任务也不能占用该端口 |
| 任务访问 | 主容器创建之前，宿主在 sidecar 独有 namespace 原子安装 nft 规则，按 socket UID 阻断任务访问 53、443、8080、18080、44772；安装失败则清理 sidecar，主容器不创建 |
| 进程及配置 | 控制 HOME 为 root 私有目录，runtime env 为 root 0600；任务不能读控制进程 environ/mem、发信号或 setuid 0 |
| 宿主代理 | 只开放健康、有限 argv 命令及降权文件路由；拒绝 init、code、PTY/session、WebSocket、任意端口与其他 UID/GID；保留上游凭据过滤 |
| 命令边界 | JSON 有界读取并拒绝重复键/未知字段；固定 UID/GID、最长 600 秒和任务 HOME；文件上传沿用流式转发，不整包缓冲 |

宿主 Server proxy 的逻辑端口仍为 44772，实际指向 sidecar namespace 的 443。远程 SDK 必须经 `use_server_proxy=True`；443 没有发布到公网，也不表示 TLS。任务普通回环 socket 仍然可以工作。

这个设计把可信控制进程与任务身份分开，不能再描述为“整个主容器非 root / 所有进程无 capabilities”。root 控制进程、宿主 Docker 权限和管理凭据仍属于可信控制面。任务只能接触自己的可写 work；input/artifacts 保留在挂载外。

## 验收证据

真实服务器：现有 Ubuntu 22.04、2 vCPU、2 GiB 标称内存、Docker 29.1.3、cgroup v2、iptables-nft。验收使用两个 128 MiB、0.25 CPU 自有任务，每个另有 64 MiB、0.1 CPU sidecar；另建短时受限 peer。**这不等于四并发或正式构建容量验收。**

| 检查组 | 结果 |
| --- | --- |
| 请求创建前拒绝、容器/sidecar 限额、只读根、工作文件往返 | 通过 |
| 任务身份、UID 0 拒绝、任务 capabilities 为零 | 通过 |
| 任务到 execd init/command/files/root-worker/PTY/session | 两个控制端口的探针均由内核拒绝；宿主校验 binding 未改变 |
| root 控制进程的信号、environ/mem、私有配置、控制端口 bind | 任务访问拒绝 |
| 宿主代理 init、UID 0、root 文件路由等额外接口 | HTTP 403；正常命令和文件不受影响 |
| 文件 worker 身份 | 上传文件宿主 owner 为 10001；不能读取 root 私有 runtime env |
| 跨 Job、metadata、宿主端口、未受限 bridge peer、凭据和策略 | 阻断；任务可读区域未发现管理密钥/token 的 hash 匹配 |
| 后台父子写入者取消 | 取消前实际观察到两个 PID；destroy 后按 PID starttime 核验停止 |
| 命令超时及可信宿主扫描 | destroy 后两次文件摘要一致，未发现继续写入 |
| 任务普通回环连接 | 通过 |

以上对应 `acceptance.py` 的 18 个布尔检查，全部为 true。额外运行 `faults.py`：在独立测试进程中注入规则安装失败，实际 sidecar 已就绪，但主容器从未创建，资源清理完成；再用 pidfd 精确杀死自有 execd，存活的低 UID 子任务仍无法 bind 443。测试替换仅用于故障注入，正常服务使用固定源码包。

在无容器、无卷的前提下重启 Docker，确认 Docker 和候选服务 PID 都已更换、专用网络规则重新安装并核验、候选健康恢复；原 8080 服务恢复健康。没有重启宿主，没有验证运行中任务跨 Docker 重启恢复。

最终：**0 容器、0 Docker 卷**；候选 8081 服务 inactive/disabled，专用网络阻断规则仍保留。原配置 SHA-256 保持 `b4500d04c25253827034395f5af281823cd87172df1f652ae8a0e398c4a6718f`，五个上游目标文件摘要未变化，依赖 `pip check` 通过。历史包只作复现证据，不用作备用后端。

## 代码检查与下一步

本地离线 profile/control/build 测试 **92 passed、1 skipped**；跳过项需要 Linux GNU patch 和固定官方 wheel，由 Linux CI 实际构建验证。Ruff 检查 src/tests/deploy 通过，mypy 72 个源文件通过。完整 Windows 和 Linux CI 的最终结论以本提交对应的运行记录为准；本地数量不替代完整 CI。

已解决 v1 的控制通道阻断项，但尚未实现耐久 Job、提交去重、全局 CPU/内存/磁盘/日志准入、崩溃对账、可信产物发布、客户端校验和全部业务调用替换。因此证据保持 `ready_for_untrusted_code=false`，不把 profile 检查全部通过写成整套迁移完成。

下一工作包按既定第二阶段做最小 Runner 闭环：本地冻结输入并经 SSH 隧道提交，服务器耐久接受后创建沙箱，停止所有写入者再发布结果，本地从冻结输入重建并验证完整输出。先跑自有 Python 小样例；之后再做故障/1、2、4 并发校准，最后统一切换调用并删除旧执行路径。现有 1 GiB 的单次验证预算不降低，新增 sidecar 的资源必须计入总准入。
