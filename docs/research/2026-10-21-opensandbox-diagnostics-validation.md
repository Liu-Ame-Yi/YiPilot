# 失败诊断与测试缓存修复：针对性实测通过

2026-10-21，提交 `96ea1ecf7a7e2785e5d31b98b8047bd020684c55`。

接续[真实项目失败记录](2026-10-21-opensandbox-real-project-validation.md)，本次完成独立失败诊断与 Hypothesis 缓存隔离。两项针对性实测通过；同一源码副本的 Ruff、mypy 通过，完整 pytest 已返回可信失败结果，仍未通过整体验收，没有新的业务 Change Handoff。

## 修改范围

- Runner 在输出收集失败时，单独原子保存有界、脱敏的执行诊断，包括原 Job/请求/sandbox 身份、执行原因、退出码与日志。新增认证只读 `GET /v1/jobs/{id}/diagnostics`，按原 Job 保留期清理。
- 诊断合同不含源码清单、成功或清理回执；无效输出即使留下半成品 ZIP，结果接口也继续拒绝。控制端校验身份与预算后注册诊断工件，使用 `REMOTE_OUTPUT_INVALID` 表明原任务已执行、输出无效；日志不可用不构成重放授权。诊断写入失败不妨碍清理和后续调度。
- 服务器通过 SDK 的 `RunCommandOpts.envs`，将隔离策略的 `HYPOTHESIS_STORAGE_DIRECTORY` 设为容器私有 `/tmp/yipilot-hypothesis`。旧脚本策略、冻结 launcher、镜像和数据库 schema 均不变；12 份既有冻结输入在新代码下重新校验通过。
- 复用现有 httpx、SQLite、工件存储和 SecretRedactor，没有新增依赖、队列、工作流或配置框架。本次没有修改二进制源文件合同和重复全仓校验调用链。

## 验证

新增缺失日志和错误分类回归在修复前失败。修复后 Runner/客户端/worker 相关组合 57 passed，业务组合 36 passed，客户端边界/模块约束 12 passed；最后 worker 路径断言 7 passed。集合重叠，不相加。另以真实存储和调度线程确认诊断写入故障、半成品结果、诊断身份篡改三个拒绝/恢复行为。

[修复提交完整 CI](https://github.com/Liu-Ame-Yi/YiPilot/actions/runs/39576976358)通过：Windows **862 passed / 12 skipped / 1 warning**，Linux Runner **66 passed / 1 warning**，profile 构建 **95 passed**；Ruff 和 mypy 81 个源码文件通过。warning 为故意构造重复 ZIP 条目的测试。

服务器安装前备份 119 条任务及配置，构建独立环境并核对全部已安装 Python 源码摘要。当前 release 为 `/opt/yipilot-runner/releases/diagnostics-96ea1ecf7a7e-8d6bcc`，SDK 1.1.1，wheel SHA-256 `5df4b19d869552620fd415bb9d4545951e48483891f8b532fa23832533af2c88`；配置、固定 profile 和工具镜像未改。

| 真实任务 | 结果 |
| --- | --- |
| 故意生成二进制输出并打印日志 | 预期 failed / OUTPUT_INVALID；诊断保留退出码 7、stdout/stderr，Bearer 值已脱敏；结果下载仍拒绝 |
| Hypothesis 缓存样例 | 1 passed；确认 gzip 缓存实际生成在容器 `/tmp`，源码无缓存污染 |
| 同一源码副本的完整 pytest | 正常返回退出码 1 及通过清单校验的结果包；仍有多项失败，最终统计不可用 |
| 同一源码副本的 Ruff | 退出码 0，All checks passed |
| 同一源码副本的 mypy | 退出码 0，81 个源码文件通过 |
| 首个失败定位 | 诊断专用，遇错即停：59 passed、1 failed；不替代上方完整测试 |

三个完整检查复用上次的冻结源码与 launcher，仅为本轮明确授权的复测创建新执行身份；没有重放未知执行。源码仍是已核对的 `0d59554` 副本，排除同样的 12 张文档 PNG，并带上此前模型生成的 pytest pythonpath 改动；本轮没有新模型调用。

## 新发现的下一项阻塞

完整 pytest 的 stdout 仅取得 4096 字节、stderr 35 字节，没有最终统计，因此不能报告完整套件的通过/失败数量，日志不完整的具体原因尚未核实。

随后以 `-x --tb=short` 单独定位，得到首个失败：`test_explicit_requests_replay_without_new_model_docker_or_approval`。这是测试内部启动的 Fixture Runner 返回的 HTTP 409，外层 OpenSandbox Job 本身已经执行并正常发布结果。

同次诊断测得容器 `/tmp` 总容量为 **64 MiB**，首个失败后空闲约 **53.33 MiB**；Fixture Runner 的提交守卫即使配置 `disk_reserve=0`，仍要求至少 **512 MiB** 空盘。该固定准入条件与实际容量冲突，足以解释首个拒绝；不把它扩大为所有完整测试失败的唯一原因。

下一步先设计**每 Job 独立、限额、可回收的磁盘临时目录**，承载 TMPDIR 与工具缓存，保留只读 rootfs、noexec 和源码不变门禁；不能靠降低生产准入守卫、扩大内存 tmpfs 或跳过测试来宣布通过。应复用现有受控挂载与清理实现，先验证首个失败，再重新完整复测；同时核对日志完整性。二进制资源原样携带和重复校验性能仍是后续独立事项。

## 收尾与证据

原 119 条任务身份、请求 key/指纹与终态保留，新增六条后共 **125 条**，schema 2，数据库完整性正常。重启后六次只读对账均返回原 Job，诊断内容仍可读，没有新增执行。

测试后 Runner/profile 停止且 disabled，SSH 隧道关闭；原 8080 服务及配置保持正常。容器、卷、任务挂载、任务 loop 设备均为 0，OOM 计数为 0。当前部署收据和 runtime-release 已指向新 release；旧环境及原收据仍保留。

备份目录为 `/srv/yipilot-runner/backups/diagnostics-20261021T093749Z-85e045`，包含 119 条执行前库、125 条执行后库、旧 drop-in 和配置。回退只切换代码与 drop-in，不能用旧库覆盖新增请求身份。服务器审计为 `/opt/yipilot-runner/diagnostics-verification.json`。

精简后的[机器可读证据](evidence/2026-10-21-opensandbox-diagnostics.json)保存身份、摘要、结果及诊断；完整输入/结果包仍在服务器和本机 `C:\Users\liuyi\AppData\Local\Temp\ydiag-35039ae5`。本次不代表完整原仓库、业务交付、重型并发或 K8s 已通过。
