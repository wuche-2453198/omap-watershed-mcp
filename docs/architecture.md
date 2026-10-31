# OMAP Watershed MCP — 架构与接口契约（实施基线）

本文件是并行实施的**唯一接口契约**。各模块按此契约实现，禁止擅自修改共享签名；需要变更先在共享任务中提出，由 Lead 统一修改本文件。

依据：《OMAP_MCP_可行性与实施方案.md》第 3–7 节、《DSH_实施任务书.md》A–E。

## 0. 范围与本机事实

- 目标交付：Windows 本地标准 MCP（stdio）+ 奥维桌面 WebSocket 适配器 + 确定性 GIS 集水区计算 + 复核图 + 草稿落图/回读/回滚 + DSH Cordis patch + Skill + 文档与测试。
- 开发机事实：macOS 26.6.2 arm64；无奥维客户端、无 Windows、无用户 DEM/参考边界、无用户奥维账号。
- 因此：**真实 Windows 奥维联机验收 = 未完成**，只能用「文档级协议实现 + 官方样例回放 + 模拟服务器」闭环验证；**用户边界验收 = 未完成**。所有证据文件必须显式标注 `verified_against_real_device: false`，禁止把模拟结果说成真实联机结果。
- 可用 Python 环境：项目内 `.venv`（Python 3.13，已装 mcp 2.3、rasterio、shapely、pyproj、numpy、scipy、matplotlib、pillow、websockets、pytest）。所有命令用 `.venv/bin/python`。

## 1. 仓库布局与写入边界

```
pyproject.toml
README.md
ovital_mcp/
  __init__.py            # Lead
  __main__.py            # Lead: stdio 入口（python -m ovital_mcp）
  config.py              # Lead
  errors.py              # Lead
  coordinates.py         # Lead: 坐标语义/轴序/CRS 工具（协议与 GIS 共用）
  store.py               # Lead: 任务目录、manifest、快照、回滚
  jobs.py                # Lead: 持久任务（子进程 worker、状态、取消）
  worker.py              # Lead: worker 入口（执行 GIS 计算并写状态/成果）
  app.py                 # Lead: AppContext（config/store/jobs/ovital/geo 懒加载）
  server.py              # Lead: MCPServer 定义 + 工具注册（18 个工具）
  tools/                 # Lead: 各工具实现（只依赖 protocol/gis 公开接口）
  protocol/              # Teammate A
    __init__.py, constants.py, models.py, codec.py, client.py,
    commands.py, errors.py
  mock/                  # Teammate A
    __init__.py, server.py, world.py, faults.py, fixtures/*.json
  gis/                   # Teammate B
    __init__.py, dem.py, crs.py, engine.py, native.py, grass.py,
    geometry.py, review.py, segment.py
  dsh/                   # Teammate C
    ovital.cordis.yml, config.example.yaml, README.md
.dsh/skills/ovital-watershed/SKILL.md   # Teammate C
scripts/                 # C: 安装脚本；B: DEM 获取；A: 回放工具；D: 验证脚本
tests/                   # 按包分文件：test_protocol_*.py (A), test_gis_*.py (B),
                         #  test_config/test_store/test_jobs/test_tools_*.py (Lead),
                         #  integration/ (D)
docs/                    # 各自文档；offline-acceptance.md 由 D 最终汇总
```

**并行写入纪律**：只写自己任务写作用域内的文件。公共文件（pyproject.toml、README.md、docs/architecture.md、ovital_mcp/{config,errors,coordinates,store,jobs,app,server}.py、tools/）归 Lead。发现契约需要变更 → 在 task 里记录并消息 Lead，不直接改公共文件。

## 2. 质量红线（所有模块共同遵守）

1. stdout 只允许 MCP 协议帧；所有日志/进度写 stderr 或文件。**任何 print 到 stdout 都是缺陷。**
2. MCP 进程启动不得产生奥维写入副作用；奥维连接必须懒建立（首次调用需要它的工具时才连）。
3. 凭据（PSK）不得出现在工具返回、日志、异常栈、任务文件、provenance 中；对外统一脱敏。
4. 失败必须显式失败（`ret<0` / 异常 / `status=failed`），禁止伪成功或把失败伪装成空数据。
5. 写入必须带幂等键 + 写后回读；断线/超时不得盲目重发"创建"导致重复对象。
6. 不得为了凑目标面积修改边界；不得静默简化破坏拓扑；不伪造坐标或精度。
7. 每份成果带 provenance：输入文件 SHA256、CRS、参数、引擎与版本、时间、`verified_against_real_device`、`simulated` 标记。
8. 协议字段/命令号/枚举只允许引用 `docs/protocol-notes.md` 中有官方来源的值；未验证项写 `待实测`，不得编造。

