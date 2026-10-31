# 版本与环境记录

本文记录交付验证时的实际环境与版本，供复现与排障。**未在本机出现过的环境（Windows、奥维客户端、DSH Windows 安装）一律标注“未验证”**，不把计划值写成已实现值。

采集时间：2026-10-30（开发机本地时间，Asia/Shanghai）。

## 1. 结论速览

| 项 | 值 | 来源 / 验证方式 |
|---|---|---|
| 开发机 OS | macOS 26.6.2（Darwin 25.2.0 / build 25G83），arm64 | `sw_vers`、`uname -m` |
| DSH | **0.1.5-rc.1** | `/opt/homebrew/bin/dsh --version` |
| DSH GUI | `http://127.0.0.1:64759`（desktop profile，Electron） | 本会话运行环境 |
| Node.js（DSH 依赖） | v26.3.0（npm 11.16.0） | `node --version` |
| 项目 Python | **3.13.13**（项目内 `.venv`） | `.venv/bin/python --version` |
| conda（本机） | 26.3.2（`~/miniconda3`，仅用于安装 GRASS） | `conda --version` |
| GRASS GIS | **8.5.0**（conda-forge `osx-arm64`，`grass-8.5.0-py314h2a8c62e_1`，环境内 Python 3.14.10） | `./.conda/grass/bin/grass --version`（需有效 `HOME`） |
| 奥维 WebSocket 手册 | **V2.7** | 手册标题与 URL：<https://www.gpsov.com/appdoc/index.php?id=32> |
| 奥维官方样例 | `omapInterfaceWeb_latest.zip`（本地文件 2,010,133 B；zip 内目录时间 2025-04-02，JS 文件 2024-06-03） | `.research/ovital-scratch/omapInterfaceWeb_latest.zip` |
| 真实 Windows + 奥维客户端 | **有（2026-10-30 实机实测）**：Windows 11 10.0.26200 + 奥维 10.7.5，Web接口监听 7699 | [win-test-report.md](win-test-report.md) |
| 奥维账号（VIP/SVIP） | **有账号但非 VIP**：`OmapLevel=0`；`20101/20104/20112` 实测 `ret=-5`，`20301/20303` `ret=-7` | 同上 |
| 用户 DEM / 参考边界 | **无** | 本机事实 |

## 2. Python 依赖（项目 `.venv`，已装并用于测试）

| 包 | 版本 | 说明 |
|---|---|---|
| Python | 3.13.13 | 本机开发用；Windows 部署建议 3.12（见 `environment.yml`） |
| mcp | 2.3.0 | 官方 MCP Python SDK（stdio 服务端） |
| mcp-types | 2.3.0 | SDK 依赖 |
| pydantic | 2.14.1 | 配置模型（`extra="forbid"`） |
| PyYAML | 6.0.3 | 配置/patch 解析 |
| numpy | 2.5.4 | native D8 计算 |
| scipy | 1.18.1 | 可选计算依赖 |
| rasterio | 1.5.2 | GeoTIFF 读写；底层 GDAL 3.12.2 |
| shapely | 2.2.0 | 几何校验/面积；GEOS 3.14.1 |
| pyproj | 3.8.0 | CRS 变换；底层 PROJ 9.8.1 |
| matplotlib | 3.11.2 | 复核图渲染（非交互后端） |
| pillow | 13.0.0 | PNG 处理/尺寸探测 |
| websockets | 17.3 | 奥维 WebSocket 客户端与模拟服务器 |
| pytest | 9.2.0 | 测试 |
| pytest-asyncio | 1.4.1 | 异步测试（交付期间由 Lead/verifier 安装） |

复现命令：

```bash
.venv/bin/python -m pip list
.venv/bin/python - <<'EOF'
import rasterio, shapely, pyproj
print("GDAL", rasterio.__gdal_version__)
print("GEOS", ".".join(map(str, shapely.geos_version)))
print("PROJ", pyproj.proj_version_str)
EOF
```

已知差异：

