> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# Yipilot 单机持久化与交付设计依据

日期：2026-09-07。范围：为新架构提供持久化、恢复和 CI/合并合同；本文是设计建议，不代表代码已实现。事实来自官方资料，业务合同为针对 Yipilot 的建议。

## 1. 事实与选型

**事实。** Python `sqlite3` 提供无需独立服务的 SQLite 访问，连接默认受创建线程约束，事务行为必须显式选择，支持在线备份。应记录实际 `sqlite3.sqlite_version`，不能从 Python 版本推断 SQLite 版本。[Python 官方文档](https://docs.python.org/3/library/sqlite3.html)

**事实。** SQLite 单个数据库事务具备原子提交机制；同一时刻只有一个写事务，`BEGIN IMMEDIATE` 可以提前取得写事务，也可能返回忙。[原子提交](https://www.sqlite.org/atomiccommit.html)、[事务](https://www.sqlite.org/lang_transaction.html)

**建议。** 单机首版以一个 SQLite 文件作为任务、阶段尝试、操作意图、事件、幂等记录的唯一状态源，文件系统保存不可变大工件。保留现有工件 hash 和文件保护；不再让 JSON 状态与数据库各自可写。默认 rollback journal + `synchronous=FULL` 已足以支持短事务。实测读写并发需要时再启用 WAL，不引入 ORM、Redis、Postgres 或通用工作流引擎。

**运行边界。** 状态目录放宿主机本地磁盘、任务可写 worktree 之外；Windows 支持本机普通目录，不支持 SMB/NFS、映射网络盘、同步盘中的活跃数据库；容器不直接挂载状态数据库。使用同一宿主 API 访问它。WAL 依赖同机共享内存，不能作为共享盘多节点方案；文件锁及 flush 仍依赖底层文件系统和硬件正确实现。[WAL](https://www.sqlite.org/wal.html)、[损坏原因](https://www.sqlite.org/howtocorrupt.html)

**版本注意。** 官方当前记录 WAL-reset 竞态影响旧版本，修复在 3.51.3 及以后，另有 3.50.7、3.44.6 回补。若启用多连接 WAL，要检查实际引擎是否包含修复；不可无条件给旧 Windows Python 开 WAL。WAL 模式也要处理 busy、长读事务妨碍 checkpoint 和 WAL 文件增长；备份使用 SQLite backup API，不只复制主 `.db`。[官方 WAL 公告](https://www.sqlite.org/wal.html#walresetbug)、[Python backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)

## 2. 最小事实模型与事务边界（建议）

可以从 `workflows`、`runs`、`operations`、`events`、`artifact_refs` 五类表开始；业务审批与检查快照可先作为版本化 JSON 数据保存在相应记录中，不为每个名词建表。

- Workflow：阶段、状态、revision、当前 run、输入版本、策略版本、等待原因、下一次检查时间。
- Run：一次阶段尝试；固定输入指纹、attempt、终态、结果工件引用、错误类别、预算消耗。阶段重试产生新 Run；传输重试返回同一个 Run。
- Operation：一个有副作用的动作；唯一操作键、规范化输入 hash、prepared/in_flight/succeeded/failed/unknown、外部对象 ID、预期 SHA、结果。`unknown` 是必须对账的状态，不能等同失败。
- Event：单调游标、任务及 Run ID、类型、结构化 payload。状态更新和生命周期事件同事务；UI 从游标增量读，内存通知仅为唤醒优化。
- Artifact ref：内容 hash、大小、类型、相对路径、生产 Run、输入版本。正式结果必须引用不可变版本。

一笔短写事务完成：校验 workflow revision 和当前阶段 → 校验/插入唯一请求键与输入 hash → 预留尝试次数及预算 → 创建 Run/Operation → 更新状态 → 追加事件。相同请求键但输入 hash 不同返回冲突，不用新的动态 attempt 重新构造原请求身份。

网络、模型、Docker、Git 命令均在事务外运行。结果短事务再用 expected revision / ownership 条件写入。不能持有数据库写锁等待几分钟模型响应。第一版一个任务只允许一个活动执行者，执行器串行处理工作区写入；多任务可有独立 worktree。禁止仅凭超时租约就让第二个执行者接管仍活着的本机进程：应确认进程/容器停止后再继续，否则数据库 fencing 并不能阻止旧进程写文件或调用远端。

## 3. 文件工件与崩溃恢复（建议）

工件写入顺序：同卷临时文件 → 写完并 flush/fsync → 校验 hash → 原子发布到内容寻址路径 → 数据库事务写引用。失败只留下可回收的未引用文件；不先提交指向尚不存在文件的引用。文件发布和数据库提交不构成共同事务：断电后必须重新验证引用文件的存在、大小和 hash，异常进入受阻状态，不假定 Windows rename 等于目录元数据已持久化。清理需宽限期，并在无活跃写入时执行；备份包含数据库快照所引用的全部工件。

| 崩溃点 | 恢复动作 |
|---|---|
| Run 尚未提交 | 无可恢复尝试；请求可重新进入事务 |
| Run 已提交，工作尚未启动 | 继续准备，或按取消状态结束；不能永久留 pending |
| 模型输出了一半 | 不把半段消息当正式结果；结束旧尝试为 interrupted，新尝试计入预算，不能保证模型侧不重复计费 |
| 工具可能已写文件 | 检查操作日志及工作区版本；可验证才接续，否则恢复到已知快照或受阻，不能直接重放任意 shell |
| 文件已发布、DB 未引用 | 暂时孤儿；重试可复用同 hash，宽限期后清理 |
| 远端请求已发、响应未落盘 | operation=unknown，先查远端事实再决定是否重试 |
| 等 CI 时进程退出 | 保存 PR/build ID、SHA、截止时间；启动后重新查询，不重新开始编码 |
| 状态已提交、UI 未显示 | 游标补读事件；不重复业务动作 |

启动恢复是生产入口的固定步骤，也可显式运行。取消先持久化 requested，再停止模型/工具/子进程，确认停止后写终态；无法确认停止则记录 cancel_pending。已经发生的 push/merge 不能通过取消自动撤销。

## 4. GitHub 交付与 SHA 合同

**事实。** REST 合并接口接受 `sha`，PR head 不符返回 409。GitHub required checks 必须属于最新提交；有测试合并提交结果时，平台还会按测试合并提交处理门禁。不能只查询旧 head 上一次绿灯。[合并 API](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)、[必需检查说明](https://docs.github.com/en/enterprise-cloud%40latest/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)

**建议。** Delivery 保存 repo、base branch、任务 branch、head SHA、base SHA、已验收文档 hash、review/verification 证据 hash、策略版本。push 后重新确认远端 head；任何 head 改变使本地验收失效。第一版默认：等待远端门禁 → 复查当前 head 和本地证据 → `merge(sha=accepted_head)` → 查询确认 merged 和 merge commit → 才完成 Workflow。不绕过远端 branch protection；base 推进则服从 up-to-date/merge queue 要求。

**事实。** GitHub auto-merge 等待所需审查和状态检查；当前 GraphQL `EnablePullRequestAutoMergeInput` 提供 `expectedHeadOid`。它是启用时的预期 head，不应解读成永远锁死后续推送；官方仅明确某些无写权限用户的变更会关闭 auto-merge。[Auto-merge](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/automatically-merging-a-pull-request)、[GraphQL 输入](https://docs.github.com/en/enterprise-cloud%40latest/graphql/reference/pulls#enablepullrequestautomergeinput)

**建议。** 如采用平台 auto-merge，必须将 Yipilot 的设计/评审/验证结论作为可信发布者产生的必需检查绑定到当前 SHA。不能依赖本地轮询及时取消旧授权；新 head 在缺少新 gate 时应被远端阻止。否则用上述 SHA 条件合并更清晰。启用 auto-merge 只是 waiting，不是完成。合并队列若启用，检查必须覆盖 queue 候选提交，不能拿普通 PR 构建替代。

**事实。** GitHub 的 skipped/neutral 可能满足平台检查规则；因此 Yipilot 不能把平台绿色直接等同“需求测试执行完毕”。[状态检查](https://docs.github.com/en/pull-requests/reference/status-checks)

**建议。** 独立验证 gate 校验实际用例、失败/跳过数、必需验收条目和被测版本。发布 gate 的凭证只在可信控制面，不能交给运行待审代码的沙箱。自动合并策略需要预授权，但不能生成伪造的人工审批。

## 5. Jenkins 结果接入及外部幂等

**事实。** Jenkins Remote API 支持触发带参数构建、读取队列及具体构建；JUnit 插件可以收集测试报告。GitHub commit statuses 支持按提交发布状态、context 和结果链接。[Jenkins Remote API](https://www.jenkins.io/doc/book/using/remote-access-api/)、[JUnit](https://www.jenkins.io/doc/pipeline/steps/junit/)、[GitHub statuses](https://docs.github.com/en/rest/commits/statuses)

**建议。** 触发参数含 task/run/operation ID、repo、确切 head SHA；Jenkins 必须 checkout 该 SHA 并报告实际 checkout SHA、job/build ID、配置版本、JUnit/工件摘要。控制面取回并校验，不接受仅有 SUCCESS 字符串的通知。queue ID 关联到固定 build ID 后持久化，禁止用 `lastSuccessfulBuild` 判断本次需求。回调只唤醒查询；重复或乱序结果按外部 ID 去重，旧 SHA 结果可存档但不放行。旧失败晚到不能覆盖新尝试成功，反之亦然。

外部动作始终执行“记录意图 → 调用 → 查询确认 → 记录结果”：

- push 超时：查询远端 ref。已到预期 SHA 则成功；不同且无法解释则冲突。
- 创建 PR 超时：按精确 repo/head/base 查询并核对任务标识，发现多个候选则受阻，不盲目再建。
- Jenkins 触发超时：按 operation 参数对账队列/构建。通用 Remote API 未提供本文可确认的服务端幂等保证；无法确定时保留 unknown。若业务允许重复构建，显式记录重复成本和选择规则。
- merge 超时：查询 PR merged 状态与 merge commit；不把网络失败当作合并失败，再重新推送/重新合并。

本地唯一键只防本地重复受理，**不能承诺外部 exactly-once**。恢复是对账与有条件重试，不是重放全部日志。无需为了这些有限动作先构造通用 Saga 引擎。

## 6. 落地时必须有的验证（建议）

在上述每个“外部调用前/后、提交前/后”故障点注入中断；验证同键异输入冲突、重试不多占次数、重复/乱序 CI 不放行旧 SHA、DB 引用缺失工件阻止推进、未知合并结果可对账、取消后无活跃写入、重启等待不重跑已完成阶段。Windows 本地磁盘做一条集成路径；共享盘明确不列支持项。本文未执行这些测试，也未声称已有实现通过。
