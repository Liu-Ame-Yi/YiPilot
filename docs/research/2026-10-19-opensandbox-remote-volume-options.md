# 远端 OpenSandbox：持久卷与只读 collector 方案核查

日期：2026-10-19。固定 OpenSandbox `release-1.1.1` commit `531700ca18a7fa5fab6a2f25d6dc68a76eddab12`。仅核查官方源码、生命周期协议和 Docker 文档；没有创建沙箱/卷、运行探针或修改应用。本稿解决远端数据往返的底层选择；YiPilot 的 Interface、状态合同和具体实现由主代理另行制定。

## 1. 结论

**方案在固定版本的挂载合同上可表达，值得作为小规模 POC 首选候选：每 Run 一个保留的 Docker named volume，任务结束后 destroy 并由可信管理端确认写入者退出，再用独立可信 collector 只读挂载原卷，经原有 SDK 文件 API 下载。** 这不需要另建导出 HTTP/SSH 数据服务。

**它还不是纯 SDK 闭环：** OpenSandbox 没有独立的卷 list/inspect/delete/attachment 管理 API；原任务 destroy 成功不证明所有写入者都退出，collector 也不会自动接管删除已有卷。因此必须保留少量远端可信管理能力，负责卷归属与排他性、实际停止/挂载核验、最后删卷。若条件是不增加任何此类能力，固定版本无法提供完整的“可信停止后导出并无残留”保证。[生命周期协议](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/specs/sandbox-lifecycle.yml)、[Docker volume 实现](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/volumes.py)

## 2. 固定源码事实

| 问题 | 1.1.1 实际行为 | 方案选择 |
|---|---|---|
| Docker PVC 是什么 | `pvc.claimName` 映射 Docker named volume，不是另一个存储服务 | task 与 collector 使用同一 daemon、同一 claim |
| 是否必须预建 | `createIfNotExists` 默认 true；先 inspect，不存在时 create_volume(name, server-managed label)；false 时缺卷返回 400 | 可自动创建；需要精确归属/权限时由可信管理端预建更清楚 |
| destroy 后保留吗 | `deleteOnSandboxTermination` 默认 false；只有“该 create 请求实际新建且 delete=true”的卷才进入该 sandbox 的待删列表 | task 明确 false，不能依赖隐含默认 |
| collector 可只读吗 | `Volume.readOnly=true` 转成 Docker bind spec 的 :ro；默认 false 是 :rw | collector 必须 true；同时 createIfNotExists=false，缺卷应失败 |
| 旧卷自动删吗 | pre-existing 卷不加入本次自动清理列表；managed cleanup 还检查 server label，失败仅 warning | collector 设置 delete=true 也不会删除它看到的已有任务卷 |
| Docker 卷配额 | PVC storage/storageClass/accessModes 是 Kubernetes provisioning hints，Docker 忽略 | 不要把 storage="1Gi" 当 Docker 磁盘上限 |
| 独立 volume API | lifecycle 路由包含 sandbox/snapshot/template 等，没有 /volumes 资源或卷引用者查询 | 最终删除/检查须管理端 Docker API/CLI 或窄服务端扩展 |