- `pytest-asyncio` 已安装（1.4.1）；`pyproject.toml` 的测试依赖含它，`environment.yml` 的 pip 段也已列出。
- `environment.yml` 的 conda 解析**未在本机验证**：本机沙箱禁止写 conda 的 envs/pkgs 缓存（`NoWritableEnvsDirError` / ToS 缓存不可写），只能确认 YAML 结构合法。Windows 上的实际解析结果属于待核验项（见 `docs/待核验清单.md` A 组/安装指南）。
- 以上版本是 **macOS arm64 + Python 3.13** 的解析结果；Windows + conda-forge 环境会解析出不同的 GDAL/PROJ/GEOS 组合，不能混用本机数字声称已验证（见 `docs/install-windows.md` 的锁版本策略）。

## 3. GRASS GIS（本机）

| 项 | 值 |
|---|---|
| 版本 | 8.5.0 |
| 安装方式 | conda-forge `osx-arm64`，安装到项目内 `./.conda/grass` |
| 可执行文件 | `./.conda/grass/bin/grass` |
| 环境 Python | 3.14.10（GRASS 环境自带） |
| 本机验证 | `HOME=<有写权限目录> ./.conda/grass/bin/grass --version` 输出 `GRASS 8.5.0`；合成 DEM 的 `r.watershed -s` → `r.water.outlet` → `r.to.vect` → `v.out.ogr` 手工链在本机跑通（临时目录 `.cache/grasstest/`，非交付物；其中 `run.log`/`run2.log` 是失败尝试，成功记录在 `run3.log`，没有被删除，避免伪造证据） |
| 未验证 | GRASS 引擎与 MCP 工具的集成测试结果、Windows 安装与路径、GRASS 在中文/空格路径下的行为 |

注意：GRASS 启动需要可写的 `HOME`/配置目录；本机在受限沙箱下直接运行会报 “Failed to create configuration directory”。部署说明与测试中应显式设置可写 `HOME` 或 GRASS 配置目录。Windows 上 GRASS 通常以 `grass.bat`/`grass84.bat` 提供，**具体启动器名称以安装产物为准，本项目未在 Windows 实机验证**。

## 4. DSH（本机）

| 项 | 值 |
|---|---|
| CLI | `/opt/homebrew/bin/dsh`，版本 0.1.5-rc.1 |
| 运行 profile | desktop（Electron 应用管理；`desktop` 为保留名，CLI 拒绝对其执行启动/`--dump-config`/插件管理） |
| GUI | `http://127.0.0.1:64759` |
| MCP 客户端包 | `@deepseek-ai/dsh-mcp-client` 0.1.5-rc.1（随 DSH 安装，未默认挂载） |
| Skill 发现 | `@deepseek-ai/dsh-skill-filesystem`：项目根（最近含 `.git` 的祖先，否则启动 cwd）下的 `.dsh/skills/<name>/SKILL.md` |
| 本机已验证 | ① patch 组合与 schema：隔离 `DSH_HOME` 下 `dsh --profile headless --patch <本仓库 patch> --dump-config` 成功组合出 `mcp-ovital`；verifier `scripts/verify_dsh_patch.py` 11/11 通过（含 mcp-client 真实 schema 校验、负向控制、未污染用户 `~/.dsh`）。② **真实 headless 会话**：发现并调用了 `mcp__ovital__get_capabilities`（奥维为模拟服务器，`simulated=true`）；通过测试 patch 的 `customSkillDirs` 成功加载 `ovital-watershed` Skill（模型复述红线/流程）；`deepseek-flash` 真实收到 `get_job`/`review_segment` 返回的 MCP Image 并完成像素级描述（最终代码树证据：`docs/evidence/dsh-headless-vision-e2e-final.out.txt`，vision4）。证据：`docs/evidence/README.md` 及其中 transcript |
| 本机未验证 | Windows DSH 安装与启动；真实 Windows 奥维数据下的 DSH 会话；**Web GUI 选中 workspace 后的项目 Skill 自动发现**（headless 无交互 workspace，本机用 `customSkillDirs` 验证）；真实 SVIP 地图图片/等高线的视觉复核 |

