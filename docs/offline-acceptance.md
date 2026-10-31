# OMAP Watershed MCP 离线验收报告（独立验证，最终定稿）

> 独立验证者（Task-4 / verifier）产出。**最终证据运行：2026-10-30 02:07（Asia/Shanghai），最终代码树（含排版 vision4 定稿 + export 报告修复）**。
>
> - 只声明**本机可验证的离线结果**；`real_device_pending` 项见 §5（均未完成）。证据分级：`official_document` /
>   `offline_simulated` / `offline_computed` / `real_device_pending`（定义见 `docs/architecture.md` §9）。
> - 验证者未修改业务源码/配置；脚本 `scripts/verify_*.py`、`tests/integration/**`。配套：`docs/verification-findings.md`、`docs/待核验清单.md`、`docs/gis-notes.md` §8.3。

## 0. 结论速览

| # | 验收面 | 结论 | 最终证据（本机可复现） |
|---|---|---|---|
| 1 | MCP stdio 真实握手与工具契约（18 工具/instructions 红线/schema/stdout 纯净） | **通过 24/24** | `scripts/verify_mcp_stdio.py` → `.cache/verify/stdio-evidence-latest.json`（sha256 `66d64f1a…`） |
| 2 | 模拟奥维闭环（真实 `run_mock_ovital.py` 子进程 + 真实 MCP stdio 客户端） | **通过**（A 组 24 项） | `scripts/verify_mock_loop.py`（84/84） |
| 3 | 断线/超时/重复响应/取消/幂等/安全/凭据 | **通过**（B+F 组 22 项） | 同上 |
| 4 | GIS 独立复算（合成解析真值逐像元掩膜/面积/确定性） | **通过**（C 组 24 项） | 同上 |
| 5 | GRASS 引擎真实性（provenance 命令/返回码/版本，非 native 冒充） | **通过**（D 组 15 项，`GRASS 8.5.0`） | 同上 |
| 6 | 图片真实传递 + 复核图排版（bbox/view_bbox/排版可读性/像素统计） | **通过**（C16/E/F 组 + verifier 目视） | 同上 + `docs/evidence/images/` |
| 7 | DSH patch 组合 + 插件 schema + 真实 headless 工具发现/Skill/视觉 | **通过（本机 headless；Lead 执行 + verifier artifact 复核）** | `scripts/verify_dsh_patch.py`（11/11，sha256 `7531dbdc…`）、`docs/evidence/dsh-headless-vision-e2e-final.out.txt` |
| 8 | 全量测试 | **通过**：**349 passed**（26.9s）；`tests/gis` **75 passed**（GRASS 无 skip）；`tests/integration` 10 passed | `pytest -o addopts='' -q` |
| 9 | 真实公开 DEM（SRTM 30 m）链路样例（补充证据，无独立参考边界） | **通过（链路可运行）**；不证明工程正确性 | Lead 证据 + verifier 独立复核（§3.4b）；`docs/real-dem-run.md` |
| 10 | 真实设备 / 用户数据验收 | **未完成（real_device_pending）** | 见 §5 |

**总判定**：本机离线闭环全部通过（阶段 2 证据脚本 **84/84 checks PASS，0 失败**，26.6s，sha256 `5d948666…`）。
复核图排版经 **v1→v4 共 4 轮**（DSH 视觉模型逐轮反馈）后定稿：bbox 语义、外扩显示范围、面板布局、刻度可读性均通过像素级回归。
真实 Windows 奥维联机与用户数据精度验收**未完成**；已知外观局限见 §6.2，不构成失败项。

## 1. 测试环境与版本（本机实测）

| 项 | 值 | 采集方式 |
|---|---|---|
| 开发机 | macOS 26.6.2 arm64（Darwin 25.2.0） | `sw_vers`、`uname -m` |
| 项目 Python | 3.13.13（`.venv`） | `.venv/bin/python --version` |
| mcp SDK | 2.3.0（stdio 服务端 + 客户端） | `.venv/bin/python -m pip list` |
| pytest / pytest-asyncio | 9.2.0 / 1.4.1 | 同上 |
| WebSocket | websockets 17.3 | 同上 |
| GIS 库 | rasterio 1.5.2（GDAL 3.12.2）、shapely 2.2.0（GEOS 3.14.1）、pyproj 3.8.0（PROJ 9.8.1）、numpy 2.5.4、scipy 1.18.1 | import 取值 |
| 渲染 | matplotlib 3.11.2、pillow 13.0.0 | pip list |
| GRASS | 8.5.0（conda-forge osx-arm64，项目内 `.conda/grass`） | `HOME=<可写目录> ./.conda/grass/bin/grass --version` |
| DSH CLI | 0.1.5-rc.1（`/opt/homebrew/bin/dsh`）；`@deepseek-ai/dsh-mcp-client` 0.1.5-rc.1 | `dsh --version` |
| 奥维客户端 / 账号 | **无**（无 Windows、无客户端、无账号） | 本机事实 |
| 用户 DEM / 参考边界 | **无** | 本机事实 |

