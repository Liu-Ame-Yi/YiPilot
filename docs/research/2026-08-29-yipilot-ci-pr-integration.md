> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# Yipilot 接入 Jenkins CI 与 GitHub PR 的调研

> 调研范围：仅 Jenkins 官方文档，以及 GitHub 官方文档/官方仓库；结论针对 Yipilot 现有的「工作区、Run、Artifact、审批、prepare-only」边界。

## 结论

可以接入，而且应把 Yipilot 定位为**变更与证据编排层**，而不是第二套 CI。最小闭环是：Yipilot 创建功能分支和 PR（仍保留人工批准）；GitHub Webhook 或 Jenkins Multibranch Pipeline 触发确定性的构建；Yipilot 回收构建编号、commit SHA、报告与结果并写入 Run Artifact；GitHub 的分支保护规则以 CI 检查和人工审批为唯一合并门禁。

这保留了现有的安全原则：模型/Agent 可以提出变更和生成证据，但不能绕过 CI 或代替人批准并合并。

```text
PRD/SRS/SDD + Yipilot Run
  -> task 专属 worktree，生成 diff
  -> 人工批准「提交/推送」
  -> feature/yipilot-<task>-<run> 分支 + GitHub PR
  -> GitHub webhook -> Jenkins Multibranch Pipeline
  -> build / unit / lint / scan / coverage
  -> Jenkins 结果显示为 PR status/check
  -> Yipilot 导入构建证据（不可变 SHA、URL、报告哈希）
  -> GitHub 必需检查全绿 + Code Owner/人工审查 + 对话已解决
  -> 具备权限的人或受限 Merge Queue 合并 main
```

## 推荐的分期实现

### 第一阶段：Jenkins 作为外部质量闸门（优先）