> 说明：`--dump-config` 只证明配置组合通过，不证明插件能成功启动或工具能注册；真实发现需 DSH 会话验证（见 `docs/待核验清单.md` E 组）。

## 5. 奥维协议与官方样例

| 项 | 值 |
|---|---|
| 手册标题 | 奥维互动地图WEB开发工具接口（V2.7） |
| 手册 URL | <https://www.gpsov.com/appdoc/index.php?id=32> |
| 第三方接口总览 | <https://www.ovital.com/openapi/> |
| 官方样例 URL | <https://download.ovital.com/pub/api/omapInterfaceWeb_latest.zip> |
| 本地样例 | `.research/ovital-scratch/omapInterfaceWeb_latest.zip`（含 `js/omapinterface.js`、`js/omapobj.js`、示例页面） |
| 手册关键结论 | 地图图片/等高线需 SVIP 及以上，其他至少 VIP（2.1.1）；端口可配置、示例 7699 非默认（2.1.2）；登录 `md5(psk).upper()[:16]`（2.1.3.5）；四套坐标枚举（2.2.2/2.4.1/2.4.8） |
| 本机状态 | **无 Windows 奥维客户端、无账号**，真实联机与权限探测未完成 |

样例 zip 的下载时间与内容以实际解压为准；不得用样例代替目标安装版本的实测响应。

## 6. 数据与参考

| 项 | 值 |
|---|---|
| 用户 DEM | 无（需项目提供带坐标系的 GeoTIFF） |
| 用户参考边界 | 无（边界精度/距离/重叠率验收未完成） |
| 公开数据（可选） | Copernicus GLO-30：约 30m，产品定义为 **DSM**（含建筑/植被），无独立审核边界；不能当裸地 DTM |
| 本机测试地形 | 合成 DEM（可解析真值）+ 公开真实 DEM 可复现性检查（见 `docs/gis-notes.md`） |

## 7. 测试状态

```bash
# 全量测试（交付前最终状态；其他模块测试由各自负责人维护）
.venv/bin/python -m pytest -q
# DSH 接入文件校验（本任务）
.venv/bin/python -m pytest tests/test_dsh_patch.py -q
```

最终门禁数字（Lead 2026-10-30 确认，与实现方/验证方一致）：**全仓 349 passed**；`tests/gis` **75 passed**（GRASS 8.5.0 真实运行、无 skip）；`tests/test_dsh_patch.py` **25 passed**。测量环境：macOS 26.6.2 arm64、项目 `.venv`。复现：`.venv/bin/python -m pytest -q -o addopts=""`（最近一次本机运行约 25–27 s，仅供参考）。

| 测试范围 | 结果 | 备注 |
|---|---|---|
| `tests/test_dsh_patch.py` | 25 passed（本机执行，2026-10-30） | patch 结构/config 示例/SKILL frontmatter/18 工具同步 |
| 协议/模拟（Teammate A） | 175 passed（`pytest tests/test_protocol_*.py tests/test_mock_server.py -q`，2026-10-30） | 证据等级 `offline_simulated`；不代表真实联机 |
| GIS（Teammate B） | **75 passed**（`pytest tests/gis -q`，GRASS 8.5.0 真实运行、无 skip；2026-10-30） | 证据等级 `offline_computed`；证据与限制见 `docs/gis-notes.md` |
| DSH headless 端到端（Lead 证据） | 通过（本机 headless + 模拟奥维 + 合成 DEM）；工具发现、Skill 加载、视觉收图均有 `.out.txt` 证据；最终代码树视觉证据为 `dsh-headless-vision-e2e-final.out.txt`（vision4），图样/元数据见 `docs/evidence/images/manifest.json` | 证据等级仍为 `offline_simulated`/`offline_computed`，不是真实设备验收；见 `docs/evidence/README.md` |
| 集成/独立验证（Teammate D） | 见 `docs/offline-acceptance.md` 与 `docs/verification-findings.md` | 真实设备项仍 `real_device_pending` |

