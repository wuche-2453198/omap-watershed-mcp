# GIS 包实现与验证说明（ovital_mcp.gis）

本文件是 `ovital_mcp/gis/` 的**实现、参数与验证证据**说明，对应
`docs/architecture.md` §5 契约与 `DSH_实施任务书.md` C/D 节。所有结论都标注
证据等级（§9 分级）；未验证项集中在 §13，禁止把 `offline_computed` 说成
实机验收。

- 证据等级：`offline_computed`（本机确定性计算）
- `verified_against_real_device: false`
- 无独立人工审核的工程边界、无 Windows 奥维客户端、无用户 DEM

---

## 1. 交付物

| 文件 | 内容 |
|---|---|
| `ovital_mcp/gis/dem.py` | `inspect_dem`（CRS/NoData/量程/基准/分辨率/SHA256）、`sample_dem_lonlat` |
| `ovital_mcp/gis/crs.py` | 米制分析网格准备（重投影/重采样/对齐）、批量折线变换、行列号工具 |
| `ovital_mcp/gis/native.py` | 确定性 D8 引擎：流向、汇流累积、出口吸附、反向 BFS、矢量化、河网链化 |
| `ovital_mcp/gis/grass.py` | GRASS 8.x 适配器（`r.watershed -s` + `r.water.outlet`） |
| `ovital_mcp/gis/engine.py` | 契约入口：`WatershedParams/WatershedResult`、`run_watershed`、`get_engine`、成果装配、`failure.json`、`load_result` |
| `ovital_mcp/gis/geometry.py` | 几何校验/量算/受控简化/锚点修订 |
| `ovital_mcp/gis/segment.py` | 边界分段、锚点 ID、`list_segments` |
| `ovital_mcp/gis/review.py` | 复核图：`render_overview` / `render_review_pack` / `render_segment` / `render_hillshade_contours` |
| `scripts/make_synthetic_dem.py` | 解析真值合成 DEM + `tests/fixtures/synthetic/expected.json` |
| `scripts/fetch_copernicus_dem.py` | Copernicus GLO-30 公开 tile 下载与检查（可选 stretch） |
| `tests/gis/` | 75 个测试：DEM/解析真值/确定性/几何/分段/复核图/GRASS/契约 |

---

## 2. 环境与版本（实测）

| 组件 | 版本 |
|---|---|
| Python | 3.13.13（项目 `.venv`） |
| numpy / scipy | 2.5.4 / 1.18.1 |
| rasterio / shapely / pyproj | 1.5.2 / 2.2.0 / 3.8.0 |
| matplotlib / Pillow / pytest | 3.11.2 / 13.0.0 / 9.2.0 |
| GRASS GIS | **8.5.0（conda-forge，macOS arm64，本机实测）** |
| OS | macOS（arm64） |

GRASS 可执行文件：`<repo>/.conda/grass/bin/grass`（约 1 GB，共享环境目录，
只读使用）。运行命令：

```bash
.venv/bin/python -m pytest tests/gis -q          # 75 passed in ~15 s（GRASS 真实运行，无 skip）
.venv/bin/python -m pytest -q                    # 全仓测试
```

### GRASS 本机兼容坑（实测，必须照做）

1. **HOME 必须可写**：GRASS 默认配置目录在 macOS 是
   `~/Library/GRASS/GRASS8`；只读 HOME/沙箱下会报
   `Failed to create configuration directory ... Operation not permitted`。
   适配器给子进程显式设置：
   `HOME=<repo>/.cache/grasshome` 且 `GRASS_CONFIG_DIR=<repo>/.cache/grasshome/grassconfig`
   （两者都 `mkdir -p`）。
2. **路径不能含空格**：本项目路径是 `.../omap mcp`，GRASS 命令行/脚本解析
   对空格敏感（`r.in.gdal input=... omap mcp/...` 会被当成多个参数）。适配器
   把所有 GRASS 输入输出放到无空格的 `tempfile.mkdtemp(prefix="omap-grass-")`
   暂存目录，算完再拷回任务目录。
3. **`r.out.gdal` 精度检查**：DCELL→Int32/Int16 会被拒绝，需要 `-f`。
4. **`-c` 只建 location 不导数据**：必须再 `r.in.gdal` 导入 DEM。
5. `--tmp-location` 也可用，但显式 `-c <dem> -e <gdb>/<loc>` 更可控（本次采用）。

---

## 3. 数据来源

