# Windows 实机测试报告（2026-10-30）

> 本报告只记录 **Windows 11 实机**上本次实际执行的命令、观测值与退出码。
> 证据等级：`offline_computed` / `offline_simulated` 仍按原定义；本次新增的奥维联机证据为
> **真实设备握手与权限探测**，但目标账号非 VIP，**没有任何成功的对象读写**，因此
> `verified_against_real_device` 继续保持 `false`，不得把本报告表述为工程验收。

## 0. 结论速览

| # | 项目 | 结果 | 证据 |
|---|---|---|---|
| 1 | Python 依赖栈（Windows 11 + Python 3.13.12 venv） | 安装成功，GDAL/GEOS/PROJ 与 macOS 开发机一致 | `.cache/verify/stdio-evidence-20261030T084704.json` |
| 2 | pytest 全量 | **341 passed, 8 skipped**（8 项全部是 GRASS 不可用，非失败） | `.cache/win-test/pytest-full-2.log` |
| 3 | MCP stdio 真实握手/契约 | **24/24 PASS**（18 工具、stdout 纯净、凭据脱敏） | `.cache/verify/stdio-evidence-20261030T084704.json` |
| 4 | 模拟奥维完整闭环（含 GIS/图片/故障注入） | **68/70 PASS**；2 项失败仅因本机没有 macOS 那次视觉跑留下的 DSH 附件 | `.cache/verify/loop/mock-loop-evidence-20261030T092046.json` |
| 5 | DSH patch 组合（真实 `dsh` CLI 0.2.1-rc.3） | **9/10 PASS, 1 SKIP**；唯一失败是版本号与交付声明不一致（非 patch 缺陷） | `.cache/verify/dsh-patch-evidence-20261030T091715.json` |
| 6 | 真实奥维 10.7.5 WebSocket 登录 | **成功**：`20000` 返回版本号 **1075**，`app_id=41`，`simulated=false` | `.cache/win-test/real-capabilities.log` |
| 7 | 真实奥维 VIP 权限 | 账号 `OmapLevel=0`：`20101/20104/20112` 返回 **ret=-5**（需 VIP），`20301/20303` 返回 **ret=-7**（需 SVIP） | 同上 |
| 8 | 真实对象读写、写草稿、回读、回滚 | **未完成（账号非 VIP）**；负向路径已实测：结构化 `OVITAL_PREREQ_ERROR`，未崩溃、未留对象 | `.cache/win-test/real-read.log`、`real-write.log` |
| 9 | 本地几何量算（真实坐标） | 785 m×1.27 km 矩形：**1,000,229.8758884581 m²**，EPSG:32648 | `.cache/win-test/real-read.log` |
| 10 | GRASS on Windows | **未安装/未验证**，8 项 GRASS 测试按设计跳过 | 同上 pytest 日志 |

## 1. 环境

| 项 | 值 | 采集方式 |
|---|---|---|
| OS | Windows 11 家庭版 中文版 10.0.26200（build 26200）x64 | `Win32_OperatingSystem` |
| Shell | Windows PowerShell 5.1.26100.9711（DSH 以 `pwsh -Command` 调用） | `$PSVersionTable` |
| Python | 3.13.12（`C:\Users\...\Programs\Python\Python313`），项目 `.venv` | `python --version` |
| 安装方式 | **pip + wheel**（`pip install -e ".[test]"`），未使用文档推荐的 conda-forge | 见 §2 偏差 |
| 原生 GIS | rasterio 1.5.2（GDAL **3.12.2**）、shapely 2.2.0（GEOS **3.14.1**）、pyproj 3.8.0（PROJ **9.8.1**） | `.venv` import |
| 科学计算 | numpy 2.5.4 / scipy 1.18.1 | 同上 |
| 渲染 | matplotlib 3.11.2 / pillow 13.0.0 | 同上 |
| MCP | mcp 2.3.0 / mcp-types 2.3.0 / websockets 17.3 | 同上 |
| 测试 | pytest 9.2.0 / pytest-asyncio 1.4.1 | 同上 |
| 奥维 | **10.7.5**（`D:\omap\omap.exe`，PID 39056），已登录，在线卫星图 | 进程/版本资源 |
| DSH CLI | **0.2.1-rc.3**（`D:\deepseekHarness\resources\runtime\cli\bin\dsh.cmd`） | `dsh --version` |
| DSH GUI | `http://127.0.0.1:52844`（Electron，正在运行） | 运行环境 |
| GRASS | 无 | `grass --version` 失败 |

