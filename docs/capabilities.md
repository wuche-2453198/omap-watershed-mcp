# 能力矩阵与证据分级

本文回答三个问题：**这套东西能做什么、依据是什么、哪些还没验证**。所有对外承诺以本表为准；文档/代码/汇报出现矛盾时，以“证据等级”最低的一方为准，并立即修正。

- 依据：`docs/architecture.md`（§6 工具契约、§9 证据分级）、`DSH_实施任务书.md`（A/D/E）、`OMAP_MCP_可行性与实施方案.md`（§2/§6/§7）、奥维 WebSocket V2.7 手册（<https://www.gpsov.com/appdoc/index.php?id=32>）、DSH 官方文档。
- 本机环境：macOS 26.6.2 arm64；无 Windows、无奥维客户端、无奥维账号、无用户 DEM/参考边界。版本细节见 `docs/version-record.md`。

## 1. 证据分级（本文所有标注只能用这几级）

| 等级 | 含义 | 能用来证明什么 |
|---|---|---|
| `official_document` | 官方文档/官方示例明确写到的能力（附章节或 URL） | 证明“接口存在且文档这么说”，**不能**证明本机/本账号可用 |
| `offline_simulated` | 本机对模拟服务器、官方样例回放的验证 | 证明协议编解码/流程闭环在本机逻辑上跑通；**不是**真实联机 |
| `offline_computed` | 本机对合成 DEM 或公开真实 DEM 的确定性计算 | 证明算法可复现、几何/面积口径可计算；**无人工审核参考边界** |
| `real_device_pending` | 必须在 Windows 奥维实机上验证的项，当前**未完成** | 当前只能算“待验”，任何“已验证”表述都不成立 |
| `unsupported` | 官方未列出、文档语义不明或本版明确不做 | 不得在实现/文档/汇报中写成支持 |

> 任务书把证据分为 `official_document / offline_simulated / real_device_pending / unsupported` 四类；架构 §9 另拆出 `offline_computed` 专指“合成/公开 DEM 的确定性计算”。本文采用五级，后两类同样**不能**冒充真实设备验证。

## 2. 会员权限要求（先看这一节）

奥维 WebSocket V2.7 手册 **2.1.1 接口使用VIP要求** 原文：

- **获取地图图片数据、获取等高线数据功能需要 SVIP 及以上**；
- **其他功能至少需要 VIP**。

补充事实与注意点：

1. 手册开头（第 5 行）要求安装 Windows PC 桌面端并升级 VIP，然后在“系统 → 系统设置 → 高级 → 第三方接口（窗口消息+Websocket）”开启接口（手册 2.1.2）。端口可自定义，手册示例 `7699` 只是示例，不是默认值。
2. 权限查询接口：`20501 获取VIP等级`（手册 2.3.19，返回 `OmapLevel`）；`20503` 在手册中出现两处语义（2.3.20 “试用SVIP” 与命令表中“判断是否为 SVIP”），以实际响应为准，**待实测**。
3. 权限不足的返回：错误表 2.2.7 定义 `ret=-5`（非 VIP，不支持此 API）、`ret=-7`（非 SVIP，不支持此 API）；但 2.3.29/2.3.30 失败表又写“VIP5 或 VIP5 以上”，与 2.1.1 的“SVIP”表述历史不一致。**以 2.1.1 的会员口径为准，最终以目标账号实际返回实测**（列入 `docs/待核验清单.md`）。
4. “数据能在奥维里显示” ≠ “第三方接口有权批量导出”。图片/等高线能力必须按账号实测，不得按界面可见推断。

## 3. 奥维协议能力矩阵