### 3.1 合成 DEM（解析真值，本仓库生成）

`scripts/make_synthetic_dem.py` 生成 15 个场景，全部为 EPSG:32650（UTM 50N）、
像元 30 m（`single_bowl_fine` 为 10 m）的 GeoTIFF。地形用**闭式公式**定义，
分水岭可数学推导，因此测试断言的是**掩膜像元集合**而不是"面积接近"：

| 场景 | 地形公式 / 分水岭 | 解析期望 |
|---|---|---|
| `two_bowl_ridge` | `z=100+0.1·min(rA²,rB²)`，A/B 碗心 (5,21)/(5,38)，分水岭=碗心垂直平分线 x=29.5（像元中心为整数，无像元落在分水岭上） | A 碗集水区 = 列0..29 = **360 像元 / 324,000 m²** |
| `two_bowl_ridge_outlet_b` | 同上，出口取 B 碗心 | 列30..59 = 360 / 324,000 |
| `single_bowl` | `z=100+0.1·r²`，41×41，碗心唯一 sink | 全部 1681 / 1,512,900 |
| `single_bowl_fine` | 同上但 10 m 网格 121×121 | 全部 14641 / 1,464,100 |
| `tilted_plane` | 严格东向下降平面（无横向分量） | 行7 全部 60 像元 / 54,000（r.water.outlet 文档的 "thin sliver"） |
| `plane_with_pit_downstream` | 东向平面 + (7,20) 深 15 m 洼地（不填洼） | 行7 列22..59 = **38 / 34,200** |
| `plane_with_pit_outlet` | 出口就是洼地 | 行6/7/8 列0..21 = **66 / 59,400** |
| `two_bowl_nodata_wall` | A 碗内 3×3 NoData 墙（行4..6,列14..16） | 360−9−14（行5列0..13 因失去更低邻居成为局部洼地）= **337 / 303,300** |
| `two_bowl_nodata_hole` | A 碗内单像元 NoData (5,15) | 360−1 = **359 / 323,100**，边界 1 个洞 |
| `valley_converge` | `z=100−0.05·row+0.1·|col−30|` 南向收敛谷（无并列坡度） | 全部 60×20 = **1200 / 1,080,000** |
| `crater_basin` | 环形山：内部向心凹陷、外侧向外倾斜的闭合盆地 | 中心集水区被环脊围住，不接触边界；掩膜落在半径 9–11 像元解析包络（253–377）内，**无截断警告** |
| `two_bowl_ridge_geo` | 同一地形按 EPSG:4490 经纬度格网采样 | 重投影路径；在分析网格上用闭式分水岭复算，允许 ±4 像元 |
| `tilted_plane_snap_offset` | 出口西偏 20 m | 吸附距离 **20.0 m**、累积 60、掩膜仍为行7 |
| `two_bowl_ridge_ambiguous_snap` | 出口放分水岭上、半径 600 m | 两侧碗心累积相同 → 必须报告"候选不唯一" |

像元面积精确 900 m²（30 m）或 100 m²（10 m），所以面积可由像元数精确复算。

### 3.2 Copernicus GLO-30（真实 DEM，可选 stretch）

`scripts/fetch_copernicus_dem.py` 按官方公开 S3 路径下载
`Copernicus_DSM_COG_10_N39_00_E116_00_DEM`（1°×1°，约 50 MB，无需认证）。
数据事实（依据 `.research/copernicus-dem.txt` 官方页面）：**DSM**（含建筑/
植被）、GLO-30 约 30 m、水平 WGS84（EPSG:4326）、垂直 EGM2008、单位米、
`RasterPixelIsPoint`、单 tile 1°×1°。

**本机结论：未能完成真实 tile 的端到端跑通（如实记录，未伪造）。**
本会话沙箱到 S3 的 HEAD 请求成功（HTTP 200，`Content-Length=50,462,276`），
但 GET 实测约 3 KB/s（2 分钟仅 0.3 MB），50 MB tile 需数小时，不可行。
因此 §13 把"真实 DEM 跑通"列为未完成；脚本与检查逻辑已交付，网络恢复后
可直接运行（命令见 §14），并且必须标注"真实 DEM、无独立审核边界，仅可
复现性与合理性检查"。

---

## 4. 坐标与面积口径

- 对外规范坐标：`EPSG:4490`（CGCS2000，`(lon, lat)`，经度在前）；
  CGCS2000 与 WGS84 差异为厘米级，对 30 m DEM 可忽略，但报告中必须写明。
