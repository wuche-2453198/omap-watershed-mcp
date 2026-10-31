# Windows 安装与验收指南

目标环境：Windows 10/11 x64 + 奥维互动地图 PC 桌面端 + DeepSeek Harness（DSH）+ 本项目本地 MCP（stdio）。

> **本指南没有在真实 Windows + 奥维环境中执行过。** 开发机为 macOS（无奥维客户端、无 Windows、无奥维账号、无用户 DEM/参考边界），因此文中的 Windows 命令、路径猜测与客户端界面描述按官方文档整理，标为“待实测”的步骤必须在目标机器上实际执行并记录证据（`docs/待核验清单.md` A–E 组）。不要把本指南的完成当成真实验收完成。

## 0. 安装前检查清单

| 项 | 要求 | 确认方式 |
|---|---|---|
| OS | Windows 10/11 x64 | `winver` |
| 奥维客户端 | Windows PC 桌面端，已登录；**至少 VIP**；要用地图图片/等高线需 **SVIP** | 奥维“系统 → 系统设置 → 高级 → 第三方接口（窗口消息+Websocket）”可打开；`20501` 查询等级 |
| 奥维接口 | 已启用 WebSocket（或 WebSocket+SSL），记下**端口**与**预共享密钥（PSK）** | 同上界面（手册 2.1.2） |
| Python | 3.12（conda-forge 环境，推荐） | `conda run -n omap-mcp python --version` |
| DSH | 0.1.5-rc.1（开发验证版本） | `dsh --version` |
| DEM | 带坐标系的 GeoTIFF（项目实测 DTM 优先） | `mcp__ovital__inspect_dem` |
| 部署目录 | 建议 `C:\work\omap-mcp`（以下命令以此为例；放别处需同步改 patch/config 路径） | 资源管理器 |
| 密钥目录 | 建议 `C:\work\omap-secrets`（**仓库外**） | 见 §3 |

## 1. 安装 Python 环境（conda-forge，推荐）

### 1.1 为什么用 conda-forge、为什么锁版本

本项目依赖的栅格/几何栈（rasterio/GDAL、pyproj/PROJ、shapely/GEOS）含原生 DLL。Windows 上最容易出问题的不是 Python 代码，而是 **GDAL/PROJ/GEOS 的来源混用**（conda 一套、pip wheel 一套、系统里又一套 → DLL 冲突、CRS 数据不一致）。因此：

- 推荐用 **conda-forge 一个环境**装齐原生依赖；
- 只把纯 Python 包（如 MCP SDK）用 pip 装进该环境；
- 部署完成后用 `conda env export > environment.lock.yml` 生成精确锁文件归档，回滚/复现以锁文件为准；
- 不建议在 Windows 上把 conda 环境的 GDAL 与 pip 安装的 `rasterio`/`gdal` wheel 混用；
- GRASS 单独一个环境（见 §2），避免它带来的 GDAL/依赖版本与主环境冲突。

### 1.2 创建环境

安装 Miniforge（或 Miniconda）后，在 PowerShell 中：

```powershell
cd C:\work\omap-mcp
conda env create -f environment.yml -n omap-mcp
conda run -n omap-mcp python -m pip install -e .
```

说明：

- `environment.yml` 固定 Python 3.12 与主要依赖的主版本范围；用 `conda env create` 首次解析。
- `pip install -e .` 让 `python -m ovital_mcp` 与 `ovital-mcp` 命令可用（也可不装，直接在仓库根以 cwd 运行 `python -m ovital_mcp`）。
- 生成锁文件并归档：

```powershell
# PowerShell 5.1 中 ">" 默认写 UTF-16，conda/YAML 可能读不了；用显式 UTF-8：
conda env export -n omap-mcp | Set-Content -Encoding utf8 environment.lock.yml
```

（若用 cmd 或 Git Bash，可直接 `conda env export -n omap-mcp > environment.lock.yml`。）

`environment.lock.yml` 属于部署产物，建议与版本记录一起归档（不要提交含本机路径/渠道凭据的文件到公开仓库）。