> 与 `docs/version-record.md`（macOS 26.6.2 / Python 3.13.13）的差异：本次为 **Windows 11 +
> Python 3.13.12 + pip wheel**；原生库版本恰好一致，但这不等于两台机器的完整环境等价。

## 2. 环境搭建与偏差

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

- 文档推荐 conda-forge（`environment.yml`）以统一 GDAL/PROJ/GEOS；本次为快速得到可测环境用了
  **pip wheel**，未执行 `conda env create`。因此：
  - `environment.yml` 在 Windows 上的**实际解析结果仍未验证**；
  - 本次 wheel 提供的 GDAL 3.12.2 与 macOS 一致属于巧合，不能当作 conda 方案已验证。
- `.venv/` 已被 `.gitignore` 忽略；`config/ovital.yaml` 与 `config/` 下的本机配置同样被忽略。

## 3. 自动化测试

### 3.1 pytest 全量

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest -o addopts="" -q -rf --basetemp=.cache\win-test\pytmp-full
```

```
341 passed, 8 skipped in 74.74s (0:01:14)
```

- 8 项 skip 全部是 `tests/gis/test_gis_grass_engine.py`（`ENGINE_UNAVAILABLE`：找不到 GRASS），
  与 macOS 的 349 passed（GRASS 无 skip）相比，数量一致、原因明确。
- **Python 3.13 在 Windows 上无兼容问题**：`asyncio` 子进程、`signal`、`subprocess`、
  `tempfile`、matplotlib 非交互后端均正常。

### 3.2 MCP stdio（真实子进程 + 真实 JSON-RPC）

```powershell
.\.venv\Scripts\python.exe scripts\verify_mcp_stdio.py
```

```
结果：24/24 项通过，0 项失败，用时 7.25s
```

覆盖：legacy initialize 握手、18 工具与 schema、instructions 红线、无奥维时结构化失败、
stdout 逐行 JSON-RPC、`--dump-tools` 单行 JSON、假 PSK 不泄漏、关闭 stdin 后 exit 0。

### 3.3 模拟奥维完整闭环

```powershell
.\.venv\Scripts\python.exe scripts\verify_mock_loop.py --skip-grass
```

```
结果：68/70 通过，2 失败，0 警告，用时 66.7s
```

- 通过项包含：真实 mock 子进程登录、写草稿/幂等/回读/回滚、断线与超时与重复响应、
  GIS 掩膜逐像元复算（360/720 像元，面积 323,999.99999965425 m²）、确定性 SHA256、
  MCP Image 真实 PNG、复核图排版、导出、凭据不泄漏、stdout 纯净。
- 2 项失败为 **E6/E6b**：脚本要在本机 DSH attachment 库中找回 macOS 那次视觉跑的 5 张图，
  本机不存在（transcript 已注明附件被清理）。属于**证据不可用**，不是产品缺陷；其余 68 项
  已独立证明图片真实传递与排版。

### 3.4 DSH patch 组合

```powershell
.\.venv\Scripts\python.exe scripts\verify_dsh_patch.py --dsh 'D:\deepseekHarness\resources\runtime\cli\bin\dsh.cmd'
```

```
结果：9/10 通过，1 失败，1 跳过，用时 1.0s
失败项：A1 本机 dsh CLI 版本 0.2.1-rc.3 ≠ 交付声明 0.1.5-rc.1
跳过项：C1 未能定位 dsh 安装包目录，无法用已安装插件 schema 校验
```

- 关键通过项：patch 是 YAML 数组 + 单条 `insert`、字段正确、无 `mcpServers`、无明文 PSK；
  隔离 `DSH_HOME` 下 `--dump-config` 成功组合出 `mcp-ovital`；对照组无该行。
- 说明：**patch 结构本身在 Windows/新版本 DSH 上可组合**；版本号差异是唯一失败点。

### 3.5 DSH 真实启动 MCP（headless）

用本机实际路径生成 `ovital_mcp/dsh/ovital.cordis.local.yml`，执行：

```powershell
dsh --profile headless --patch D:\omap-watershed-mcp\ovital_mcp\dsh\ovital.cordis.local.yml "<prompt>"
```

- `--dump-config` 组合成功（`command=D:/omap-watershed-mcp/.venv/Scripts/python.exe`，
  `cwd`、`OVITAL_MCP_CONFIG` 均正确）。
- headless 启动后，DSH **实际拉起了 MCP 子进程**，日志可见：
  `starting ovital-mcp 0.1.0 ... config=D:\omap-watershed-mcp\config\ovital.yaml` 与
  `registered tools: <18 个>`。
- 模型回合因本机 CLI 凭据缺失而中止：
  `MISSING_CREDENTIAL: llm-deepseek: no API key for provider route "deepseek-official"`。
  GUI 使用的是账号平台凭据（`deepseek-account-platform`），CLI headless 没有对应 key，
  因此**未能**在 Windows 上完成“模型发现 18 工具并实际调用”的闭环；E1/E2 仍为部分完成。

## 4. 真实奥维联机

### 4.1 客户端设置（实测界面，与手册文字有差异）

奥维 10.7.5 的实际路径是：**系统 → 系统设置 → 高级 → 第三方接口**，下拉项为：

```
默认(窗口消息) / 不启用 / 窗口消息 / Web接口 / 窗口消息 + Web接口
```

没有手册所述的 “WebSocket(手册)” 选项。选择 **Web接口** 后，右侧 `..` 打开
**Web接口设置**：

```
服务选项: 不启用 → 启用WebSocket协议
服务器端口号: 0 → 7699
[✓] 启用HTTP WebSocket接口
[ ] 启用SSL
```

保存并确定后，`Get-NetTCPConnection` 实测 `0.0.0.0:7699 LISTEN`（PID 39056）。
**该版本 Web接口设置中没有预共享密钥（PSK）字段。**

> 设置变更已征得用户同意；本次把“第三方接口”改为 Web接口并监听 7699。

### 4.2 登录与能力探测

用 `ws://127.0.0.1:7699`、任意测试 PSK 调用 `get_capabilities`：