- 分析/量算：一律投影到米制 UTM（`coordinates.metric_crs_for`，自动分带），
  默认由出口经纬度决定；`WatershedParams.metric_crs` 可显式覆盖（必须是投影 CRS）。
- 面积：shapely 平面多边形面积（水平投影面积），**不是坡面面积**；成果里记录
  `metric_crs` 与 `area_method`。禁止在 EPSG:4490 上直接算面积。
- 边界输出：EPSG:4490；`boundary_crs` 字段显式标注。
- DEM 检查会报告官方轴序（如 EPSG:4490 的 WKT 轴序是 lat,lon）与栅格读取约定
  （rasterio 始终 x=lon/easting、y=lat/northing），避免轴序混淆。

---

## 5. native 引擎（确定性 D8）

实现文件 `ovital_mcp/gis/native.py`，流程与契约 §5.2 一致：

1. **DEM 检查**：`inspect_dem`（CRS/NoData/量程/垂直基准/DSM-DTM 未知/SHA256），
   警告全部进入成果 `warnings`（前缀 `[DEM]`）。
2. **统一分析网格**：重投影到米制 UTM（`prepare_analysis_dem`）。若 DEM 已是
   目标 CRS 且分辨率一致，则**直接复制文件、不重采样**——这样解析真值测试
   才能逐位比对像元。否则 `rasterio.warp.reproject`（默认 bilinear，可
   `extra.resampling=nearest`），分析范围向外对齐到分辨率整数倍。
3. **D8 流向**：8 邻域最陡下降；邻域顺序固定
   `NW,N,NE,W,E,SW,S,SE`（编码 0..7），比较用严格 `>`（并列取先出现者）→
   完全确定。无更低邻居 = 无出流（-1）。**不填洼**。
4. **汇流累积**：利用"D8 只流向严格更低像元"→ 按高程降序排列后是严格下三角
   线性系统 `(I−A)acc=1`，用 `scipy.sparse.linalg.spsolve_triangular`
   （C 实现，避免 1300 万像元级 Python 循环）；scipy 异常时退回 Kahn 拓扑排序。
   累积值 = 上游像元数（含自身，与 GRASS `accumulation` 口径一致）。
5. **出口吸附**（`snap_outlet`）：
   - 半径内取**汇流累积最大**的有效像元；并列时取距输入点最近、再取平坦索引
     最小（确定性）；
   - 记录吸附前后距离、累积、半径内最大累积、候选点列表；
   - 半径内无有效像元 / 出口在分析范围外 → **显式报错**；
   - `snap_radius_m=0` → **关闭吸附**：严格用输入点所在像元，`snap_distance_m=0`、
     `outlet_used=outlet_input`，仍报告该像元累积；落在 NoData 显式失败。
6. **上游掩膜**：从吸附出口做**反向 BFS**（用接收像元建前驱 CSR，按层向量化
   扩展，O(边数)）。掩膜像元数必须等于出口累积（D8 不变量），不等则告警。
7. **矢量化**：`rasterio.features.shapes(connectivity=8)` → 取含出口的连通部件
   → shapely 校验（可选受控简化）→ 回投 EPSG:4490。
8. **截断/NoData/洼地检查**：掩膜接触分析范围边界 → `truncated=true` + 警告；
   紧邻 NoData 的流域像元计数 → 警告；出口像元无出流方向 → 警告；不填洼。
9. **河网**：`acc ≥ threshold` 的像元按"headwater/交汇点之间"**链化**为折线
   （不是逐像元画线段），半像元容差简化；链数/顶点数/像元数有硬上限
   （20,000 链 / 2,000,000 顶点 / 4,000,000 像元），超限跳过并告警，
   绝不拖垮主计算。阈值默认 `max(min_accum_cells, 0.5%×流域像元数)`，可
   `extra.stream_threshold_cells` 覆盖。
10. **中间成果**：`analysis_dem.tif`、`flow_direction.tif`（本引擎编码，见
    provenance `native.flow_direction_encoding`）、`flow_accum.tif`。

### native 解析真值验证结果（逐像元，`snap_radius_m=0`）