### 1.3 备选：python.org + venv（不推荐用于生产）

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e .
```

局限：`rasterio` 等 wheel 自带的 GDAL 与系统其他 GIS 软件可能冲突；无法用 conda 统一管理 PROJ/GEOS 数据。若采用此方案，验收时必须额外核对 `rasterio.__gdal_version__`、`pyproj.proj_version_str` 与 CRS 数据完整性（`pyproj.datadir.get_data_dir()`）。

### 1.4 依赖自检

```powershell
conda run -n omap-mcp python -c "import ovital_mcp, mcp, rasterio, shapely, pyproj, numpy, scipy, matplotlib, PIL, websockets; print('deps ok')"
conda run -n omap-mcp python -c "from ovital_mcp.config import load_config; c=load_config(r'C:\work\omap-mcp\config\ovital.yaml'); import json; print(json.dumps(c.safe_dict(), ensure_ascii=False))"
```

第二条输出必须**脱敏**（应看到 `psk_configured: true` 而不是密钥内容）。

## 2. （可选）安装 GRASS GIS

GRASS 只用于可选引擎 `gis.engine: grass` 或 `auto` 的优先尝试；没有 GRASS 时 `auto` 会回退到内置 native D8 引擎。**本机 macOS 已验证 GRASS 8.5.0（conda-forge）；Windows 未验证。**

```powershell
conda create -n omap-grass -c conda-forge grass=8.5 -y
conda run -n omap-grass grass --version
```

- Windows 上 GRASS 通常以 `grass.bat`/`grass84.bat` 之类的启动器提供，**具体名称以安装产物为准**（本项目未在 Windows 实机确认）。用 `where grass` 或在 `%CONDA_PREFIX%\Scripts` 下确认后，把完整路径填入 `config/ovital.yaml` 的 `gis.grass_cmd`（例如 `C:/work/miniforge3/envs/omap-grass/Scripts/grass.bat`）。
- GRASS 启动需要可写配置目录；若启动失败，检查 `HOME`/用户配置目录权限（本机 macOS 上曾因受限 HOME 失败）。
- 未安装 GRASS 时不要写 `engine: grass`；保持 `auto`/`native`。

## 3. 凭据文件（PSK）与权限

1. 在**仓库外**建目录（示例 `C:\work\omap-secrets`）。
2. 限制 ACL 到当前用户：

```powershell
$dir = 'C:\work\omap-secrets'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
icacls $dir /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F"
```

3. 写入 PSK（文件首行即密钥，**UTF-8 无 BOM**、不要多余空行）：

```powershell
$psk = Read-Host -Prompt '输入奥维预共享密钥（不会回显到日志）' -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($psk))
[IO.File]::WriteAllText(
    'C:\work\omap-secrets\ovital_psk.txt',
    $plain,
    (New-Object System.Text.UTF8Encoding($false)))