1. 仓库提交版本化 `Jenkinsfile`，使用 **Multibranch Pipeline + GitHub Branch Source**。Jenkins 会发现包含 `Jenkinsfile` 的分支；该模型也支持 PR，构建环境提供 `CHANGE_ID`（PR 编号）。这样 CI 定义随代码接受审查并自带审计轨迹。[Jenkins Pipeline as Code](https://www.jenkins.io/doc/book/pipeline/)；[Jenkins Multibranch/PR](https://www.jenkins.io/doc/book/pipeline/multibranch/)
2. `Jenkinsfile` 内只做确定性工作，例如依赖安装、语言对应的 test/lint/scan、coverage 和测试报告归档。不要让 LLM 在 CI 阶段自由执行或把 Jenkins 作为 Agent 的 shell。
3. Yipilot 新增只读 `ci import`（或 `run ci-status`）能力：按 **commit SHA + job/build URL** 拉取状态、构建号、开始结束时间、控制台摘要和报告 Artifact；只接受该 Run 已记录的分支头 SHA。Jenkins Remote API 是 REST-like JSON API，可查询、触发构建；有参作业可 `POST /job/JOB_NAME/buildWithParameters`，适合将 `yipilot_run_id`、`commit_sha` 作为追踪参数。[Jenkins Remote Access API](https://www.jenkins.io/doc/book/using/remote-access-api/)
4. 触发方式首选 GitHub webhook/Multibranch 自动发现；只有「人工批准后重跑」才由 Yipilot 调 Jenkins API。若主动触发，参数必须只允许已登记字段，绝不接受任意命令。

### 第二阶段：PR 创建与人工审查

1. 新增 `code apply --push`，但它应要求一个与 Run、目标 SHA、目标分支绑定的审批；执行结果只允许创建 `feature/yipilot-...` 分支，禁止推送 `main`。
2. 新增 `pr create`：上传 Run 的 PRD/SRS/SDD、测试计划和最小的 evidence summary（不要上传密钥或完整控制台日志），并把 PR number、head SHA、URL 回写为 Artifact。
3. 新增只读 `pr watch`：监听/轮询 PR review、required check、merge queue 状态，把事件时间线导入 Run。GitHub 可通过 REST 创建 review；若将来让 Yipilot 发布机器审阅，只应发布 `COMMENT`，不要让机器人作 `APPROVE`，保留人类批准作为合并条件。[GitHub PR Reviews API](https://docs.github.com/en/rest/pulls/reviews)

### 第三阶段：GitHub Actions 的可选替代/补充

对于没有 Jenkins 的仓库，Yipilot 可调用 `POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches`，传入固定 ref 和最多 25 个受 schema 限制的 inputs；该接口需要 `Actions: write`。这不是开放 shell，而是触发版本化的 workflow。[GitHub Actions workflow dispatch API](https://docs.github.com/en/rest/actions/workflows)

## GitHub 合并门禁应如何配置

对 `main`/发布分支启用保护规则（或组织 ruleset）：

- 禁止直接 push、force-push 和删除；限制允许 push 的人/应用。
- 要求 PR、至少一位有写权限的人工 reviewer；涉及关键目录时要求 Code Owner；新 push 后撤销旧批准，或要求由非最后推送者批准。
- 要求唯一命名的 CI checks（例如 `jenkins / verify`、`security / scan`）成功，并选择可信 GitHub App 为状态来源；必要时要求分支已同步基线、所有 review 对话已解决。
- 繁忙主干使用 Merge Queue：它会在最新目标分支及队列组合变更上重跑必需检查，合格才合并。

这些能力是 GitHub 原生的分支保护职责；Yipilot 只报告和链接证据，不能把自身内部 Harness 结果当成绕过这些规则的凭据。[GitHub protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)

## 接口、事件与数据模型

建议新增独立 port，而非把 Jenkins/GitHub SDK 混进领域层：

```text
CiProvider
  trigger(run_id, commit_sha, pipeline_ref) -> CiBuildRef
  get_status(build_ref) -> CiBuildStatus
  collect_artifacts(build_ref) -> [Artifact]

PullRequestProvider
  create(head_sha, base, title, body) -> PullRequestRef
  get_status(pr) -> PullRequestStatus
  list_reviews(pr) -> [Review]
```

关键实体字段：`provider`、`repository_id`、`branch`、不可变 `head_sha`、`jenkins_job_full_name`、`queue_id/build_number`、PR number/URL、检查名、结论、外部 URL、报告 hash、采集时间和原始响应的脱敏摘要。状态机应明确区分 `ci_queued`、`ci_running`、`ci_failed`、`review_pending`、`merge_eligible`；`merge_eligible` 只是提示，绝不是执行 merge 的授权。

入站 GitHub Webhook 可订阅 `pull_request`、`check_run`、`pull_request_review`、`push`；先用原始 UTF-8 body 对 `X-Hub-Signature-256` 做 HMAC-SHA256 常量时间比对，再解析事件。GitHub 明确要求验证签名，且不要将 secret 硬编码或提交到仓库。[GitHub webhook signature validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)；[Webhook events](https://docs.github.com/en/webhooks/webhook-events-and-payloads)

若要把 Yipilot 的质量结果展示为 GitHub Checks，应该使用 GitHub App 安装令牌和最小的 `Checks: write` 权限。Checks API 的写入语义属于 GitHub App（而不是普通 OAuth/PAT）；check run 可带 annotation 和 `details_url` 回到 Yipilot 的证据页。[GitHub Checks API](https://docs.github.com/en/rest/checks/runs)

## 安全与审批清单

- 用 GitHub App installation token 取代长期 PAT；按仓库安装，并给最小权限：读取 contents/metadata，创建 PR 时 `Pull requests: write`，发布检查时 `Checks: write`，触发 Actions 才额外 `Actions: write`。
- Jenkins 使用专用服务账号/API token；Jenkins 官方建议 API token 优先于 crumb；凭据应按 Folder/Item 下沉范围，绝不写入 `Jenkinsfile`、事件或 Artifact。Jenkins 也要求不要在 controller 内置节点上运行构建。[Jenkins credentials](https://www.jenkins.io/doc/book/security/credentials/)；[Jenkins API security](https://www.jenkins.io/doc/book/using/remote-access-api/)
- Jenkins Controller 与构建 agent 分离；非受信任 fork 的 PR 不获得部署、推送或生产凭据。Yipilot 采集的日志要脱敏、限量，并以 SHA 绑定，防止提示注入或日志中泄露令牌。
- 把「创建 PR」「推送分支」「触发付费/部署流水线」「merge」设为不同审批动作；默认仅允许人完成最后 merge。PR 每次 head SHA 改变后，Yipilot 之前的 CI/审查证据均标记过期。

## 可借鉴的 GitHub 官方项目

最接近 Yipilot 思想的是 [github/gh-aw](https://github.com/github/gh-aw)（GitHub Agentic Workflows）。它把自然语言 Agent 工作流编译为 GitHub Actions 的 lock workflow；官方建议把构建、测试、lint、部署保持为确定性 workflow，而把需推理/生成的工作交给默认沙箱、只读权限及受控 safe outputs 的 Agent。这正好支持「Yipilot 做证据与编排，Jenkins/Actions 做确定性门禁，PR 做人工批准」的三层分工。[gh-aw 官方说明](https://github.github.com/gh-aw/about/)

可先借鉴其两个机制，而非直接引入它：

1. **声明/编译产物分离**：把可读的策略配置与被执行的锁定 CI 配置都纳入版本控制。
2. **默认只读、受控写出**：Agent 的写操作只可创建分支/PR/评论，不能直接改默认分支或代替人批准。

## 最小可交付切片

建议先做「Jenkins 结果回流」而不是「自动合并」：为一个 Python 仓库建立 Multibranch Pipeline；让 Yipilot 将 Run ID 和 head SHA 写入 PR body/构建参数；实现 `ci import/status`，把 Jenkins 结果固定为 Artifact；最后在 GitHub 将该 Jenkins check 和 1 个人工批准设为 main 的必需条件。该切片验证了追溯链、权限边界和人工关口，同时不需要让 Yipilot 持有合并权。