| 场景 | 期望像元 | 实测像元 | 掩膜集合 | 面积 (m²) | 结论 |
|---|---:|---:|---|---:|---|
| two_bowl_ridge | 360 | 360 | **完全一致** | 324,000.000 | ✅ |
| two_bowl_ridge_outlet_b | 360 | 360 | **完全一致** | 324,000.000 | ✅ |
| single_bowl | 1681 | 1681 | **完全一致** | 1,512,900.000 | ✅ |
| single_bowl_fine（10 m） | 14641 | 14641 | **完全一致** | 1,464,100.000 | ✅ |
| tilted_plane | 60 | 60 | **完全一致** | 54,000.000 | ✅ |
| plane_with_pit_downstream | 38 | 38 | **完全一致** | 34,200.000 | ✅ |
| plane_with_pit_outlet | 66 | 66 | **完全一致** | 59,400.000 | ✅ |
| two_bowl_nodata_wall | 337 | 337 | **完全一致** | 303,300.000 | ✅ |
| two_bowl_nodata_hole | 359 | 359 | **完全一致**（1 个洞） | 323,100.000 | ✅ |
| valley_converge | 1200 | 1200 | **完全一致** | 1,080,000.000 | ✅ |
| crater_basin | 253–377（包络） | 357 | 包络内、无截断 | 321,300.000 | ✅ |
| two_bowl_ridge_geo（EPSG:4490） | 分析网格闭式复算 | 420 | 差 0 像元（容差 ±4） | 378,751.661（分析网格更大） | ✅ |
| tilted_plane_snap_offset | 60 | 60 | 完全一致；吸附 20.0 m | 54,000.000 | ✅ |
| two_bowl_ridge_ambiguous_snap | 360（A 或 B 半区） | 360 | 完整半区；`ambiguous=true` | 324,000.000 | ✅ |

- 掩膜像元数 = 出口累积像元数 = 面积/像元面积（三条独立口径互相校验）。
- 面积由像元数 × 900 m² 精确复算，误差 < 1e-9 相对。
- 这些数字来自 `pytest tests/gis/test_gis_native_engine.py`，测试逐像元断言
  `np.array_equal(mask, analytic_mask)`。

---

## 6. GRASS 适配器

实现文件 `ovital_mcp/gis/grass.py`。实测配方：

```bash
# 1) 建临时 location（-c 只建投影，-e 建完退出）
grass -c <analysis_dem.tif> -e <gisdbase>/<location>
# 2) 脚本（--exec bash <script.sh>）：
r.in.gdal input=<analysis_dem.tif> output=dem --overwrite
g.region raster=dem -a
r.watershed -s elevation=dem accumulation=acc drainage=dir --overwrite
r.out.gdal -f input=acc output=<acc.tif> type=Int32 --overwrite
r.out.gdal -f input=dir output=<dir.tif> type=Int16 --overwrite
# 3) Python 侧在 acc 栅格上做出口吸附（复用 native.snap_outlet）
r.water.outlet input=dir output=basin coordinates=<east>,<north> --overwrite
r.to.vect input=basin output=basin_vect type=area --overwrite
v.out.ogr input=basin_vect output=<boundary.geojson> format=GeoJSON type=area --overwrite
r.out.gdal -f input=basin output=<basin.tif> type=Byte --overwrite
```

- 分析 DEM 由 Python 侧先统一重投影（与 native 同一网格），GRASS location 由该
  米制 DEM 创建；出口吸附复用 Python 侧 acc 采样，保证两种引擎"吸附语义"一致。
- `r.watershed -s` = SFD/D8；**不填洼**（文档：最小代价算法不要求预先填洼），
  NoData 空洞不填补并显式警告。
- `drainage` 编码：1=NE,2=N,3=NW,4=W,5=SW,6=S,7=SE,8=E，-1=洼地、其他负值=
  流向分析范围外。**已用倾斜平面实测验证**（内部像元全为 8=East，东边界出口为 -8），
  写入 provenance `grass.drainage_encoding`。
- `accumulation` 负值语义（文档）：负值表示"可能有分析范围外来水"，绝对值为
  保守估计 → 出现负值时警告"分析范围可能不足"。`flow_accum_cells` 记录绝对值。
- provenance 记录：`grass --version` 首行、可执行文件、location、每阶段命令与
  脚本、stderr 摘要（截断 4000 字符）、HOME/GRASS_CONFIG_DIR 覆盖路径、
  编码表、负累积像元数、`fill_depressions=false`。
- 不可用（找不到可执行文件/版本失败）→ `EngineUnavailableError`，绝不伪成功。
  显式配置的 `grass_cmd` 是**权威**的：找不到就报不可用，不会悄悄回退 PATH 的其它
  GRASS。