## 2. 验证方法与独立性声明

1. **真实进程、真实协议**：模拟奥维是独立子进程（`scripts/run_mock_ovital.py --port 0 --ready-file`），
   MCP 服务也是独立子进程；中间是真实 WebSocket 与真实 MCP stdio JSON-RPC（逐行 transcript 已存证据）。
2. **GIS 独立复算**：用 `scripts/make_synthetic_dem.py` 的**解析真值**（地形公式推导的掩膜像元集合/面积，
   非被测实现生成）逐像元比对掩膜、`area_m2 == 像元数 × cell_area`、两次运行掩膜 SHA256 一致。
3. **验证有效性防护**：`start_watershed` 对相同输入自动幂等；本 harness 所有任务使用**本次运行唯一
   `idempotency_key`**，确保验证的是最终代码树新跑的任务，而非命中历史任务。
4. **GRASS 真实性**：从任务 `provenance.json` 核对实际执行命令数组、可执行文件、返回码、
   `r.watershed -s`/`r.water.outlet` 脚本内容与 `GRASS 8.5.0` 版本；不接受 `engine_used=grass` 单字段自证。
5. **图片真实性 + 排版**：MCP `CallToolResult` 的 Image 块必须 base64 解码为 PNG magic、PIL 可解码、
   尺寸=工具元数据、像素非空白；排版做**像素级独立统计**（地图面板宽/高占比、连续白带、x 轴刻度簇间距），
   不只信渲染器自报的 `layout`；DSH 侧核对 attachment 对象哈希（§3.6）。
6. **诚实边界**：DSH 模型会话由 Lead 执行，verifier 无 API Key、**未重跑模型**，只做 transcript/产物/attachment 复核 →
   证据等级="Lead 提供的 agent 执行证据 + verifier artifact 复核"，headless + 模拟奥维 + 合成 DEM，**不是真实联机**；
   不可得数据（真实 Windows 奥维、VIP/SVIP 账号、用户 DEM 与审核边界、真实地图/等高线接口）不用于推断。

## 3. 逐项证据与实测结果

### 3.1 MCP stdio 协议层（24/24 PASS）

| 检查 | 实测结果 |
|---|---|
| 真实 `initialize` 握手（mcp SDK Client → 子进程 `python -m ovital_mcp`） | 协议版本 `2025-11-25`；服务 `ovital-watershed` 0.1.0 |
| instructions 红线 | 含"不得为了凑目标面积而修改边界""不得伪造坐标""verified_against_real_device"等 |
| `tools/list` | **恰好 18 个**工具，与 `docs/architecture.md` §6 一致；全部 `inputSchema.type=object` 可解析 |
| 无奥维 `get_capabilities` | `status=ok`、`connection.available=false`、`error.code=OVITAL_CONNECTION_ERROR`、0.03s |
| provenance 诚实性 | `simulated=false`、`verified_against_real_device=false`、`unverified[]` 10 项 |
| 无 PSK 配置启动 | 服务可启动，报 `CONFIG_ERROR`（DSH 探测进程兼容，非崩溃） |
| 错误路径 | `get_selection`→`OVITAL_CONNECTION_ERROR`；缺 DEM→`DATA_VALIDATION_ERROR`；之后服务仍 `ok` |
| stdout 纯净 | 服务端输出全部为 JSON-RPC 2.0（0 非协议行）；stderr 日志、无 Traceback；exit=0 |
| 凭据 | 随机假 PSK 不出现在协议帧/日志/证据 |
| 诊断入口 | `--dump-tools` stdout 仅 1 行 JSON（18 工具），日志只走 stderr |

### 3.2 模拟奥维闭环（A 组 24 项 PASS）