| 能力 | 命令 | 官方依据 | 证据等级 | 本机状态 / 限制 |
|---|---|---|---|---|
| WebSocket 连接、登录（预共享密钥） | `20000` | 手册 2.1.3.5、2.3.1；官方样例 | `official_document` + `offline_simulated` | 模拟服务器闭环；真实 Windows 鉴权 `real_device_pending`。PSK 派生（md5 后取前 16 位）按官方样例实现，**不得**把派生值写入配置/日志 |
| 查询奥维版本 | `20006` | 手册 2.3.24 | `official_document` + `offline_simulated` | 成功响应 `ret` 为版本号（非 0），失败语义需实测；`real_device_pending` |
| 查询 VIP 等级 / SVIP 判断 | `20501` / `20503` | 手册 2.3.19 / 2.3.20 | `official_document` | 目标账号等级与接口开放情况 `real_device_pending` |
| 读取选中对象基本信息 | `20104` | 手册 2.3.17、2.2.3（Flag 语义） | `official_document` + `offline_simulated` | 只返回 ID/类型/名称，详细坐标需再 `20101`；`real_device_pending` |
| 读取选中对象树形结构 | `20110` | 手册 2.3.18 | `official_document` + `offline_simulated` | `LatlngType` 语义见手册 2.2.2；`real_device_pending` |
| 读取收藏夹对象（含图形） | `20101` | 手册 2.3.11、2.4.8 | `official_document` + `offline_simulated` | 图形 `Latlng` 为扁平数组、示例为 lat,lng 顺序（轴序 `real_device_pending`）；适配器对外统一 `(lon,lat)` |
| 添加/替换收藏夹对象（文件夹/标签/图形/轨迹/路线） | `20112` | 手册 2.3.12、2.4.5–2.4.10 | `official_document` + `offline_simulated` | `ObjID=0` 自动生成、非 0 为**替换**语义 → 写前必须快照；字段/数量限制 `real_device_pending` |
| 删除收藏夹对象 | `20103` | 手册 2.3.13 | `official_document` + `offline_simulated` | 回滚依赖本次变更记录；`real_device_pending` |
| 图形量算（顶点数/周长/面积/区域数） | `20802` | 手册 2.3.48 | `official_document` + `offline_simulated` | 文档标注面积 m²、周长 km；**表格字段名与示例不一致**（`iPointCnt` vs `PointCnt` 等），适配器兼容两者；投影/孔洞口径 `real_device_pending` |
| 获取地图图片数据 / 图片文件 | `20301` / `20302` | 手册 2.3.29 / 2.3.30；openapi 页 | `official_document`（需 SVIP） | 返回地图图片（base64 PNG），**不是**原始 DEM；本机无 SVIP 账号 → `real_device_pending` |
| 获取等高线 | `20303` | 手册 2.3.31；openapi 页 | `official_document`（需 SVIP） | 数据格式/批次/是否可作复核依据 `real_device_pending` |
| 屏幕中心/左上/右下角点 | `20016` | 手册 2.3.58 | `official_document` + `offline_simulated` | 仅视图取证；不能单靠角点保证任意三维视图的像素↔经纬度映射 |
| 奥维窗体可显示区域 | `20730` | 手册 2.3.52 | `official_document` + `offline_simulated` | 最小化时返回 0/负值，工具需显式处理 |
| 当前点像素比例 | `20212` | 手册 2.3.28 | `official_document` | 辅助配准，`real_device_pending` |
| 获取系统坐标系参数 | `20222` | 手册 2.3.51 | `official_document` + `offline_simulated` | `iPrjType`（WGS84/Xian80/Krasovsky/UTM/CGCS2000/Custom）与 `CoordType` 是**另一套**枚举，不可与 LatlngType 混用 |
| 事件：奥维被关闭 / 保存按钮 / 对象变动 | `20008` / `20009` / `20015` | 手册 2.3.2 / 2.3.3 / 2.3.49 | `official_document` + `offline_simulated` | 异步事件无请求 ID，按 cmdid 入队；`real_device_pending` |
| 坐标语义四套枚举 | `CoordType` / `LatlngType` / `OmapLatlng.CoordType` / `Shape.Gcj02` | 手册 2.2.2 / 2.4.1 / 2.4.8 | `official_document` | 四套取值**互不相同**，必须分别解析（例：顶层 CoordType 0=CGCS2000；LatlngType 0=系统坐标系；Shape.Gcj02 0=CGCS2000）；真实值 `real_device_pending` |
| 多区域/孔洞表示 | `20112` / `20802`（`AreaCnt`、`OuterRgnIdx` 等字段） | 手册 2.3.12/2.3.48/2.4.8 | `unsupported`（首版） | 字段存在但语义/限制未文档化 → 首版只写**单区域、单部件**草稿；多区域/洞/超长顶点 `real_device_pending` |
| 对象数量/顶点数上限 | — | — | `unsupported` | 手册未给出明确上限，不承诺大对象；`real_device_pending` |
| 从奥维接口导出原始 DEM | — | — | `unsupported` | 官方能力未列出导出 DEM；DEM 输入按独立 GeoTIFF 设计 |
| 通过 OPEN+ 在奥维内做插件按钮 | — | openapi/OPEN+ 页 | `unsupported`（首版） | 与桌面第三方 WebSocket 是不同入口，首版不做 |

