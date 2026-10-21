# 独立磁盘临时目录与完整 pytest 实测

2026-10-21，Runner 修复提交 `10b6309095db959dc971826952cb482ba2d47602`、`acf2176ce0e04c3903970178aa362068676f3c9d`。

接续[失败诊断与缓存修复](2026-10-21-opensandbox-diagnostics-validation.md)，同一冻结源码副本在真实 OpenSandbox 中完整 pytest **855 passed、9 skipped、1 warning**，227.76 秒；退出码为 0，stdout 1424 字节、stderr 0，最终统计完整。只删除预定的一次性 launcher，没有其他源码变化。warning 是故意构造重复 ZIP 条目的测试。

该结论对应此前的 `0d59554` 源码副本：仍排除 12 张文档 PNG，保留此前模型生成并应用的 pytest pythonpath 改动。完整测试的 262 个输入文件（含 launcher）、命令、策略和预算逐字节核对不变，仅更新执行身份。本轮没有新模型调用或业务 Change Handoff，不代表原始二进制仓库及当前控制代码在同一个远程业务 Run 中已验收。

## 实施

- 复用 `runner/disk.py` 的 ext4、UUID、loop 所有权和正常卸载检查，为工具 Job 增加同一任务独占的 `tmp.fs` 与 `tmp/`，固定 1 GiB、131072 个 inode、nodev/nosuid/noexec，预分配空间；work 仍为 256 MiB、32768 个 inode。
- Runner 串行核对空盘保留量与分配，SDK 显式挂载该任务 tmp 到 `/job-tmp`，设置 `TMPDIR`；隔离入口的 Hypothesis 缓存也进入该目录。基础 profile 继续使用原 64 MiB `/tmp`，只读 rootfs、内存/CPU/日志预算和固定镜像未改。
- 固定 profile 只允许同一 Job 的精确 work/tmp 路径及挂载目标，拒绝额外挂载、其他任务路径、别名和只读替换。Runner 与 profile 排空后一起升级。
- 临时数据不进入源码清单或结果包；停止所有写入者后卸载、分离 loop、删除临时镜像。部分创建也可回收，所有权或卸载检查失败仍保留隔离和额度。没有新依赖、数据库迁移或第二套调度机制。

## 实测与根因

新增回归在旧实现上 **5 failed、4 passed**；修复后针对性组合 **154 passed、2 skipped、1 warning**，Ruff、mypy 81 个源文件通过。真实 Linux 文件系统的 inode 容量断言在旧几何上失败，在最终几何上通过；另核对 noexec、ENOSPC 和完整回收。

| 阶段 | 真实 Job | 结果 |
| --- | --- | --- |
| 第一轮临时盘 | 容量、缓存、执行限制 | 1 passed；UID 10001，noexec、ENOSPC、缓存隔离正常 |
| 第一轮临时盘 | 原 HTTP 409 失败用例 | 1 passed，10.27 秒 |
| 第一轮临时盘 | 原完整 pytest | 后半段失败，错误报告触发 128 KiB 输出上限；没有完整统计 |
| 第一轮临时盘 | 首个新失败定位 | 587 passed、1 failed、8 skipped；字节仍有 849477632，inode 仅余 3/32768，SQLite 无法创建数据库 |
| 最终 inode 几何 | 容量、缓存、执行限制 | 1 passed；真实容器确认 131072 个 inode |
| 最终 inode 几何 | 原 HTTP 409 失败用例 | 1 passed，10.03 秒 |
| 最终 inode 几何 | 原完整 pytest | 855 passed、9 skipped、1 warning，227.76 秒，退出码 0 |

第一处是 64 MiB tmpfs 无法满足测试内部 Runner 的 512 MiB 空盘守卫；第二处是沿用 work 的 32768 inode 导致文件数量先耗尽。最终增加临时盘 inode，保持 1 GiB 字节上限，没有降低准入守卫、放宽执行权限或跳过失败测试。Ruff/mypy 的同副本成功证据见前一轮，本轮未重复运行这两个远程命令。

[临时盘提交 CI](https://github.com/Liu-Ame-Yi/YiPilot/actions/runs/39599629741)通过：Windows 870 passed / 16 skipped，Linux Runner 70 passed，profile 构建 95 passed。最终 inode 提交 `acf2176` 的[独立完整 CI](https://github.com/Liu-Ame-Yi/YiPilot/actions/runs/39603564876)也已确认 Windows、Linux Runner、profile 三个 job 全部成功。后者绑定最终控制代码；服务器855项通过绑定上述旧冻结源码，两个范围不混用。

## 部署与收尾

先备份 125 条任务，再部署独立 Runner/profile release；第一轮及诊断新增 4 条后备份 129 条，再部署最终几何。旧环境、配置、drop-in 与前后任务库都保留；回退仅切换代码与入口，不能用旧库覆盖新请求身份。

最终 release 为 `/opt/yipilot-runner/releases/private-temp-inodes-acf2176ce0e0-42aea3`，对应 profile 为 `/opt/opensandbox/yipilot-profile/releases/private-temp-inodes-acf2176ce0e0-42aea3`。两次部署均核对全部81个已安装 Python 源码摘要及独立 profile 构建摘要；SDK仍为1.1.1。

最终共 **132条任务**：74 succeeded、35 failed、13 cancelled、10 uncertain，历史身份、指纹和终态保留，schema 2、完整性正常。重启只读对账仍返回原 Job，没有新执行；容器、卷、work/tmp挂载、任务loop均为0，临时镜像已删除，OOM计数为0。Runner/profile停止且disabled，原8080服务、配置及既有业务/真实项目/诊断收据保持原摘要。本机两类临时SSH隧道均关闭。

第一轮备份 `/srv/yipilot-runner/backups/private-temp-20261021T124028Z-586bdc` 保存125→129条；最终备份 `/srv/yipilot-runner/backups/private-temp-inodes-20261021T125729Z-29238d` 保存129→132条。最新服务器审计为 `/opt/yipilot-runner/private-temp-inodes-verification.json`，并已区分历史并发/隔离验收与本轮范围。

[机器可读证据](evidence/2026-10-21-opensandbox-temporary-storage.json)保留七个 Job 的身份、摘要、结果和有界日志片段；原始结果包在服务器和两份本机临时目录，见证据。后续优先处理二进制源资源原样传输与原仓库业务主链路；重型四并发和 K8s 仍未验收，历史轻任务并发记录不能代表新 release 的新容量验收。
