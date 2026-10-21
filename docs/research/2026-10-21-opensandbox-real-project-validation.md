# 真实模型与 YiPilot 项目实测：尚未通过完整验收

日期：2026-10-21。接续[六 Job 受信样例验收](2026-10-21-opensandbox-business-validation.md)，本次使用已有 DeepSeek 连接、真实 YiPilot 源码和华为云 OpenSandbox。结论是**真实模型生成与远程应用成功，真实项目完整验证未通过**。没有生成 Change Handoff，不放行正式切换。

后续[失败诊断与缓存修复](2026-10-21-opensandbox-diagnostics-validation.md)已通过针对性服务器复测，Ruff/mypy 通过，完整 pytest 仍失败；首个新阻塞为容器临时空间与测试内部 Runner 准入冲突。下文保留修复前的失败范围，当前安排以后续记录为准。

## 实际执行范围

控制端修复提交为 `0d59554eabcb1e943dd7443e0e6eb924622ef63d`；服务器仍运行已验收的独立环境 `g4-bc1e83844ba6-99ac0a`，profile 和工具镜像未改。控制端使用真实 `ProjectApplication`、`ModelSelectionService` 和默认 `RunnerClient`，没有替换模型端口或远程执行器。

第一次直接接入完整仓库，在模型调用前以 `SANDBOX_SOURCE_DENIED` 拒绝：仓库中 12 张架构文档 PNG 不符合现有“UTF-8 可补丁文本”规则，没有调用模型或新建远程 Job。

第二次仅在独立测试源副本中删除这 12 张文档截图，保留全部源码、测试、依赖和部署配置。261 个文件共 5,035,615 字节；该副本 Git tree 经独立核对，与控制端修复提交只有这 12 个删除项的差异。原仓库截图保留，不将这个副本的结果扩大为完整原仓库可用。

真实模型任务为：仅将 `pyproject.toml` 的 pytest `pythonpath` 从 `["."]` 改为 `[".", "src"]`，用于不做 editable 安装的 src 布局验证。显式模型上下文只有该配置文件；提案路径、修改后的 TOML 及 Git 差异均被检查。改动只应用到独立任务 worktree，未将未通过验证的模型提案合入主仓库。

## 结果

| 环节 | 实际结果 | 证据或边界 |
| --- | --- | --- |
| 默认模型 | DeepSeek Flash，reasoning-high，正常 stop | 4.13 秒；424 字节正文；总用量 1,387 tokens |
| 远程应用 | 成功，退出码 0 | Job `7732791a30294304b2f92436ca540b37`，UID 10001，执行约 4.53 秒 |
| 本地接收与应用 | 成功 | 新任务 worktree 只有预期的一行配置改动；重新打开应用后接受证据 gate 通过 |
| 完整 pytest | 已实际运行，结果收集失败 | Job `e286693941354c4da9f8176e426e10f0`，Runner 终态 failed / `OUTPUT_INVALID` |
| Ruff / mypy | 未执行 | 本地子 Run 被取消，没有提交对应远程 Job |
| 修复与交付 | 未完成 | 未产生新的模型修复 Run 或 Change Handoff |

失败 Job 没有发布 `result.zip`。现有失败分支没有保留可用的 pytest stdout、stderr 和退出码，因此**不知道测试集通过了多少，也不能断言 pytest 命令本身通过**。应用侧将结果下载的拒绝映射为 `RUNNER_REQUEST_REJECTED`，这与 Runner 已执行后报告 `OUTPUT_INVALID` 的事实需要一起解释，不能解释为“从未创建远程任务”。

## 已定位的问题

1. **二进制资源合同过窄。** 完整原仓库的 PNG 在 Git 源码导出阶段被拒绝；这不是传输、服务器配置或内存问题。不能通过静默丢弃业务资源来宣布整仓兼容。
2. **测试缓存落入受控源码目录。** 对已卸载工作盘使用只读 `debugfs` 检查，发现新生成的 `.hypothesis/unicode_data/15.0.0/charmap.json.gz`，21,726 字节，SHA-256 `635be22cd52f26007d6a50c890cf0ce06dea6271bc7126cf4bb8adef4b0f1d6a`。实际字节具有 gzip 头、包含 NUL 且不是 UTF-8，足以触发当前输出拒绝；这是确定的触发因素，不代表已经排除所有其他测试问题。没有挂载或执行失败工作盘内的代码。
3. **失败诊断缺失。** `Runner._publish` 在源码扫描失败后不发布结果包，连已经收集的有界日志和退出事实也没有持久化，阻碍控制端诊断和有限修复。拒绝不可信输出仍然必要，应单独保留失败诊断，不能把部分结果当成成功证据。
4. **完整仓库复核慢。** 单次冻结源码扫描实测约 2.17 秒；从远程应用成功到实际 pytest 提交之间，还经过了多轮完整扫描、差异重建、源副本导出及三个验证输入冻结，累计达到分钟级。当前测量尚未把各环节耗时完全分摊，不能把它都归因于某个函数；需进一步 profile 后精简调用链。

