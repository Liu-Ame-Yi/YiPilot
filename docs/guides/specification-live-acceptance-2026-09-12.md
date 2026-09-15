# 2026-09-12 需求与设计真实模型验收

实际运行 `tests/live/test_specification_live.py`，使用公共 ProjectApplication、现有 ModelSelectionService/OpenAICompatibleModelPort、SQLite 与工件存储。没有替换模型响应、评审、源码快照或存储。

独立临时 Git 仓库只有 `value.py` 与 `test_value.py`：原函数返回 1，需求为设计改成整数 2、保留接口并更新测试。程序只生成文档和独立评审，原样例代码没有改变，也没有运行样例测试、Docker、自动编码或远端操作。

## 实际通过的配置和证据

- 模型：`deepseek-1/deepseek-v4-flash`，临时 variant `spec-acceptance`，`reasoning_effort=none`，4096 字节输出预算、60 秒超时。复用已有配置/variant 机制，临时目录只复制公开模型设置，凭据仍从原用户存储读取；没有修改全局配置。
- 测试结果：`1 passed in 18.02s`。SRS 生成 → 需求评审 → SDD 生成 → 设计评审，各一次独立真实调用，无修订。最终状态 `completed`。
- 工作流 ID：`7c7780f1-6e5b-4039-9713-5e71d5029d39`。
- SRS SHA-256：`97b4d9f532b6ae5988213bb782f9769e7f217e6ef3dc736098a2dc269185e312`；需求评审 SHA-256：`47fbbff43b59798208c6caa5c7324de338b95936714911c062c5aff3a785b511`。
- SDD SHA-256：`b1410ea46666ca074ff081620a153239fb99434f86c4b4355fde54cc79146581`；设计评审 SHA-256：`40265006aba83b2b6072fe8c250ad41b89499cdefca20e61e0308780a884c74c`。
- 新建应用实例后 resume 返回同一状态，Run 数不变。SQLite integrity_check=ok，foreign_key_check=[]。输入和四个模型阶段各有一个 Run，另有已有 task_init Run。

SRS 实际包含 AC-1 返回值、AC-2 更新并通过现有测试、AC-3 保留接口；SDD 绑定上述 SRS 哈希并映射全部三个验收项。代码事实引用两份已读取代码，pytest 的推断明确放在 assumptions。两份 Review 都绑定对应文档 SHA-256、返回 accept 和空问题列表。

完整本机现场已复制保留在 Git 忽略的 `.pytest_cache/specification-live-20260912/attempt-931/`，包含公开配置、样例、独立 state.db、输入、prompt、响应、文档、评审和 specification-result.json。不是可移植发布包；仓库元数据中的临时路径仍指向原运行现场。

## 未通过的尝试与边界

1. 全局默认配置两次触发现有适配器 MODEL_RESPONSE_LIMIT_EXCEEDED，均在 SRS 生成停止；第二次加入精简输出提示仍失败。没有放宽适配器的响应上限或伪装成功。
2. 临时 reasoning_effort=low 仍触发同一响应上限，停止并保存不确定调用回执。
3. 临时 reasoning_effort=none 的首次响应把代码证据混进了 SRS 验收项的需求来源；程序以 SPECIFICATION_OUTPUT_INVALID 拒绝，未进入设计。之后补充了提示中的允许需求来源列表，保持原校验规则，再以新请求在新样例中运行，通过上述四阶段。

已保留同目录中的 attempt-929（low 失败）、attempt-930（none 非法输出）和 attempt-931（通过）。两次默认配置尝试的原临时目录已被 pytest 轮换清理，失败结果留在本次执行日志中，不能声称其完整现场仍在。默认全局模型配置的完整闭环**未通过**；成功只证明上述临时 variant 和两文件样例的路径，不代表任意仓库、长文档或模型配置可用。

评审返工、次数耗尽、澄清、需求变更、提交边界重启、调用中断、非法输出和事务回滚由离线 ScriptedModel 测试覆盖，不冒充这些分支的真实模型验收。

复现（联网、使用用户现有配置，需要明确 opt-in）：

```powershell
$env:YIPILOT_RUN_LIVE='1'
$env:YIPILOT_LIVE_PROFILE='deepseek-1'
$env:YIPILOT_LIVE_SPECIFICATION='1'
$env:YIPILOT_SPEC_REASONING_EFFORT='none'
.venv/Scripts/python.exe -m pytest tests/live/test_specification_live.py -o addopts='' -q -s -p no:cacheprovider
```

YIPILOT_LIVE_PROFILE 是仓库现有 live 测试收集门禁；本测试实际按 ModelSelectionService 的默认 selector 选择模型。未设置 YIPILOT_SPEC_REASONING_EFFORT 时按全局默认配置原样运行，不会自动关闭推理。
