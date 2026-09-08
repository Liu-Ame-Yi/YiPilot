# Python 应用入口

现有 CLI 参数和 `/run`、`/apply`、`/verify` 确认次数不变。需要嵌入其他界面或脚本时，从同一业务入口调用：

```python
from pathlib import Path
from yipilot.application.project_application import ApprovalPrompt, ProjectApplication

application = ProjectApplication(
    Path(r"C:\work\yipilot-project"),
    settings_dir=Path(r"C:\work\yipilot-settings"),
)
result = application.generate(
    "1001", "sample", "add validation for empty input",
    model_selector="local/coder",
    context_files=("src/service.py",),
    request_id="reviewable-request-1",
)
proposals = application.proposals("1001")
```

调用前仍需初始化任务、注册仓库、配置模型；应用还需要登记本地固定 Docker 镜像。`generate` 仅生成提案，不允许执行或写入源码。调用方从候选列表向用户展示提案，再提交其选择的 `proposal_run_id` 和审阅过的 `proposal_hash`。

```python
def confirm(prompt: ApprovalPrompt) -> bool:
    # 将完整 prompt 展示给你的用户，返回其明确的同意/拒绝。
    # 此例默认拒绝；实际界面在这里提供人工决策，不构造批准记录。
    return False

selected = proposals[0]  # 示例；实际使用用户选择并审阅的候选
apply_run_id = application.apply(
    "1001", str(selected["proposal_run_id"]),
    proposal_hash=str(selected["proposal_hash"]), confirm=confirm,
)
if apply_run_id is not None:
    outcome = application.verify(
        "1001", apply_run_id, confirm=confirm, model_selector="local/coder",
    )
```

`confirm` 必须返回真正的 `bool`，不能返回字符串或批准 ID。应用入口生成只读 `ApprovalPrompt`，包含绑定的任务/Run、动作、策略与指纹，以及 Docker 请求或可信 diff。业务层保存批准/拒绝记录，执行层重新校验证据。不要将 `lambda _: True` 当成人工批准替代品；离线测试中的这种回调只代表测试输入。

`apply` 在两个独立批准点暂停调用：执行离线 Docker 副本，以及写入已审阅输出。拒绝返回 `None`，失败抛出 `DomainError`（底层文件错误也可能传播）；不会降级至宿主机执行。回调结束后仍会检查状态与证据，过期提案哈希不能放行。

`applied_runs(task_id)` 列出成功的 sandbox apply Run；`verify` 会再次验证所选 Run 的实际输出和 worktree。`VerificationOutcome` 分别返回 `handoff`、原始 `error`、`repair`、`repair_error` 或 `cancelled`。验证失败仍是失败；自动生成修复提案不等于完成修复，需再次审阅、应用和验证。修复沿用现有次数、时间和上下文预算。

该入口是同步 Python 调用，不读取 stdin，也不是后台 worker 或可跨进程恢复的审批协议。键盘中断仍向调用方传播；已有 Run 尽可能收敛为取消。文件持久化尚不提供事务化结束与事件提交，完整 durable respond/resume 仍属 V2 后续工作。