### 3.1 本机离线验证记录（`offline_simulated`，2026-10-30）

以下是对**本地模拟奥维服务器**跑通的闭环证据（由实现/验证方提供，verifier 会独立复验；脱敏样例见 `docs/protocol-samples/` 与 `docs/protocol-notes.md`）：

| 验证项 | 结果 | 说明 |
|---|---|---|
| 登录 `20000` | 返回 `ret=817`（版本号语义） | 证明“成功 ret=版本号、不是 0”的处理分支可跑通；**模拟值，不代表真实客户端版本** |
| 能力探测 `probe_capabilities` | **7 个探测项全部 ok**（覆盖 9 个命令：20006、20501、20222、20104、视图组合 20730+20016+20212、20301、20303） | 证明适配器能解析这些响应结构；单项失败会记录而整体不抛异常；不代表真实账号有对应权限（`docs/protocol-notes.md`） |
| 写入 `20112` | 写 folder + shape + sign，返回 ObjID | 模拟服务器的对象树写入；真实 20112 语义 `real_device_pending` |
| 回读 `20101` | 顶点与写入完全一致（delta=0） | 只证明**模拟服务器**上的坐标往返自洽；真实奥维轴序/语义 `real_device_pending` |
| 量算 `20802` | 返回 `PointCnt/AreaCnt/Perimeter(km)/AreaSize(m²)` | 字段名容错在模拟响应上验证；真实投影/孔洞口径 `real_device_pending` |
| 地图图片 `20301` | 返回可解码 PNG（文件头 magic 正确）。**字节数随 bbox/像素参数变化，不是固定值**：样例 `docs/protocol-samples/simulated-map-20301.png` 为 500×500、16,148 B；另一次请求 21,586 B | 模拟服务器自绘 PNG；真实地图图片需 SVIP 且未实测 |
| 删除 `20103` | 删除成功 | 仅证明删除请求/响应路径；真实删除与回滚 `real_device_pending` |
| 面积口径对照 | 模拟服务器面积与本地米制面积**数量级一致** | 口径（投影/椭球/孔洞）**未验证**，不能当作真实奥维面积，也不能用“接近”证明边界正确 |

> 结论：上述只支持“协议编解码 + 模拟闭环”结论，**不能**写成真实联机验证；真实 Windows 验收见 `docs/待核验清单.md`。

## 4. 本仓库 MCP 工具能力（对外，18 个）

工具名在 DSH 中为 `mcp__ovital__<tool>`；契约以 `docs/architecture.md` §6 为准（契约表 17 个 + 辅助工具 `list_jobs`，共 18 个）。下表“证据等级”指该工具当前**整体**可声称的级别，不因代码存在而升级。

