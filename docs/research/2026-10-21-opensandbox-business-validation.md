# OpenSandbox 服务器业务演练结果

日期：2026-10-21。结论：**受信样例的真实业务主链路通过；常驻启用、真实模型和用户项目切换尚未完成。** 受测代码为 `bc1e83844ba6a6b26cc377ed7bdfed5a671f2352`，其[完整 CI](https://github.com/Liu-Ame-Yi/YiPilot/actions/runs/39560425142)为 Windows 848 passed/12 skipped、Linux Runner 65 passed、profile 95 passed，Ruff/mypy 通过。本记录是后续证据文档，不把文档提交视为另一次运行时部署。

本机业务经 SSH 回环隧道访问服务器 Runner，再由官方 SDK 调用固定 OpenSandbox profile。仅模型提案使用受信 fixture；下面六次命令实际在服务器沙箱运行，均使用 `yipilot-tools-v3` 固定镜像、512 MiB/1 CPU/128 PID 预算与隔离 Python 入口。

| 顺序 | 真实执行 | 退出码 | 验证结果 |
| --- | --- | --- | --- |
| 1 | 初始补丁应用，将值 1 改为 2 | 0 | 只应用批准的输出 |
| 2 | pytest，预期值为 3 | 1 | 真实失败诊断进入有限修复 |
| 3 | 修复补丁应用，将值改为 3 | 0 | 新提案、新审批、新 Run |
| 4 | pytest | 0 | 通过 |
| 5 | Ruff | 0 | 通过 |
| 6 | mypy | 0 | 通过并生成 Change Handoff |

生成与修复共两次 fixture 模型调用，六处审批均保存指纹。完整输入包、远程请求、Job/sandbox 身份、结果、输出审批与交付工件可关联。重新打开控制端存储后，交付工件摘要和已接受结果均通过校验；原样例源仓库仍为值 1 且 Git clean，新任务 worktree 为值 3，没有 pending/running/paused Run。已完成 Run 的 recover 保持原终态，未重放任务。

## 部署与数据

服务器先排空并备份 SQLite、配置和发布记录，再从授权 GitHub 拉取受测提交。新环境位于 `/opt/yipilot-runner/releases/g4-bc1e83844ba6-99ac0a/venv`，Python 3.11.15、SDK 1.1.1；运行依赖按摘要锁定，wheel 摘要为 `6ee20126e4b354005ca9c56d812a44980bb3cc9940c4b2188b38f4d28b1e3220`。81 个已安装源码文件与受测提交匹配，旧 DockerWorker 模块不存在；原 profile 补丁和工具镜像未替换。

systemd 的 `yipilot-runner.service.d/release.conf` 指向新环境，旧 venv 和备份保留。备份目录为 `/srv/yipilot-runner/backups/g4-20261021T073244Z-a04f8a`，含演练前、后的数据库。部署收据 `/srv/yipilot/deployment.json` 与 `/opt/yipilot-runner/runtime-release.json` 已记录受测提交、CI、wheel、运行路径及停止状态。

历史 111 条任务的 ID、请求 key、指纹与终态均保留；加上新六条共 117 条，schema 2、integrity_check=ok。保留上限仍为 32 份负载，其余为去重墓碑，正常保留清理不等于丢失请求身份。Runner 重启后只读查询六条原请求，均返回原 Job，未新增执行。旧程序不能直接混用含新脚本策略的任务库；回退必须保留新增去重身份，不能用 111 条旧库覆盖 117 条新库。

## 清理与边界

六条结果均记录 writers_stopped/resources_removed=true；宿主复核容器、卷、任务 work 挂载和 loop 设备均为 0，OOM 计数为 0。一次运行间隙采样可用内存为 914960 KiB，停止后为 1106388 KiB，这不是峰值或四个重任务的容量保证。Runner 和固定 profile 在演练后停止并保持 disabled；原 8080 服务健康，原配置摘要未变，密钥配置未改，管理端口未公开。

仍未验证：真实模型、任意用户项目及额外依赖、真实基础设施上的业务中断恢复、重构建并发和 K8s；常驻服务、本机持久凭据与隧道仍未启用。下一步按此受测版本配置本机连接，再用选定用户项目做首次受控切换。

无凭据的逐 Job 证据、摘要和部署/重启清理收据见 [JSON 记录](evidence/2026-10-21-opensandbox-business.json)。

后续真实默认模型与 YiPilot 项目实测已另行完成：模型和应用通过，完整验证失败；本轮样例的成功不变，也不能替代该真实项目的未通过结论。最新边界、119条任务收尾和修复安排见[真实项目记录](2026-10-21-opensandbox-real-project-validation.md)。