依据：[服务端 PVC schema](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/api/schema.py)、[Python PVC/Volume](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/sdks/sandbox/python/src/opensandbox/models/sandboxes.py#L445)、[inspect/auto-create](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/volumes.py#L152)、[记录待删卷](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/volumes.py#L74)、[请求卷挂载](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/volumes.py#L336)、[delete/cleanup](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/docker_service.py#L1111)、[完整生命周期路由](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/specs/sandbox-lifecycle.yml)。

这里优先采用函数实现与 schema 的默认值。volumes.py 的旧 docstring 仍说卷必须预建，不能据此忽略同一函数内已经实现的 create_if_not_exists 分支。

### 2.1 subPath 不是本方案必需条件

不设 subPath 时，服务端生成 `claimName:/mount/path:ro|rw`，直接使用 named volume。设 subPath 时要求 local driver，inspect 取得 Mountpoint 后生成实际宿主目录 bind；只做词法检查及“路径可访问时”的 symlink realpath 检查，OSError 时跳过后者，存在性留给 Docker 创建时处理。因此首版建议每 Run 独占整卷，不用一个共享大卷再靠 subPath 划分信任边界。[subPath 实现](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/volumes.py#L222)

readOnly 只限制该次挂载：Docker 允许同一卷同时挂到其他容器，也允许其他挂载为 rw；这不是冻结原卷。空卷挂到镜像内有内容的目录时，Docker 默认复制镜像目录内容，OpenSandbox 生成的 mount spec 没有 nocopy；collector 要使用固定空挂载点，验证创建 collector 不向卷引入内容。[Docker volumes](https://docs.docker.com/engine/storage/volumes/)、[bind spec 构造](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/volumes.py#L368)

## 3. 可实施的最小流程（设计推论，尚未实测）

本轮具体实施候选由主代理进一步收敛为：受限 SSH 管理工具只承担 prepare、inspect/release、GC 三类窄管理操作；prepare 预建独占卷并写 project/run/attempt/request-hash 归属标签，task 与 collector 都设 createIfNotExists=false；源码、输入和结果全部通过 SDK/server_proxy 传输，SSH 不传数据、不提供任意 shell。它是 YiPilot 拟新增的维护能力，**不是上游已有协议或已验证实现**。下面保留 auto-create 分支作为上游能力说明，不作为首版默认。

1. **先登记卷。** 本地持久化 Run/attempt → claimName → 远端 server/daemon 身份；claim 使用不可混淆且符合 DNS 名称限制的独占名称。可信管理端验证新卷归属、排除旧数据与同名复用。自动创建卷只带通用 server-managed label，不自动记录 YiPilot 的 Run 所有权。
2. **任务写卷。** task 的工作根挂到该卷，read_only=false、delete_on_sandbox_termination=false。预建卷时 create_if_not_exists=false；由任务创建时 true。输入通过 SDK bytes 上传到此根，不能仅写在容器临时 rootfs。非 root 的 UID/GID、初始目录 owner/permissions 和卷容量须先验证；auto-create 没有设置 owner/chown/配额的参数。
3. **日志先收。** stdout/stderr、execution ID 和命令结果在 destroy 前有界保留；task rootfs 消失后不能再靠原 execd 回收。将最终工作树留在 Run 卷上。
4. **可信停止与排他。** destroy task 后，由远端可信管理操作检查主容器、sidecar、其他挂载该卷或其宿主路径的写入者，并在整个采集期间禁止重新附加 rw 写入者。只检查 sandbox GET 404 或 task 标签不够；Docker 之外的写入者属于宿主可信边界，不能靠 SDK 列表自动排除。
5. **创建 collector。** 使用固定可信镜像 digest、固定 entrypoint/扫描程序；同卷挂到固定空目录 /run-data，read_only=true、create_if_not_exists=false、delete_on_sandbox_termination=false。scanner 只把卷中内容当数据，生成完整 manifest 并把导出包写到 collector 自己的私有目录；不执行卷里的脚本、不加载其中配置/插件。
6. **SDK 下载并本地复核。** 下载可信 scanner 的 manifest 与经过校验的文件/导出包；本地继续验字节数、文件数、路径/链接/类型、hash 和完整树，最后再进入 YiPilot 既有批准/应用流程。缺卷、缺文件、扫描不完整、超限或 hash 不符都不能当成功；是否需要打包并不改变这条验证原则。
7. **分开回收。** finally 销毁 collector，可信管理操作再确认无引用并删除准确的 Run 卷，记录成功或待回收状态。collector 创建/下载失败可在仍保持无写入者的卷上重试采集，不能由此重放原任务；最终删卷失败要留账。

这条流程复用 OpenSandbox 的 create/destroy/files 传输面；可信管理操作不是新增任意命令服务，也不应把 Docker socket 或服务器凭据交给 task/collector。SDK 配置与操作失败时的状态合同由上层统一处理。

### 3.1 一个只展示挂载选择的例子

下面仅是已核对模型可表达的配置，**不是已运行代码，也不包含安全/生命周期实现**：

```python
from opensandbox.models.sandboxes import PVC, Volume

# 原任务已经创建/保留同名卷；collector 绝不能补建缺失卷。
collector_volume = Volume(
    name="run-results",
    pvc=PVC(
        claim_name=recorded_run_volume,
        create_if_not_exists=False,
        delete_on_sandbox_termination=False,
    ),
    mount_path="/run-data",
    read_only=True,
)
```

模型及 flags 依据：[Python Volume/PVC](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/sdks/sandbox/python/src/opensandbox/models/sandboxes.py#L445)。

## 4. 哪些功能仍需补，不能由“只读 collector”自动得到

- **停止/冻结证明：** Docker delete 先 kill/remove 主容器，sidecar cleanup 是 best effort。read-only collector 不会强迫其他 writer 停止；需要运行时实际核验与排他绑定，不能将 Docker remove 结果扩大为全系统冻结证明。[delete](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/docker_service.py#L1111)、[sidecar cleanup](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/networking.py#L414)
- **卷删除：** 保留的卷即使当初由 server 自动创建，collector 将其视为 pre-existing；没有 SDK volume-delete。需要有限的管理端 remove_volume。对所有卷执行 prune 不能替代按 Run ledger 精确删除；“collector 清空文件”也不等于卷资源被删除。[卷清理函数](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/volumes.py#L439)
- **可信完整导出：** SDK 有 read_bytes/read_bytes_stream、list_directory/search/get_file_info，未找到通用“安全全树导出+不可变 manifest”API。需要固定 scanner 自己限制遍历、拒绝链接/特殊文件和体积攻击；文件 API 的普通路径读取不能独自证明没有跟随恶意链接或漏掉删除项。[Filesystem Interface](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/sdks/sandbox/python/src/opensandbox/services/filesystem.py)
- **collector 的信任：** 任务卷只能挂数据目录，不能覆盖 /opt/opensandbox、entrypoint、scanner、环境/配置或导出程序；任务控制的 mode/links/data 不能改变 scanner 或让它执行代码。若 execd 和任务同 UID、管理面可被直接调用、或 collector 控制程序可被数据篡改，此方案仍需前置门槛，不能仅因新建 collector 就宣布可信。此处保留为实际隔离/身份/只读挂载探针，不重复展开先前管理面调研。
- **归属和独占：** PVC claimName 验证是名称格式，不是卷租户授权；没有在这条 Docker volume 路径看到 Run owner ACL、独占 writer 租约或跨请求唯一性限制。需要可信后端保存不可变关联并禁止用户指定任意别的 claim；元数据或名字本身不是安全证明。[验证函数](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/validators.py#L439)、[挂载验证](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/volumes.py#L49)

## 5. host bind 替代方案

如果远端已经有一个管理进程直接管理 Run 目录，host bind 也可实现 task rw → destroy → collector ro：host.path 必须是服务端/daemon 能对应到同一内容的路径，配置 allowed_host_paths，并在远端真实 filesystem 上做 realpath/权限核验。OpenSandbox 会验证路径并 mkdir，但没有提供“按 Run 删除宿主目录、枚举宿主写入者、冻结目录”的生命周期 API；这些仍归可信管理端。[host volume 实现](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/docker/volumes.py#L96)、[host path 校验](https://github.com/opensandbox-group/OpenSandbox/blob/531700ca18a7fa5fab6a2f25d6dc68a76eddab12/server/opensandbox_server/services/validators.py#L364)

named volume 减少对远端目录布局和 server/daemon 路径视图的依赖，代价是最后还要 Docker 管理端删卷；host bind 便于已有可信扫描/清理程序直接访问，但增加路径 allowlist、同一目录证明和宿主写入者管理。两者都不能直接挂本地 Windows controller 的 D: 目录到独立远端服务器。

## 6. POC 判定项

只验证这一条方案即可，不需要先做通用 export 服务：

- 任务写入后 destroy，原卷仍存在；collector false-auto-create + ro 能读到同一完整内容，写卷实际被拒绝。
- 缺卷返回失败，没有自动生成新空卷；同名旧卷/错误 server/daemon 身份不能通过 Run 绑定核验。
- collector 不改变空卷或普通卷的内容，不执行卷内文件，恶意链接/权限/文件数/大文件被完整拒绝或按既有合同处理。
- 停止/检查与 collector 采集之间无法重新附加 writer；destroy 成功但 sidecar/其他引用残留时进入待清理。
- collector 失败后卷留存可重试采集，最终远端准确删卷并确认；超时、客户端重启和下载中断不重放原任务。

以上为源码支持的候选设计和验收项，尚无本项目端到端通过证据。