## 3. 坐标契约（Lead 实现，A/B 必须复用，不得另写一套）

文件：`ovital_mcp/coordinates.py`

- 内部规范坐标：`CANONICAL_CRS = "EPSG:4490"`（CGCS2000 地理坐标，**经度在前 lon,lat**）。CGCS2000 与 WGS84 差异为厘米级，报告中说明；需要 WGS84 时用 pyproj 显式转换并记录。
- 坐标语义枚举 `CoordSemantic`：`SYSTEM / CGCS2000 / GCJ02 / PLANE / MERCATOR`。
- **四套互不相同的协议枚举必须分开解析**（V2.7 手册的已知陷阱，见 `docs/protocol-notes.md`）：
  - `COORD_TYPE_TOP`（顶层 CoordType，手册 2.2.2）：0=CGCS2000, 1=GCJ02, 2=SYSTEM
  - `LATLNG_TYPE`（LatlngType，手册 2.2.2/2.3.11）：0=SYSTEM, 1=CGCS2000, 2=GCJ02, 3=PLANE, 4=MERCATOR
  - `OMAP_LATLNG_COORD_TYPE`（OmapLatlng.CoordType，手册 2.4.1）：0=GCJ02, 1=CGCS2000, 2=PLANE
  - `SHAPE_GCJ02`（Shape.ObjectDetail.Gcj02，手册 2.4.8）：0=CGCS2000, 1=GCJ02, 2=PLANE
- 轴序：Shape.Latlng 是**扁平的 lat,lng,lat,lng…（纬度在前）**（手册 2.4.8 示例 39.01,106.57 可证）。OmapLatlng 结构是 `lat`/`lng` 命名字段。对外统一 `(lon, lat)`。所有解析/构建函数必须在 docstring 写明轴序。
- GCJ-02 与 CGCS2000 的互转：实现标准公开算法，但标记 `approximate`，仅当配置 `allow_gcj02_conversion=true` 时允许自动转换，否则抛 `CoordinateSemanticError` 并提示改用 LatlngType=1 读取。
- 面积：绝不用 EPSG:4490 的地理坐标直接算面积；必须投影到米制 CRS（`metric_crs_for(lon, lat, kind="utm")`，UTM 自动分带）用 shapely 计算，记录 EPSG 与算法。
- 公开 API（固定签名）：
  ```python
  CANONICAL_CRS: str
  class CoordSemantic(str, Enum): SYSTEM, CGCS2000, GCJ02, PLANE, MERCATOR
  @dataclass(frozen=True) class LatLng: lat: float; lng: float; semantic: CoordSemantic
  def semantic_for_top_coord_type(v: int) -> CoordSemantic
  def semantic_for_latlng_type(v: int) -> CoordSemantic
  def semantic_for_omap_latlng_coord_type(v: int) -> CoordSemantic
  def semantic_for_shape_gcj02(v: int) -> CoordSemantic
  def latlng_to_canonical(latlng: LatLng, *, allow_gcj02_conversion: bool = False) -> tuple[float, float]  # (lon, lat)
  def canonical_to_latlng(lon: float, lat: float, semantic: CoordSemantic, *, allow_gcj02_conversion: bool = False) -> LatLng
  def gcj02_to_cgcs2000(lon: float, lat: float) -> tuple[float, float]
  def cgcs2000_to_gcj02(lon: float, lat: float) -> tuple[float, float]
  def metric_crs_for(lon: float, lat: float, *, kind: str = "utm") -> str
  def transform_geometry(geom, src_crs: str, dst_crs: str)
  def area_m2(geom, crs: str) -> float
  def verify_canonical_lonlat(lon: float, lat: float) -> tuple[float, float]
  def crs_axis_order(crs: str) -> str  # "lonlat" | "latlon" | "easting_northing"
  ```
- 异常：`CoordinateSemanticError`（定义在 `errors.py`）。

## 4. 协议包契约（Teammate A）