| 工具 | 用途 | 依赖权限 | 证据等级 | 关键限制 |
|---|---|---|---|---|
| `get_capabilities` | 读能力/版本/连接/权限/`simulated`/`unverified` | 连接奥维需 ≥VIP | `official_document` + `offline_simulated`（连接失败也结构化返回） | 不等同真实联机；输出包含 `unverified[]` 清单 |
| `get_selection` | 读当前选中对象 | ≥VIP | `official_document` + `offline_simulated` | 仅 20104 基本信息 |
| `read_object` | 读对象详情/顶点/量算 | ≥VIP | `official_document` + `offline_simulated` | 20802 口径未实测；轴序需实机回读确认 |
| `get_scene` | 取地图图片 + 元数据 | **SVIP** | `official_document` | 像素≠经纬度；配准 `real_device_pending`；失败必须显式报权限错误 |
| `inspect_dem` | DEM 质量检查 | 无 | `offline_computed` | 高程基准/DSM-DTM 未知必须警告 |
| `start_watershed` | 起集水区持久任务 | 无（用 `outlet_obj_id` 作出口时需 ≥VIP） | `offline_computed` | 参数为 `dem_path` + `outlet_lonlat` 或 `outlet_obj_id`（兼容别名 `obj_id`）；长任务走 job 轮询；实际引擎/版本写入 provenance |
| `get_job` | 任务状态/成果/复核图 | 无 | `offline_computed` | `include_images=true` 最多返回 2 张 MCP Image，`review_figure_count` 为总数；图片能否到达模型取决于 DSH/模型视觉能力 |
| `cancel_job` | 取消任务 | 无 | `offline_computed` | 终止子进程；幂等 |
| `list_jobs` | 最近任务列表（辅助上下文恢复） | 无 | `offline_computed` | 仅本地任务记录，不含奥维对象状态 |
| `review_segment` | 分段复核图与统计 | 无（叠加奥维图片时需 SVIP） | `offline_computed` | 按 `segment_id` 出该段 Image + 段统计/锚点 ID + `all_segments`；模型必须实际收到图片才算复核 |
| `revise_candidate` | 基于已有任务重算（保留父任务成果） | 无 | `offline_computed` | 支持改 `outlet_lonlat`/`snap_radius_m`/`min_accum_cells`/`simplify_tolerance_m`/`engine`/`metric_crs`；`anchors_lonlat` 仅记录锚点并在对比中体现，**v0.1 不支持按锚点/分段做局部几何编辑**；报告前后面积与影响范围 |
| `validate_geometry` | 拓扑/几何检查 | 无 | `offline_computed` | 修复不得静默改拓扑；可选受控简化并报告变化 |
| `measure_geometry` | 水平面积/周长 | 无 | `offline_computed` | 必须投影到米制 CRS；禁止用 EPSG:4490 直接算面积 |
| `write_draft` | 写奥维草稿（写前快照/幂等/回读） | ≥VIP | `offline_simulated` | 参数 `draft_folder`/`name`/`comment`/`parent_folder_id`/`idempotency_key`；真实写入 `real_device_pending`；v0.1 只接受**单一无洞多边形**（多部件/孔洞显式报错）；幂等键可自动派生；超时先 `read_back` |
| `read_back` | 回读对象与本地对比 | ≥VIP | `offline_simulated` | 面积差需先统一投影/椭球/单位口径；模拟回读 delta=0 不能证明真实奥维轴序正确 |
| `finalize_draft` | 标记正式成果 | ≥VIP | `unsupported`（未实测） | 必须显式 `confirm=true`；通过 20112 替换实现，**替换语义 `real_device_pending`**；写前自动快照 |
| `rollback_change` | 恢复写前快照 | ≥VIP | `offline_simulated` | 删除本次创建对象并尝试恢复被替换对象；回滚非无损，需人工核对 |
| `export_result` | 导出成果 | 无 | `offline_computed` | 支持 `geojson`/`kml`/`report`/`projected_json`；`gpkg` 需可选 `fiona`，缺失时显式报错（不承诺 gpkg 可用）；返回 SHA256 |

> 若最终 `tools/list` 与本节不一致，以 `docs/architecture.md` §6 与 `get_capabilities` 输出为准，并同步修正本节；不把“代码里存在”当作能力承诺。

## 5. GIS 计算与复核能力

