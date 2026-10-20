# 华为云 OpenSandbox 基础安装记录

日期：2026-10-19。服务器通过本机 SSH 别名 `huawei-opensandbox` 管理，主机名 `hcss-ecs-ccf9`。本次完成基础服务安装与单沙箱验证；未修改 YiPilot 应用代码，也未安装拟议的 ExecutionRunner。

2026-10-20 后续检查与独立进程隔离探针见[第一阶段验证](2026-10-20-opensandbox-phase1-validation.md)。临时补丁没有安装到本页所述常驻服务，不能把探针配置当作已生效的默认配置。

## 1. 已完成的步骤

| 顺序 | 实际操作 | 验证结果 |
| --- | --- | --- |
| 1 | 生成专用 SSH 密钥，用户通过华为云终端安装公钥；核对用户回传的 ED25519 主机指纹 | 严格主机校验及非交互密钥登录成功 |
| 2 | 使用现有华为云 Ubuntu 签名软件源安装 `docker.io`、`python3-venv` | Docker 29.1.3，cgroup v2/systemd；AppArmor、默认 seccomp 可用 |
| 3 | 在 `/opt/opensandbox/venv` 安装 `opensandbox-server==1.1.1` 与 `opensandbox==1.1.1` | 主包 SHA-256 与官方 PyPI 一致，`pip check` 通过 |
| 4 | 拉取官方 execd 与轻量 Python 镜像，并固定摘要 | 最终 execd 与官方 1.1.1 BOM 一致；普通 Docker 断网 Python 容器验证通过 |
| 5 | 生成服务器端 API 密钥，创建 `opensandbox.service` | 服务 active，健康检查 200；管理接口无密钥 401、有密钥 200 |
| 6 | 通过 SDK 创建一个 0.5 CPU、512 MiB 的 Python 沙箱 | argv 命令、文件往返、退出码、实际资源与权限检查通过 |
| 7 | SDK 销毁沙箱并核查 Docker | 测试标签对应容器和全部容器数均为 0，无需紧急手工清理 |
| 8 | 本机临时建立 SSH 转发访问远端服务 | 健康接口正常，管理接口无密钥仍返回 401；验证后临时隧道已关闭 |

