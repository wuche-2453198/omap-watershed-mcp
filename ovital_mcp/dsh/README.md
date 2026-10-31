# OMAP Watershed MCP 的 DSH 接入说明

本目录的三个文件用于把本仓库的本地 MCP 服务（stdio）接入 DeepSeek Harness（DSH）：

| 文件 | 用途 |
|---|---|
| `ovital.cordis.yml` | DSH Cordis **patch 覆盖层**（overlay）：向 profile 组合树插入一行 `@deepseek-ai/dsh-mcp-client` 插件配置，以 stdio 启动本仓库的 MCP。 |
| `config.example.yaml` | **MCP 服务自身**的配置样例（奥维端点、PSK 文件路径、数据目录、GIS 引擎）。 |
| `README.md` | 本文件：启动、探测、工具名、常见错误。 |

> 本接入**没有**在 Windows + 奥维实机上验收过（开发机为 macOS，无奥维客户端）。本文件依据 DSH 0.1.5-rc.1 官方文档与本机验证编写，真实 Windows 验收项见 `docs/待核验清单.md`。

## 1. 前置条件

1. **DSH**：0.1.5-rc.1（本机验证版本；其他版本未验证）。
   - 本机路径 `/opt/homebrew/bin/dsh`，Web GUI 运行在 `http://127.0.0.1:64759`（desktop profile）。
   - CLI 启动器支持 `--patch <path>`（可重复）作为**最后一层**覆盖：profile bundles → profile 的 `cordis.patch.yml` → `$DSH_HOME/cordis.patch.yml` → `--patch`。
2. **Python 环境**：项目内 `.venv`（Windows 为 `C:\work\omap-mcp\.venv\Scripts\python.exe`，POSIX 为 `.venv/bin/python`），依赖见仓库根 `pyproject.toml` / `environment.yml`。
3. **奥维客户端**（仅真实联机需要）：Windows 奥维 PC 桌面端，账户至少 VIP；**地图图片数据（20301/20302）与等高线（20303）需要 SVIP 及以上**（手册 2.1.1）。在本机没有奥维时，MCP 仍可启动（连接是懒建立的），`get_capabilities` 会明确报告未连接。
4. **配置文件**：把 `config.example.yaml` 复制到部署目录（默认示例为 `C:/work/omap-mcp/config/ovital.yaml`），至少设置 `ovital.endpoint` 与 `ovital.psk_file`。PSK 只在单独文件中，不写进 patch、不写进环境变量。
5. **DEM**：至少一个带坐标系的 GeoTIFF（项目实测 DTM 优先；公开数据见 `docs/gis-notes.md`，注意 Copernicus GLO-30 是 DSM 不是裸地 DTM）。

## 2. 启动 DSH（带 patch）

> 仓库脚本 `scripts/install_windows.ps1` / `scripts/install_posix.sh` 会生成按本机路径改写好的 `ovital_mcp/dsh/ovital.cordis.local.yml`（以及 `config/ovital.yaml` 与受限密钥目录），可直接用它替代下面的 `ovital.cordis.yml`。脚本不接收、不写 PSK，仍需手工把密钥粘贴进密钥文件。

### Windows（PowerShell / cmd）

```bat
dsh web --patch C:\work\omap-mcp\ovital_mcp\dsh\ovital.cordis.yml
```

### POSIX（macOS/Linux，本机调试）

```sh
cd "/path/to/omap mcp"
dsh web --patch "$PWD/ovital_mcp/dsh/ovital.cordis.yml"
```

`dsh web` 等价于 `dsh --profile web`。patch 文件路径可以指向磁盘上任意位置（不必在仓库内）。

### 持久启用（不想每次都带 `--patch`）

按官方 MCP 接入文档：把 `ovital.cordis.yml` 中**单个 `insert` patch 条目**合并进用户 patch 层——

- 只对一个 profile 生效：`$DSH_HOME/profiles/<name>/cordis.patch.yml`
- 对全机所有 profile 生效：`$DSH_HOME/cordis.patch.yml`