```

4. 再限制文件 ACL：

```powershell
icacls 'C:\work\omap-secrets\ovital_psk.txt' /inheritance:r /grant:r "$($env:USERNAME):F"
```

注意：

- 不要把 PSK 写进 `config/ovital.yaml` 的 `psk` 字段、DSH patch、环境变量、任务文件或日志。DSH 会清理子进程环境中名字匹配 `KEY|PASSWORD|SECRET|TOKEN` 的变量和全部 `DSH_*`，**不能依赖环境变量继承**。
- PSK 文件不要放进仓库/网盘同步目录；不要提交 Git。
- 若用记事本保存，务必确认保存为“UTF-8 无 BOM”（带 BOM 会在首行引入不可见字符，导致鉴权失败）。

## 4. 配置 MCP

也可以用仓库脚本自动完成这一步与 §5 的路径替换（脚本不接收、不写 PSK；仍需手工粘贴密钥）：

```powershell
powershell -ExecutionPolicy Bypass -File C:\work\omap-mcp\scripts\install_windows.ps1
```

脚本会：创建/更新 conda 环境、复制配置并替换 Windows 占位路径、创建受限密钥目录、生成按实际路径改写的 `ovital_mcp\dsh\ovital.cordis.local.yml`（供 `--patch` 使用）。**该脚本尚未在真实 Windows 上执行过**，首次使用建议先加 `-CheckOnly` 检查环境。

手工步骤（或脚本失败时）：

```powershell
New-Item -ItemType Directory -Force -Path C:\work\omap-mcp\config | Out-Null
Copy-Item C:\work\omap-mcp\ovital_mcp\dsh\config.example.yaml C:\work\omap-mcp\config\ovital.yaml
notepad C:\work\omap-mcp\config\ovital.yaml
```

至少修改：

| 字段 | 示例 | 说明 |
|---|---|---|
| `ovital.endpoint` | `ws://127.0.0.1:7699` | **端口必须与奥维客户端“第三方接口”设置一致**；7699 只是手册示例，不是默认值。启用 SSL 时用 `wss://` |
| `ovital.psk_file` | `C:/work/omap-secrets/ovital_psk.txt` | 仓库外的密钥文件；不要用 `psk` 明文 |
| `storage.data_root` | `C:/work/omap-mcp/data` | 任务/成果/快照/日志目录；不要指向网络盘或同步盘 |
| `gis.engine` | `auto` | `auto`/`native`/`grass`；无 GRASS 时用 `auto` 或 `native` |
| `gis.grass_cmd` | `grass` 或完整路径 | 仅 GRASS 引擎需要 |
| `logging.file` | `C:/work/omap-mcp/data/logs/ovital_mcp.log` | 日志写文件（stdout 只能输出 MCP 协议帧） |

坐标语义保持 `ovital.latlng_type: 1`（CGCS2000）、`ovital.shape_gcj02: 0`；除非有实测控制点，不要打开 `allow_gcj02_conversion`。

## 5. 接入 DSH（Cordis patch）

编辑 `C:\work\omap-mcp\ovital_mcp\dsh\ovital.cordis.yml`，确认三处路径与实际部署一致：

```yaml
command: 'C:/work/omap-mcp/.venv/Scripts/python.exe'   # conda 环境则改成 ...\envs\omap-mcp\python.exe
cwd: 'C:/work/omap-mcp'
env:
  OVITAL_MCP_CONFIG: 'C:/work/omap-mcp/config/ovital.yaml'
```

若已运行 `scripts\install_windows.ps1`，它会生成按实际路径改写好的 `ovital_mcp\dsh\ovital.cordis.local.yml`，可直接用它替代下面的 `ovital.cordis.yml`。

启动（临时覆盖，不改全局配置）：

```powershell
dsh web --patch C:\work\omap-mcp\ovital_mcp\dsh\ovital.cordis.yml
```

先 dry-run 校验 patch：

```powershell
$env:DSH_HOME = "$env:TEMP\dsh-patch-check"
dsh --profile headless --patch C:\work\omap-mcp\ovital_mcp\dsh\ovital.cordis.yml --dump-config | Select-String -Pattern 'mcp-ovital' -Context 0,12
```

持久启用：把 patch 文件中**单个 `- insert:` 条目**合并进 `$DSH_HOME\profiles\<profile>\cordis.patch.yml`（单 profile）或 `$DSH_HOME\cordis.patch.yml`（全机）；不要覆盖已有文件。

启动后：

1. 等工具发现完成（初始发现是异步的），日志中应看到 `mcp-client(ovital)`；失败也有对应错误日志。
2. 调用 `mcp__ovital__get_capabilities`，确认 18 个工具可用、连接状态、VIP 级别、`simulated=false`。
3. Skill 位于 `C:\work\omap-mcp\.dsh\skills\ovital-watershed\SKILL.md`；**在仓库根启动 DSH/把该目录选为 workspace** 才能被项目级 Skill 发现（项目根 = 最近含 `.git` 的祖先，否则启动 cwd）。

## 6. 验收步骤（真实 Windows 必须执行；当前**未完成**）

逐项执行并保存证据（脱敏后的工具返回、日志片段、截图）。任何一项未通过，都不得在报告中写“已验证”。

> 本机（macOS headless + 模拟奥维 + 合成 DEM）已有的 DSH 工具发现、Skill 加载与视觉链路证据见 [evidence/README.md](evidence/README.md)（含 3 个插件 transcript）。它们属于 `offline_simulated`/`offline_computed`，**不能替代**下表的 Windows 实机验收；真实联机与用户数据边界精度仍为未完成。