### 6.1 关键实证：GRASS SFD ≠ 严格 D8

`r.watershed -s` 的 SFD 用 **A^T 最小代价路径**（Ehlschlaeger 1989），不是
"逐像元最陡下降"。两者在真实地形通常高度一致，但在**径向对称碗**这种合成地形
上会产生可量化的方向差异：

| 场景 | native（严格 D8，解析真值） | GRASS（A^T 最小代价 SFD） | 说明 |
|---|---:|---:|---|
| tilted_plane（无并列坡度） | 60 像元 / 54,000 m² | **60 / 54,000** | 完全一致 |
| valley_converge（二维收敛，无并列坡度） | 1200 / 1,080,000 | **1200 / 1,080,000** | 完全一致 |
| single_bowl（径向对称碗） | 1681 / 1,512,900（=解析真值） | 821 / 738,900 | 方向差异 61/1681=3.6%，流域被分解 |
| two_bowl_ridge | 360 / 324,000 | 181 / 162,900（⊂ A 半区） | 同上 |

已排除适配器 bug：
- 用 GRASS 自己的 `drainage` 图从出口做反向 BFS，**精确复现** GRASS 自己的
  basin（821 像元），证明读取/对齐/编码/矢量化一致；
- 差异不在并列坡度（该碗上严格 D8 的"最大坡度并列"像元数为 0），而在
  A^T 最小代价路径本身；
- 在无并列坡度的单坡/收敛地形上两者精确一致。

**结论：SFD/D8 两种实现不等价，不能混接方向编码，也不能用 native 的解析真值
硬套 GRASS。** 验证口径按引擎分开：

- native：解析真值逐像元一致（§5 表）；
- GRASS：① 在两种算法应等价的解析地形上精确一致（tilted_plane、valley_converge）；
  ② 自洽性（basin = 自身 drainage 反向 BFS、矢量面积 = 掩膜像元数 × 像元面积）；
  ③ 确定性（两次运行指纹一致）；④ 几何有效；⑤ 与 native 交叉比较并记录差异
  原因（本表）。

---

## 7. 确定性（可复现性）

同一输入 + 同一参数，两次运行（不同任务目录）必须得到完全相同的：

- `provenance.determinism.fingerprint_sha256`（掩膜字节 + 边界 WKB + 规范化参数）
- `mask.tif` 文件 SHA256
- `boundary.geojson` 文件 SHA256
- `analysis_dem.tif` 文件 SHA256
- `params_sha256`、`boundary_wkb_sha256`

测试 `test_native_determinism_two_runs_identical` /
`test_grass_determinism_two_runs_identical` 逐项断言。`boundary.geojson` 不含
时间戳，因此文件字节可复现；时间戳只出现在 `provenance.json`。

---

## 8. 几何、分段、复核图

### 8.1 几何（`geometry.py`）

- `validate_geometry`：有效性/自交、环闭合、环方向（RFC 7946）、连续重复点、
  细长片（紧凑度 `4πA/P²` 与最小外接矩形短长边比）、多部件、洞；全部问题写入
  中文 `issues`，**只报告不改几何**。
- `measure`：米制水平面积/周长/顶点数/部件/洞 + 方法与 CRS；拒绝地理 CRS。
- `simplify_preserving`：容差上限 `max(1 m, 1%×等效半径)`，超限显式拒绝；
  `preserve_topology=True`；简化后几何无效则拒绝（不静默 make_valid）；
  保护点只在**本来就在边界附近**（距离 ≤2×容差）时插回，内部出口（如碗心 sink）
  不会被硬插成尖刺（会记录到 `skipped_protected_points`）；报告含顶点数变化、
  Hausdorff 偏差、面积变化与 warnings。
- `revise_with_anchors`：按锚点移动最近顶点，超过 `max_move_m` 显式报错；
  报告移动量、面积/周长变化。

### 8.2 分段（`segment.py`）

- `segment_boundary(boundary, n_segments | max_len_m, ...)` → `BoundarySegment`
  列表；段端点吸附到**原始顶点**，锚点 ID `A-<段号:02d>-<原始顶点索引:05d>`，
  1 号段起点为出口（优先 `outlet_lonlat`，其次 GeoJSON 属性
  `outlet_vertex_index`）；每段含长度/平均/最低/最高高程/顶点数/bbox。
