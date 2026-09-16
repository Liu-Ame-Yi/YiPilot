# 2026-09-16 模型响应与阶段合同实施记录

## 任务计划与实际范围

1. 核对 Git、公共入口和共享模型调用链，保留已有 SQLite 等未提交修改，建立 `codex/model-stream-contracts`。
2. 分离 token、正文、思考、事件及传输预算；检查正常结束，记录安全回执，保留旧 ModelPort 调用接口。
3. 为 SRS/SDD/Review/状态标记版本，增加每阶段一次的持久化格式修复，不放宽证据与评审门禁。
4. 运行相关测试、仓库检查和真实 `deepseek-flash` 样例；更新状态和入口文档，按职责提交。

步骤 1–4 已完成，验证结果见下文，提交按模型传输与阶段协议两项职责拆分。未新增数据库、模型客户端、工作流框架、自动编码或远端动作。

## 真实模型验证

直接运行 `tests/live/test_specification_live.py`，使用默认 `deepseek-1/deepseek-flash`、60 秒每次超时、4096 字节正文预算、新独立的 8192 output tokens；没有设置 reasoning_effort、没有修改全局配置或替换响应。临时 Git 仓库为 value.py/test_value.py，需求是设计从返回 1 改为整数 2，保留接口并更新测试。

结果 `1 passed in 58.59s`。四次真实调用顺次通过，修订次数均为 0、状态 completed；重建 ProjectApplication 后 resume 返回相同结果，Run 数不变，样例源代码未改变。SQLite integrity_check=ok、foreign_key_check=[]。

| 阶段 | 正文字节 | 思考字节 | 传输字节 | completion tokens | 结束原因 |
| --- | ---: | ---: | ---: | ---: | --- |
| SRS 生成 | 1182 | 12345 | 919132 | 3046 | stop |
| 需求评审 | 449 | 5633 | 416345 | 1379 | stop |
| SDD 生成 | 1107 | 6488 | 561987 | 1864 | stop |
| 设计评审 | 436 | 21123 | 1577385 | 5235 | stop |

四次传输均超过旧 256 KiB 上限，正文均低于原 4096 字节预算，直接证实应分离预算，不能用正文大小代替 SSE 总流量。思考正文未保存；表中是实际接收计数与供应商返回用量。

- flow_id：`623c4f28-de95-4c67-a1d7-8234de3f6ac3`
- SRS SHA-256：`df32ae1e33babf7dbc4e5c1a9fab4dc4958fe3a8390244ffb4d1f65cc1108def`
- 需求评审 SHA-256：`0fa801571f01d29dcccffd8958451da5dcff1ef285d163089b773b86d5000d0c`
- SDD SHA-256：`9a5da89c8a58c26dc38397ac6cb5b84a062ed9f74127fe79c68252478bba7143`
- 设计评审 SHA-256：`8f6fd6956ce19ad2a6aba1da776cdb18db40735abf123c0430716dffbf7687ae`

本机证据保留于 Git 忽略的 `.pytest_cache/stream-contracts-live-20260916-default/test_real_specification0/`，含独立 SQLite、来源仓库、输入、提示、响应回执、文档与评审。该次运行在补充 `received` 回执字段之前完成，因此该字段不在本次不可变旧回执中；已有 `complete=true` 和 `finish_reason=stop`。后续代码变更的验证另行记录，不改写旧证据。

复现时选择新的、尚不存在的 basetemp 路径，避免 pytest 清理旧证据：

```powershell
$env:YIPILOT_RUN_LIVE='1'
$env:YIPILOT_LIVE_PROFILE='deepseek-1'
$env:YIPILOT_LIVE_SPECIFICATION='1'
# 不设置 YIPILOT_SPEC_REASONING_EFFORT；使用全局已选模型。
.venv/Scripts/python.exe -m pytest tests/live/test_specification_live.py -o addopts='' -q -s -p no:cacheprovider --basetemp=<新的临时目录>
```

## 补充回执后的真实复核

第二次运行同一真实测试，仍为全局默认 `deepseek-flash`，结果 `1 passed in 80.14s`。这次独立设计评审指出初版 SDD 的执行测试步骤与本次只生成文档、不执行样例测试的约束冲突，返回 revise。程序自动重新生成 SDD，再独立评审得到 accept。最终 `revisions={srs:0,sdd:1}`、`format_repairs={}`、completed，共 6 次真实调用；已接受的 SRS 没有重新生成。重启恢复不增加 Run，样例源码不变，SQLite 两项一致性检查通过。