依据：`.research/ovital-scratch/manual-32.md`（WebSocket V2.7 手册，官方 URL https://www.gpsov.com/appdoc/index.php?id=32）、官方样例 zip（`.research/ovital-scratch/zip-32/omapInterfaceWeb_latest/`，含 `js/omapinterface.js`、`js/omapobj.js`）。必须逐项引用手册行号/章节到 `docs/protocol-notes.md`。

### 4.1 `ovital_mcp/protocol/models.py`

```python
@dataclass(frozen=True) class OvitalVersion: ret:int; app_id:str|None; raw:dict
@dataclass class ObjNode:                     # 20101/20110/20104 返回节点
    obj_id: int; type_id: int; name: str|None; parent_id: int|None; srv_id: str|None
    tm_modify: str|None; raw: dict; children: list["ObjNode"]  # 详情树用
@dataclass class ShapeObject:
    obj_id: int; name: str; comment: str; parent_id: int|None
    vertices: list[tuple[float,float]]        # 规范 (lon,lat) EPSG:4490
    source_semantic: CoordSemantic; axis_order_used: str; raw: dict
@dataclass class ShapeMetrics: obj_id:int; point_count:int; area_count:int; area_m2:float; perimeter_km:float; raw:dict
@dataclass class SelectedObject: obj_id:int; type_id:int; name:str|None; raw:dict
@dataclass class ObjectWriteResult: obj_id:int; type_id:int; parent_id:int|None; raw:dict
```
`parse_*` 必须容忍手册与示例的字段名差异（如 20802 表 `iPointCnt/dPerimeter/dAreaSize` vs 示例 `PointCnt/Perimeter/AreaSize`）；缺字段抛 `ProtocolDecodeError`，但保留 raw。

### 4.2 `ovital_mcp/protocol/client.py`

```python
class OvitalClient:
    def __init__(self, endpoint: str, psk: str, *, connect_timeout_s: float = 5.0,
                 request_timeout_s: float = 10.0, logger=None)
    connected: bool
    async def connect(self) -> OvitalVersion      # 发 20000，md5(psk).upper()[:16]；ret==版本号；ret==0 用 msg.errmsg 抛 AuthError
    async def close(self) -> None
    async def request(self, cmdid: int, msg: dict | None = None, *, timeout: float | None = None) -> OvitalResponse
    async def next_event(self, cmdid: int | None = None, timeout: float | None = None) -> OvitalResponse
    async def __aenter__/__aexit__
```
硬性要求：
- 响应只按 cmdid 关联（协议无请求 ID）：**同一 cmdid 的请求必须串行**（每 cmdid 一把 asyncio.Lock）；不同 cmdid 可并发。
- 未被等待的响应/事件按 cmdid 进事件队列（如 20008 奥维被关闭、20009 保存按钮、20015 对象变动）。
- 连接断开：正在等待的请求以 `OvitalConnectionError` 失败（不自动重试）；`request()` 不自动重连，由上层决定；重连后不得自动重发写请求。
- 日志只记录 cmdid、耗时、ret、脱敏 msg；PSK 与 md5psk 永不落日志。
- `endpoint` 仅支持 `ws://`/`wss://`；端口未配置时用 config 值，不得自造默认（文档示例 7699 只是示例，配置必须显式）。

### 4.3 `ovital_mcp/protocol/commands.py`

高层命令封装（供 `tools/` 直接调用；返回模型对象）：

```python
async def get_version(client) -> int                                # 20006
async def get_vip_level(client) -> dict                             # 20501
async def get_selected(client, flag: int = 1) -> list[SelectedObject]          # 20104
async def get_selected_tree(client, latlng_type: int = 1) -> list[ObjNode]     # 20110
async def get_objects(client, obj_id: int, latlng_type: int = 1) -> list[ObjNode]  # 20101
async def get_shape(client, obj_id: int) -> ShapeObject             # 20101 后按 Shape 结构解析
async def get_shape_metrics(client, obj_id: int) -> ShapeMetrics    # 20802
async def write_objects(client, *, parent_id: int, name: str, children: list[dict]) -> list[ObjectWriteResult]  # 20112
async def delete_objects(client, obj_ids: list[int], *, no_group: int = 1) -> None  # 20103
async def get_map_image(client, *, bbox_lonlat: tuple[float,float,float,float],
                        max_level: int, max_pixel: int, coord_type: int = 0) -> bytes  # 20301, PNG bytes
async def get_contours(client, *, bbox_lonlat, coord_type: int = 0, **kwargs) -> dict  # 20303
async def get_viewport(client) -> dict                              # 20730 / 20016 / 20212 组合
async def get_screen_corners(client) -> dict                        # 20016
async def get_system_crs(client) -> dict                            # 20222
```
`commands.py` 只做协议编解码与单次调用；业务重试、幂等、快照由 Lead 的 tools 层实现。

