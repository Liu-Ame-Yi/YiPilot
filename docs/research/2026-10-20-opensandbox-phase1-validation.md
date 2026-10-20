# OpenSandbox 第一阶段：基线修复与隔离验证

日期：2026-10-20（Asia/Shanghai）。本轮按用户授权开始[迁移计划](2026-10-19-opensandbox-implementation-plan.md)第一阶段。**已修复三个测试环境问题，并取得隔离组合的可行性证据；G1 正式隔离门槛仍未放行。** Runner 和 YiPilot 应用执行迁移尚未实施，实验补丁没有安装到常驻服务。

后续 v1 确认了 task-side init 重绑阻断项，v2 已修复并完成 18 项 profile、故障和 Docker 重启验收；最新状态见[v2 实测](2026-10-20-opensandbox-control-isolation.md)。下文保留此前实验批次的事实，不作为当前服务状态或整套迁移放行依据。

## 1. 代码基线的具体修改

| 问题 | 复现与措施 | 验证 |
| --- | --- | --- |
| CI 收集测试时找不到 `tests.helpers` | 干净 Git archive 中，`pytest.exe --collect-only` 失败；`python -m pytest` 成功。在 `pyproject.toml` 统一设置 `pythonpath = ["."]` | 8 个收集错误消失，742 个测试可收集；此前受影响的 8 个文件共 80 个测试通过 |
| Windows CI 深临时目录使 Git 无法写 pack keep 文件 | 同一真实 repo-sync 测试，70 字符的 basetemp 失败、33 字符的基目录通过。CI 改用 `$env:RUNNER_TEMP/yipilot-tests` | 原有 repo-sync/GitRunner 测试 27 个通过；下一次完整 CI 的克隆错误消失 |
| Rich 彩色错误提示使 `--planner` 字符串被 ANSI 样式拆开 | `TERM=xterm-256color`、`FORCE_COLOR=1` 时原断言失败；用已有 Rich 的 `Text.from_ansi(...).plain` 检查实际文字 | 同一 CLI 回归测试在彩色条件下通过，仍检查非零退出和被拒绝的参数名 |

Ruff 与 mypy（72 个源文件）通过。没有保留实验性的 GitRunner 修改；CI 修复针对测试运行位置，未承诺 Git 支持任意深度的 Windows 仓库路径。

已经完成的 CI 记录：

