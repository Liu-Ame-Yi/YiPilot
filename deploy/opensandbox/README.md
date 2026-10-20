# OpenSandbox 1.1.1 固定隔离 profile v2

**只允许受信 fixture 验收，尚未放行正式不可信任务。** 2026-10-20 的 v2 已通过 18 项服务器验收、控制进程死亡与隔离安装失败测试、Docker 重启恢复。此前 v1 的 init 重绑漏洞已修复；[最新记录](../../docs/research/2026-10-20-opensandbox-control-isolation.md)说明验证范围及剩余门槛。

这份目录只负责 Linux Docker profile，没有 Runner、任务调度或客户端 SDK 包装。YiPilot 的 Windows 程序和旧执行链路本轮未变。当前 Python 镜像是隔离测试基底，尚非包含 pytest/Ruff/mypy/Git 的正式工具镜像。

## 制品及边界

| 文件 | 职责 |
| --- | --- |
| `server.patch`、`policy.py` | 创建前固定镜像、请求、资源与唯一 work 挂载；RO rootfs/nnp/PID/tmpfs/memswap；限制 sidecar；修正内部就绪、代理寻址及代理权限 |
| `control.py` | root 控制进程与 UID/GID 10001 任务分权；在主容器启动前安装 UID 网络规则；固定命令和文件身份，关闭额外控制接口 |
| `build.py` | 校验五个受影响上游文件的 SHA-256；向新目录复制并应用无 fuzz 补丁，编译并记录五个文件及两个本地模块的摘要；不改 site-packages |
| `network.py` | 创建/校验专用 internal bridge，关闭 ICC/IPv6；宿主 INPUT 阻止任务新建连接，DOCKER-USER 阻止到/从该桥的转发；不匹配 SSH 或其他网桥 |
| `config.toml.example`、两个 `.service` | 回环 8081、独立管理密钥、专用 SQLite；网络检查先于服务启动；Docker 重启时重建规则 |
| `acceptance.py`、`faults.py` | 自有任务的真实 Docker/SDK/宿主验收；覆盖 init、身份、文件、网络、控制进程、停止写入者，以及安装失败清理和控制端口接管 |

Server 和 SDK 均固定 1.1.1，任务/execd/egress 使用文件内固定摘要。没有预制一份重复 execd：保留上游向每个沙箱私有 runtime volume 注入的单一路径。该卷是 RW，但目录和二进制对任务 UID 不可写，不能称为全挂载只读。

容器控制进程使用 root，仅增加 SETUID、SETGID、NET_BIND_SERVICE；命令和文件 worker 强制 UID/GID 10001，实测任务 capabilities 为零。不能把 Docker inspect 的 User=0 隐藏为“整容器非 root”。官方 execd 经小型启动脚本监听容器内 HTTP 443；这不是公网 HTTPS。namespace 的低端口阈值必须为 1024，宿主 nft 按 UID 拒绝任务访问控制端口；安装失败则不启动主容器。控制 HOME、runtime env 文件为 root 私有。任务普通回环连接仍可用。

宿主 Server proxy 只开放健康、argv 命令及降权文件操作；强制命令 UID/GID、超时和任务环境，文件路由转入上游低 UID worker。init、code、PTY/session、WebSocket、任意端口与 root 文件路由均拒绝。root execd 原始 API 只供可信宿主，不提供给任务。服务管理密钥仍是独立的第一道宿主鉴权。

sidecar 固定 64 MiB、等量 memswap、0.1 CPU、64 PID；主容器显式请求 128 MiB–1 GiB、0.1–1 CPU、128 PID，强制等量 memswap。这里的请求上限不代表宿主准入预算或四并发能力；未来 Runner 必须将 sidecar 一起计费，并约束实际同时执行的总量。

## 构建与主机前提

仅适用于已验证的 Ubuntu 22.04/x86_64、Docker 29.1.3、iptables-nft 后端及系统 Python 3.10 的独立 OpenSandbox 环境。`patch`、Docker CLI、iptables、modprobe、sysctl、nsenter 和 nft 必须存在；不更换系统 Python。未来 YiPilot Runner 的 Python >=3.11 环境另行部署。

在 Linux 主机上，`build.py SOURCE DESTINATION` 的 SOURCE 是已安装的 `opensandbox_server` 包，DESTINATION 必须不存在。安装依赖继续使用原环境的锁定清单，不能以复制包代替依赖核验。构建收据是 `DESTINATION` 同级的 `profile-build.json`。源码不符、目标已存在、补丁不适配或编译失败均不发布；构建失败留下的目录应保留排查，重新构建使用新的目录。

当前服务器布局为：

```text
/opt/opensandbox/venv/                         原版 1.1.1 依赖，未修改
/opt/opensandbox/yipilot-profile/
  opensandbox_server/                         固定源码补丁包
  deploy/                                    本目录的部署文件
  profile-build.json                          构建摘要
  acceptance-result.json                      最近一次验收结果
  control-faults-result.json                  故障验收结果
  restart-result.json                         已排空后的 Docker 重启结果
/etc/opensandbox/yipilot-profile.toml          root:root 0600 独立密钥
/srv/yipilot-runner/jobs/<32hex>/work          10001:10001 0700
```

所有 Job 祖先目录必须为 root 所有、无 group/other 写权限且不是链接；input/artifacts 留在任务挂载外。本目录不创建或接受任意用户宿主目录。调用方未来由 Runner 提供，必须在真实写入者停止后才能可信扫描。

独立服务通过 `PYTHONPATH` 选择这份正常源码包，没有进程内 monkeypatch。配置从模板生成随机独立密钥，禁止把模板占位符直接投入使用。管理密钥、Docker 权限和 Job 目录选择权只在可信宿主。所有 SDK 访问必须 `use_server_proxy=True`：internal bridge 不提供宿主发布端口，远端客户端不能直接使用内部端点。

## 网络检查及更新

`network.py --install` 只安装专用桥和规则，可重复执行；`network.py` 无参数只验证。安装会加载 br_netfilter 并设置 `net.bridge.bridge-nf-call-iptables=1`。已有专用名称的网络/链若不符合预期，拒绝覆盖。规则必须位于 INPUT/DOCKER-USER 的首条，Docker 的 FORWARD 首条也必须经过 DOCKER-USER。IPv6 未开放，sidecar 还禁用共享 namespace 的 IPv6。

停止服务时保留专用阻断规则，避免残留任务突然获得网络；不运行全局 prune。专用网络使用稳定的 owner 标签，profile 版本只标记任务。此次从 v1 网络迁移时，先停止服务并确认无容器、卷和网络成员，再删除唯一匹配的自有空网络并重建；不会覆盖不匹配的同名资源。

已在任务排空后真实重启 Docker，确认网络规则重建、候选进程更换及健康恢复。尚未做宿主重启、运行中任务跨重启恢复或上游升级验收；这些不能由本次排空重启替代。

更新先排空自有任务、停止服务，在新目录构建并验证完整收据，再切换并启动。不要原地修改运行中的 Python 文件。服务器保留了失败 v1 和早期构建包，用于复现差异；它们没有 systemd 入口，也不作为备用执行后端。不同 profile 必须使用新执行合同，旧审批不能继承授权。

本轮新服务验收后已停止且未启用开机启动。原回环 8080 服务及配置保留，仅供受信样例。当前还没有 Runner 的耐久接受、全局资源/日志/磁盘准入、可信产物发布和业务切换；验收收据因此保持 `ready_for_untrusted_code=false`。正式启用还需后续完整闭环验收。