| 检查 | 实测结果 |
|---|---|
| 模拟服务器 | 真实子进程启动，ready JSON `simulated=true`，随机端口 |
| `get_capabilities` | `available=true`、`simulated=true`、版本 817、`evidence_level=offline_simulated`、`verified_against_real_device=false` |
| 命令探测 | 7 个探测组（覆盖 20006/20501/20222/20104/viewport/20301/20303）全部 ok |
| `get_selection` | `status=ok`、`count=0`+warning（mock 无预置选中：**限制如实标注**，非空选中路径未覆盖） |
| `get_scene` | MCP Image：PNG 707×706、18,513 B；PNG tEXt `Description="SIMULATED MAP - NOT REAL OVITAL DATA"`、`Simulated=True` |
| `write_draft` | 独立文件夹+图形+出口标签；写前快照 snapshot_id；写后回读顶点 `max_delta_deg=0.0` |
| 幂等重复写 | 同一 `obj_id`、`reused=true`，不重复创建 |
| `read_object` | 名称/4 顶点/EPSG:4490/cgcs2000；轴序声明"内部统一 (lon,lat)、奥维 Shape 为 lat,lng 扁平" |
| `read_back` | 顶点数一致；20802 量算 950,881.95 m²（模拟世界局部米制投影真算）；面积口径差异显式说明 |
| `finalize_draft` | 无 `confirm=true` → `DATA_VALIDATION_ERROR`；`confirm=true` → ok 并记录版本/change_id |
| `rollback_change` | 删除本次创建 folder+shape+sign；再次回滚幂等；回滚后 `read_back`→`OVITAL_NOT_FOUND` |

### 3.3 故障注入 / 取消 / 幂等 / 安全（B+F 组 22 项 PASS）

| 场景 | 实测结果 |
|---|---|
| 断线（`drop-on-write`） | `status=error`+`OVITAL_CONNECTION_ERROR`，消息明确"协议无请求 ID，不能自动重试"；mock 只收到 1 次 20112（**未盲目重发创建**） |
| 超时（`timeout-on:20112`） | `OVITAL_TIMEOUT`，2.04s 返回（request_timeout 2s）；mock 只收到 1 次写 |
| 重复响应（`duplicate-20112`） | 安全去重：首次写成功；同幂等键重试 `reused=true`、不再发写请求（共 2 次 20112=建文件夹+写图形） |
| 取消运行中任务 | 终态 `cancelled`、`error.code=JOB_CANCELLED`；重复取消幂等 |
| `start_watershed` 幂等 | 相同输入+参数返回同一 `job_id`；显式不同 `idempotency_key` 新建任务 |
| 凭据/伪造 | 全量产物 0 处伪造 `verified_against_real_device=true`；工具输出/日志/任务文件/JSON 证据无 PSK 明文；模拟+伪造配置标志仍 false |
| stdout | 每个会话逐行 JSON-RPC，0 非协议行 |

### 3.4 GIS 独立复算（C 组 24 项 PASS）

场景 A：`two_bowl_ridge`（60×12，EPSG:32650，30 m；解析真值：列 0..29 全像元 = 360 cells = 324,000 m²）

| 检查 | 实测结果 |
|---|---|
| `inspect_dem` | CRS=EPSG:32650、60×12、30 m、SHA256=文件实算一致 |
| native 集水区 | `succeeded`、`engine_used=native`、`engine_version=native-d8/1 (numpy 2.5.4)`、面积 **323,999.999999 m²**（相对真值误差 2.4e-15） |
| **掩膜逐像元复算** | 360/720 像元；逐像元等于解析规则（columns≤29）——非只对总数 |
| 面积自洽 | `area_m2 == mask_cells × 900 m²`；边界 `valid/闭合/单部件/无洞`；`measure_geometry` 面积一致 |
| 确定性 | 两次运行掩膜 SHA256 完全一致 `d14152885c92f20b…`，面积一致 |
| `get_job(include_images=true)` | 2 张 MCP Image（≤2 契约上限），`review_figure_count=5`；PNG 可解码、尺寸=figure 元数据（本次 overview 1184×433 / seg-01 1171×557） |
| `review_segment` | Image + 段统计 + `all_segments`=4 + 稳定锚点 ID；`figure.bbox_lonlat` 与 `segment.bbox_lonlat` 逐值一致（EPSG:4490，F-11 已修复） |
| F-11/排版回归 | `figure.bbox_lonlat`=真实段 bbox 且为经纬度；`view_bbox_lonlat` 为外扩显示范围且 ⊇ bbox；信息面板在图外、比例尺/指北针在地图内、面板高度占比 61.4%–78.4%、图幅纵横比 2.10 等（`description` 见 `docs/gis-notes.md` §8.3） |
| 导出 | geojson 坐标 EPSG:4490 经纬度、kml 合法、report 存在（修复后报告列出复核图清单 overview+seg-01..，已独立复测）、projected_json 可用；**gpkg 缺 fiona 显式 `EXPORT_ERROR`**（无静默降级）；返回 SHA256 |
| `revise_candidate` | 新任务+父子面积/对称差/Hausdorff 报告（不静默改边界） |

