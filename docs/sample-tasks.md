# 示例任务与工具链

以下是 5 个自然语言任务示例，说明模型应如何组织工具调用、何时必须判定“结果不可靠/未完成”。工具名在 DSH 中为 `mcp__ovital__<tool>`；完整契约见 `docs/architecture.md` §6，行为约束见 `.dsh/skills/ovital-watershed/SKILL.md`。

> 所有示例在真实 Windows 奥维上的执行**尚未验证**（本机无奥维/无账号/无用户 DEM）。示例中的“预期结果”是流程目标，不是已取得的结果；真实联机前不要对外承诺。

---

## 示例 1：从奥维选中坝址，用项目 DEM 生成上游集水区草稿

**用户说法（自然语言）**：

> 我在奥维里已经选中了坝址标签。用我放在 `D:\gis\dem\project_dtm.tif` 的 DEM，把坝址以上的集水区画出来，先存成草稿给我看。

**预期工具链**：

1. `get_capabilities` → 确认连接状态、`simulated`、`verified_against_real_device`、VIP 等级、`unverified[]`；
2. `get_selection` → `read_object`（取得选中标签的 obj_id、坐标、语义）；
3. `inspect_dem`（`dem_path=D:\gis\dem\project_dtm.tif`）→ 报告 CRS/分辨率/覆盖/NoData/高程基准；
4. `start_watershed`（`dem_path` + `outlet_obj_id`，或直接给 `outlet_lonlat`；`engine=auto`）→ 拿 `job_id`；
5. `get_job`（轮询）→ 任务成功，记下出口吸附距离、面积、provenance；
6. `get_job(include_images=true)` → 概览复核图（最多 2 张，`review_figure_count` 为总数）；
7. `review_segment` 对出口、关键山脊、鞍部、DEM 边缘逐段取局部图；模型必须**实际看到图片**并在回复中描述证据；
8. 向用户汇报疑点与候选；用户确认后 `write_draft`（独立草稿文件夹、幂等键）→ `read_back`（回读顶点/面积差异、口径说明）；
9. `export_result`（可选，`report`/`geojson`）；定稿需用户再次确认后 `finalize_draft(confirm=true)`。

**何时判定结果不可靠（必须写进汇报）**：

- `get_capabilities` 返回 `simulated: true`（连的是模拟服务器）或 `connection.available=false`；
- DEM 覆盖不足/出现截断警告，或 NoData 比例高、分辨率明显粗于工程需求；
- 出口吸附距离过大，或吸附点落在支流交汇/可能跨山脊处；
- 复核图未真正出现在模型中（只有路径或“图片被拒绝”诊断），导致无法完成看图复核；
- `read_back` 顶点差异超出协议往返预期（面积口径不一致时不得直接说“一致”）；
- `verified_against_real_device=false`：本轮结果只能算离线/文档级验证。

---

## 示例 2：用户要求“面积凑到 5 平方公里”

**用户说法**：

> 集水区大概应该 5 平方公里，你调到 5 平方公里左右，形状好看一点。

**预期处理**：

1. 明确拒绝“以目标面积优化边界”：面积只能做合理性对照，不能作为修改边界的依据；形状好看也不是依据。
2. 若已有候选（`list_jobs`/`get_job` 找到任务），报告当前面积、量算方法与 CRS，说明差异可能来自 DEM/出口/分辨率/口径，而不是“边界需要凑”。
3. 允许的路径：用证据驱动的复核 —— `review_segment` 看图找真实疑点 → `revise_candidate`（分段/锚点；v0.1 锚点只记录并重算参数，不会强行把边界拉过锚点）→ 重新计算并报告前后面积与影响范围。
4. 若用户坚持“必须等于 5 平方公里”，说明这属于工程约束，需要用户提供目标对应的依据（如设计洪水位/断面），不能由模型猜。

**工具链**：`list_jobs`/`get_job` → `review_segment` →（证据充分时）`revise_candidate` → `get_job` → 汇报；**不调用**任何“按面积改边界”的操作（不存在此工具）。

**何时判定结果不可靠**：

- 模型为贴近目标面积移动/平滑了边界 → 该结果直接判定为**不可用**；
- 只比较面积接近就宣称边界正确；
- 修订前后面积变化显著但边界依据（图/分段/锚点）没有记录。

---

## 示例 3：复核已有草稿北侧是否越过鞍部

**用户说法**：

> 上次生成的草稿我担心北边那段翻过鞍部了，帮我查一下北侧边界，必要时局部修正。

**预期工具链**：