| 能力 | 实现方式 | 证据等级 | 说明 |
|---|---|---|---|
| DEM 检查 | rasterio 读取，检查 CRS/轴序/分辨率/NoData/量程/覆盖/高程基准 | `offline_computed` | 公开 Copernicus GLO-30 是 **DSM**（含建筑/植被），不是裸地 DTM；不能当实测地形 |
| 内置 native 引擎 | numpy + rasterio 确定性 D8：流向→汇流累积→出口吸附→反向 BFS→矢量化→回投 EPSG:4490；**不填洼** | `offline_computed` | **本机已验证**：14 个解析真值场景逐像元一致（掩膜=解析真值，面积=像元数×像元面积，如 two_bowl_ridge 360 像元/324,000 m²）；两次运行指纹与文件 SHA256 一致；证据 `docs/gis-notes.md` §5/§7、`tests/gis` 75 passed；真实流域精度仍取决于 DEM（`real_device_pending`） |
| GRASS 引擎 | `r.watershed -s`（SFD）+ `r.water.outlet` + `r.to.vect` + `v.out.ogr` | `offline_computed`（本机 GRASS 8.5.0 真实运行、无 skip）/ Windows `real_device_pending` | 本机 macOS conda-forge GRASS 8.5.0 端到端跑通（tests/gis 中 GRASS 用例真实执行）。**重要限制：GRASS SFD 与严格 D8 不等价**——径向对称碗场景 GRASS 821 像元 vs native 解析真值 1681 像元（流域被分解），单坡/收敛地形两者精确一致；方向编码不可混接，验收必须按引擎分开（`docs/gis-notes.md` §6.1）。Windows 安装/运行未验证 |
| 出口吸附 | 半径内汇流累积最大栅格；报告原始/吸附点与距离 | `offline_computed` | 距离过大/支流交汇/可能跨山脊时必须保留候选并请用户确认 |
| 几何校验 | 闭合/自交/重复点/细长片/多部件/洞 | `offline_computed` | 简化只在明确容差内，保护出口/鞍部/关键转折 |
| 边界分段 | 段 ID 稳定可复现，含长度/平均高程/锚点 | `offline_computed` | 段 ID 用于模型复核与修订，不让模型手写长顶点 |
| 复核图 | matplotlib 非交互：hillshade、等高线（标注等高距）、河网、出口、候选边界、编号分段、比例尺、指北针、CRS/范围/像素；分段图按 bbox 长宽比自适应版式 | `offline_computed`（排版经 3 轮修复） | **最终 DSH 视觉复核（vision4）确认**：3 张 MCP 图片真实到达并被像素级判读；概览图与横版（seg-01）/竖版（seg-02）分段图的主地图面板均可读；`figure.bbox_lonlat` 与 `segment.bbox_lonlat` 逐位一致且为经纬度（EPSG:4490），`view_bbox_lonlat` 包含 bbox；`crs`/`pixel_size_m` 元数据与图面一致。每图 ≤4MB（实测单图约 110–172 KB）。证据：`docs/evidence/dsh-headless-vision-e2e-final.out.txt`、`docs/evidence/images/manifest.json`。存在 4 条**外观局限**（不构成失败，见 §7.1）；Windows 实机视觉复验仍待做 |
| 面积/周长 | shapely 在米制投影（UTM 自动分带）计算，记录 EPSG/算法 | `offline_computed` | 与 20802 的“奥维口径”比较前必须统一投影/椭球/孔洞处理 |
| 与用户参考边界比对 | 需用户提供审核过的边界与独立参考 | `real_device_pending` | 本机无参考边界，边界距离/重叠率/面积差验收**未完成** |
| 真实公开 DEM 管线 | Copernicus GLO-30（DSM，约 30 m） | `real_device_pending`（本机未跑通） | 本会话沙箱到 S3 GET 约 3 KB/s，50 MB tile 无法完成；下载/检查脚本已交付，网络恢复可跑。结果必须标注“真实 DEM、无独立审核边界、仅可复现性/合理性检查” |

## 6. DSH 接入与模型能力