场景 B：`single_bowl`（41×41 正方形 DEM，产生竖版分段图；验收口径按 Lead 最终裁定）

| 图 | 画布 | 纵横比 | 地图面板宽 | 面板高度占比 | x 刻度簇/间距 | 布局 | 结论 |
|---|---|---|---|---|---|---|---|
| overview | 1011×1078 | 0.938 | 899 px | 83.4% | 7 / 128.4 px | inside（图内信息卡） | 合格 |
| seg-01（横） | 1225×753 | 1.627 | 1116 px | 77.3% | 6 / 178.2 px | stack | 合格 |
| seg-02（竖） | 859×929 | 0.925 | 429 px | 83.6% | 4 / 107.2 px | side（地图左+信息右） | 合格 |
| seg-03（横） | 1226×727 | 1.686 | 1117 px | 76.5% | 7 / 152.7 px | stack | 合格 |
| seg-04（竖） | 848×929 | 0.913 | 410 px | 83.6% | 6 / 68.3 px | side | 合格 |

C16 综合可读性断言（全部满足）：地图面板宽 ≥400px、高度占比 ≥55%、x 轴刻度间距 ≥55px 或旋转无重叠、
信息面板/比例尺/指北针/坐标轴互不遮挡、无 >15% 连续纯白带。结果 **PASS**（竖版 side 布局右侧信息栏下方留白
按"地图面板外区域"记录，不作为失败；见 §6.2）。

### 3.4b 真实公开 DEM（SRTM 30 m）链路样例（补充证据；**无独立参考边界**）

> 来源：Lead 运行记录 `docs/real-dem-run.md` + 归档 `docs/evidence/real-srtm-*`（含 F-14 修复后报告与 job-result 摘要）。
> **定性：offline_computed 补充链路样例，不是工程精度验收。**

- **数据/结果**：SRTM 1″ `N39E115`（AWS elevation-tiles-prod，约 30 m；`.hgt`→GeoTIFF EPSG:4326，SHA256 `f4bd6149…`）；
  出口为 D8 累积自动选点（吸附 16.13 m、累积 25,857 像元，**非用户坝址**）；`native`(D8)、EPSG:32650/27.58 m，
  **面积 19,673,902.93 m²**、周长 28,797.63 m、608 唯一顶点/1 部件/0 洞、`truncated=false`；9 张复核图（overview+seg-01..08）。
- **verifier 独立复核**（清理前读原始产物）：GeoJSON→pyproj→shapely 复算面积 19,673,902.93 m²（相对差 0.00）、周长/几何一致；
  任务字段 `snap=16.126`/`accum=25857`/`native`/`EPSG:32650` 一致；9 图 SHA256/bytes 全部一致。
- **F-14 修复与交叉复现（02:14 复现运行 `j-…181428`）**：修复后报告列出 9 张复核图（SHA256 `daab4254…`）；两次独立运行
  **任务产物 `boundary.geojson` 逐字节相同（`8d791669…`）、几何坐标哈希 `e66dda7b…` 一致、9 图逐字节相同、面积/周长逐位一致、
  DEM SHA256 相同**；`export_result` 导出 geojson 含 job_id/task_id（`f53b3ea3…`，不随任务产物归档）；job 摘要归档
  `docs/evidence/real-srtm-job-result.json`（`116987ab…`）。
- **边界**：无独立人工审核参考边界，未计算面积差/边界距离/正确率；SRTM 雷达地表模型、垂直基准未知 → 不能支撑工程精度；
  报告 §3 图路径指向 `.cache`（将被清理），稳定引用以 `docs/evidence/images/real-srtm-*` + manifest 为准。
### 3.5 GRASS 引擎真实性（D 组 15 项 PASS）

