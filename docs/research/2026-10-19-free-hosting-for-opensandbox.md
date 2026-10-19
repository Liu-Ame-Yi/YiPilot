# OpenSandbox 免费运行资源调研：接受境外、不绑信用卡

> 核对日期：2026-10-19（Asia/Shanghai）。状态：信息调研；未注册、领取资源、启动实例或付费。
> 用途：本地 YiPilot，通过远端 Runner 调用 OpenSandbox + Docker。用户希望可并发设计，但服务器配置尚未确定。
> 用户明确不想绑定信用卡；本稿优先筛无需任何银行卡的途径，普通云允许借记卡的情形单列，不把“拒绝所有支付方式”当成已确认要求。学生资格尚待用户答复。
>
> 后续状态：用户已取得华为云Flexus 2核/2GiB/40GiB/Ubuntu22.04/2Mbps/100GB节点；本文件保留为资源候选调研，不据此继续申请。实际节点安排见[接入方案第10节](2026-10-19-opensandbox-integration-alternatives-review.md#10-已取得的华为云-flexus-节点2026-10-19用户提供)。

## 1. 结论与优先次序

**在本次核实的服务中，没有找到对普通个人普遍开放、无需信用卡/学生资格、长期常开且可承诺本系统4–8活跃沙箱的免费通用服务器。** 有可行的免费开发验证和有条件的 VM 试用；两者不能混成永久免费VPS。

1. **符合学生资格：优先 Azure for Students。** 官方明确免信用卡，有真正 VM 方向；仍需核实实际 SKU/区域配额和总成本。
2. **不依赖学生资格、先做远程 POC：优先 GitHub Codespaces。** 有每月个人免费额度和 Docker-in-Docker 使用路径；受小时、存储和自动停止限制。
3. **需要真实 VM 做短期验证：核对腾讯云/阿里云新用户试用。** 是限期/抵扣活动，无卡领取不能仅凭活动页未提卡就保证。
4. **自动化验证：用 GitHub Actions 跑集成测试。** 它适合 CI，不是常驻 Runner 服务。

这是按本系统运行需求作出的建议，不代表已证明 OpenSandbox 在任何免费环境完整通过；平台风控、用户资格、区域、可选机型和库存未登录验证。

## 2. 可用与有条件候选

| 候选 | 免费资源与期限 | 信用卡/资格 | 对本系统的判断 |
| --- | --- | --- | --- |
| GitHub Codespaces | GitHub Free 个人账户每月120 core-hours、15 GB-month；例如2核约60运行小时，4核约30小时，8核约15小时，均为除法换算且还受存储限制 | 无有效支付方式时，用完额度会阻止继续使用；机器类型以实际可选为准 | 首选交互式远程POC；不能承担24/7服务 |
| Azure for Students | $100 credits，12个月有效，另有选定免费服务；不等于一台大内存VM全年免费 | 明确无需信用卡；18+、合资格院校全日制学生、学校邮箱验证 | 有资格时最值得核实的真实VM候选；预算与小规格限制仍在 |
| 腾讯云试用 | 常见个人2核2G/1个月；另有个企同享2核4G、4核8G卡片及限量条件 | 实名、产品新用户；公开页未提信用卡，实际支付/领取流程未验证 | 短期Linux VM POC候选；可领配置与库存不保证 |
| 阿里云ECS试用 | 个人300元抵扣额，3个月有效，最高4vCPU/8GiB；并非免费常开3个月 | 实名、ECS产品新用户等；未验证领取阶段银行卡/余额/风控要求 | 可做短期VM测试；有每小时抵扣上限和超额收费项 |
| GitHub Actions | 私有仓库GitHub Free每月2,000分钟及500MB artifact额度；公共仓库标准runner免费 | 账号启用/风控资格未实测；不能把公开仓库权益直接套到私有项目 | 适合OpenSandbox部署/故障/并发CI测试；不是常驻服务 |
| Railway匿名VM体验 | 官方宣称2vCPU/2GB、60分钟构建窗口；之后24小时认领，未认领删除 | 明确无需账号或信用卡，以SSH key识别；共享容量限制 | 只列快速临时体验；未确认Docker daemon权限，不作完整OpenSandbox推荐 |

依据：[Codespaces计费](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces)、[学生计划](https://learn.microsoft.com/en-us/azure/education-hub/find-ids)、[腾讯活动](https://cloud.tencent.com/act/pro/free)、[阿里规则](https://help.aliyun.com/zh/ecs/user-guide/ecs-free-trial)、[Actions计费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)、[Railway免费VM](https://railway.com/free-vm)。国内/学生/普通云的详细资格与到期行为见[专项试用核查](2026-10-19-free-cloud-trials.md)。

## 3. Codespaces：当前最适合免卡远程验证的候选

### 3.1 额度不是连续120小时

GitHub个人Free的120小时按核心倍率计费：2核每运行1小时消耗2 core-hours，4核消耗4。上表60/30/15小时是换算，不含其他codespace消耗，也不是24小时常开额度。停止后存储继续计量，镜像、产物和自定义容器可能较快消耗15 GB-month；无支付方式额度耗尽后停止使用而不是自动提供无限计算。[计费规则](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces)、[额度优化说明](https://docs.github.com/en/codespaces/troubleshooting/troubleshooting-included-usage)

不同机型不保证全部可用，实际核心、内存与磁盘在创建页和机器列表核对；不要只按core-hours猜可用RAM。[机器类型说明](https://docs.github.com/en/codespaces/customizing-your-codespace/changing-the-machine-type-for-your-codespace)

### 3.2 为什么能作为候选，为什么不能当常驻VPS

- 官方有Docker-in-Docker运行和pull镜像的文档；这是具备Docker试验路径的正向证据，不是本项目bridge、egress、UID/cgroup和可信停止已经通过。[官方Docker使用](https://docs.github.com/en/codespaces/reference/allowing-your-codespace-to-access-a-private-registry)
- 默认空闲30分钟停止，可设5–240分钟；当前官方文档还规定连续运行最多12小时，主动使用也会自动停，之后需重启。[timeout](https://docs.github.com/en/codespaces/setting-your-user-preferences/setting-your-timeout-period-for-github-codespaces)、[lifecycle](https://docs.github.com/en/codespaces/about-codespaces/understanding-the-codespace-lifecycle)
- 停止会停止进程，保存数据不等于正在执行的Job仍运行；重建会清除/workspaces外修改，停止闲置的codespace默认30天后自动删除。Runner的状态/工件位置、重启恢复和备份要按此处理。[生命周期](https://docs.github.com/en/codespaces/about-codespaces/understanding-the-codespace-lifecycle)
- 本地访问可使用私有端口转发；不要为了省认证把管理入口公开。远端私有URL的REST访问还有GitHub认证层，session重启token会变化；可先用官方客户端本地转发简化POC。[端口访问](https://docs.github.com/en/codespaces/developing-in-a-codespace/forwarding-ports-in-your-codespace)

建议POC：同一Codespace内部运行Runner、OpenSandbox和Docker；只给本地转发Runner入口。先实测实际Docker拓扑与主机目录视图一致，再上传无敏感fixture。不能因服务器方案原本用了/srv目录，就把需要跨重建保留的账本放到Codespaces不保留的位置。默认镜像、DinD与bind路径需实测，不能直接宣称与裸Linux VM完全等价。

## 4. Azure for Students与国内短期VM

Azure学生计划无需卡、$100/一年是官方事实；资格需学校邮箱及认可全日制高等教育身份。额度耗尽/到期不续期或升级会停止，仍合资格可在周年重新验证。[正式offer](https://azure.microsoft.com/en-us/pricing/offers/ms-azr-0170p)、[教育FAQ](https://learn.microsoft.com/en-us/azure/education-hub/faq)

免费VM月额度中的微型SKU只有约1GiB内存级别，不能把它当成8个沙箱的主机；更大VM要计算credits消耗，磁盘/IP/流量也可能消耗预算。准确SKU、用途限制和资格详见[学生与国内试用核查](2026-10-19-free-cloud-trials.md)。

腾讯/阿里的公开活动提供真实VM方向，适合首次完整验证。但“实名即可、完全不需支付验证”尚无本次端到端证据。腾讯所选卡片的库存/续费停机条款、阿里300元抵扣的每小时限额和不抵扣项目，必须在领取前确认；不把优惠券有效期当免费运行时长。[腾讯活动](https://cloud.tencent.com/act/pro/free)、[阿里ECS规则](https://help.aliyun.com/zh/ecs/user-guide/ecs-free-trial)

## 5. Actions：可承担CI，不提供常驻服务器

普通公共仓库Linux标准runner当前为4vCPU/16GB RAM/14GB SSD；私有仓库Linux标准runner为2vCPU/8GB RAM/14GB SSD。不是所有标签都等价，单核ubuntu-slim是不同执行环境。[runner规格](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)

GitHub Free标准runner账户并发上限当前20个Job，每个hosted Job最多6小时；这些是平台上限，不是保留的实时容量，也不是允许将CI转作永久VPS。[Actions limits](https://docs.github.com/en/actions/reference/limits)

可在一个CI Job中启动OpenSandbox与fixture沙箱，回收后保存短期测试证据；也可用matrix测试多个配置。matrix的8个Job各跑不同VM，不能把它当成“单服务器8沙箱并发”的吞吐证明。私有项目不应为了免费额度公开源码；artifact额度和保留期也需管理。[计费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

本轮没有触发任何workflow，也没有修改仓库CI或把私有文件上传到GitHub。

## 6. 常见免费名单里不适合本轮的项目

| 名称 | 实际限制/资格 | 结论 |
| --- | --- | --- |
| Oracle Always Free | 普通新用户通常需手机号/信用卡验证；当前纯Always Free A1总额度2 OCPU/12GB、可遇capacity不足或闲置回收 | 不作为普通无卡方案；旧4核24GB说法不能直接沿用 |
| Google Cloud | 免费试用需有效信用卡或其他支付方式；Free Tier也需billing account。e2-micro每月限定美国区域和免费资源限额 | 不作为已核实免支付验证途径 |
| AWS标准Free Plan | 标准入口要求payment method；有限发布新入口仅写might request，免卡资格未确认 | 不能向用户保证无卡领取 |
| AWS Educate | 无卡，但权益是限时教学labs，不是长期个人VM | 适合学云，不替代Runner主机 |
| Azure普通Free Account | 信用卡或合规借记卡等验证，地区有差异 | 用户若接受借记卡，可另核对；本轮不依赖它 |
| Hugging Face Spaces | 当前新建Docker/Gradio compute Space需要PRO/组织付费计划；CPU Basic小时价FREE不等于无付费资格门槛 | 旧“2核16GB免费Docker服务器”教程不能直接采信 |
| Render Free | 15分钟无入站流量休眠；SQLite/上传文件等本地变更重启/休眠会丢，免费不可挂持久盘 | 不适合同机耐久Runner；Dockerfile部署不证明拥有Docker daemon管理权 |
| Railway常规trial | $5/30天，之后Free $1/月；trial服务内存/网络有额外限制，另有匿名60分钟VM体验 | 不把试用金/VM认领窗口解释为长期免费VPS |

Oracle依据：[免费资源现行文档](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)、[注册前提](https://docs.oracle.com/en/learn/cloud_free_tier/index.html)。Oracle文档在本轮显示2 OCPU/12GB，未对邀请/教育免卡特殊渠道、付费账户额外额度或ARM制品进行资格核实。

其他依据：[Google规则](https://docs.cloud.google.com/free/docs/free-cloud-features)、[Hugging Face当前概览](https://huggingface.co/docs/hub/spaces-overview)、[Render免费限制](https://render.com/docs/free)、[Railway试用](https://docs.railway.com/pricing/free-trial)。AWS/Azure的例外和原始条款见[专项核查](2026-10-19-free-cloud-trials.md)。

未找到平台明确授权管理Docker daemon/内核能力时，不能仅看到“支持Dockerfile”就认定可运行OpenSandbox的Docker后端；本轮不建议为迁就免费PaaS另写一套运行时。

## 7. 对本项目的下一步建议

1. 先按学生资格/产品新用户资格筛选，未确认资格不注册更多试用平台。
2. 纯免卡、非学生场景优先在Codespaces做有限时长的远程POC；仅使用无敏感fixture，验证create/run/stop/可信导出/下载/GC和中断恢复。
3. 同时可用Actions做离线和真实Docker集成CI；不让两者变成两套业务后端。
4. 有学生资格或国内试用可领时，再在普通Linux VM验证单机Runner架构和实际并发。4/8仍作为能力设计与压测档位，启用额度按资源预算；免费微型机不承诺8个CPU密集任务同时高效运行。
5. 如果最终坚持“零费用、无信用卡、长期常开”，优先考虑自己已有的闲置Linux主机/本地VM做运行节点；是否有可用设备未知，不假设已有资源，也不为此购买硬件。

本轮未验证账号能领到哪种机器、实际网络可达性、Docker/egress兼容、学生资格或并发性能；未发生注册、资源创建、上传项目和费用。免费额度、活动库存、自动停止和数据保留可能变化，领取时以具体账号的正式条款与确认页为准。
