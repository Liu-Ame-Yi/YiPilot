> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# Yipilot 代码缺口：调用链、触发条件与补齐方向

日期：2026-09-07。基线：当前工作区，包括既有未提交修改。此轮只分析，不修改实现。

这是对[架构整理](2026-09-07-yipilot-architecture-assessment.md)的代码层补充。下文区分“已复现行为”“静态确认的能力缺口”和“需要专项验证的风险”，不把设计建议写成已经发生的故障。

## A. 已复现的实现问题与能力限制

### 1. 生成请求的幂等身份没有包括上下文

- 位置：`src/yipilot/workflow/code_service.py:119`，`src/yipilot/persistence/task_store.py:174`。
- `generate` 的 Operation payload 包含 instruction hash、alias、model selector/revision、request id，但不含 context_files、源码基线或文档版本。已有 Run 会在处理 context_files 前直接返回。
- 复现：同一需求、同一 request id，第一次传 a.py，第二次传 b.py；两次返回同一 run_id，第二次没有调用生成流程，也不报告输入冲突。
- 影响：用户修正上下文或自动重试时可能拿到旧提案。默认 request id 又取需求 hash，所以省略 request id 时也可能命中旧结果。
- 最小补齐：冻结实际阶段输入并纳入请求指纹；同一幂等键对应不同输入应冲突，同一输入重放才返回旧结果。重放已冻结请求与启动新输入版本要有明确合同。

### 2. 修复请求的幂等重放被动态 attempt 破坏

- 位置：`src/yipilot/workflow/repair_service.py:57`。
- 先统计当前 attempt，再把 attempt+1 写进请求 payload，最后才按 request id 查 Operation；次数限制也先于已完成请求查找。
- 复现：预置该 request id 的 attempt=1 请求，模拟已有一次修复后用相同 request id 再调用，得到 `OPERATION_CONFLICT`，没有返回已有 Run。达到两次时还会先得到 `REPAIR_ATTEMPT_LIMIT`。
- 影响：上层因响应丢失而重试会失败，不能依赖 request id 安全重放。
- 最小补齐：先识别已有请求并返回原结果；只有新尝试才原子地预留 attempt 和检查预算。

### 3. I/O 异常可留下无法继续的 pending Run

- 位置：`src/yipilot/workflow/code_service.py:321`，`src/yipilot/workflow/recovery_service.py:14`。
- `_run` 只捕获 DomainError；源码准备前后出现 OSError 可以直接抛出。Run 已提前建立，但还未开始 Runtime 生命周期。
- 复现：对源码准备注入 OSError，调用退出后 Run 仍为 pending。
- 同一 request id 再来会复用该 Run；现有 RecoveryService 只处理 running/paused，对 pending 原样返回。源码搜索只找到测试调用 RecoveryService，没有生产恢复入口。
- 最小补齐：由统一执行入口保证准备、运行、终态收敛；对孤立 pending 记录有可检查的恢复策略。恢复还需识别真正活跃的执行者，不能无条件把所有 running 都判为死亡。

### 4. 普通二进制资源会阻断整个源码导出

- 位置：`src/yipilot/application/sandbox_sources.py:90`、`:323`。
- `_export` 遍历纳入范围的 Git blobs，并对每个 blob 调用 `_text`；所有文件必须能解码为 UTF-8，且不含 NUL。后续才处理 gitignore 过滤。
- 复现：用假 Git 提供一个 docs/logo.png blob，真实 `_export` 返回 `SANDBOX_SOURCE_DENIED`。
- 影响：有 PNG、数据库 fixture 或其他二进制资产的 Python 仓库可能连代码提案准备都做不了，即使本次完全不编辑这些文件。这是当前受控范围限制，不等同于路径保护错误。
- 最小补齐：区分“允许保留的仓库资源”和“允许文本编辑/发送模型的文件”；二进制可按大小限制复制、hash 校验和只读保留，不应自动送给模型或允许随意修改。

### 5. 流式接口实际先缓冲完整响应