| 检查 | 实测结果 |
|---|---|
| GRASS 可用性/版本 | `./.conda/grass/bin/grass --version` → `GRASS 8.5.0`（原始输出已录制） |
| GRASS 任务 | `engine_used=grass`、`engine_version="GRASS 8.5.0"`；`tilted_plane` 面积 **53,999.999999 m²**（解析真值 54,000） |
| **provenance 三阶段命令** | `create_location`/`watershed_sfd`/`water_outlet` 全部 rc=0；命令可执行文件=本机 `.conda/grass/bin/grass`；脚本含 `r.watershed -s`、`r.water.outlet`、`r.to.vect`/`v.out.ogr`；记录 `HOME`/`GRASS_CONFIG_DIR` 覆盖 |
| 掩膜自洽 | GRASS 掩膜像元数 × 900 == GRASS 面积 |
| 引擎差异（如实记录） | `single_bowl`：GRASS(SFD) **1,189,800 m²** vs native(D8) 解析真值 **1,512,900 m²**（差 -323,100）。两算法/口径不同，**不声明等价**；`docs/gis-notes.md` 已定义各自验证口径 |
| 不可用路径 | `engine=grass` + 无效 `grass_cmd` → 任务显式 failed 且 `job.error.code=ENGINE_UNAVAILABLE`（F-12 已修复）；`engine=auto` → 回退 native 并记录实际引擎 |

### 3.6 图片内容真实传递（E+F 组 17 项 PASS + verifier 目视）

1. **MCP 协议层**：`get_job`/`review_segment` 返回 Image 内容块（PNG magic/可解码/尺寸=元数据）；复核图非空白
   （像素标准差≈41–45，256 灰度级）；overview 与分段图 SHA256 不同（非复用同一张）。
2. **verifier 目视（`read_image` 读同源 PNG）**：概览图含标题、信息面板（CRS/范围/像元/面积/周长/等高距/出口/引擎/关键警告）、
   等高线高程标注、蓝色河网、红色闭合边界、红色星形"出口"、指北针 N/网格北、比例尺、编号分段；
   分段图含外扩地形上下文、橙色高亮分段、锚点 ID、比例尺/指北针不被信息面板遮挡、信息面板（含 view bbox 说明）。
3. **DSH 最终视觉链路（vision4，最终代码树；证据保留说明见下方注）**：
   - `docs/evidence/dsh-headless-vision-e2e-final.out.txt`（sha256 `347febe0…`）：模型真实收到并查看 3 张 MCP Image，
     给出尺寸/地图占比/空白判断、bbox 与 segment 逐值一致且在经纬度范围、view_bbox 包含 bbox、
     面积 1,512,900 与 `measure_geometry` 一致、`simulated=true`/`offline_computed` 区分、并如实指出
     "边界=DEM 边缘、不能当真实分水岭"；
   - **attachment 独立验证**：模型/Transcript 声称的 5 个 sha256 均在本机 DSH attachment 库找到、内容哈希自洽；
     同一视觉跑时间窗内新增的 attachment 尺寸与任务 figure 元数据一致（overview/分段图确实被附件层接收）；
   - **口径修正**：attachment sha256 是 DSH 附件层转码 WebP 内容哈希，**不等于**原始 PNG SHA256（原始见 `docs/evidence/images/manifest.json`）；
   - 排版迭代：v1→v2→v3→**v4（final）** 共 4 轮，vision4 对应最终代码树；中间 transcript 保留于 `docs/evidence/`。

> **证据保留如实说明**：Lead 提取证据后清理了 `.cache/dshtest`（18 工具注册日志/任务 result.json）与 `.dsh-home` 附件库；
> verifier 清理前已独立复核 5/5 attachment 哈希；清理后仍可复核交付 transcript、5 张证据图+manifest（SHA256/bbox/layout 一致）、
> `~/.dsh` 中仍保留的 2/5 attachment，以及 vision4 时间窗内与交付图尺寸一致的 attachment（overview 1011×1078、seg-02 859×929）；
> 其余 3 个 attachment 已被 DSH 缓存清理，不可再独立复核（报告不隐瞒此限制，不影响结论）。

### 3.7 DSH patch / Skill / 工具发现（11/11 PASS + Lead headless 证据复核）