- `list_segments(boundary_geojson, n_segments=8, metric_crs=None)`：worker/工具
  层接口，返回 dict 列表（`segment_id` 从 1 开始、锚点 ID、长度、顶点数、
  `mean_elev_m`、`bbox_lonlat`）。
- `segments.geojson` 属性同时写 `bbox_lonlat`（顶点经纬度 bbox）与折线坐标，
  避免与米制 bbox 混用（F-11 回归）。

### 8.3 复核图（`review.py`）

- `render_overview(result, extra_layers=None)` → `ReviewFigure`（契约）；
- `render_review_pack(result, out_dir, max_pixels=1600)` → worker 接口
  `{"ok","warnings","figures"}`：概览 + 默认 4–8 段局部图，写
  `<out_dir>/review/manifest.json`；
- `render_segment(result, segment_id, out_dir=None, max_pixels=1600)` → 单段
  dict（同样结构）；
- `render_hillshade_contours(dem_path, out_path, ...)` → 通用地形面板。

每张图包含：hillshade 底图、带等高距标注的等高线、河网、出口星标、候选边界、
编号分段（高亮目标段 + 锚点 ID）、比例尺、指北针（网格北）、范围/CRS/像元尺寸/
面积/引擎/等高距/数据来源文字、`offline computed` 与
`verified_against_real_device=false` 标识。

实测（two_bowl_ridge，max_pixels=1200）：5 张图（overview + seg-01..04），
PNG 可被 PIL 解码，尺寸约 654–1047 px、63–137 KB，远低于 4 MB 上限。
`figure.bbox_lonlat` 与 `figure.crs="EPSG:4490"` 口径一致（F-11 修复）：
分段图 bbox 取自分段顶点的经纬度 bbox，与 `segment.bbox_lonlat` 误差 ≤1e-6 度。

**分段图布局（经 DSH 真实视觉模型 vision3 反馈后的最终修复）**：

- **带地形上下文的显示范围**：以段 bbox 为基准每侧外扩 `max(300 m, 0.25×段长)`、
  最小边长约 600 m，并平移到 DEM 覆盖范围内；再按排版需要收窄过长的一边
  （目标显示长宽比 ≥0.37），既保留两侧地形/邻接分段，又避免大面积白带。
- **口径分离**：`figure.bbox_lonlat` 始终是段**真实 bbox**（F-11 修复保持）；
  `figure.view_bbox_lonlat` 是外扩后的**显示范围**；图注写明"显示范围已外扩
  用于地形上下文（段真实 bbox 见 bbox_lonlat）"。
- **两种自动布局**：地图面板长宽比 ≤0.9 用 `stack`（地图在上、两列紧凑信息条
  在下、图注并入信息条）；>0.9 用 `side`（地图在左、信息在右）。两种布局
  信息面板都在图外，比例尺/指北针固定在地图面板角落，互不遮挡。
- **地图主体占比**：分段图地图面板高度占比目标 ≥60%（布局元数据
  `figure.layout.map_panel_height_fraction`，实测 61.4%–83.6%）；图幅纵横比
  限制在 [0.5, 2.2]（预留 `bbox_inches="tight"` 裁剪余量）。
- **窄图刻度防粘连**：窄图（图幅纵横比 <0.9 或地图面板宽 <420 px）x 轴主刻度
  用 `MaxNLocator(nbins=4)`（≤5 个）并旋转 35°（ha=right）；元数据记录
  `x_major_tick_count` / `x_tick_rotation_deg` / `narrow`。
- **颜色语义统一**：橙色 = 编号分段（overview 全部统一橙色 + 图例说明；
  分段图目标段橙色加粗、其它段灰色弱化），红色 = 候选边界，蓝色 = 河网，
  红星 = 出口。
- **概览图**：显示范围外扩 4%（`figure.layout.view_padding_fraction=0.04`，
  信息框落在边缘地形上）；信息面板新增"关键警告"行（`truncated=true` /
  出口无出流方向 / 垂直基准未知 / DSM-DTM 未知，有则显示）与图例行；
  方形 DEM 保持图内叠加（vision3 已验证 72.8% 地图占比）。