| # | 步骤 | 操作 | 通过标准 | 证据 |
|---|---|---|---|---|
| 1 | 依赖自检 | §1.4 两条命令 | 输出 `deps ok`；配置脱敏摘要正常 | 终端输出 |
| 2 | MCP 独立启动 | 仓库根运行 `python -m ovital_mcp`（stdio，应等待输入；stderr 有日志、stdout 无日志污染） | 进程稳定、无 stdout 垃圾 | stderr 片段 |
| 3 | patch 组合 | `dsh --profile headless --patch ... --dump-config` | 输出含 `mcp-ovital` 与正确 `command/cwd/env` | dump 片段 |
| 4 | DSH 工具发现 | 启动带 patch 的 DSH 会话，看日志与工具列表 | 出现 18 个 `mcp__ovital__*`；`mcp-client(ovital)` 无错误 | 日志/会话截图 |
| 5 | Skill 发现 | 在仓库根启动并新建会话 | Skill catalog 含 `ovital-watershed` | 会话截图 |
| 6 | 能力探测 | `mcp__ovital__get_capabilities` | `status=ok`；`connection.available=true`；版本号/VIP 等级；`simulated=false`；`unverified[]` 可见 | 工具返回（脱敏） |
| 7 | 登录鉴权 | 上一步已隐式验证；若失败看错误码 | `20000` 返回版本号；错误信息不含 PSK | 返回 |
| 8 | 会员权限 | `20501`/`20503` 结果；试 `20301`/`20303` | 明确 VIP/SVIP 等级与接口可用性（无权限应显式失败） | 返回 |
| 9 | 读取选中对象 | 奥维中选中坝址/取水口标签 → `get_selection` → `read_object` | 坐标语义/轴序符合预期；顶点可解析为 EPSG:4490 `(lon,lat)` | 返回 + 奥维截图 |
| 10 | 控制点往返 | 用已知控制点写入/回读（`write_draft` → `read_back`） | 顶点往返误差在协议允许精度内（记录数值，不承诺工程精度） | 数值记录 |
| 11 | DEM 检查 | `inspect_dem`（用户 DEM） | CRS/分辨率/NoData/覆盖/高程基准检查项齐全；warnings 如实 | 返回 |
| 12 | 集水区任务 | `start_watershed` → `get_job` 轮询 | 任务成功；provenance 含引擎/版本/参数/输入 SHA256 | job 摘要 |
| 13 | 分段复核图 | `get_job(include_images=true)`、`review_segment` | **模型会话中实际出现图片**并能描述图中边界/等高线/出口；仅文件路径不算（本机 headless 已用模拟奥维验证该链路，Windows + 真实奥维仍待验：`docs/evidence/`） | 会话记录/截图 |
| 14 | 疑点修订 | `revise_candidate`（改计算参数 + `anchors_lonlat` 记录锚点；v0.1 不做局部几何编辑） | 返回修改前后面积与影响范围；不静默改边界 | 工具返回 |
| 15 | 写草稿/回读 | `write_draft` → `read_back` | 草稿在独立文件夹；回读顶点/面积差异有口径说明；幂等重试返回同一 ObjID | 返回 + 奥维截图 |
| 16 | 定稿/回滚 | `finalize_draft(confirm=true)` → `rollback_change` | 版本记录；回滚报告逐项列出删除/恢复结果 | 返回 |
| 17 | 导出 | `export_result`（geojson/kml/report/projected_json；gpkg 需可选 fiona） | 文件生成；摘要含 CRS/面积口径与 SHA256；GPKG 不可用时显式报错 | 文件 + 返回 |
| 18 | 视觉链路 | 用 `deepseek-flash` 会话；确认图片被模型实际接收 | 图片出现在会话；模型基于图回答；不把转发错误当模型无视觉（本机 headless 已验证图片真实到达并被像素级判读，最终证据 `docs/evidence/dsh-headless-vision-e2e-final.out.txt`；此为 macOS + 模拟奥维，Windows 实机仍待验） | 会话记录 |
| 19 | 真实数据验收 | 用审核过的流域边界做精度统计（分段距离/面积差/关键鞍部） | 按工程约定容差；本机无参考边界 → **未完成** | 验收报告 |