### 4.4 `ovital_mcp/mock/server.py`（模拟奥维）

- `websockets` 服务，`MockOvitalServer(psk: str, host="127.0.0.1", port=0)`，`async start()` 返回实际端口，`async stop()`；`url` 属性。
- 模拟命令：20000、20006、20101、20103、20104、20110、20112、20802、20301、20303、20016、20730、20212、20222、20501。行为依据手册；对未实现命令返回 `ret:-4`。
- `world.py`：内存对象树（文件夹/标签/图形），20112 写后 20101 可回读、20802 用局部米制投影真算面积/周长/顶点数；`ObjID` 语义按手册（0 自动生成，非 0 替换）。
- 明确自标识：所有工具返回的 capabilities 带 `simulated: true`、`verified_against_real_device: false`；20301 返回的 PNG 上要画 "SIMULATED MAP" 水印 + 范围文字 + 经纬网。
- `faults.py`：可注入 `drop_on_write`（收到 20112 断开）、`timeout_on(cmdid)`、`duplicate_20112` 等，用于测试幂等/断线防护。
- `fixtures/`：从官方样例与手册示例改写的脱敏 request/response JSON，带 `_source` 字段引用手册章节；PSK 示例统一用测试值，禁止真实凭据。

### 4.5 A 的测试（tests/test_protocol_*.py / test_mock_*.py）

必须覆盖：登录成功/失败、ret=版本号语义、未知 cmdid、串行化（并发同 cmdid 不串包）、事件队列、断线时 pending 失败、reconnect 不重放写、写后回读坐标往返（≤1e-7 度级）、轴序 lat-first、四套坐标枚举各自映射、20802 字段差异、PSK 不泄露（caplog 断言）。

## 5. GIS 包契约（Teammate B）

### 5.1 `ovital_mcp/gis/dem.py`

```python
@dataclass class DemInfo:
    path: str; sha256: str; crs_wkt: str|None; crs_epsg: int|None; is_geographic: bool
    width: int; height: int; res_x: float; res_y: float; nodata: float|None
    min_elev: float|None; max_elev: float|None; nodata_fraction: float
    extent_lonlat: tuple[float,float,float,float] | None   # minx,miny,maxx,maxy EPSG:4490；无 CRS 时为 None
    vertical_datum_known: bool; vertical_datum_note: str
    warnings: list[str]; provisional: bool = False
def inspect_dem(path: str|Path) -> DemInfo
```
必须检查/报告：CRS 缺失、地理/投影、轴序、NoData、量程异常（如全部 0/负值/超过 9000m）、分辨率、覆盖范围、高程基准未知（DSM/DTM 未知同样警告）、文件 SHA256。

### 5.2 `ovital_mcp/gis/engine.py`（对 tools 的唯一入口）

