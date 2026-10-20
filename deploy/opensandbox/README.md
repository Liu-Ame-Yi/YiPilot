# OpenSandbox 1.1.1 隔离候选制品

**只允许受信 fixture 验收，尚未放行不可信任务。** 2026-10-20 实测 `/internal/init` 仍可由任务改写 execd binding；`acceptance.py` 因此应失败。不要去掉失败断言，也不要启用到 YiPilot 的正式执行链路。[实测记录](../../docs/research/2026-10-20-opensandbox-profile-validation.md)。

这份目录只负责 Linux Docker profile，没有 Runner、任务调度或客户端 SDK 包装。YiPilot 的 Windows 程序和旧执行链路本轮未变。当前 Python 镜像是隔离测试基底，尚非包含 pytest/Ruff/mypy/Git 的正式工具镜像。

## 制品及边界

| 文件 | 职责 |
| --- | --- |
| `server.patch`、`policy.py` | 创建前限制镜像、请求形状、资源和唯一 Job work 挂载；主容器强制非 root、只读根、caps/nnp/PID/tmpfs/memswap；sidecar 创建时限额；内部网络就绪和代理寻址 |
| `build.py` | 校验四个受影响上游文件的 SHA-256，拒绝其他源码；向新目录复制并应用无 fuzz 补丁，编译和生成摘要收据；不改 site-packages |
| `network.py` | 创建/校验专用 internal bridge，关闭 ICC/IPv6；宿主 INPUT 阻止任务新建连接，DOCKER-USER 阻止到/从该桥的转发；不匹配 SSH 或其他网桥 |
| `config.toml.example`、两个 `.service` | 回环 8081、独立管理密钥、专用 SQLite；网络检查先于服务启动；Docker 重启时重建规则 |
| `acceptance.py` | 自有小任务的真实 Docker/SDK/宿主验收，记录失败项和残留；初始化可变用例保持红色 |

Server 和 SDK 均固定 1.1.1，任务/execd/egress 使用文件内固定摘要。没有预制一份重复 execd：保留上游向每个沙箱私有 runtime volume 注入的单一路径。该卷是 RW，但目录和二进制对任务 UID 不可写，不能称为全挂载只读。

sidecar 固定 64 MiB、等量 memswap、0.1 CPU、64 PID；主容器显式请求 128 MiB–1 GiB、0.1–1 CPU、128 PID，强制等量 memswap。这里的请求上限不代表宿主准入预算或四并发能力；未来 Runner 必须将 sidecar 一起计费，并约束实际同时执行的总量。

## 构建与主机前提

仅适用于已验证的 Ubuntu 22.04/x86_64、Docker 29.1.3、iptables 后端及系统 Python 3.10 的独立 OpenSandbox 环境。`patch`、Docker CLI、iptables、modprobe、sysctl 必须存在；不更换系统 Python。未来 YiPilot Runner 的 Python >=3.11 环境另行部署。

在 Linux 主机上，`build.py SOURCE DESTINATION` 的 SOURCE 是已安装的 `opensandbox_server` 包，DESTINATION 必须不存在。安装依赖继续使用原环境的锁定清单，不能以复制包代替依赖核验。构建收据是 `DESTINATION` 同级的 `profile-build.json`。源码不符、目标已存在、补丁不适配或编译失败均不发布；构建失败留下的目录应保留排查，重新构建使用新的目录。

当前服务器布局为：

```text
/opt/opensandbox/venv/                         原版 1.1.1 依赖，未修改
/opt/opensandbox/yipilot-profile/
  opensandbox_server/                         固定源码补丁包
  deploy/                                    本目录的部署文件
  profile-build.json                          构建摘要
  acceptance-result.json                      最近一次验收结果
/etc/opensandbox/yipilot-profile.toml          root:root 0600 独立密钥
/srv/yipilot-runner/jobs/<32hex>/work          10001:10001 0700
```

所有 Job 祖先目录必须为 root 所有、无 group/other 写权限且不是链接；input/artifacts 留在任务挂载外。本目录不创建或接受任意用户宿主目录。调用方未来由 Runner 提供，必须在真实写入者停止后才能可信扫描。

独立服务通过 `PYTHONPATH` 选择这份正常源码包，没有进程内 monkeypatch。配置从模板生成随机独立密钥，禁止把模板占位符直接投入使用。管理密钥、Docker 权限和 Job 目录选择权只在可信宿主。所有 SDK 访问必须 `use_server_proxy=True`：internal bridge 不提供宿主发布端口，远端客户端不能直接使用内部端点。

## 网络检查及更新

`network.py --install` 只安装专用桥和规则，可重复执行；`network.py` 无参数只验证。安装会加载 br_netfilter 并设置 `net.bridge.bridge-nf-call-iptables=1`。已有专用名称的网络/链若不符合预期，拒绝覆盖。规则必须位于 INPUT/DOCKER-USER 的首条，Docker 的 FORWARD 首条也必须经过 DOCKER-USER。IPv6 未开放，sidecar 还禁用共享 namespace 的 IPv6。

停止服务时保留专用阻断规则，避免残留任务突然获得网络；不运行全局 prune。以后启用正式服务前，必须补齐控制通道隔离，并验证 Docker/主机重启、启动/销毁失败与版本升级。当前这组 systemd 重启关系仅部署配置已检查，尚未做真实重启演练。

更新先排空属于 Runner 的任务、停止服务，在新目录构建并验证完整收据，再切换 unit 指向并启动。不要原地修改运行中的 Python 文件。服务器保留了两份本轮验收包的历史目录，用来复现已修复的宿主映射问题；它们没有 systemd 入口，也不作为备用执行后端。

本轮新服务已停止且未启用开机启动。原回环 8080 服务及配置保留，仅供受信样例。后续修复完成后再按真实验收结果决定启用，不能因 `/health` 返回成功而视为放行。