- 实测（two_bowl_ridge，max_pixels=1200）：seg-01（东西向 900 m）
  **1171×557（纵横比 2.10、地图面板 61.4%）**；seg-02（南北向 360 m）
  **1179×763（纵横比 1.55、地图面板 77.6%）**；x 轴刻度 7–8 个可读。
  竖版/窄图用例（新增 600 m×1800 m 长条收敛谷 fixture）：
  **788×929（纵横比 0.85、地图面板 83.6%）、x 轴主刻度 5 个且旋转 35°**。
  所有分段图信息面板与比例尺/指北针矩形不相交（测试断言）。
- 细长 DEM 的 overview 自动改用图外信息面板；方形 DEM 保持图内叠加。


---

## 9. 异常路径与显式失败

| 情况 | 行为 |
|---|---|
| DEM 缺 CRS | `inspect_dem` 警告 + `provisional=true`；引擎 `prepare_analysis_dem` 显式报错 |
| DEM 高程全 0 / 常数 / 超量程 / 高 NoData | 警告 + `provisional=true` |
| 出口在 DEM 范围外 | `ok=false` + `failure.json`（错误信息含行列号） |
| 出口在 NoData 且 `snap_radius_m=0` | 显式失败，不悄悄挪出口 |
| 吸附半径内无有效像元 | 报错并给出最近有效像元距离 |
| 出口在洼地/无出流像元 | 警告"出口所在栅格无出流方向…不填洼" |
| 分析范围截断 | `truncated=true` + 警告（掩膜接触边界像元数） |
| NoData 空洞紧邻流域 | 警告 + 像元计数；NoData 墙造成局部洼地/掩膜洞时如实报告 |
| 出口累积与掩膜像元数不一致 | 警告（>0.1%） |
| 简化容差超限 | 显式拒绝并保留原边界（warning，不静默） |
| 参数非法（DEM 不存在、出口非数值、负半径、未知 engine） | 抛 `DataValidationError`（唯一抛异常的情况） |
| 引擎不可用（GRASS 缺失/版本失败） | `ok=false` + `failure.json` + `ENGINE_UNAVAILABLE` |
| 复核图生成失败 | 只加 warning，不阻断边界成果；`render_segment` 返回 `ok=false` 不伪造图 |

成果文件名稳定：`boundary.geojson`、`mask.tif`、`provenance.json`、
`result.json`；失败写 `failure.json`（含 error/error_code/traceback）。
`load_result(task_dir)` 可回读成功或失败成果。

---

## 10. 与架构契约的差异记录（只增不改）

`docs/architecture.md` §5 的字段/签名全部保留；以下为**只增不改**的补充
（Lead 已确认）：

1. `WatershedResult` 增加字段：`boundary_crs`（默认 `EPSG:4490`）、
   `outlet_vertex_index`（出口在边界顶点中的索引，用于分段锚定）、
   `truncated`（分析范围截断布尔）、`error_code`；
   增加 `to_dict()` / `from_dict()`（`result.json` 序列化）。
2. `engine.py` 增加稳定文件常量与 `load_result(task_dir)`；
   `run_watershed` 失败返回 `ok=False` 而不抛异常（仅参数非法抛
   `DataValidationError`，符合 Lead 的 worker 契约）。
3. `review.py`：`render_segment` 按 Lead 的 tools 契约返回
   `{"ok","warnings","figures"}` dict（而不是架构 §5 的 `ReviewFigure`）；
   `ReviewFigure` 仍是 `render_overview`/`render_hillshade_contours` 的返回类型。
4. `segment.py` 增加 `list_segments`、`choose_segment_count`、
   `segments_to_geojson`。
   `geometry.simplify_preserving` 返回 `SimplificationResult`：既支持
   `geometry, report = ...` 解包（本包内部/测试），也支持 `result.report`
   属性访问与 dataclass 序列化（tools 层直接 `jsonable` 不会崩）。
5. `WatershedParams.extra` 支持的键（全部可选）：
   `max_cells`（默认 16,000,000）、`resampling`（bilinear/nearest/cubic）、
   `n_segments`、`segment_max_len_m`、`stream_threshold_cells`、
   `render_overview`（默认 true）、`grass_cmd`、`grass_timeout_s`（默认 1800）、
   `grass_keep_staging`。
6. `min_accum_cells` 语义：作为河网阈值下界与"出口是否在汇流线上"的警告阈值，
   **不**裁剪流域掩膜（流域必须包含全部上游像元）。

---

## 11. 未验证项与限制（必须如实报告）

1. **无独立人工审核的工程边界**：所有边界正确性证据来自合成解析真值与算法
   自洽性，没有用户/测绘审核边界；`verified_against_real_device=false`。