```python
@dataclass class WatershedParams:
    dem_path: str
    outlet_lonlat: tuple[float,float]                    # EPSG:4490
    engine: str = "auto"                                # "auto"|"native"|"grass"
    snap_radius_m: float = 90.0
    metric_crs: str | None = None                       # None=自动
    target_res_m: float | None = None                   # None=保持 DEM 分辨率
    min_accum_cells: int = 3
    simplify_tolerance_m: float = 0.0
    extra: dict = field(default_factory=dict)

@dataclass class WatershedResult:
    task_dir: str
    ok: bool; error: str|None
    boundary_geojson: str | None        # EPSG:4490，属性含 area_m2/metric_crs/engine
    mask_geotiff: str | None            # 二值上游掩膜（分析 CRS）
    area_m2: float | None; perimeter_m: float | None
    geometry_valid: bool; multipart: bool; hole_count: int
    outlet_input: tuple[float,float]; outlet_used: tuple[float,float]
    snap_distance_m: float; flow_accum_cells: int; cell_area_m2: float
    metric_crs: str; engine_used: str; engine_version: str
    analysis_extent_lonlat: tuple[float,float,float,float]
    dem_info: dict; warnings: list[str]; provenance: dict
    artifacts: dict[str, str]

class WatershedEngine(Protocol):
    name: str
    def available(self) -> tuple[bool, str]
    def run(self, params: WatershedParams, out_dir: str|Path) -> WatershedResult
def get_engine(preference: str = "auto") -> WatershedEngine
def run_watershed(params: WatershedParams, out_dir: str|Path) -> WatershedResult
```
- 引擎选择：`auto` → 先 `grass`（若 `grass` 可执行且版本可用），否则 `native`；两者都不可用则报 `EngineUnavailableError`。**必须记录实际用哪个引擎及版本**。
- `native.py`：numpy + rasterio 的确定性 D8 实现（重投影到米制 CRS → D8 流向 → 汇流累积 → 出口吸附到半径内累积最大栅格 → 反向 BFS 上游掩膜 → `rasterio.features.shapes` 矢量化 → 取包含出口的连通部件 → shapely 校验/可选简化 → 回投 EPSG:4490）。**不填洼**（与 GRASS 最小代价算法一致），NoData 空洞显式检查并警告；分析范围若可能截断上游必须警告。
- `grass.py`：subprocess 调 GRASS（`grass_cmd` 配置，默认 `grass`）。用 DEM 建临时 location（`grass -c DEM -e GISDBASE/LOCATION`），`g.region raster=dem -a`、`r.watershed -s elevation=... threshold=... drainage=dir accumulation=acc`、`r.water.outlet`、`r.to.vect`、`v.out.ogr format=GeoJSON`；出口吸附：导出 acc 栅格在 Python 中吸附（复用 native 的吸附工具函数），再 `r.water.outlet` 于吸附点。命令、版本、stderr 摘要写入 provenance。**本机若无法安装 GRASS，保留完整实现 + 配置说明，但不得声称已验证**。
- `geometry.py`：`validate_geometry(geom) -> GeometryCheck`（闭合、自交、环方向、重复点、细长片、多部件、洞）、`measure(geom, metric_crs) -> GeometryMeasure`（水平面积/周长/顶点数/部件/洞）、`simplify_preserving(geom, tolerance_m, protected_points) -> GeometryMeasure + 变更报告`、`revise_with_anchors(geom, anchors, ...)`。几何修复不得静默改拓扑：任何改动进 warnings/报告。
- `segment.py`：`segment_boundary(boundary, n_segments or max_len_m) -> list[BoundarySegment(segment_id, start_anchor, end_anchor, vertices, length_m, mean_elev...)]` 供 `review_segment` 使用；分段锚点 ID 稳定可复现。
- `review.py`：生成复核图（matplotlib，无交互后端）：`render_overview(result, extra_layers) -> ReviewFigure`、`render_segment(result, segment_id) -> ReviewFigure`、`render_hillshade_contours(...)`。每图带：范围/CRS/像素大小/比例尺/指北针/编号分段/出口/河网/候选边界/等高线（标注等高距）；输出 PNG 记录 `ReviewFigure(image_path, title, bbox_lonlat, crs, pixel_size_m, caption, segment_id, data_sources)`。**图必须能被 MCP Image 直接返回**，且每图 ≤ 4MB。

### 5.3 B 的测试与数据

- `scripts/make_synthetic_dem.py`：生成两类合成 DEM（GeoTIFF、带辅助 CRS 与 NoData 变体）：
  1. 解析地形（可数学推导分水岭；例如两坡向中间谷地 + 明确山脊），用于**精确**验证算法；
  2. 含洼地/NoData 洞/边界出口的地形，用于异常路径。
  预期流域掩膜/面积写为 fixture（`tests/fixtures/synthetic/`），测试断言掩膜集合与解析真值一致（cell 级），以及边界面积与解析面积误差阈值。
- `scripts/fetch_copernicus_dem.py`（可选 stretch）：从公开 S3 下载 Copernicus GLO-30 单 tile（无需认证，已实测 206 可访问），用于真实地形可复现性跑通；报告中必须标注 "真实 DEM、无独立审核边界"。
- 测试覆盖：DEM 检查、D8 确定性（两次运行 sha256 一致）、出口吸附报告、NoData/截断警告、几何校验、分段、复核图生成（PNG 可解码、尺寸/文字存在）、native 引擎解析真值。GRASS 测试在 `grass` 不可用时 skip 并在报告记录。