这验证了实际的评审拒绝→修订→再评审→通过；不宣称评审不存在误判。所有调用回执为 `received=true`、`complete=true`、`finish_reason=stop`。

| 阶段 | 正文字节 | 思考字节 | 传输字节 | completion tokens |
| --- | ---: | ---: | ---: | ---: |
| SRS 生成 | 1589 | 7931 | 642125 | 2127 |
| 需求评审 | 399 | 13806 | 962505 | 3190 |
| SDD 初版 | 1331 | 4302 | 407937 | 1352 |
| 设计评审（revise） | 735 | 15322 | 1182486 | 3924 |
| SDD 修订 | 1506 | 6354 | 577978 | 1917 |
| 设计评审（accept） | 451 | 12174 | 944117 | 3133 |

- flow_id：`cea8ab1a-26a2-442e-b42c-34cc57b2db38`
- accepted SRS：`d0dd84725f320b9e2dac87b09fb4450f390884df6134ef47758a5623cc7f4f6d`
- 需求评审：`43810167d6a8753020b577884582521cd22de337e9411068cfd0075cce97ebfa`
- 被拒绝 SDD：`e1ad20b05e7d49de720f2cad4046faeebfaddd4051332f6ef22e7a92b2a91262`
- accepted SDD：`3e6921d91862c4cc722721115584c984333bad2ac20802a81d7a3b802630d359`
- 设计评审：`44289f1764dd107e5c862d22484ef59093f4a84757f5f8db71c626271571f74e`

完整现场位于 `.pytest_cache/stream-contracts-live-20260916-final/test_real_specification0/`。真实测试后补充了版本字段必须是整数、本地事务失败回执的区分，以及流结束时再次检查超时/取消；这些收尾边界由下述离线复测验证，不把它记成第三次真实调用。

## 限制

真实验证覆盖两文件需求闭环、一次设计返工和超过旧上限的实际传输，不等于任意长文档都能生成。截断、坏 JSON、格式修复、断流、澄清与失败恢复由离线测试注入；没有冒充这些失败分支的真实平台验收。输入 token 为保守估计，未接入精确 tokenizer、自动压缩、全仓库检索或分章节文档生成。正文和网络保护上限仍然有效。

已完成的旧工件可以读取并按原哈希复用；未完成旧流程的冻结模型快照缺少新增预算时会停止并要求新请求。原 code generate/apply/verify 的业务入口和授权边界保留；共享模型客户端对不完整响应的拒绝是有意加强。

## 离线与仓库检查

最终相关测试：`111 passed in 362.34s`，涵盖 models、架构检查、全部需求设计场景、原 PatchProposal 与模型事件路径。随后补充本地事务失败回执的 `received_valid` 区分，受影响的格式修复、事务回滚、真实 adapter 拒绝截断、版本约束再次复测：`12 passed, 31 deselected in 67.46s`。最终共享 adapter 单测（含 UTF-8/秘密跨分片、长思考、各种错误结束及延迟 EOF）`39 passed in 0.65s`。Ruff、mypy（72 个源文件）、specification help 与 diff 格式检查通过。

另以 e2e0f63 的旧 adapter 和新 adapter 在同一进程、同一 MockTransport 输入上作回归对照（非真实模型）：超过旧传输上限的思考流从 MODEL_RESPONSE_LIMIT_EXCEEDED 变为正确接收短正文；带 length 结束原因的合法 JSON 从被接受变为 MODEL_OUTPUT_TRUNCATED。证明同时修复了误拒绝与错误放行。

全量离线命令 `.venv/Scripts/python.exe -m pytest -o addopts='' -q -p no:cacheprovider`：`690 passed, 42 skipped in 1367.37s`。日志保留在 `.pytest_cache/stream-contracts-full-20260916.log`。全量收集启动后新增的测试和收尾修改由上述最终相关/针对性复测覆盖；结果不相加冒充互不重复的测试数量。42 项跳过没有被计为通过，真实模型两次验收单独执行并在上文记录。