- `scripts/verify_dsh_patch.py` 11/11：隔离 `DSH_HOME` 下 `dsh --profile headless --patch ovital_mcp/dsh/ovital.cordis.yml --dump-config`
  组合出 `mcp-ovital` 行（serverName=ovital/transport=stdio/args/60000ms/failOnStartupError=false）；
  不带 patch 的对照无该行；config 通过**已安装 dsh-mcp-client 0.1.5-rc.1 真实 schemastery schema**（负向控制：
  非法 transport 被拒）；未修改用户真实 `~/.dsh`。
- Lead headless 证据（verifier 复核 artifact）：18 个 `mcp__ovital__*` 工具被发现并调用；
  `.dsh/skills` 通过官方 `customSkillDirs` 加载成功（模型复述红线/流程）；`deepseek-flash` 收到 MCP Image
  并完成像素级描述；`docs/evidence/images/` 5 张最终图与 `manifest.json` SHA256 一致（verifier 复核通过）。
- **未完成**：DSH Web GUI 会话内自动 Skill 发现、Windows 实机加载/工具发现。

## 4. 证据分级映射

| 证据类别 | 等级 | 说明 |
|---|---|---|
| 奥维 V2.7 手册命令/字段/会员要求、GRASS 算法文档、DSH MCP/Skill 文档 | `official_document` | 附官方 URL/章节（`docs/capabilities.md` §9） |
| 模拟奥维闭环（登录/读写/回读/回滚/故障注入） | `offline_simulated` | 只证明协议编解码与流程逻辑，**不是真实联机** |
| 合成解析真值 DEM 的 native/GRASS 计算、确定性、掩膜/面积、复核图排版 | `offline_computed` | 无人工审核参考边界；真实精度取决于用户 DEM |
| DSH headless 工具发现/Skill/视觉（Lead 执行 + verifier artifact 复核） | `offline_simulated` + agent 执行证据 | 非 verifier 独立复跑；非真实设备；本轮最终证据为 vision4 |
| MCP 协议/stdout/凭据/错误路径/图片解码 | 本机可复现工程证据 | 不是功能验收的替代 |
| Windows 奥维联机、账号权限、轴序/替换语义/20802 口径 | `real_device_pending` | **未完成** |
| 用户 DEM + 审核边界精度（面积差/边界距离/鞍部/支流） | `real_device_pending` | **未完成** |

## 5. 明确未完成项（不得用离线证据替代）

1. 真实 Windows 奥维联机：接口启用/端口/PSK 派生与鉴权、目标账号 VIP/SVIP、地图图片（20301/20302）与等高线（20303）实际可用性；
2. `20112` 写入后 ObjID 生成/替换语义、ParentID 挂接、数量限制；`Shape.Latlng` 真实轴序；`20802` 投影/孔洞口径；
3. 真实奥维断线/重连下的重复写入行为与幂等（本机仅故障注入模拟）；
4. 用户真实 DEM 的 CRS/高程基准/覆盖完整性；与**经审核参照边界**的面积差、边界距离、关键鞍部/支流正确性；
5. DSH Web GUI 会话内 Skill 自动发现；Windows DSH 实机加载与真实工具发现；
6. 真实地图图片/等高线进入视觉模型的复核（本机仅模拟图片 + 合成 DEM 图）；
7. Copernicus 真实 DEM 未跑通/未纳入验收（本机只用合成解析真值 DEM；公开 DSM 不能支撑工程精度）。

完整清单与验证方法见 `docs/待核验清单.md`（A1–A16 / B1–B10 / C1–C12 / D1–D10 / E1–E12）。

## 6. 已知问题与修复状态

### 6.1 已修复/已记录问题