## 6. MCP 工具契约（Lead 实现；Teammate C 的 Skill 依赖此表）

共 **18** 个工具（§6 原设计 17 个 + 辅助工具 `list_jobs`）。服务名 `ovital`；DSH 中工具名 `mcp__ovital__<tool>`。所有返回含 `status`（`ok|error`）、`warnings`、`provenance`、可追踪 ID。`verified_against_real_device` 在本构建中恒为 `false`（只有运维人员完成真实设备验收并显式设置 `ovital.real_device_verified: true` 后才可能为 true，本机未设置）。长计算用持久任务。

| # | 工具 | 关键输入 | 关键输出 / 行为 |
|---|---|---|---|
| 1 | `get_capabilities` | – | 版本、连接状态（懒连接，不写地图）、权限探测结果、命令支持矩阵、限制、`simulated`、`verified_against_real_device`、`unverified[]` 清单 |
| 2 | `get_selection` | `flag=1`, `latlng_type=1` | 选中对象列表（ID/类型/名称），仅 20104；不读大对象 |
| 3 | `read_object` | `obj_id`, `latlng_type=1` | 对象详情：图形顶点（EPSG:4490 lon/lat）、名称、备注、语义、原始字段摘要 |
| 4 | `get_scene` | `bbox_lonlat`, `max_level`, `max_pixel` | 奥维地图图片（MCP Image）+ 元数据；SVIP 不可用时明确报错并给本地复核图替代建议 |
| 5 | `inspect_dem` | `dem_path` | `DemInfo` 全量 + warnings + 数据摘要 |
| 6 | `start_watershed` | `dem_path`, `outlet_lonlat` 或 `outlet_obj_id`（兼容别名 `obj_id`）, `engine`, `snap_radius_m`(0=不吸附), `metric_crs`, `target_res_m`, `min_accum_cells`, `simplify_tolerance_m`, `idempotency_key` | 持久任务 ID；立即返回，不阻塞；幂等键可选 |
| 7 | `get_job` | `job_id` | 状态/阶段/进度/错误/成果摘要；`include_images` 可选返回复核图（Image） |
| 8 | `cancel_job` | `job_id` | 取消运行中任务；终止子进程；幂等 |
| 9 | `review_segment` | `job_id`, `segment_id?`(默认 1), `max_pixels?` | 分段复核图（Image）+ 局部高程/等高线/河网/边界统计 + 锚点 ID；v0.1 不支持 candidate_id/bbox 检索（列待实现） |
| 10 | `revise_candidate` | `job_id`, `outlet_lonlat`, `snap_radius_m`, `min_accum_cells`, `simplify_tolerance_m`, `engine`, `metric_crs`, `anchors_lonlat`, `reason` | 触发重算任务；v0.1 锚点只记录并在 get_job 里做 impact 对比，不强行把边界拉过锚点；不支持 constraints/segment_ids 局部编辑（列待实现） |
| 11 | `validate_geometry` | `geojson` 或 `job_id` | 拓扑/环/重复点/细长片/多部件/洞检查结果 |
| 12 | `measure_geometry` | 同上 + `metric_crs?` | 水平面积/周长/顶点数 + 计算方法/CRS/单位 |
| 13 | `write_draft` | `job_id` 或 `geojson`, `draft_folder`(草稿文件夹名), `name`, `comment`, `parent_folder_id`, `idempotency_key`, `expected_area_m2` | 写独立草稿文件夹（先建/复用文件夹），20112 写图形 + 出口标签；写后回读复核；返回 obj_id、回读坐标/量算差异；写前快照 |
| 14 | `read_back` | `obj_id` | 回读对象 + 20802 量算 + 与本地成果对比（坐标差、面积差、CRS 口径说明） |
| 15 | `finalize_draft` | `obj_id`, `confirm=true`, `new_name?`, `comment?`, `version_note?` | 标记正式成果（20112 替换语义，写前快照）；保存版本记录；不支持移动文件夹（target_folder 列待实现） |
| 16 | `rollback_change` | `change_id`（必填；job 关联可从 change.task_id 查） | 删除本次创建对象 + 尝试恢复被替换对象；幂等；回滚后清除对应幂等键；返回回滚报告 |
| 17 | `export_result` | `job_id`, `format`(geojson/kml/report/projected_json/gpkg) | 导出文件路径 + 摘要（GPKG 需可选 fiona，不可用时显式报错；projected_json 为米制投影非标准 GeoJSON） |
| 18 | `list_jobs` | `limit` | 最近任务列表（job_id/状态/进度），用于恢复上下文（辅助工具，§6 原表 17 个之外新增，2026-10-30 经 Lead 与文档执行方确认） |