**不要整体覆盖已有文件**（里面可能已有其他用户 patch）；用编辑方式追加这一个 `- insert:` 条目。改完重启 DSH（profile 的 `patchReload` 为 `live` 时也可能热应用，是否生效以 DSH 日志为准）。

### 启动前校验 patch 能否被 DSH 组合（不启动服务）

可以用一个隔离的 DSH home 做 dry-run，避免污染本机 profile：

```sh
# POSIX
DSH_HOME=$(mktemp -d) dsh --profile headless --patch "$PWD/ovital_mcp/dsh/ovital.cordis.yml" --dump-config | grep -A15 mcp-ovital
```

```powershell
# Windows PowerShell
$env:DSH_HOME = "$env:TEMP\dsh-patch-check"; dsh --profile headless --patch C:\work\omap-mcp\ovital_mcp\dsh\ovital.cordis.yml --dump-config
```

`--dump-config` 只组合并打印配置树，不启动插件、不连接奥维。注意 `desktop` 这个名字被 Electron 应用保留，CLI 会拒绝对它做启动/配置 dump/插件管理。

仓库另附 `scripts/verify_dsh_patch.py`（独立验证脚本），会检查 patch 结构、字段、真实 `dsh-mcp-client` schema、隔离 `DSH_HOME` 组合与负向控制（本机 DSH 0.1.5-rc.1 上 11/11 通过）。

## 3. 接入后的工具名

`serverName: ovital` → 模型看到的工具名为 `mcp__ovital__<rawName>`，例如：

```
mcp__ovital__get_capabilities
mcp__ovital__get_selection
mcp__ovital__read_object
mcp__ovital__get_scene
mcp__ovital__inspect_dem
mcp__ovital__start_watershed
mcp__ovital__get_job
mcp__ovital__cancel_job
mcp__ovital__list_jobs
mcp__ovital__review_segment
mcp__ovital__revise_candidate
mcp__ovital__validate_geometry
mcp__ovital__measure_geometry
mcp__ovital__write_draft
mcp__ovital__read_back
mcp__ovital__finalize_draft
mcp__ovital__rollback_change
mcp__ovital__export_result
```

共 18 个工具：`docs/architecture.md` §6 契约表 17 个 + `list_jobs`（辅助上下文恢复的任务列表，非原契约表）。以最终 `tools/list` 输出为准；工具契约与模型侧使用流程约束见 Skill：`.dsh/skills/ovital-watershed/SKILL.md`。

## 4. 探测与验收（本机/Windows 通用）

1. 启动 DSH 后**等待工具发现完成**（初始发现是异步的；慢或崩溃的服务可能延迟启动）。日志中应出现 `mcp-client(ovital)` 的连接/工具注册记录，失败也有对应错误。
2. 在会话中调用 `mcp__ovital__get_capabilities`：
   - `status: ok` 且 `data.version` / 命令支持矩阵 / 权限探测结果可出现；
   - 若本机没有奥维或未开接口，应看到**明确的连接错误**，而不是伪造成功；
   - `simulated: true` 表示连的是模拟服务器（`scripts/run_mock_ovital.py`），**不是**真实奥维；
   - 真实联机时为 `verified_against_real_device: true`，否则为 `false`。
3. 选中奥维对象后调用 `mcp__ovital__get_selection` → `mcp__ovital__read_object`，核对坐标语义与轴序（对外统一 `(lon, lat)`，EPSG:4490）。
4. 复核图验证：`mcp__ovital__get_job`（`include_images=true`）或 `mcp__ovital__review_segment` 返回 MCP Image。**只有模型在会话中实际描述/使用了图中内容才算“看到了图”**；工具只返回文件路径不算验证。
5. Skill 验证：`@deepseek-ai/dsh-skill-filesystem` 的项目根 = 最近的含 `.git` 祖先，**没有 `.git` 时用 DSH 启动时的 cwd**。因此应在仓库根启动 DSH（或在仓库根初始化 `.git`）并把该目录选为 workspace；否则 `.dsh/skills/ovital-watershed/SKILL.md` 不会被发现。会话 skill 目录里应能看到 `ovital-watershed`。