Docker 官方下载源及 Docker Hub 在预检时连接失败，因而选择现有 Ubuntu 维护包和可达的公共镜像仓库。官方 PyPI 下载缓慢，停止本次下载进程后改用华为云 PyPI 镜像；没有关闭 TLS 校验或修改全局 pip 配置。只安装必要包及其依赖；APT 同时更新了相关 Python 3.10 补丁包，没有执行全系统升级、重启、autoremove 或全局 Docker prune。[华为云 PyPI 镜像说明](https://www.huaweicloud.com/special/pypi-jingxiang.html)

## 2. 最终版本与镜像

- Docker：Ubuntu `docker.io` 29.1.3；containerd 2.2.1；runc 1.3.4。
- OpenSandbox Server / Python SDK：均为 1.1.1。服务运行在独立 Python 3.10 环境；未来 YiPilot Runner 的 >=3.11 要求尚未处理。
- execd：`sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/execd@sha256:cda2bc35409bbf57561599b316cafb899598f7dce671332e7ceda14eb1cfeded`。
- Python：`public.ecr.aws/docker/library/python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258`，来源 tag 为 `3.12-slim-bookworm`。

execd 摘要已按[官方 1.1.1 BOM](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/docs/releases/1.1.1.yaml)核对。记录摘要不等于已完成 Cosign 身份/供应链签名验收；本次未运行该项验收。

实际碰到的两项兼容差异：

1. Docker 后端拒绝 `secure_access=True`；该选项用于其他运行时的安全接入能力。本次使用 Docker 支持的请求，管理 API 由密钥认证，发布端口限定回环。
2. 固定源码中的示例配置仍引用 `execd:v1.1.0`。用它执行 1.1.1 SDK 的 argv 命令，实测返回 400 `RunCommandRequest.Command required`；换成 BOM 的 `release-1.1.1` 摘要后，相同 argv 验证通过。后续应按发布清单匹配组件，不能只固定 PyPI 包或照抄示例标签。

## 3. 配置与文件位置

以下路径均在服务器上；配置文件包含密钥，不应粘贴到聊天、仓库或普通日志中。

| 位置 | 用途 |
| --- | --- |
| `/opt/opensandbox/venv` | 独立 Server/SDK 环境 |
| `/etc/opensandbox/config.toml` | 配置与 API 密钥，root 所有，目录 0700、文件 0600 |
| `/etc/systemd/system/opensandbox.service` | systemd 单进程服务，开机启动、失败重启 |
| `/var/lib/opensandbox/opensandbox.db` | 已配置的服务存储路径；不代替 YiPilot Job 数据库 |
| `/srv/yipilot-runner/jobs` | 预留的唯一业务 host-bind 允许根；尚无 Runner 或实际 Job |
| `/opt/opensandbox/images.lock.json` | 镜像摘要及旧探针镜像记录 |
| `/opt/opensandbox/verified-packages.json` | 两个主包的官方 PyPI SHA-256 校验记录 |
| `/opt/opensandbox/requirements.lock.txt` | 本次已安装依赖快照；主包文件引用对应保留的 downloads 目录 |
| `/opt/opensandbox/smoke.py` | 本次受信单沙箱验证脚本 |
| `/opt/opensandbox/smoke-result.json` | 最近一次沙箱验证记录 |
| `/opt/opensandbox/installation-result.json` | 安装完成后的服务/资源采样 |

已生效配置：Server `127.0.0.1:8080`；Docker bridge；`publish_host=127.0.0.1`；`cap_drop=[ALL]`；`no_new_privileges=true`；PID 上限 128；API 密钥启用；host-bind 仅允许 `/srv/yipilot-runner/jobs`。单沙箱创建时显式设置 512 MiB / 500m，已从 Docker inspect 和沙箱内 cgroup 文件交叉验证。

`server.limit_concurrency=64` 是 HTTP 连接/请求的限制，**不是 64 个沙箱槽位**。当前尚无 Runner 的全局沙箱额度、内存预算准入或持久队列；SDK 创建请求也不能省略资源预算，默认内存请求会超过这台机器的合理分配范围。

## 4. 日常操作

在本机 PowerShell 连接：

```powershell
ssh huawei-opensandbox
```

登录服务器后查看状态、日志：

```bash
systemctl status opensandbox --no-pager
journalctl -u opensandbox -n 50 --no-pager
curl --fail http://127.0.0.1:8080/health
```

手动复验单沙箱时，一次运行一份，预留 512 MiB 预算：

```bash
/opt/opensandbox/venv/bin/python /opt/opensandbox/smoke.py
```

需要从本机浏览器访问时，在另一个本机 PowerShell 窗口运行并保持窗口打开：

```powershell
ssh -N -L 127.0.0.1:18080:127.0.0.1:8080 huawei-opensandbox
```

然后访问 `http://127.0.0.1:18080/health` 或 `/docs`。Ctrl+C 关闭转发。创建/管理沙箱仍要求 API 密钥；正式方案由服务器 Runner 持有该密钥，本地项目调用 Runner，不把管理密钥下发给任务沙箱。

停止服务只停止控制进程，不能假定已停止所有沙箱。运维停止前应先停止接收任务、核对并销毁明确归属的沙箱；未知容器保留调查，禁止通过全局 prune 代替逐任务清理。

## 5. 安装后的实际基线与边界

2026-10-19 19:37 北京时间采样：Docker/OpenSandbox 均 active；操作系统可见内存 1771.2 MiB，MemAvailable 1051.4 MiB；OpenSandbox service MemoryCurrent 137.4 MiB；根盘可用 32.21 GiB；swap 仍为 0；无残留容器。这是空闲采样，服务内存包含方式与单进程 RSS 不同，不能作为执行峰值或容量保证。

单沙箱测试设定的上限是 512 MiB，结束命令后一次容器 memory usage 采样为 7.28 MiB；这既不是 Python 运行峰值，也不是 pytest/mypy/构建任务的占用。未执行 2/4 并发或重负载压测，4 个轻量沙箱目标仍须单独验收。

当前只运行了自有受信测试。实测容器用户仍为 root，rootfs 可写，bridge 出网尚未加入 deny 策略；这与现有 YiPilot DockerWorker 的非 root、只读 rootfs、断网合同存在差异。正式接收模型生成或其他不可信仓库代码前，须完成对应运行 profile 与隔离验收，不能因本次基础连通性通过就视为等价替换。

下一步的安排已收敛到[远程迁移实施计划](2026-10-19-opensandbox-implementation-plan.md)：代码基线与隔离验收 → 最小 Runner Job 链路 → 含 Runner 开销的 1/2/4 并发及故障验收 → 全调用替换。Kubernetes、Redis、Celery、预热池和多租户服务均未部署。总体方案取舍见[接入方式与远程 Runner 设计](2026-10-19-opensandbox-integration-alternatives-review.md)。