| 字段 | 实测值 |
|---|---|
| `connection.available` | `true` |
| `connection.version.value` | **1075**（`20006` 返回版本号） |
| `connection.app_id` | `"41"` |
| `connection.simulated` | `false` |
| `vip.OmapLevel` | **0** |
| `vip.LoadOK` | 1 |
| `system_crs.coord_type` | 2（geographic） |
| `20104`（选中对象） | **ret=-5** 不是 VIP 用户 |
| `20301`（地图图片） | **ret=-7** 不是 SVIP |
| `20303`（等高线） | **ret=-7** 不是 SVIP |
| `viewport(20730+20016+20212)` | ok |

**PSK 发现（A4）**：由于客户端没有 PSK 字段，本次用任意 32 位随机 PSK 计算 `md5psk`
仍登录成功（ret=1075）。说明该版本 **不校验手册所称的预共享密钥**；`md5psk` 字段与派生
算法在 10.7.5 上不构成鉴权。此结论与手册 V2.7 不一致，已记入待核验项。

### 4.3 读写负向路径（账号非 VIP）

| 工具 | 奥维命令 | 结果 |
|---|---|---|
| `get_selection` | 20104 | `status=error`，`code=OVITAL_PREREQ_ERROR`，`ret=-5`，`retryable=false` |
| `read_object(obj_id=1)` | 20101 | 同上 |
| `write_draft(geojson, ...)` | 20112 | 同上；**未创建任何对象/文件夹** |
| `measure_geometry(geojson)` | —（本地） | `status=ok`，1,000,229.8758884581 m²，EPSG:32648 |

价值：证明了**真实设备上的错误语义、结构化错误码、不崩溃、不盲目重试、不产生半成品**；
但没有证明“成功读对象/成功写草稿/回读/量算/回滚”。

## 5. 发现并修复的问题

### 5.1 Windows 路径写进 YAML 双引号导致 16 项测试失败（已修复）

初跑：`17 failed, 324 passed, 8 skipped`。其中 16 项失败根因相同：
测试/验证脚本用 `f'  data_root: "{tmp_path}"'` 拼 YAML，Windows 路径 `D:\...` 在 YAML
双引号标量里是非法转义（`found unknown escape character 'o'`），配置加载直接 `CONFIG_ERROR`。

修复：用 `json.dumps(str(path))` 生成 YAML 标量（JSON 字符串是合法的 YAML 双引号标量，
反斜杠会被正确转义）：

| 文件 | 行 |
|---|---|
| `tests/test_server.py` | 49 |
| `tests/test_config.py` | 30–33（同时覆盖 `psk` / `psk_file`） |
| `tests/integration/test_stdio_client.py` | 210 |
| `scripts/verify_stdio_harness.py` | 129 |
| `scripts/verify_mcp_stdio.py` | 459 |

