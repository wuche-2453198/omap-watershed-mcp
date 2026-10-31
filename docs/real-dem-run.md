# 真实公开 DEM 样例运行记录（SRTM 30 m，北京西部山区）

本文件记录 **2026-10-30 本机**用公开真实地形数据跑通完整工具链的记录，用于证明
"真实地形 + 真实投影重采样 + 完整 MCP 工具链"可运行。它**不是**工程精度验收：
本机没有 Windows 奥维客户端，也没有该流域的独立人工审核边界或权威集水面积。

## 1. 数据来源与预处理

| 项目 | 内容 |
|---|---|
| 数据 | SRTM 1 arc-second（约 30 m）DEM 瓦片 `N39E115`（北纬 39–40°，东经 115–116°） |
| 来源 | AWS Open Data `elevation-tiles-prod/skadi`（`https://s3.amazonaws.com/elevation-tiles-prod/skadi/N39/N39E115.hgt.gz`） |
| 原始格式 | `.hgt`（3601×3601，int16），无空洞（void=0） |
| 本地转换 | 直接解释为 GeoTIFF（EPSG:4326，1 arc-second 网格），高程范围约 -1–2849 m |
| 数据性质 | SRTM 为雷达地表模型（含植被/建筑影响），**不是裸地 DTM**；垂直基准为 EGM96，但 GeoTIFF 本身未声明，工具已给出"垂直基准未知/DSM-DTM 未知"警告 |
| 文件 SHA256 | `f4bd61497a84f6bb1fe34a9f6d51764f386345dcd809f333d8877f37a8c7a11f` |

> 说明：项目按任务书要求以"项目提供的 GeoTIFF"为正式输入；本样例只是为了在缺少
> 用户数据时验证真实地形链路。未下载 Copernicus GLO-30（沙箱到 S3 的 GET 实测约
> 3 KB/s，50 MB 不可行，已在 `docs/gis-notes.md` 如实记录）。

## 2. 出口选择（可复现、非人工审核点）

- 出口不是"用户点选"的真实工程坝址：为做本机链路验证，在分析网格上用 D8 汇流累积
  扫描，选取"累积 2 万–8 万像元、高程 < 900 m"的候选并取中位累积像元。
- 最终出口：输入 `(115.6736125, 39.8525625)`；D8 吸附后 `(115.6734366, 39.8525103)`
  （吸附距离 **16.13 m**，累积 25,857 像元，像元面积 760.87 m²）。
- 该点位于北京西部山区（门头沟一带）一条支沟出口，**没有独立参考面积/边界**。

## 3. 计算参数与结果

| 项目 | 值 |
|---|---|
| 任务/作业 ID | 首次运行 `j-20261029-180027-722b33`；为修复 F-14（导出报告复核图清单）用同一 DEM/参数复现运行 `j-20261029-181428-9c0ac0` 生成正式报告。两次运行几何逐字节一致、复核图 SHA256 相同、DEM SHA256 相同 |
| 引擎 | `native`（内置 D8，确定性；不是 GRASS 回退） |
| 分析 CRS / 分辨率 | EPSG:32650（UTM 50N）/ 27.58 m；分析栅格 3186×4077 |
| 水平面积 | **19,673,902.93 m²（19.67 km²）**，EPSG:32650 平面面积 |
| 周长 | 28,797.63 m |
| 顶点/部件/洞 | 608 / 1 / 0（几何有效，无自交） |
| 分析范围截断 | `truncated=false`（掩膜未接触分析范围边界） |
| DEM 警告 | 地理坐标需投影、重采样平滑、垂直基准未知、DSM/DTM 未知等（见 provenance） |
| 复核图 | overview + seg-01..08 共 9 张（`docs/evidence/images/real-srtm-*.png`） |

## 4. 工具链证据（真实 MCP stdio 会话）

通过 MCP 客户端真实进程调用（参数与结果见 `docs/evidence/images/real-srtm-manifest.json`）：