| 能力 | 依据 | 证据等级 | 说明 |
|---|---|---|---|
| DSH 加载 MCP（stdio）并暴露 `mcp__ovital__*` | `@deepseek-ai/dsh-mcp-client` README；官方 MCP 接入指南 | `official_document` + 本机 `offline_simulated` | patch 组合与 schema：本机 DSH 0.1.5-rc.1 隔离 DSH_HOME `--dump-config` 成功、verifier `scripts/verify_dsh_patch.py` 11/11 通过。**真实会话发现**：本机 headless 会话已真实发现并调用工具（奥维为模拟服务器，`docs/evidence/dsh-headless-tool-discovery.out.txt`）；**Windows 实机与真实奥维下的 DSH 会话 `real_device_pending`** |
| DSH 发现项目 Skill | `@deepseek-ai/dsh-skill-filesystem` README（`<projectRoot>/.dsh/skills`） | `official_document` + 本机 `offline_simulated` | 项目根 = 最近的含 `.git` 祖先；无 `.git` 时用启动 cwd。本机 headless 会话通过测试 patch 的 `skill-filesystem.customSkillDirs` 显式加载 `<repo>/.dsh/skills` 成功（`docs/evidence/dsh-headless-skill-loading.out.txt`，模型复述了红线与流程）；**Web GUI 选中 workspace 后的自动发现 `real_device_pending`** |
| 模型收到 MCP 图片 | mcp-client README（图片仅在模型接受图片输入且附件能力开启时投影） | `official_document` + 本机 `offline_simulated` | 本机 headless 会话用 `deepseek-flash` 真实收到 `get_job(include_images=true)`/`review_segment` 的 MCP Image，并描述了标题、指北针、比例尺、等高线高程、河网、分段编号等像素内容；最终代码树的证据为 `docs/evidence/dsh-headless-vision-e2e-final.out.txt`（vision4，含 3 张图的尺寸/SHA256/bbox 判读，图样与元数据见 `docs/evidence/images/manifest.json`）；**Windows + 真实奥维数据下的视觉复核 `real_device_pending`** |
| 第三方代理/网关的图片 `detail` 兼容 | 任务书 D 节；DeepSeek 视觉文档 | `real_device_pending` | 若路由拒绝 `original` 而支持 `high`，按该路由用兼容值并确认效果；不得把转发错误当成模型无视觉能力 |
| DSH patch 的持久启用 | 官方 patch 层优先级（profile → home patch → `--patch`） | `official_document` | 可用 `dsh web --patch ...` 临时启用，或合并进用户 patch 层；`desktop` profile 名被 Electron 保留 |

## 7. 明确不支持 / 不承诺

### 7.1 v0.1 明确不支持 / 待实现（防止按错误参数或越界能力使用）

- **多部件/带孔边界写入**：`write_draft` 只接受单一无洞多边形，多部件/孔洞会**显式报错**；不得为写入而合并部件或填洞。
- **按锚点强制局部几何编辑**：`revise_candidate` 的 `anchors_lonlat` 只记录复核锚点并参与前后对比，**不会**把边界拉过锚点；没有“拖动顶点/局部替换边界”的工具。
- **`candidate_id`/`bbox` 复核检索**：`review_segment` 只有 `job_id`/`segment_id`/`max_pixels`；需要分段时先看返回的 `all_segments` 再用 `segment_id`。
- **`finalize_draft` 的 `target_folder` 参数**：不存在；定稿参数为 `obj_id`/`confirm`/`new_name`/`comment`/`version_note`。
- **`rollback_change(job_id=...)`**：不接受 `job_id`，只接受 `change_id`（从 `write_draft`/`finalize_draft` 返回获得）。
- **复核图的 4 条外观局限（vision4 判读，不影响判读/验收，但不要宣称"完美"）**：① 竖版 seg-02 右侧信息面板下部约 20% 为大面积空白；② 概览图信息卡叠加在地图左上（半透明，UI 元素之间无重叠，会遮住少量地图内容）；③ seg-01 底部说明文字偏小；④ `layout.x_major_tick_count` 元数据写 5，而实际可读主刻度为 4（元数据计数问题，不是刻度被裁）。
- **未知参数不保证报错**：MCP SDK 可能静默忽略工具 schema 之外的参数；请按 `tools/list` 的实际 schema 调用（本项目文档已按实现签名编写）。
- **GeoPackage 导出**：`gpkg` 需可选依赖 `fiona`，缺失时显式报错；不得承诺 gpkg 一定可用（`geojson`/`kml`/`report`/`projected_json` 不依赖 fiona）。

### 7.2 不承诺 / 不支持

