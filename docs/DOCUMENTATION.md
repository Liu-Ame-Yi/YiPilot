# 文档维护规则

## 权威边界

- 用户最新明确决定确定产品范围；集中维护 docs/product/vision.md。
- 当前能力由当前源码和验证证据确定，汇总到 docs/product/status.md。发现文档与代码冲突时修正文档，不用文档否认代码或假称代码已实现。
- docs/superpowers/specs/2026-09-07-yipilot-v2-architecture.md 是当前技术提案；技术取舍与用户已确认需求分别标记。
- docs/product/roadmap.md 是唯一当前实施顺序；CONTEXT.md 只维护术语。
- guides 和 README 只给当前可用命令，未来 CLI 示例只放提案且明确标记。
- 历史规格、计划、研究、矩阵及规划书不成为自动续做的待办清单。

## 修改时同步

| 变化 | 同步位置 |
| --- | --- |
| 输入/输出、范围、人工介入、完成定义 | vision、V2、roadmap |
| 实现了或移除了命令 | status、README、对应 guide；检查真实 --help |
| 核心术语 | CONTEXT，及引用该术语的阶段合同 |
| 技术设计变化 | V2；图受影响时更新并重新检查 |
| 新验证证据 | 记录日期、版本与范围，不能用历史测试数量证明新架构 |
| 旧文档失效 | 顶部标记 Historical reference / Superseded 并指向当前入口；有历史价值则保留，不在下方继续混写新路线 |

状态使用：已确认产品范围、Proposed 技术提案、Implemented 当前证据、Historical reference 历史资料。禁止把“有设计”“有 fixture”“类已存在”写成“生产闭环完成”。

文档修改后检查 git diff --check、本地 Markdown 链接，以及当前指南是否包含已移除参数。本次清理不升级产品版本、不修改运行状态、不执行发布操作。