| 编号 | 严重度 | 描述 | 状态（最终） |
|---|---|---|---|
| F-01～F-04、F-07 | 中/低 | 工具契约参数名不一致、17 vs 18 工具、README config 步骤、弱测试 | **已修复并经复验** |
| F-09 | 中（伪成功通道） | provenance 曾以"连上非模拟服务端"直接标记真实设备已验证 | **已修复**：仅人工配置 `ovital.real_device_verified=true` 且非模拟才 true；mock/伪造场景均 false（含回归测试） |
| F-11 | 中 | 分段复核图 `figure.bbox_lonlat` 曾为米制坐标却标 EPSG:4490 | **已修复**并回归：与 `segment.bbox_lonlat` 逐值一致、合法经纬度 |
| R-01 | 中（排版/判读） | 分段图曾被压扁、信息面板遮挡比例尺/刻度、大量空白（vision1–v3 三轮反馈） | **已修复（vision4 定稿）**：外扩地形上下文 + `stack`/`side` 自适应布局 + 窄图刻度限制旋转 + 布局/像素断言通过 |
| F-12 | 低 | engine 不可用曾报 `ENGINE_FAILED` 而非 `ENGINE_UNAVAILABLE` | **已修复**：实测 `job.error.code=ENGINE_UNAVAILABLE`（worker 透传原错误码） |
| F-13 | 低 | `docs/capabilities.md` 探测项数（7 组 vs 9 命令）、PNG 字节数（随请求参数变化）表述不精确 | 文档建议（不阻塞验收） |
| F-14 | 低（证据一致性） | `export_result` 报告复核图清单曾为空；已交付 SRTM 报告曾为修复前产物 | **已修复并经 verifier 复测**：修复后报告 SHA256 `daab4254…`（§3 列出 9 张图）；新增 `real-srtm-job-result.json`（`116987ab…`）归档 job 证据；两次独立运行产物逐字节一致（见 §3.4b） |
| F-15 | 低（文档措辞） | `real-dem-run.md`/`evidence/README.md` 曾写"boundary.geojson 整体哈希因含 job_id/时间不同"；实测任务产物 boundary 两次运行逐字节相同，哈希不同的是 `export_result` 导出 geojson（含 job_id/task_id） | **已修复并复核**：两份文档已按实测改正（含报告 §3 图路径失效注记）；任务产物 boundary 逐字节相同（`8d791669…`）、导出 geojson `f53b3ea3…` 因含 job_id/task_id 不同 |

### 6.2 已知外观局限（vision4 反馈，**不作为失败项**；已与 Lead 对齐口径）

1. 竖版 `side` 布局右侧信息栏下方约 20–22% 空白（地图面板外留白，整图连续白带仅 3.9%；地图 429px 宽/83.6% 高、刻度可读）。
2. 方形 DEM 的 overview 信息卡叠放地图左上（`mode=inside`，地图面积占比约 73.8%，UI 元素互不遮挡但遮部分等高线/河网）；seg-01 图注字号偏小。
3. `layout.x_major_tick_count` 与实际绘制有 ±1 偏差（报 5 实际 4 簇，不作强契约）；极端宽扁 DEM 的 overview 纵横比可 >2.2（two_bowl 2.73，分段图仍 [0.5,2.2]）。

## 7. 剩余风险

1. **模拟≠真实**：字段容错、轴序、ObjID 语义、会员权限以模拟/手册为依据，真实客户端可能不同；本报告不提供真实联机结论。
2. **无参考边界**：解析真值只证明算法确定性与掩膜正确，不证明真实流域工程正确；DEM 分辨率/DSM-DTM/出口定位是主误差源。
3. **两引擎口径差异**：native(D8) 与 GRASS(SFD) 在复杂地形差异显著（single_bowl 差 32.3 万 m²），必须按项目口径选引擎并明示，不能互认"正确"。
4. **视觉证据来源**：DSH 视觉/工具发现为 Lead 执行、verifier 复核 artifact；verifier 未重跑模型；且为 headless+模拟奥维+合成 DEM，不能表述为真实联机。
5. **错误码/契约稳定性**：MCP 参数校验失败返回纯文本（非 `status/provenance` 结构），自动化调用方需兼容；F-13 文档口径仍待收尾（F-15 已修复）。
6. **真实 DEM 样例边界**：SRTM 雷达地表模型（含植被/建筑）、垂直基准未知、出口自动选点、无独立审核参考边界 → 只能证明链路可运行，不得引用其面积/边界做工程结论；
   且 Lead 清理 `.cache/dshtest`/`.dsh-home` 后部分原始中间产物不可再独立复核（§3.6 已如实记录）。
6. **mock 覆盖限制**：模拟服务器无预置选中对象（`get_selection` 非空路径未覆盖）；mock 20104/20112 行为不等于真实奥维。
7. **排版外观局限**：见 §6.2（side 布局留白、overview 信息卡叠图、图注字号、ticks 元数据 ±1、极端宽扁 overview 纵横比）。

## 8. 复现命令与证据哈希