- 位置：`src/yipilot/models/openai_compatible.py:46`、`:131`，`src/yipilot/agent/model_planner.py:76`。
- `stream()` 返回 tuple；`_sse_chunks()` 读取完整网络响应后才 return；ModelPlanner 此后才遍历并写 model.delta。
- 复现：使用 httpx.MockTransport 分两次发送 delta，观察到 network complete 先于 caller 的第一个 delta。
- 影响：HTTP 层用了 SSE，但用户侧事件并未实时出现；中途异常也无法按当前接口把先前收到的 delta 逐步交给上层。
- 最小补齐：统一成可逐步消费的流接口，并在迭代生命周期内正确关闭连接、脱敏、限制输出和传递取消。

### 6. 不同模型错误在 Run 结果中被抹平

- 位置：`src/yipilot/agent/agent_runtime.py:171`。
- Provider 能产生 MODEL_RATE_LIMITED、MODEL_AUTH_FAILED、MODEL_CANCELLED 等错误，但 `_planner_error_code` 不保留这些类型，统一映射为 PLANNER_FAILED。
- 复现：分别传入上述三种 DomainError，均得到 PLANNER_FAILED。
- 影响：细节可能仍在模型证据中，但 Run 的调用者不能直接区分“稍后再试”“需要配置凭据”“用户取消”；若接自动工作流会被迫反查日志。
- 最小补齐：保留稳定错误分类；取消进入 cancelled，限流/暂时失败可按预算重试，认证错误等待配置，不进行代码修复。

### 7. 退出码通过不足以证明需求测试已执行

- 位置：`src/yipilot/workflow/sandbox_verification.py:50`、`:639`。
- 当前 Sandbox 验证主要核实执行结果、容器退出、清理和源码未漂移，没有收集测试数量、skip 情况、验收用例对应关系。
- 局部复现：仅包含一个被 skip 的 pytest 测试，pytest 返回 0。此检查没有启动 Docker；结合源码可确认当前 gate 没有区分这一情况的测试报告规则。
- 影响：在其余检查也通过时，Handoff 的“固定检查通过”仍可能没有任何实际业务测试执行。注意：通常“一个测试都没收集到”与“收集到但全部跳过”退出行为不同，不能混称。
- 最小补齐：保存并解析结构化测试结果，明确至少哪些验收测试必须执行。进一步核对修改前后的测试集合及跳过配置，避免通过删除/跳过测试得到错误的通过结论。

## B. 静态确认的产品能力缺口

### 8. 验证环境没有受管项目的依赖准备合同

- 位置：`src/yipilot/workflow/sandbox_verification.py:50`，`Dockerfile:1`，`src/yipilot/infrastructure/docker_worker.py:449`。
- 验证固定执行 pytest、`ruff check src tests`、`mypy src`；worker 离线、只读根文件系统，使用登记的 image digest。仓库 Dockerfile 安装的是 Yipilot 的依赖，没有根据受管项目锁文件构建环境的自动阶段。
- 当前可由操作者预制合适镜像，但“给任意 Python 项目目录就能测试”并不成立；平铺目录、不同 Python 版本、第三方运行依赖都可能阻断。
- 需要明确项目 profile：Python 版本、源码/测试路径、锁文件、所需工具和环境镜像来源。依赖构建与离线测试分阶段处理，不以打开任意网络/命令权限解决。

### 9. 修复上下文不足，且预算统计不是原子的

- 位置：`src/yipilot/workflow/repair_service.py:205`、`:226`、`:238`。
- 修复只取当前发生变化的前 16 个文件，每个截前 4096 字节，加有限的失败输出；没有按错误读取未修改测试、调用方、配置的循环。
- 因此根因位于未修改文件时，模型可能没有必要上下文；总上下文超限会直接失败，没有压缩/再检索策略。
- 次数是先扫描再创建，缺少覆盖整个“检查并预留”的链锁；同时触发修复存在绕过次数上限的竞争风险，本轮未做并发复现。
- 无 chain artifact 的失败尝试会被计数函数跳过；时间只在入口检查，不是覆盖整个修复执行的统一截止时间。不能把现有常量称为完整任务预算。
- 应按冻结链记录原子分配尝试，给失败分配明确的计数规则，并提供诊断驱动的受控上下文补充。

### 10. token、字节、费用预算没有分开