## 8. 已知环境限制（影响结论）

1. **无 Windows 奥维**：所有 `real_device_pending` 项只能留待真实环境验收，不能用 mock 通过代替。
2. **无用户 DEM/参考边界**：无法给出工程精度结论；只能给出算法可复现性与合理性检查。
3. **无 SVIP 账号**：地图图片（20301/20302）与等高线（20303）未实测；`get_scene` 只能验证错误路径与模拟返回。
4. **DSH Web GUI 会话未做视觉链路验证**：本机 headless 会话已验证 MCP Image 真实到达并被模型像素级判读（证据 `docs/evidence/dsh-headless-vision-e2e-final.out.txt`），但 Web GUI 选中 workspace 后的 Skill 自动发现与视觉会话、Windows 实机视觉复核仍未完成。
5. **GRASS 需可写 HOME**：受限环境需显式配置，否则启动失败。

## 9. 变更记录

| 日期 | 变更 | 影响 |
|---|---|---|
| 2026-10-30 | 首次记录：DSH 0.1.5-rc.1、手册 V2.7、Python/依赖/GRASS 8.5.0 版本 | 交付基线 |
| 2026-10-30 | 补 Windows 实机版本：Windows 11 26200、Python 3.13.12、奥维 10.7.5、DSH CLI 0.2.1-rc.3、账号 OmapLevel=0 | 见 §10 与 [win-test-report.md](win-test-report.md) |

## 10. Windows 实机（2026-10-30）

本次在真实 Windows 上执行了完整离线测试与真实奥维握手/权限探测，详细记录见
[win-test-report.md](win-test-report.md)。要点：

| 项 | 值 |
|---|---|
| OS | Windows 11 家庭版 中文版 10.0.26200（build 26200）x64 |
| Python | 3.13.12，项目内 `.venv`（pip + wheel，**未用 conda-forge**） |
| 原生栈 | rasterio 1.5.2 / GDAL 3.12.2；shapely 2.2.0 / GEOS 3.14.1；pyproj 3.8.0 / PROJ 9.8.1 |
| 其他 | numpy 2.5.4、scipy 1.18.1、matplotlib 3.11.2、pillow 13.0.0、websockets 17.3、mcp 2.3.0、pytest 9.2.0 |
| 奥维 | **10.7.5**（`D:\omap\omap.exe`），已登录；第三方接口实测为「Web接口 + 启用HTTP WebSocket接口」，端口 7699 |
| DSH CLI | **0.2.1-rc.3**（`D:\deepseekHarness\resources\runtime\cli\bin\dsh.cmd`），非交付声明的 0.1.5-rc.1 |
| GRASS | **无**（8 项 GRASS 测试 skip） |

测试结果：

```text
pytest                    341 passed, 8 skipped（GRASS 不可用）  74.74s
scripts/verify_mcp_stdio  24/24 PASS
scripts/verify_mock_loop  68/70 PASS（E6/E6b 缺 macOS 附件，环境性失败）  66.7s
scripts/verify_dsh_patch  9/10 PASS, 1 SKIP（A1 版本号 0.2.1-rc.3；C1 找不到插件包目录）
真实奥维 20000 登录        成功，版本 1075，app_id 41，simulated=false
真实奥维 VIP 权限          OmapLevel=0；20101/20104/20112 ret=-5；20301/20303 ret=-7
```

**仍未完成**：任何成功的真实对象读写（账号非 VIP）、VIP/SVIP 权限矩阵、GRASS on Windows、
Windows DSH 模型会话的工具发现/调用（headless 缺 CLI 凭据）、真实 DEM 精度验收。
`verified_against_real_device` 在任何产物中继续保持 `false`。

Windows 上发现并已修复的项目问题：测试/验证脚本把 Windows 路径写进 YAML 双引号标量
（16 项失败）；模拟奥维独立进程在 Windows 无法用 `send_signal(SIGTERM)` 优雅停止。
细节与行号见报告 §5。