2. **无 Windows 奥维客户端**：本机 macOS，无法验证真实写入/回读/面积口径；
   相关证据只能来自模拟协议（Teammate A/Lead）。
3. **无真实工程 DEM**：本会话未能下载 Copernicus GLO-30 tile（沙箱到 S3 的 GET
   约 3 KB/s，50 MB 不可行；HEAD 200 正常）。真实地形管线**未跑通**，脚本已交付。
4. **DSM 非 DTM**：公开 30 m 数据为 DSM（含植被/建筑），分水岭可能偏移；
   没有独立等高线/实测点做精度校核。
5. **GRASS SFD 与严格 D8 不等价**（§6.1）：两引擎结果不保证一致；GRASS 在
   径向对称/平坦地形上可能把流域分解，需人工复核；不能混接方向编码。
6. **不填洼**：真实洼地/水库/涵洞会截断上游；`native` 与 GRASS 都按各自算法
   处理，成果会显式警告，但不代表工程汇水范围。
7. **高程基准**：DEM 文件普遍不声明垂直基准；未做 EGM2008/大地高改正。
8. **简化/修订**：受控简化有容差上限；锚点修订只做顶点级移动，不做局部重算
   （`revise_candidate` 的"重算"由 Lead 的 jobs 层调度）。
9. 复核图是"可读的人工判读材料"，不是精度证据；图上等高线来自同一 DEM 插值，
   与边界不是独立数据源。

---

## 12. 证据分级声明

- `official_document`：GRASS `r.watershed`/`r.water.outlet` 行为、Copernicus DEM
  数据规格（引用官方页面，见 `.research/`）。
- `offline_computed`：本文件中所有解析真值结果、确定性指纹、GRASS 本机实测、
  复核图生成 —— 均为本机确定性计算。
- `real_device_pending`：Windows 奥维真实读写/回读、用户审核边界、真实工程 DEM
  精度校核。

---

## 13. 复现命令

```bash
cd "/Users/huangsirsmacbookairm5/Public/code/project/omap mcp"

# 1) 生成解析真值合成 DEM（如需重建 fixture）
MPLCONFIGDIR=$PWD/.cache/mpl .venv/bin/python scripts/make_synthetic_dem.py

# 2) GIS 测试（GRASS 可用则真实运行，不可用则 skip 并记录）
MPLCONFIGDIR=$PWD/.cache/mpl .venv/bin/python -m pytest tests/gis -q

# 3) 全仓测试
MPLCONFIGDIR=$PWD/.cache/mpl .venv/bin/python -m pytest -q

# 4) 手工跑一个解析场景（native）
.venv/bin/python - <<'PY'
import json
from pathlib import Path
from ovital_mcp.gis import WatershedParams, run_watershed
fx = Path("tests/fixtures/synthetic")
sc = json.loads((fx/"expected.json").read_text())["scenarios"]["two_bowl_ridge"]
res = run_watershed(WatershedParams(
    dem_path=str(fx/sc["dem"]),
    outlet_lonlat=tuple(sc["outlet_lonlat"]),
    engine="native", snap_radius_m=0.0,
), Path("/tmp/two_bowl_run"))
print(res.ok, res.area_m2, res.flow_accum_cells)   # True 324000.0 360
PY

# 5) 手工跑 GRASS 引擎（需要 .conda/grass 或 PATH 中的 grass）
.venv/bin/python - <<'PY'
import json
from pathlib import Path
from ovital_mcp.gis import WatershedParams, run_watershed
fx = Path("tests/fixtures/synthetic")
sc = json.loads((fx/"expected.json").read_text())["scenarios"]["tilted_plane"]
res = run_watershed(WatershedParams(
    dem_path=str(fx/sc["dem"]), outlet_lonlat=tuple(sc["outlet_lonlat"]),
    engine="grass", snap_radius_m=0.0,
), Path("/tmp/grass_plane_run"))
print(res.ok, res.engine_version, res.area_m2)     # True GRASS 8.5.0 54000.0
PY

# 6) Copernicus 真实 DEM（本会话因网络限速未完成；网络正常时）
.venv/bin/python scripts/fetch_copernicus_dem.py --dest-dir .cache/copernicus-dem
# 然后用 fetch 输出的 tile 路径作为 dem_path 跑 run_watershed，
# 报告必须标注"真实 DEM、无独立审核边界，仅可复现性/合理性检查"
```