1. **不承诺**“仅凭截图/影像自动画出工程级分水岭”。模型看图为辅助，边界正确性依赖 DEM、出口与坐标处理。
2. **不支持**从奥维接口导出原始 DEM（官方未列出该能力）；DEM 需项目提供 GeoTIFF。
3. **不支持**目标面积优化：不得为凑面积改边界；面积接近不代表正确。
4. **不支持**首版写多区域/孔洞/超长顶点对象（语义未文档化）；首版只写**单区域、单部件、无洞**草稿，多部件/孔洞会**显式报错**（不得为了写入而合并部件或填洞）。
5. **不支持** v0.1 的**局部几何编辑**：没有“拖动顶点/按锚点重建边界/局部替换一段边界”的能力；`revise_candidate` 只能改计算参数后整体重算（出口/吸附半径/汇流阈值/简化容差/引擎/CRS），`anchors_lonlat` 仅记录复核锚点。局部几何编辑列为待实现/待实测。
6. **不承诺**米级/亚米级精度：公开 30m DSM 不能支撑；工程精度取决于用户实测 DTM、出口定位与控制点。
7. **不支持**用 `deepseek-v4-pro` 看图（该模型不支持图片输入）。
8. **不支持**把模拟服务器（`simulated: true`）结果当作真实联机结果；mock 通过不能代替 Windows 验收。
9. **不支持**依赖环境变量传奥维 PSK（DSH 会清理 `*KEY/*PASSWORD/*SECRET/*TOKEN` 与 `DSH_*`）；必须走配置文件 `psk_file`。
10. **不支持**地下水/岩溶/跨流域引水/人工排水等工程汇水范围自动推定；只能作为项目条件记录并标注不确定段。

## 8. 未实测清单（与 `get_capabilities.unverified[]` 对齐）

1. 真实 Windows 奥维客户端的接口启用、端口、PSK 派生与鉴权（本机未联机）；
2. 目标账号 VIP/SVIP 等级、地图图片与等高线接口实际可用性；
3. `20112` 写入后 ObjID 生成/替换语义、ParentID 挂接规则与数量限制；
4. 图形 `Latlng` 扁平数组轴序（文档示例 lat,lng；需实机回读确认）；
5. `20802` 面积/周长的投影口径与孔洞处理；
6. 多区域/孔洞/超长顶点对象在 `20112`/`20802` 上的表示与限制；
7. 断线重连后 `20112` 重复写入行为与幂等性（本机仅模拟注入）；
8. 地图图片/等高线接口返回数据格式及是否满足复核需要；
9. DSH 视觉模型在真实会话中收到 MCP 图片并完成判读（本机未配置 API Key/未做真实会话验证）；
10. 用户真实 DEM、坐标系统与经审核参照边界的精度验收（缺少数据与人工参考）。

详细验证方法、所需材料与分组见 `docs/待核验清单.md`。

## 9. 官方来源

- 奥维第三方接口（能力总览，含“获取地图数据/等高线需 SVIP”）：<https://www.ovital.com/openapi/>
- 奥维 WebSocket 开发接口 V2.7 手册：<https://www.gpsov.com/appdoc/index.php?id=32>
- 奥维 WebSocket 官方样例：<https://download.ovital.com/pub/api/omapInterfaceWeb_latest.zip>
- DSH MCP 客户端：<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md>
- DSH MCP 接入指南：<https://deepseek-harness.github.io/deepseek-harness/en/guide/mcp-memory>
- DSH Skill 文件系统：<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/skill/skill-filesystem/README.md>
- DeepSeek 视觉（`deepseek-flash` 支持 JPEG/PNG/GIF/WebP）：<https://api-docs.deepseek.com/guides/vision>
- DeepSeek 模型与价格（`deepseek-flash` 支持 Vision，`deepseek-v4-pro` 不支持）：<https://api-docs.deepseek.com/quick_start/pricing/>
- GRASS `r.watershed`：<https://grass.osgeo.org/grass-stable/manuals/r.watershed.html>
- GRASS `r.water.outlet`：<https://grass.osgeo.org/grass-stable/manuals/r.water.outlet.html>
- Copernicus DEM（产品为 DSM；GLO-30 约 30m）：<https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM>

## 10. 如何复核本文

```bash
# 全部测试（含协议/ GIS / DSH patch 校验）
.venv/bin/python -m pytest -q
# 单独校验 DSH 接入文件
.venv/bin/python -m pytest tests/test_dsh_patch.py -q
```

在真实环境里，以 `mcp__ovital__get_capabilities` 返回的 `capabilities`、`permissions`、`simulated`、`verified_against_real_device`、`unverified[]` 为准；本文与工具输出不一致时，先修文档/工具，再对外使用。