- `31037bf` 修复收集后，完整测试暴露深路径问题：30 failed、510 passed、42 skipped、160 errors。[运行记录](https://github.com/Liu-Ame-Yi/YiPilot/actions/runs/39371324132)
- `7cc79e3` 修复临时目录后，只余彩色输出断言：1 failed、699 passed、42 skipped。[运行记录](https://github.com/Liu-Ame-Yi/YiPilot/actions/runs/39373714340)
- 本提交包含最后的 ANSI 断言修复；必须以该提交的完整 `offline-ci` 结果作为最终基线证据，不用局部通过代替全量。[分支 CI](https://github.com/Liu-Ame-Yi/YiPilot/actions/workflows/ci.yml?query=branch%3Acodex%2Fgithub-sync)

## 2. 实测默认隔离差异

探针只运行自有 Python 小样例，使用每次唯一标签和明确预算。结构化结果及脚本 SHA-256 已保存为[探针证据](evidence/2026-10-20-opensandbox-probes.json)。服务器脚本和结果保留在 `/opt/opensandbox/probes/`，不含本地模型/GitHub 私有凭据。

| 检查 | 实际结果 | 含义 |
| --- | --- | --- |
| 默认 Python 镜像 | UID/GID 为 0；rootfs 可写；可写 `/etc` 中的探针文件 | 不能直接作为正式任务 profile |
| 镜像固定 `USER 10001:10001` | SDK 命令和 `/tmp` 文件往返成功；写 `/etc` 被拒绝 | 非 root 兼容，不必因初始化失败而放宽任务权限 |
| 默认 bridge | 可 TCP 连接宿主 SSH、`169.254.169.254:80`、另一测试沙箱的 execd | 回环发布端口不等于容器间隔离；metadata 仅检查 TCP，未读取内容或凭据 |
| 只读 rootfs 直接注入 | Docker `put_archive` 返回 400、read-only 错误 | 启动路径需要调整，不能只补一个 read_only 标记 |
| 256 MiB 沙箱的 MemorySwap | 默认 inspect 为 512 MiB | 这是配置上限；宿主当前没有 swap，不能当成实际已发生交换 |
| 内置另一个隔离模式 | 源码会增加 `SYS_ADMIN` 并放宽 AppArmor/seccomp | 本轮没有启用；不能视为旧 profile 的无差异替代 |

## 3. 现成网络策略能解决什么

使用独立临时 Server `127.0.0.1:18081`，配置 `egress.mode="dns+nft"`，请求显式 `defaultAction="deny"` 和 `deny *`。egress 镜像按[官方 1.1.1 BOM](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/docs/releases/1.1.1.yaml)固定为 `sha256:bce2cc2e4dc3a3594678b91ac29efb0669fccebfdfc137727018d3b087fbb13a`。

- 从受限任务向宿主 SSH、metadata、另一沙箱 execd 发起 TCP 连接均被阻断；与默认 bridge 的可连接结果形成对照。公网目标两轮均不可连接，单凭该目标不足以证明策略有效。
- 一个未受限的测试容器仍能向受限沙箱 `/command` 发起无 token 请求，得到 HTTP 200，并实际执行打印固定 marker 的命令。**该策略管出站，不能宣称其提供了独立的入站认证。**
- 同沙箱 `127.0.0.1:44772/ping` 无 token 返回 200。不能把 lifecycle 的 API key 与 Docker execd 的接入性质混为一谈。
- sidecar 默认 `HostConfig.Memory=0`；本次在创建后只对自有 sidecar 加了 64 MiB/等量 memswap 与 0.1 CPU 限制，以约束探针。正式实现必须在创建前施加限额，不能把这次事后 update 当成上游已支持的全局预算。
- SDK destroy 后主容器、sidecar 和 runtime volume 均为零残留。

## 4. 只读与网络组合的可行性证据

单独只读实验先证明：预制固定版本 execd/bootstrap 的镜像、跳过动态注入后，可以在 UID 10001、只读 rootfs、受限 `/tmp` 中执行命令与文件往返。它不是正式部署方案。

随后组合 `dns+nft` 禁出网，进一步发现 `/opt/opensandbox` 会被每个沙箱的 runtime volume 覆盖；不能简单依赖镜像中的同一路径文件。组合实验因此保留原版运行时复制，只在确认该目录是实际 RW mount 时省去对只读根目录的 mkdir 注入。相关变化仅存在于临时 Python 进程中。

最后一次组合探针确认：

- 任务 UID 10001；根文件系统由 Docker inspect 和沙箱内 `statvfs` 同时确认为只读。
- 256 MiB 内存、等量 MemorySwap，`/tmp` 为 64 MiB、noexec/nosuid/nodev tmpfs，Docker 日志驱动为 none。
- `CapDrop` 包含 ALL，未增加 task capabilities，未使用 privileged 或 unconfined；网络分支额外列出 NET_ADMIN drop。
- SDK 命令和临时文件写读成功；运行时目录/execd 文件对任务 UID 不可写。runtime volume 本身是 RW，**不能把它写成所有挂载都只读**。
- SDK 显式请求 UID/GID 0，返回明确 permission denied/operation not permitted；没有成功执行 UID 0 命令。
- metadata TCP 连接被阻断；任务、sidecar 和卷最终清理完成，实验镜像标签删除。

该实验说明基本启动、权限和网络组合可行；未证明生产 profile、全部对抗场景或整体业务迁移完成。正式实现应在预制与受控注入之间选择一个清楚的准备路径，合并重复步骤，并把临时替换改为受测试、固定制品的正常配置实现。

## 5. G1 尚需完成的措施

| 工作 | 放行证据 |
| --- | --- |
| 将经过验证的主容器 profile 变为正常配置/固定补丁，强制 UID、RO rootfs、工作/临时目录、caps、nnp、PID、CPU、内存/memswap 和日志限制 | 创建前拒绝不完整/不合规请求，实际 inspect 与任务内事实一致；升级有回归 |
| 任务和 sidecar 的资源限制均在创建前生效 | sidecar 不再为无限内存，计入全局预算；创建/失败清理有界 |
| 明确并强制管理通道与容器间访问合同 | 所有不可信 Job 必须使用 deny profile；未受限容器混部署、控制接口可达性、初始化接口和同沙箱篡改需对抗验收，不能只依赖 localhost 发布 |
| 完成完整工作目录挂载与可信停止后采集测试 | 任务仅写自己的工作/临时目录，控制与产物区不可写，取消/超时后确认所有写入者已停止 |
| 固定可复现补丁/镜像并组合回归 | 不以进程内 monkeypatch 上线，不默认维护大规模上游 fork；评估净改动与升级成本 |

因此本轮不把现有常驻服务改为正式默认，不接收模型生成或其他不可信项目代码。下一工作包是把上述最小 profile 整理为固定、可测试的改动，再通过 G1；随后实现 Runner Job 链路。Runner Python >=3.11 环境、并发 1/2/4、耐久 Job 与恢复仍按后续门槛安排。

## 6. 本轮留下的服务器状态

原 `opensandbox.service` 仍使用 `/etc/opensandbox/config.toml` 和 `/opt/opensandbox/venv`。实验未修改其已安装源码或配置；服务继续仅监听 `127.0.0.1:8080`。

保留的新增内容是固定摘要的 egress 镜像缓存、探针脚本/结果/日志和受限实验目录。临时 API 凭据文件已删除，临时进程停止，18081/18082/18083 已关闭；最后采样为 **0 容器、0 Docker 卷**。没有全局 prune、系统重启或执行真实模型工作负载。

用户目前无需手工安装、开放端口或重配密钥。真实项目试用应等固定 profile 和 Runner 链路通过验收后，再在本地按实际输入/结果作业务审批。
