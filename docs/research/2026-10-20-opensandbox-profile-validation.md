# OpenSandbox 固定 profile：部署候选与阻断项

**Historical reference：这是 v1 的失败验收记录。** 控制通道阻断项已由 [v2 修复与实测](2026-10-20-opensandbox-control-isolation.md)接替；下文保留当时事实，不作为当前服务状态。

日期：2026-10-20（Asia/Shanghai）。Implemented 范围：已形成固定版本的 Server 隔离候选制品，并在现有华为云上完成受信 fixture 验收。**12 组检查中 11 组通过，execd 初始化重绑检查失败，G1 未放行。** 尚未实现 Runner，也未迁移 YiPilot 的正式执行调用。

这是[第一阶段记录](2026-10-20-opensandbox-phase1-validation.md)的后续实施，按[迁移安排](2026-10-19-opensandbox-implementation-plan.md)执行。部署文件与构建说明见 [deploy/opensandbox](../../deploy/opensandbox/README.md)，结构化结果见[本轮证据](evidence/2026-10-20-opensandbox-profile.json)。

## 实际修改

- 使用正常源码补丁包替代此前进程内实验替换。保留原版 site-packages；构建只接受四个目标文件的固定 1.1.1 摘要，拒绝覆盖已有 release，生成补丁后摘要并编译。
- Server 在创建副作用之前校验固定镜像、显式 deny-all、有限资源、Job 标签、固定 supervisor 与唯一 work 挂载。拒绝请求自带 env、扩展隔离开关、任意入口、额外宿主路径或不完整资源。
- Job 所有祖先目录必须是 root 所有且不可由 group/other 写入；work 必须是普通私有目录、属于 10001:10001。符号链接 mount point 在创建前拒绝。
- 主容器创建时强制 UID/GID 10001、RO rootfs、ALL cap-drop、no-new-privileges、128 PID、内存等量 memswap、有限 noexec/nosuid/nodev tmpfs、16 MiB shm 和 none 日志驱动。
- sidecar 在创建时设置 64 MiB 内存/等量 memswap、0.1 CPU、64 PID，drop ALL 后只增加所需 NET_ADMIN；不再依赖创建后 update 才获得限额。
- 专用 internal bridge 关闭 ICC 和 IPv6；宿主桥接过滤、INPUT 和 DOCKER-USER 规则补上到/从该桥的限制。网络脚本重复运行成功，遇到同名不合规网络/链会拒绝覆盖。

现有单次请求的 1 GiB 验证预算没有被改小。候选镜像仍是 Python 隔离测试基底，不是正式工具镜像；Runner 全局资源准入和 sidecar 计费也未实现。

## 修复的真实启动差异

直接把上游 sidecar 放入 internal bridge 后，首次正常创建超时。对照观察确认：sidecar 正常运行，宿主到其内部 18080 端口可连接；该网络没有提供就绪检查所依赖的宿主映射端口。上游等待 30 秒后报 connection refused 并清理。

将 sidecar 就绪检查改为内部地址后，主容器已正常运行，非 root execd 也在监听，但 SDK server proxy 仍返回 502。进一步确认 `get_endpoint(resolve_internal=True)` 对 sidecar 分支仍强制走宿主映射。因此同时修正代理寻址，使用真正拥有 network namespace 的 sidecar 内部地址，保留其策略 API 认证头。SDK 随后正常完成创建、命令与文件往返。

这两处修改都保留 internal bridge 和资源限制。内部端点不能直接供远程客户端访问，调用必须使用 `use_server_proxy=True`。构建与验收记录含本次最终补丁摘要；早期失败包仅保留在服务器历史目录，没有服务入口。

## 最终实测

使用两个 128 MiB、0.25 CPU 任务，各自附带 64 MiB、0.1 CPU sidecar；另有一个短时、受限的 default-bridge peer 作为入站对照。这是隔离检查，**不是四并发容量或性能验收**。