## 7. 故障排查

| 现象 | 检查 | 处理 |
|---|---|---|
| `conda env create` 解析失败 | `channel` 是否 conda-forge；网络/代理 | 更新 conda；检查代理；必要时用 `--override-channels` |
| `import rasterio` 报 DLL 错误 | 是否混装 pip GDAL/rasterio | 重建环境，只保留 conda-forge 原生栈；不要混用 wheel |
| 找不到 `grass` | GRASS 环境是否创建；启动器名称 | `conda run -n omap-grass grass --version`；用完整路径填 `gis.grass_cmd`；或改用 native |
| DSH 无 `mcp__ovital__*` 工具 | `command/cwd` 路径、Python 能否 import、`serverName` 冲突、patch 是否生效 | 手动跑 `python -m ovital_mcp` 看 stderr；看 DSH 日志 `mcp-client(ovital)`；用 `--dump-config` 校验 |
| 连接失败/鉴权失败 | 奥维是否运行；第三方接口是否启用；端口/协议（ws/wss）是否一致；PSK 是否一致；IP 白名单 | 逐项核对；用官方样例先验证接口；检查 PSK 文件 BOM/空白 |
| `ret=-5` / `ret=-7` | 账号会员等级 | 升级 VIP/SVIP；无权限时用本地 DEM/复核图 |
| 中文乱码/编码错误 | Windows 代码页；Python UTF-8 模式 | patch 已设 `PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`；日志写文件 |
| 复核图看不到 | 模型是否支持图片、附件功能、图片大小/像素限制 | 用 `deepseek-flash`；检查图片 ≤4MB/像素上限；确认不是只拿到路径 |
| 端口被占用/防火墙 | `netstat -ano \| findstr :<端口>`；奥维设置 | 改奥维端口并同步 `endpoint`；放行本机回环 |
| WSL/容器里运行 MCP | 网络命名空间与路径映射 | 奥维在 Windows，MCP 也应在同一 Windows 环境跑；**不要假设 WSL 的 127.0.0.1 等于 Windows** |
| 写入后出现重复对象 | 超时后是否重复创建 | 先 `read_back` 确认；用 `idempotency_key`；必要时 `rollback_change` |

## 8. 安全与运维

- 凭据：PSK 仅存仓库外文件（ACL 限制）；日志/工具返回/provenance 一律脱敏；不要把 PSK 贴到会话、截图、issue。
- 写操作：`write_draft` 有写前快照 + 幂等键 + 回读；`finalize_draft` 需 `confirm=true`；失败/超时先 `read_back` 再决定。
- 数据：`data_root` 下保存任务、快照、成果与日志；定期按 `storage.retention_days` 清理；敏感工程数据不要放同步盘。
- 升级：升级 DSH/Python 依赖/奥维客户端后，重跑 `--dump-config`、`get_capabilities` 与 §6 验收步骤，并更新 `docs/version-record.md`。
- 卸载/重置：停止 DSH；删除 conda 环境；删除部署目录与 `data_root`；凭据文件单独销毁。

## 9. 本指南未验证事项（必须保留在报告中）

1. Windows 上 conda-forge 环境的实际解析版本与 DLL 兼容性；
2. GRASS 在 Windows 的安装路径/启动器名称与 `r.watershed` 运行；
3. 奥维第三方接口界面、端口、PSK 与 IP 白名单的实际配置；
4. 真实账号的 VIP/SVIP 权限、`20000` 登录与各命令返回；
5. 20112 写入/替换、20101 回读、20802 量算的真实语义；
6. DSH 在 Windows 上发现 18 个工具、加载 Skill、视觉模型收到复核图；
7. 用户 DEM/参考边界的精度验收。

完成以上任一项后，请把证据（脱敏）补进 `docs/待核验清单.md` 对应条目，并更新 `docs/version-record.md`。