1. `read_object`（草稿 obj_id）或 `list_jobs` + `get_job`（找回 job_id 与成果）；
2. `read_back`（obj_id、job_id）→ 获取奥维回读顶点/20802 量算，与本地候选对比；
3. `review_segment`（先看返回的 `all_segments` 找出北侧对应 `segment_id`，再按该 `segment_id` 取局部图）→ 复核图 + 分段高程/等高线/河网统计 + 锚点 ID；
4. 模型看图判断：分水岭是否在山脊、是否跨鞍部纳入邻谷、关键支流是否纳入/漏掉；给出“分段 ID + 位置 + 证据”；
5. 若确有疑点：`revise_candidate`（改 `outlet_lonlat`/`snap_radius_m`/`min_accum_cells`/`simplify_tolerance_m`/`engine` 等参数，并用 `anchors_lonlat` 记录疑点锚点）→ 新任务 → `get_job` → 再次 `review_segment` 验证；
6. 报告修改前后面积/影响范围/仍存疑部位；用户确认后才 `write_draft` 更新草稿；需要撤销时 `rollback_change`。

**注意（v0.1 事实）**：`revise_candidate(anchors_lonlat=...)` **只记录锚点并重算/报告参数，不会强行把边界拉过锚点**；不要声称“已按锚点重建边界”。如果证据指向边界形态错误，应更换参数/数据重算，或明确标注为待人工处理。

**何时判定结果不可靠**：

- 没有 DEM 覆盖北侧山脊/鞍部区域，却给出“已确认不跨鞍部”；
- 复核图未实际收到或分辨率不足以判断鞍部；
- 模型只凭影像纹理判断（把影像河谷当山脊）；
- 修订后只看面积变化，未复核分段边界是否改善。

---

## 示例 4：没有 DEM，也没有 SVIP（降级路径）

**用户说法**：

> 我没有 DEM，奥维账号也没开 SVIP。你直接用地图影像帮我把集水区描出来吧，最后定稿。

**预期处理**：

1. `get_capabilities` → 确认无 DEM、无 SVIP（20301/20303 会显式报权限错误，不应伪造图片）；
2. 明确告知：**不能**用影像/水面分割代替 DEM 汇流计算；没有自然分水岭依据，输出不能叫“集水区成果”。
3. 允许的降级：沿等高线/影像的**辅助描绘草稿**（若用户提供离线影像/等高线数据，或已有可用的本地参考线）；`write_draft` 可写草稿，但汇报必须标注：
   - 未做 DEM 汇流计算、非工程成果；
   - 判读依据与待复核部位（尤其鞍部、平缓分水岭、支流交汇）；
   - 不得 `finalize_draft` 定稿。
4. 建议用户补充：带坐标系的 DEM（项目实测 DTM 优先）、SVIP（如需地图图片/等高线接口）、或其他可复核资料。

**工具链**：`get_capabilities` →（有本地参考数据时）`validate_geometry`/`measure_geometry` → 可选 `write_draft`（草稿）→ 汇报降级说明；**不调用** `start_watershed`（无 DEM）、`finalize_draft`。

**何时判定结果不可靠**：

- 无 DEM 却给出精确面积/精度承诺；
- 把影像水面或行政边界当作集水区；
- 草稿被标记为正式成果；
- 声称等高线/地图图片可用而实际接口返回无权限错误。

---

## 示例 5：定稿并导出 KML

**用户说法**：

> 这个草稿我看过了，没问题。标成正式成果，再导出一份 KML 给我。

**预期工具链**：

1. `read_back`（确认草稿当前状态、回读坐标/量算与本地一致，或差异已解释）；
2. `export_result`（`format=kml`；也支持 `geojson`/`report`/`projected_json`，`gpkg` 需可选 `fiona`）→ 返回文件路径、SHA256 与摘要（CRS/面积口径）；
3. 用户明确确认后 `finalize_draft(obj_id, confirm=true, version_note=...)` → 保存版本记录、返回 `change_id`/`snapshot_id`；
4. 汇报：定稿版本、回读一致性、导出文件位置、`20112` 替换语义未在真实设备验证的提醒；告知可用 `rollback_change(change_id)` 回退。

**何时判定结果不可靠 / 不应定稿**：

- 用户没有明确确认（`confirm=true` 必须由用户决定，不能模型自行设置）；
- 回读差异未解释（投影/椭球/单位/孔洞口径未统一）；
- 仍有未复核的关键分段（鞍部/支流/DEM 截断）；
- 真实设备未验证：定稿/替换/回滚的真实行为属 `real_device_pending`，报告中必须保留该声明；
- 导出 GPKG：需要可选 `fiona`，缺失时工具会显式报错，不要承诺 GPKG 一定可用。