| 检查 | 结果 |
| --- | --- |
| 缺失网络策略、可变 tag、env、扩展隔离、缺内存、越界宿主路径、符号链接 mount point | 均在创建前拒绝；容器/卷集合未增加 |
| 主容器与 sidecar 创建时资源、身份、RO/caps/nnp/PID/tmpfs/logs | Docker inspect 与候选值一致 |
| SDK 命令、work 文件上传/下载、临时目录写入 | 通过 |
| `/etc`、runtime 目录/execd、Docker socket、挂载外控制目录 | 任务不能写入；input/artifacts/宿主 SSH 目录不可读 |
| 显式 UID/GID 0 命令及 raw socket | 权限拒绝 |
| 跨 Job、metadata、宿主 SSH/管理端口；未受限 bridge peer 到任务 execd | 连接失败 |
| 管理 key 与 sidecar token 的 hash 对照 | 任务可读 env、proc environ 和 runtime env 文件中未发现对应值；没有把原值放入探针命令 |
| 任务无 sidecar token 修改 `/policy` | 401/403 拒绝 |
| 取消后台父子写入者 | 实际观察到 2 个宿主 PID，destroy 后均停止；使用 PID starttime 防止误认复用 |
| 命令 timeout 后 destroy、宿主采集 | 真实停止后两次宿主文件摘要一致 |
| 任务调用本机 `/internal/init` | **HTTP 200，`sandboxId` 被改为 `fixture-rebound`；失败** |

最终验收脚本有意以非零退出：初始化重绑不是允许忽略的测试。其余通过项不能替代全部 G1 条件。Docker lifecycle、Job 关联与可信采集必须由宿主事实决定，不能信任任务内可改写的 execd binding。

## 当前服务状态

- 新 `opensandbox-profile.service` 已安装，用回环 8081 完成受信验收后停止，开机启动为 disabled。
- `yipilot-sandbox-network.service` 的专用阻断规则保留；检查通过。没有停止 Docker、重启机器或全局 prune。
- 原 `opensandbox.service` 仍仅回环 8080、active；原配置摘要保持 `b4500d04c25253827034395f5af281823cd87172df1f652ae8a0e398c4a6718f`，没有覆盖原密钥或包。
- 最终 **0 容器、0 Docker 卷**。保留少量自有 Job fixture 目录、制品、日志与收据作为证据，没有把它们当作 Runner 数据库。
- systemd unit 的静态验证通过；未做 Docker/宿主真实重启演练。

## 代码检查与后续顺序

新增离线守卫与构建测试：本地 40 passed、1 skipped；跳过项需要 Linux、GNU patch 和固定 Server wheel。Ruff 检查 src/tests/deploy 全部通过，mypy 72 个源文件通过。新增 Linux CI 从官方 wheel 的固定 SHA-256 安装源码且不装运行依赖，实际应用补丁并核查原包未被修改；完整 Windows CI 与 Linux 构建结论以本提交运行记录为准。既有 `ca95ea6` 完整基线为 700 passed、42 skipped：[记录](https://github.com/Liu-Ame-Yi/YiPilot/actions/runs/39375829322)。

下一工作包首先解决 **execd 管理通道可由任务操作** 的问题，覆盖 init、直接 command/files/session、身份和凭据，而非仅移除一个 route 后宣称隔离完成。[固定上游 Init Apply](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/components/execd/pkg/web/controller/init_runtime.go)说明其 binding 替换路径；本轮已经把此前源码推导变成真实失败用例。仅修改 Server HTTP 鉴权不会阻止任务访问同容器 localhost。

控制边界修复后，重跑失败用例及全部 profile 验收，补启动失败清理、重启与升级回归，再评估 G1。此后才做最小 Runner Job 闭环、1/2/4 容量测量和全部业务调用替换。用户目前无需新增服务器配置、开放端口或发送凭据。