```bash
cd "/Users/huangsirsmacbookairm5/Public/code/project/omap mcp"

# 全量测试（349 passed；tests/gis 75 passed 无 skip；integration 10 passed）
.venv/bin/python -m pytest -o addopts='' -q

# MCP stdio 协议层（24/24）
.venv/bin/python scripts/verify_mcp_stdio.py

# DSH patch 组合 + 插件 schema（11/11，隔离 DSH_HOME）
.venv/bin/python scripts/verify_dsh_patch.py

# 阶段 2 完整闭环（84/84；需可写 HOME 供 GRASS）
HOME="$PWD/.cache/grasshome" .venv/bin/python scripts/verify_mock_loop.py

# 集成回归（真实 stdio 握手 + mock 闭环）
.venv/bin/python -m pytest -o addopts='' tests/integration -q
```

| 证据文件 | SHA256（sha256sum） | 说明 |
|---|---|---|
| `.cache/verify/stdio-evidence-latest.json` | `66d64f1a16161e52ded495b64c52c22983749b5d569bcafb81b6fca86c2a8389` | 24 项 stdio 证据 |
| `.cache/verify/dsh-patch-evidence-latest.json` | `7531dbdc50e2876a479e826a10073960b6bfc97555003d99e7e3a4ad47cc5794` | 11 项 DSH patch 证据 |
| `.cache/verify/dsh-dump-config.txt` | `a9a958f43949e49bb0d6a6d826a0a1fb0f25dd4d8edaebcb3b80e84428c30452` | 隔离 DSH_HOME 组合树 |
| `.cache/verify/loop/mock-loop-evidence-latest.json` | `5d948666f27b7aa3289a93adcbed80181e36ed6738589da16cc0c8c47d37acaa` | 阶段 2 全部 84 项 |
| `docs/evidence/dsh-headless-vision-e2e-final.out.txt` | `347febe07306c2522ba98baeff4a4c0ef361aa6a45464bbe6e07d404d012a05b` | vision4 最终视觉 transcript |
| `docs/evidence/images/manifest.json` | `d7e5b449f941128824763b08de2ca4cf517a524af36e552b0689e0a14e18894e` | 最终 5 张证据图 + sha256/bbox/view_bbox/layout |
| `docs/evidence/README.md` | `e09e4cb8111ef37f614f91975c0736c3daee58758258b117f8f5f39dce211572` | DSH 证据说明（含哈希口径） |
| `.cache/verify/loop/images/` | 每张图 sha256 见 evidence JSON | MCP Image 解码副本（本次 overview 1184×433、seg-01 1171×557） |
| `.cache/verify/synthetic/` | 见 `expected.json` | 合成解析真值 DEM |
| `docs/evidence/images/final-sample-*.png` | 见 manifest | vision4 最终代码树产物（5 张，SHA256 已复核） |
| `docs/evidence/real-srtm-boundary.geojson` | `8d7916698d267fd58883998daa4923950768487b1dda157afb585caf022afdd5` | 真实 SRTM 边界（608 顶点，verifier 独立复算面积一致） |
| `docs/evidence/images/real-srtm-manifest.json` | `453f4ab61cc8fa3082692dcf1c1fd8d15443ab65df23ce68ea605bf0b6b0be87` | 9 张真实 DEM 复核图 manifest（SHA256 已复核） |
| `docs/real-dem-run.md` | `102422d15fc95a4ae6acabe3d2d6c9a72dc1b8d42cb49edf46f37836084c21a2` | 真实公开 DEM 样例运行记录（F-15 措辞已修正） |
| `docs/evidence/real-srtm-report.md` | `daab42541401761c492d3bbc82eb2edd2154684e89ea7feff6f09032a6438800` | 修复后成果报告（§3 含 9 张复核图，F-14 已修复） |
| `docs/evidence/real-srtm-job-result.json` | `116987abb09a647e769227d88ae6b70e487f7b5d1521f377b3dd22bdb28bc93f` | 复现运行任务摘要（参数/面积/DEM/9 图/导出哈希） |
| `docs/evidence/README.md` | `9efe75031bfcd6069b8be11c4790c766614f48ccfd008e06c5aa5dfa47a878ba` | DSH/SRTM 证据说明（含哈希口径、F-14/F-15 说明） |

> 说明：`.cache/` 为可重建的本机证据目录，非交付物；交付物是脚本 + 本报告 + `docs/verification-findings.md` + `docs/evidence/`。

## 9. 签署与边界

- 验证者：独立 QA（Task-4 / verifier）。本报告只对上述命令在**最终仓库代码树**上的实际输出负责；被执行方声明、文档声称与
  mock 结果**仅在证据支持范围内**被采信，未验证项逐条列为 `real_device_pending`。与 findings、待核验清单配套使用。