## 本轮已完成的修复和连接配置

已有公开模型配置在 ModelEntry 顶层保存 `reasoning_effort`，当前严格 schema 拒绝读取。原 `providers.json` 已备份，将该选项移入已有 variant 的 `request_options`，默认选择改为 `deepseek-1/deepseek-flash:reasoning-high`；原 auth 文件未改。

真实连通测试还复现了 gzip 响应被 `iter_raw()` 直接当 JSON 解析的问题。共享模型客户端现在请求 `Accept-Encoding: identity`，在读取前拒绝违反该约定的压缩响应，保持原始传输预算与解析预算一致；新增 JSON/SSE 协商及未请求压缩拒绝共四项回归，修复前失败、修复后通过。真实非流式探测和后续真实流式提案均已通过。没有添加依赖。

本机默认用户设置已保存 `http://127.0.0.1:18090`，Runner key 只放在 Windows Credential Locker，引用为 `keyring:runner-huawei`；未把实际密钥写入仓库或命令参数。任务中的 2,922 个非 ZIP 文件经实际凭据字节扫描未发现模型 key 或 Runner key。SSH 隧道仅用于本次测试，已关闭；这不等于配置了开机常驻连接。

修复提交的[完整 CI](https://github.com/Liu-Ame-Yi/YiPilot/actions/runs/39570327349)通过：Windows 852 passed / 12 skipped / 1 warning，Linux Runner 65 passed / 1 warning，profile 构建 95 passed；Ruff、mypy 81 个源码文件通过。集合有重叠，不相加；这些 CI 结果不能代替本次失败的真实项目验证。

## 清理与数据

原 117 条任务的身份、请求 key、指纹及终态全部保留；新增成功应用和失败 pytest 两条后共 119 条，数据库 integrity_check=ok，无 queued/running/cleanup_failed。未重放失败或未知任务。

备份：`/srv/yipilot-runner/backups/real-project-20261021T085843Z-6670d3/jobs.db`。测试后 Runner 与固定 profile 已停止且 disabled；原 8080 服务健康，原配置摘要不变。容器、卷、work 挂载及 work loop 设备均为 0，主机 OOM 计数仍为 0。停止后可用内存快照为 1,091,656 KiB，这不是峰值或重任务并发保证。失败工作盘保留供审计，未做全局清理。

测试驱动收尾时发生了一次自身保护断言失败：驱动以 `git diff HEAD` 比较前后状态，而本轮期间已将验证过的压缩修复提交，HEAD 变化使该断言失效，并打断其自动关闭隧道。独立证据收集已重新核对源 tree、已接受应用 gate 和所有 Run 终态；按父子进程身份关闭遗留的 SSH 子进程，再完成服务器资源审计。没有把驱动退出码视为业务成功证据。

## 下一步措施与验收顺序

| 顺序 | 修改位置与措施 | 通过条件 |
| --- | --- | --- |
| 1 | `runner/service.py` 与客户端错误映射：输出拒绝时独立保存有界、脱敏的执行诊断，明确区分“提交被拒绝”和“已执行、输出无效” | 对真实失败 Job 可取得原身份、失败阶段和日志；仍不生成成功结果或重放任务 |
| 2 | 固定验证 launcher / 工具环境：将 Hypothesis 等测试缓存放到独立 `/tmp` 路径；保持批准源码和固定命令门禁 | 整套 pytest 运行后没有新增源码文件；缓存不能越界写宿主；保留缓存行为的回归测试 |
| 3 | `sandbox_sources.py`、`remote_bundle.py` 与输出 gate：设计资源原样携带的合同，文本补丁与不可修改的二进制资源有明确边界 | 原仓库 12 张 PNG 原样参与输入并通过摘要校验；篡改、新增或删除未授权资源仍被拒绝；控制端与 Runner 按同一合同升级 |
| 4 | profile `accepted → prepare → _inputs → run → handoff` 的全仓处理；复用同一信任边界内已读取的冻结事实，批量读取 Git 对象，删除重复编排 | 在相同 261 文件输入上记录前后耗时；漂移、审批、符号链接、硬链接与包篡改测试继续通过，不跨可变边界缓存校验结论 |
| 5 | 重新执行真实项目生成/应用、完整 pytest/Ruff/mypy、交付与重新打开后的消费检查 | 三个真实检查均通过，有新的可信 Change Handoff；再决定常驻启用 |

重型并发、真实业务中断恢复及 K8s 继续保留为独立验收项。本次没有为了得到绿色结果缩减测试集或放宽不可信输出门禁。

机器可读证据见 [JSON 记录](evidence/2026-10-21-opensandbox-real-project.json)。服务器清理收据位于 `/opt/yipilot-runner/real-project-verification.json`；本机两个测试目录和未入库的 `tmp/real-*` 保留原始诊断。