### 5.2 模拟奥维独立进程无法在 Windows 优雅停止（已修复）

原测试用 `Popen.send_signal(SIGTERM)`；Windows 上 Python 会走 `TerminateProcess`（强杀），
子进程退出码 1，`收到停止信号` 永远不会打印，测试必然失败（第 17 项失败）。

修复：
- `scripts/run_mock_ovital.py` 新增 `--stop-file`（异步轮询，跨平台触发同一
  `_request_stop` 路径），并注册 `SIGBREAK` 处理器（Windows 进程组 Ctrl+Break 可优雅停止）；
- `tests/test_mock_server.py` 在 `os.name == "nt"` 时写停止文件，POSIX 仍用 SIGTERM。

### 5.3 `%TEMP%` 中的陈旧 `pytest-current` 重解析点导致 pytest 收尾崩溃（环境问题，已规避）

```
PermissionError: [WinError 5] 拒绝访问。: 'C:\Users\...\Temp\pytest-of-...\pytest-current'
```

该目录项是 2026-10-09 遗留的目录符号链接（目标 `..`），`fsutil reparsepoint query` 也被拒绝，
当前用户无权删除。pytest 在 sessionfinish 清理死链接时对其 `stat` 失败，导致
“测试全跑完但进程异常退出”。规避：显式 `--basetemp=.cache\win-test\pytmp-*`（本次所有全量
运行均如此）。这是本机环境问题，不是项目代码问题；建议 Windows 用户遇到时同样指定
`--basetemp`，或由管理员删除该遗留链接。

## 6. 未完成项（继续保持 `real_device_pending`）

1. **成功的真实读写链路**：`read_object`/`get_selection`/`write_draft`/`read_back`/
   `finalize_draft`/`rollback_change` 均因账号非 VIP 未执行成功路径。
2. **VIP/SVIP 权限矩阵**：仅测到 `OmapLevel=0` 的拒绝返回；VIP/SVIP 账号下的
   `20104/20301/20303` 行为未知。
3. **PSK 语义**：10.7.5 无 PSK 字段且不校验 `md5psk`；其他版本/企业版是否一致未知。
4. **GRASS on Windows**：未安装，8 项测试跳过；`r.watershed`/`r.water.outlet` 未在 Windows 跑过。
5. **DSH 模型闭环**：headless 缺少 CLI 凭据，未完成“模型发现 18 工具并调用”；GUI 侧未验证。
6. **视觉链路**：未在 Windows 会话中让模型读取复核图（模拟闭环的图片传递已由 3.3 证明）。
7. **真实 DEM（用户数据）**：未提供，未做精度统计。

## 7. 复现命令

```powershell
# 0) 环境
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"

# 1) 全量测试（务必带 --basetemp，见 §5.3）
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest -o addopts="" -q -rf --basetemp=.cache\win-test\pytmp-full

# 2) stdio 契约
.\.venv\Scripts\python.exe scripts\verify_mcp_stdio.py

# 3) 模拟闭环（无 GRASS）
.\.venv\Scripts\python.exe scripts\verify_mock_loop.py --skip-grass

# 4) DSH patch
.\.venv\Scripts\python.exe scripts\verify_dsh_patch.py --dsh 'D:\deepseekHarness\resources\runtime\cli\bin\dsh.cmd'

# 5) 真实奥维（需先在客户端启用 Web接口/7699；PSK 文件在仓库外）
.\.venv\Scripts\python.exe .research\win-test\real_device_probe.py
.\.venv\Scripts\python.exe .research\win-test\real_device_read.py
.\.venv\Scripts\python.exe .research\win-test\real_device_write.py
```

证据目录（均被 `.gitignore` 忽略，仅本机留存）：

- `.cache/win-test/`：pytest 日志、依赖版本、真实联机 capabilities/读写结果、DSH headless 日志；
- `.cache/verify/`：stdio（24/24）、mock loop（68/70）、DSH patch（9/10）JSON 证据与 transcript。

## 8. 对仓库文档的影响

- `docs/version-record.md` 增加“Windows 实机”一节（本报告 §1）。
- `docs/待核验清单.md`：A1/A2/A3 标记为已完成；A4/A5/A6 标记为部分完成并写明实测结论；
  B/C/D/E 组其余项保持未完成。
- 其余“未在本机出现过的环境一律标未验证”的原则不变：本报告只覆盖上述 Windows 实机事实，
  不把 macOS 的离线证据迁移成 Windows 结论。
