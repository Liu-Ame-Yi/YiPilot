# DeepSeek、Docker 与 GitHub 真实验收记录

日期：2026-09-11。任务分支：`codex/live-github-deepseek-validation`。本轮没有修改生产实现、提交或推送当前项目；临时样例自己的 Git 初始提交只用于建立可验证基线。

## 已完成：真实模型与本地执行链路

使用用户已配置的 `deepseek-1/deepseek-v4-flash`，通过现有 ModelSelectionService 和 ProjectApplication 真实服务组合执行。没有替换模型、Docker、持久化或编排。API Key 继续留在用户级存储，未复制到项目或容器。

样例是独立 Python 仓库：把 `add(left, right)` 的减法改为加法，既有测试含三个断言。最终对照样例的两个 Python 文件带 shebang。测试现场位于本机 `.pytest_cache/live-validation-20260911-scripts/`，包含原始仓库、独立项目 state.db、模型工件、容器回执及汇总 JSON；此目录由 Git 忽略。

| 检查 | 实际结果 |
| --- | --- |
| DeepSeek API 最小探测 | request_passed |
| 模型生成并校验补丁 | succeeded；只修改指定 return 表达式 |
| 相同请求重放 | 返回原 Run 和结果 |
| 同键不同输入 | OPERATION_CONFLICT |
| 拒绝执行审批 | cancelled，未放行 |
| 真实 Docker 应用 | succeeded，退出码 0 |
| 容器内 pytest | 1 passed，三个断言通过 |
| 容器内 Ruff | All checks passed |
| 容器内 mypy | 1 个源文件通过 |
| 应用、验证重放 | 复用结果，不再次请求审批 |
| 新建应用实例与独立进程读取 | 数据库完整性、外键及终态唯一性通过 |
| 工件审计 | errors=[]，orphans=[] |
| 原始样例仓库 | 保持干净，改动仅在任务工作区 |

环境：Docker 29.2.1，WSL2 Linux backend，镜像 `sha256:cc2468926e3b6780449641ef764b580da2f06428defd80922ee90cb254ee4278`。执行回执记录 network=none、UID=10001、fallback=false；应用和三项验证容器均已清理。ECI 值未知，不宣称启用了 ECI。

最终生成 Run：`7a50a027-50c2-48ae-acc8-43cead224b19`；应用 Run：`e1ee6f61-9613-4003-bc71-2cb788a57b47`；验证 Run：`14d24ad1-f144-4aa8-b323-aaa24992a55c`。Handoff SHA-256：`0c356c0008e4422f0a8349b389acdfcce99564863a014526741b10c03fabc423`。

## 保留的失败与限制

1. 首次混用 CodexSandboxOffline 创建缓存、liuyi 访问 Docker，受控 Git 读取报 dubious ownership / GIT_FAILED。同一只读命令在两个执行用户下分别成功与失败，确认用户上下文差异。重新在同一用户下创建整个独立项目后通过；没有设置 safe.directory=* 或关闭所有权检查。失败现场保留在 `.pytest_cache/live-validation-20260911/`。
2. 不带 shebang 的普通 Python 样例在 Docker 中 pytest 通过，但 Ruff 将文件判为可执行并报 EXE002，验证正确失败，没有 Handoff。现有有限修复流程被触发，但该次模型修复 Run 为 PLANNER_FAILED。此失败没有被重写为成功，保留在 `.pytest_cache/live-validation-20260911-user/`。
3. 最终通过的是带 shebang 的独立对照样例，没有禁用检查规则。它不能证明普通 Python 仓库的 Windows 挂载权限兼容性已经解决，也不能证明自动修复环境类故障可靠。后续需单独验证文件模式与镜像内实际 Ruff 配置，不能为了放行而自动减少必需检查。

## GitHub MCP 与 Actions 真实验收

用户提供私有测试仓库 `Liu-Ame-Yi/Test01`。通过 GitHub MCP 创建 `codex/live-validation-20260911` 分支和 [草稿 PR #1](https://github.com/Liu-Ame-Yi/Test01/pull/1)，只新增 calculator.py、test_calculator.py 和最小 Actions workflow。没有上传本项目或密钥，没有合并 PR。

| 阶段 | PR head SHA | Actions 结果 |
| --- | --- | --- |
| 正确实现 | `3e0cf6a04aca8bd0e2db9e73135ec0eb1a5357d8` | [34602848349](https://github.com/Liu-Ame-Yi/Test01/actions/runs/34602848349)：success |
| 故意改成减法 | `edad506acb81e459235de025b491ce6fac2e3ba7` | [34602934580](https://github.com/Liu-Ame-Yi/Test01/actions/runs/34602934580)：failure，AssertionError: -1 != 5 |
| 恢复加法，测试不变 | `ef8e0f6a60018a76b23dcfa949322500f583b54c` | [34603009132](https://github.com/Liu-Ame-Yi/Test01/actions/runs/34603009132)：success |

三个运行均由 pull_request 触发。实际 checkout 的 tested merge SHA 依次为 `4d0f0e546bbd8ed8f75d01f422553c3135410146`、`9e9bda174ca6ecb07ce7feba5474949c34919a51`、`63241ceb795ed9b405c0e30f47116d1279f13597`；base SHA 均为 `94c559ee882f5e373080440f9a7a0c0e8de8df63`。对应 job ID 为 103274100723、103274388899、103274636214，已读取日志核对 checkout 和断言结果。成功运行各执行一个 unittest 方法，包含三个断言。查询分别绑定各 head SHA，没有将旧成功算作新提交成功。

Workflow 使用只读 contents 权限、Ubuntu runner、三分钟超时，无部署或模型密钥。checkout@v4 在本次 runner 上成功运行，但日志有 Node 20 弃用提示；后续产品接入时需重新选择受支持 action 版本。

这是 MCP 操作 GitHub 的平台实测，远端修复由本次测试显式提交；不是 DeepSeek 自动修复 GitHub CI 的端到端验收。Yipilot 的 GitHub/Actions 交付模块、required checks 门禁、远端发布恢复仍未实现或验收，不标记为完成。本轮本地仅更新验收文档，未重跑完整离线套件；先前 SQLite 全量结果不变。

本地工作分支为 `codex/live-github-deepseek-validation`，原未提交修改保留，未对本地 agent 仓库执行提交或推送。远端五个测试提交仅位于 Test01 的测试分支。