## 5. 常见错误与处理

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 会话中看不到 `ovital-watershed` Skill | 项目根判定：未在仓库根启动/未选该目录为 workspace/没有 `.git` 标记 | 在仓库根启动 DSH（或初始化 `.git`）并把仓库目录选为 workspace；确认 `.dsh/skills/ovital-watershed/SKILL.md` 存在且 frontmatter 合法（name/description） |
| DSH 启动正常，但没有 `mcp__ovital__*` 工具 | `command`/`args`/`cwd` 路径不对；Python 依赖缺失导致子进程退出；`serverName` 冲突；patch 未生效 | 看 DSH 日志中 `mcp-client(ovital)` 行；手动执行 `C:\work\omap-mcp\.venv\Scripts\python.exe -c "import ovital_mcp"`；确认 patch 文件被 `--patch` 引用；同一注册作用域内 `serverName` 必须唯一 |
| 工具存在，调用返回连接失败 | 奥维未运行；第三方接口未启用；端口与客户端设置不一致；PSK 错误；IP 白名单不含本机 | 按手册“系统 → 系统设置 → 高级 → 第三方接口（窗口消息+Websocket）”核对；先用官方样例/模拟服务器验证链路；PSK 文件首行是否有换行/空格问题 |
| 登录返回 `ret=0` 且 `msg.errmsg` 为鉴权失败 | 预共享密钥不匹配 | 重新在奥维端设置/复制 PSK；不要用 MD5 后的值填配置（适配器内部会转换） |
| `ret=-5`（不是 VIP）/ `ret=-7`（不是 SVIP） | 会员权限不足；手册旧文案写 VIP5，以实际账号返回为准 | 升级会员；地图图片/等高线需 SVIP；无权限时使用本地 DEM 与本地复核图 |
| 子进程启动报编码错误或中文乱码 | Windows 控制台代码页 | patch 已显式设置 `PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`；不要把日志 print 到 stdout |
| stdout 出现非 JSON 行，MCP 握手失败 | 依赖或代码向 stdout 打印日志 | 本项目约定日志只写 stderr/文件；如接了第三方包装脚本，检查其输出 |
| 请求超时 | 单次调用设计 < 5s；大图/大 DEM 不应同步计算 | 用 `start_watershed` → `get_job` 轮询；`toolCallTimeoutMs: 60000` 只是上限 |
| 图片没有出现在会话 | 当前模型不支持图片输入、附件功能未启用、或图片超过大小/像素限制 | 用支持视觉的模型（如 `deepseek-flash`；`deepseek-v4-pro` 不支持图片）；确认 DSH 附件能力；复核图限制 `image_max_bytes`/`max_image_pixels` |
| DSH 清理环境变量后连不上 | 依赖继承 `*_KEY/*_TOKEN` 或 `DSH_*` | 不依赖环境变量传凭据；用配置文件 `psk_file`；patch 只传非秘密的 `OVITAL_MCP_CONFIG` 路径 |

## 6. 安全与运维

- **凭据**：PSK 只在 `psk_file` 指向的文件中；Windows 用当前用户 ACL、POSIX `chmod 600`；工具返回、日志、任务文件、provenance 一律脱敏（`ovital_mcp/config.py` 的 `safe_dict()`/日志过滤器）。
- **写操作**：任何写奥维前先快照；`write_draft` 使用幂等键 + 写后 `read_back`；超时不得盲目重发“创建”。
- **模拟 vs 真实**：模拟服务器响应带 `simulated: true`，20301 图片带 “SIMULATED MAP” 水印；不得把模拟结果写成真实联机结果。
- **版本**：记录 DSH 版本与 patch 快照；DSH 仍为开发预览版，升级后应重跑 `--dump-config` 与探测步骤。

## 7. 相关文档

- 仓库内：`docs/architecture.md` §6（工具契约）、§7（DSH 接入要求）；`docs/capabilities.md`（能力与证据分级）；`docs/install-windows.md`（完整 Windows 安装）；`docs/待核验清单.md`（真实设备待验项）。
- 官方：MCP 客户端 README <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md>；MCP 接入指南 <https://deepseek-harness.github.io/deepseek-harness/en/guide/mcp-memory>；Skill 文件系统 <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/skill/skill-filesystem/README.md>。