- 位置：`src/yipilot/models/openai_compatible.py:56`，`src/yipilot/domain/model.py:24`，`src/yipilot/models/selection.py:119`。
- `output_budget_bytes` 与 model.output_limit 的最小值直接进入请求字段 `max_tokens`，同一数值又用于输出字节限制。
- 即使当前 output_limit 的单位约定不够明确，output_budget_bytes 与 max_tokens 的单位混用在代码中是确定的。中文、多字节文本或大补丁不能按二者一比一理解。
- context_limit 被配置并冻结，但当前请求路径没有利用它做模型上下文容量核算；响应 usage/任务累计费用也未形成状态合同。
- 需要独立的模型输出 token 上限、输出字节安全上限、输入预算和任务累计预算，不要通过移除字节保护解决。

### 11. 取消与恢复能力停在底层，没有产品入口闭环

- 位置：`src/yipilot/transport/cli/interactive.py:89`，`src/yipilot/models/selection.py:30`，`src/yipilot/workflow/recovery_service.py:14`。
- `/cancel` 明确不可用，交互 run 是同步执行；默认 model port factory 没接任务取消信号。底层 Provider 的 is_cancelled 和 Docker 的 cancel_event 确实存在，不能误称底层完全不能取消。
- 缺少用户取消→持久请求→模型/工具停止→Run 终态→资源核实的一条路径。恢复也没有生产调用入口、阶段 checkpoint 或活跃执行者判别。
- 少人工干预仍需要可随时停止与恢复，而不是必须让终端一直开着。

### 12. 新沙箱阶段没有统一事件生命周期

- 位置：`src/yipilot/workflow/sandbox_verification.py:222`、`:497`。
- 该模块直接多处 set_run_status，没有使用 JsonlEventJournal 写统一的 run.started/run.succeeded/run.failed；worker 有自己的执行证据，但不能替代业务阶段终态事件。
- `run watch` 按持久事件观察，因此各类 Run 在状态文件与事件表现上不统一。后续一个总任务进度界面会需要拼接多套事实。
- 建议统一“执行阶段并结束”的入口，明确状态与事件写入中断后的对账方式，避免每个 service 自己维护生命周期。

### 13. 缺少真正的端到端修复验收

- 位置：`tests/unit/transport/test_interactive.py:255`。
- 当前可定位到的修复专门测试 mock 掉 RepairService.repair，验证终端输出；它不执行真实 RepairService 的幂等、次数统计、上下文、生成以及再次应用。
- 现有 Docker tests 覆盖很多执行保护，但关键词与调用搜索未找到真实修复服务贯穿“失败验证→修复提案→应用→再验证”的测试。
- 前轮 602 个离线测试通过与上述缺口并不矛盾。下一批测试应验证用户主线和真实 service 组合，而不是继续只验证界面会说“已生成修复提案”。

## C. 与上一轮结论的连接

以下已有架构缺口仍成立：真实文档生成与语义评审未接通；缺少顶层交付工作流与需求版本绑定；真模型是单次 PatchProposal；远端 commit/push/PR/CI/merge 未实现；终端承载编排；旧宿主与新沙箱路径并存。

本轮新增发现说明，在增加自动化推进前，还需要让阶段本身具备可信输入、重放、错误分类、环境与验收合同，否则自动化只会更快地重复错误。

建议优先顺序：

1. **先修运行可靠性**：生成指纹、修复幂等、失败 Run 收敛与错误分类。
2. **再补真实项目适配与验收**：二进制保留、项目依赖环境、结构化测试报告、修复主线测试。
3. **随后完成自动化控制**：工作流阶段推进、统一事件、取消恢复、真实文档评审，最终接远端交付。

## 验证说明

- 本轮做了 7 项局部行为检查：前 4 项同一脚本通过；流式消费次序和错误映射断言通过；all-skipped pytest 在限定临时目录后验证返回 0。
- 使用 unittest.mock/httpx.MockTransport 和临时文件隔离外部依赖，没有请求真实 Provider、启动 Docker、修改受管仓库或进行远端 Git 写入。
- 第一次 all-skipped 检查因 pytest 从临时文件向外寻找收集根目录，遇到 Windows 目录权限错误；随后显式限定 rootdir/confcutdir，在工作区临时目录重跑通过。该错误属于检查环境，不计为项目缺陷。
- 此轮没有再次跑全套测试，也没有把上轮结果冒充本轮验证。并发预算问题、真实依赖环境失败及完整 Docker Handoff 的 all-skipped 场景仍需专项测试。