行为规则：
- `start_watershed`/`revise_candidate` 用 `jobs.py` 子进程执行，`get_job` 轮询；单次 MCP 调用 < 5s。
- `write_draft` 幂等键保存在任务目录；重复调用返回同一 obj_id，不重复创建；超时后必须 `read_back` 确认而非重写。
- 任何写奥维操作前把将被替换对象的快照（20101 原始 raw）落到 `store` 的 `snapshots/`，供 `rollback_change`。
- `get_capabilities` 必须把"模拟 vs 真实"、"已验证 vs 待实测"分开列出。

## 7. DSH 接入与 Skill（Teammate C）

- `ovital_mcp/dsh/ovital.cordis.yml`：Cordis patch 数组，insert `@deepseek-ai/dsh-mcp-client` 条目：`serverName: ovital`、`transport: stdio`、`command: C:/work/omap-mcp/.venv/Scripts/python.exe`（示例，注释说明替换）、`args: ['-m','ovital_mcp']`、`cwd`、`env: {OVITAL_MCP_CONFIG: ...}`（**不放大密钥**）、`toolCallTimeoutMs: 60000`、`failOnStartupError: false`、`reconnect` 默认。另给 POSIX 版本注释。
- `ovital_mcp/dsh/config.example.yaml`：MCP 配置样例（endpoint/psk_file/data_root/gis engine）。
- `.dsh/skills/ovital-watershed/SKILL.md`：frontmatter `name: ovital-watershed` + `description`（中文，触发词：奥维、集水区、汇水区、流域、分水岭、坝址、取水口）。正文流程：读能力 → 读出口 → 检查 DEM → 起任务 → 分段复核（必须看图）→ 疑点/修订 → 写草稿 → 回读 → 汇报 → 定稿/回滚。必须写入红线：不得为凑面积改边界、不得把影像河谷当山脊、疑点必须标注、无 DEM 降级策略、区分模拟与真实、未验证项声明。
- `docs/capabilities.md`：能力矩阵（官方文档确认 / 本机模拟验证 / 待实测 / 不支持），逐项引用官方来源；会员权限（地图图片/等高线需 SVIP，其他至少 VIP）；不把文档未列能力写成支持。
- `docs/install-windows.md`：Windows 安装（conda-forge 环境：python 3.12、rasterio、shapely、pyproj、numpy、scipy、matplotlib、pillow、websockets、mcp、grass；或分两个环境）、`.venv`/conda 路径、DSH patch 启动方式、奥维客户端设置（系统→系统设置→高级→第三方接口，端口/预共享密钥/IP 白名单）、安全（凭据文件权限、日志脱敏）、验收步骤。
- `scripts/install_windows.ps1`、`scripts/install_posix.sh`、`environment.yml`（conda-forge GIS 环境，锁版本策略说明）。
- `README.md` 技术部分由 C 起草、Lead 终审（先写实现说明，不吹能力）。
- `docs/sample-tasks.md`：3–5 个示例任务（含中文自然语言、预期工具链、如何判断结果不可靠）。

## 8. 开发与验证命令

```bash
cd "/Users/huangsirsmacbookairm5/Public/code/project/omap mcp"
.venv/bin/python -m pytest -q                       # 全部测试
.venv/bin/python -m ovital_mcp                      # 启动 MCP（stdio）
.venv/bin/python scripts/run_mock_ovital.py         # 启动模拟奥维（独立进程）
.venv/bin/python -m pytest tests/gis/test_native_engine.py -q
```

## 9. 证据分级（写入所有报告）

- `official_document`：官方文档明确写到的能力（附 URL/章节）。
- `offline_simulated`：本机对模拟服务器/官方样例回放验证通过。
- `offline_computed`：本机对合成 DEM / 公开真实 DEM 的确定性计算结果（注明数据来源、无人工审核边界）。
- `real_device_pending`：必须在 Windows 奥维实机验证（列出清单）。
- 禁止把 `offline_simulated` 表述为 `real_device_verified`。