1. `inspect_dem`：CRS=EPSG:4326、分辨率约 23.9 m×30.7 m（中心纬度）、无 NoData、8 条数据质量警告；
2. `start_watershed` → `get_job` 轮询（约 30 s）→ `succeeded`；
3. `get_job(include_images=true)`：返回 overview + seg-01 两张 MCP Image（另有 9 张图在任务目录）；
4. `review_segment(1)`：分段复核图；
5. `measure_geometry`：19,673,902.93 m² 与任务记录差 0.000 m²（同口径）；
6. `export_result(format=report)`：生成成果报告（`docs/evidence/real-srtm-report.md`，由修复后代码的复现运行 `j-20261029-181428-9c0ac0` 生成；任务/成果摘要见 `docs/evidence/real-srtm-job-result.json`）。
   该报告 §3 内的图片路径指向复现运行的临时工作目录（`.cache/leadtest/...`，已清理）；
   等价的归档图件见 `docs/evidence/images/real-srtm-*.png` 与 `real-srtm-manifest.json`（逐字节一致）。
7. `write_draft`：把边界写入**本地模拟奥维服务器**的独立草稿文件夹，写后回读 **608 个顶点**、
   20802 量算成功（`read_back`），随后 `rollback_change` 删除本次创建对象。
   —— 这一步只证明写入/回读/回滚**代码路径**（模拟协议），**不是**真实奥维落图。

边界文件：`docs/evidence/real-srtm-boundary.geojson`（EPSG:4490，属性含面积/CRS/引擎/证据级别）。

## 5. 可复现性

- 同一 DEM、同一出口、同一参数两次独立运行（`j-…180027` 与 `j-…181428`）实测：
  **任务产物 `artifacts/boundary.geojson` 逐字节相同（SHA256 `8d791669…`）、几何坐标逐值一致、
  面积/周长逐位一致、9 张复核图 PNG 逐字节相同、DEM SHA256 相同**；
  `export_result` 导出的 GeoJSON 文件哈希（`f53b3ea3…`）不同，是因为其 `properties`
  含 `job_id`/`task_id`/时间字段；其几何与 `artifacts/boundary.geojson` 完全一致。
- native 引擎确定性测试已在 `tests/gis` 中逐像元与指纹断言。
- DEM 下载脚本：`scripts/fetch_copernicus_dem.py` 面向 Copernicus；SRTM 下载/转换命令
  记录在本文件 §1（`.hgt` 直接按 GeoTIFF 读取即可）。
- 本样例原始 SRTM 文件（约 26 MB）**未放入仓库**（`docs/`/`tests/` 均不含原始 DEM），
  仅保留可复核的成果与图件。

## 6. 明确未验证（不要引用本文件证明以下事项）

1. 该边界与真实地形分水岭/权威集水面积的一致性：**无独立人工审核参考**，未计算面积差
   或边界距离；不能声称"正确率/误差"。
2. 真实 Windows 奥维客户端上的草稿落图、回读、面积口径：本样例的写入是**模拟服务器**。
3. 真实工程精度：SRTM 30 m 为公开雷达地表模型、含植被/建筑影响、垂直基准未知；窄山脊、
   平缓分水岭与工程级边界需要更高精度 DTM 与人工复核。
4. 数据覆盖：SRTM 瓦片为 1°×1° 截断，出口所在分析范围未截断是就本瓦片而言；上游若跨出
   瓦片需换更大范围 DEM。

## 7. 结论

- **能证明**：真实公开 DEM → 真实投影/重采样 → 确定性 D8 集水区 → 几何/面积/复核图 →
  MCP 图片返回 → 模拟奥维草稿写入/回读/回滚 的整条链路可运行，且与合成解析真值测试
  相互独立（真实地形没有解析真值，只有合理性检查）。
- **不能证明**：工程边界正确性或真实奥维联机可用性。这两项仍是 `real_device_pending` /
  需要独立参考数据后才能验收。
