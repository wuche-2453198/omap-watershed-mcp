# 集水区成果报告 j-20261029-181428-9c0ac0

- 生成时间（UTC）：2026-10-29T18:14:36+00:00
- 任务 ID：`j-20261029-181428-9c0ac0`；任务类型：watershed
- 证据级别：offline_computed（本机计算，未经真实 Windows 奥维联机与人工审核边界验收）
- 模拟连接：False；real_device_verified：false

## 1. 输入与参数

- DEM：`/Users/huangsirsmacbookairm5/Public/code/project/omap mcp/.cache/realdem/beijing_west_srtm.tif`
- DEM SHA256：`f4bd61497a84f6bb1fe34a9f6d51764f386345dcd809f333d8877f37a8c7a11f`
- 计算引擎：native（native-d8/1 (numpy 2.5.4)）
- 分析投影 CRS：EPSG:32650
- 出口输入：[115.6736125, 39.8525625]；出口采用：[115.67343660332646, 39.85251030352568]；吸附距离：16.125981511414434 m
- 分析范围（EPSG:4490 bbox）：[114.97087416758738, 38.98682254715339, 116.01457580118, 40.01297696931619]

## 2. 成果量算

| 项目 | 数值 | 口径 |
|---|---|---|
| 水平面积 | 19673902.93067287 m² | shapely 在 EPSG:32650 的平面面积 |
| 周长 | 28797.62639426929 m | 同上 |
| 几何有效 | True | 闭合/自交检查 |
| 部件/洞 | multipart=False / holes=0 | |

## 3. 复核图

- `overview`：/Users/huangsirsmacbookairm5/Public/code/project/omap mcp/.cache/leadtest/data-regen/jobs/j-20261029-181428-9c0ac0/artifacts/review/overview.png（segment=None）
- `seg-01`：/Users/huangsirsmacbookairm5/Public/code/project/omap mcp/.cache/leadtest/data-regen/jobs/j-20261029-181428-9c0ac0/artifacts/review/seg-01.png（segment=1）
- `seg-02`：/Users/huangsirsmacbookairm5/Public/code/project/omap mcp/.cache/leadtest/data-regen/jobs/j-20261029-181428-9c0ac0/artifacts/review/seg-02.png（segment=2）
- `seg-03`：/Users/huangsirsmacbookairm5/Public/code/project/omap mcp/.cache/leadtest/data-regen/jobs/j-20261029-181428-9c0ac0/artifacts/review/seg-03.png（segment=3）
- `seg-04`：/Users/huangsirsmacbookairm5/Public/code/project/omap mcp/.cache/leadtest/data-regen/jobs/j-20261029-181428-9c0ac0/artifacts/review/seg-04.png（segment=4）
- `seg-05`：/Users/huangsirsmacbookairm5/Public/code/project/omap mcp/.cache/leadtest/data-regen/jobs/j-20261029-181428-9c0ac0/artifacts/review/seg-05.png（segment=5）
- `seg-06`：/Users/huangsirsmacbookairm5/Public/code/project/omap mcp/.cache/leadtest/data-regen/jobs/j-20261029-181428-9c0ac0/artifacts/review/seg-06.png（segment=6）
- `seg-07`：/Users/huangsirsmacbookairm5/Public/code/project/omap mcp/.cache/leadtest/data-regen/jobs/j-20261029-181428-9c0ac0/artifacts/review/seg-07.png（segment=7）
- `seg-08`：/Users/huangsirsmacbookairm5/Public/code/project/omap mcp/.cache/leadtest/data-regen/jobs/j-20261029-181428-9c0ac0/artifacts/review/seg-08.png（segment=8）

## 4. 警告与限制

- [DEM] CRS=EPSG:4326，地理坐标系，官方轴序=latlon；栅格读取按 x=经度/easting、y=纬度/northing（rasterio 约定）
- [DEM] DEM 是地理坐标系，分辨率以「度」为单位；面积/距离/吸附必须投影到米制 CRS（引擎会自动处理并记录）
- [DEM] 分辨率 0.000277778°×0.000277778°，在中心纬度约 23.9 m×30.7 m
- [DEM] 垂直基准未知：文件未声明垂直基准，无法判断高程是大地高（椭球高）还是正高/正常高；同一区域内混用不同基准会带来米级到数十米级系统偏差。Copernicus GLO-30 官方为 EGM2008 正高，但 GeoTIFF 本身不声明，需按数据源文档另行确认。
- [DEM] 无法从文件判断数据是 DSM 还是 DTM：若为 DSM（如 Copernicus GLO-30），树高/建筑会抬高地表，分水岭可能偏移，工程边界应结合实测/等高线复核
- 分析投影：EPSG:32650；分析栅格 3186×4077，分辨率 27.5839 m，像元面积 760.873 m²
- DEM 已重投影/重采样到 EPSG:32650（bilinear）；重采样会给高程与坡度带来小幅平滑，窄山脊/平缓分水岭需复核
- 出口吸附（native）：输入点 (386535.00, 4412235.00) → 像元 (行 632, 列 2165)，距离 16.13 m，半径内最大汇流累积 25857 像元
- 出口已从输入点吸附到汇流累积最大的栅格（16.13 m）：计算使用吸附后的出口；原始出口与吸附出口均记录在 provenance
- 河网：汇流累积 ≥ 200 像元的河道，链化后 70 条折线（仅用于复核展示）
- 出口所在栅格没有出流方向（洼地/平地/分析范围边界）：上游范围以该栅格为终止；引擎不填洼，若该处是真实洼地/水库/人工拦蓄，请人工确认上游边界是否应另行定义
- 面积相近不代表边界正确；本边界未与独立人工/测绘参考比较。
- 奥维 20802 的面积口径未实测，落图回读面积不可直接与本地米制面积等同。
- 高程基准/DSM-DTM 情况见 DEM 检查警告；真实工程精度需更高精度数据与人工复核。

## 5. 未完成的验收项

- 真实 Windows 奥维联机读写与回读
- 用户账号 VIP/SVIP 权限与地图图片/等高线接口
- 用户提供的 DEM、坐标系统与经审核参照边界的精度验收
- DSH 视觉模型在真实会话中接收复核图并完成判读（由独立验证步骤完成）

## 6. 复现

- 任务参数：`{"dem_path": "/Users/huangsirsmacbookairm5/Public/code/project/omap mcp/.cache/realdem/beijing_west_srtm.tif", "outlet_lonlat": [115.6736125, 39.8525625], "engine": "native", "snap_radius_m": 0.0, "metric_crs": null, "target_res_m": null, "min_accum_cells": 200, "simplify_tolerance_m": 0.0, "review_max_pixels": 1200, "grass_cmd": "grass", "max_cells": 20000000, "data_root": "/Users/huangsirsmacbookairm5/Public/code/project/omap mcp/.cache/leadtest/data-regen", "latlng_type": 1, "task_id": "t-20261029-181428-e4f2ce"}`
