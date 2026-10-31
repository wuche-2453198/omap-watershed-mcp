# DSH 实施任务：奥维集水区绘图 MCP + Skill

请先完整阅读同目录《OMAP_MCP_可行性与实施方案.md》，按其设计在 Windows 上实现；本任务书不表示已有可运行代码。

## 目标

用户在奥维互动地图中点选坝址/取水口后，可以用自然语言要求生成上游集水区。系统应沿分水岭形成闭合范围，在奥维中生成可编辑草稿，量算水平面积，提供边界复核依据，支持局部修订、定稿与回滚。运行宿主为 DeepSeek Harness，日常模型首选官方 API `deepseek-flash`。

集水区不是可见水面。“面积接近”不能替代边界正确。不要以目标面积优化边界，不要伪造坐标或声称数据无法支持的精度。

## 技术约束

- 本地标准 MCP，首版 stdio，通过官方桌面 WebSocket 接口与奥维协作。
- 将奥维原始命令封装在独立适配器，不让模型直接拼协议。
- 用 Python GIS 栈和选定版本的 GRASS GIS 等确定性引擎完成水文、几何和面积计算。模型负责理解、判读与编排。
- DEM 先支持项目提供的 GeoTIFF；不得假设奥维接口一定能导出原始 DEM。
- 首版用一个视觉 Flash 模型即可；V4 Pro 当前不能直接处理图片，不作为看图替代。
- DSH 使用其当前官方 Cordis MCP patch，Skill 使用 `.dsh/skills/ovital-watershed/SKILL.md`；勿直接套用其他客户端的 `mcpServers` 格式。
- 不开发不必要的 OPEN+ 插件、专用模型训练或复杂多模型调度。

## 执行顺序和交付

### A. 环境与官方协议探测

确认实际 Windows、奥维、DSH 版本；读取官方 WebSocket 手册与样例，核对接口启用、鉴权、端口、会员权限、坐标语义、对象点数限制与区域/洞表示。不能虚构端口、命令 ID 或响应字段。先用官方样例建立连接。

V2.7 手册已注明普通功能至少 VIP，地图图片/等高线数据需 SVIP。常用 `CoordType=0` 指 CGCS2000，不能当作 WGS84；`LatlngType` 的枚举另行解析。官方示例端口 7699 可配置。登录、对象设置和图形量算可分别从 `20000`、`20112`、`20802` 定位。后者周长单位为千米，面积为平方米；字段表与示例有差异，应以官方样例和实测响应验证。

交付 `capabilities.md`、版本记录、脱敏协议样例和待核验列表。若实际账号无地图/等高线权限，支持本地 DEM/影像输入；不要将文档未列出的能力写成已支持。

### B. 最小精确读写闭环

实现能力查询、读取选择、读取对象、新建多边形、更新顶点、回读、查询面积周长、恢复本次变更。用独立测试文件夹，不覆盖用户原成果。

验证：同一已知多边形写前写后顶点一致到协议允许的精度；经纬度轴顺序和坐标转换有控制点依据；断线或超时不会盲目重复创建。

MCP stdout 仅输出协议，日志走 stderr/文件。凭据不进入模型结果或日志。启动进程不得自动改地图，需兼容 DSH 的探测进程和重启。不要依赖 DSH 自动继承常见密码/密钥变量。

### C. 一个真实集水区的确定性计算

记录原始出口与吸附出口，进行 DEM 覆盖、NoData、投影、分辨率和高程基准检查。采用适配引擎的地形处理方式；若用 GRASS `r.watershed`，不要机械地预先填平所有洼地。

计算流向/汇流累积、出口上游范围、矢量边界、有效几何与面积。正确处理分析范围截断、出口误吸附和邻近支流。保存输入文件摘要、引擎版本、参数、成果和失败原因。

GIS 长任务用持久任务 ID、状态查询和取消接口；不要依赖一次很长的同步 MCP 调用。

### D. 图像复核与草稿落图

生成概览与带编号分段的局部复核图，包括等高线/阴影地形、河网、出口、候选边界和配准信息。验证模型实际收到 MCP 图片；仅返回文件路径不能代表模型已看到图片。

如果经过第三方代理，验证其图片 `detail` 参数；若路由拒绝 `original` 而支持 `high`，在该路由使用兼容值并确认图像处理效果，不要把转发错误当成模型无视觉能力。

模型指出疑点与依据，工具接受分段、锚点或约束修订。保存修订前后边界，报告局部与整体影响。写入奥维草稿后回读坐标与面积；明确量算方法和单位。

### E. DSH 接入与验收

交付安装与依赖锁定方式、DSH Cordis patch、Skill、说明文档、示例任务与成果报告。用用户审核过的流域边界做验证，分开统计面积差、边界距离、关键鞍部与支流正确性。保留未参与调参的样例。

## 必须保留的质量约束

1. 坐标小数位、写入成功、面积相近均不能单独证明工程精度。
2. 不凭影像外观把河谷当成山脊，不跨越关键鞍部纳入邻近流域。
3. 投影、底图偏移、轴顺序与高程基准逐项记录，不重复做偏移。
4. 模型不手写长顶点列表，优先传候选/分段/锚点 ID。
5. 独立人工参考、与 DEM 同源的等高线和模型自评分别标识，不能混为独立验证。
6. 发现岩溶、涵洞、人工引排水或平缓分水岭等特殊情形时，记录工程条件与不确定段。
7. 几何修复和简化不得静默改变集水区拓扑或抹去关键转折。
8. 使用已授权操作连续推进；只在关键资料或边界证据确实缺失时提出具体问题。

## 最终验收证据

- 真实 Windows 奥维协议读写与回读记录。
- 控制点与多边形坐标往返验证。
- 至少一个真实流域的完整计算与落图证据，之后扩展有代表性的案例集。
- 同一输入和参数可重现；异常场景有可观察失败，避免伪成功。
- DSH 真实发现 MCP 工具、加载 Skill、视觉模型收到工具图片并完成复核。
- 草稿、正式成果、版本和回滚可用。
- 报告写清使用的数据、坐标系统、面积口径、边界疑点和精度适用范围。

若当前机器没有可操作的 Windows 奥维、DEM 或参考边界，继续完成能够独立完成的适配器、模拟协议、GIS 文件处理与文档工作；将真实联机和边界验收明确列为未完成，不能用 mock 通过代替它们。

## 官方资料

- 奥维第三方接口：<https://www.ovital.com/openapi/>
- 奥维 WebSocket 手册：<https://www.gpsov.com/appdoc/index.php?id=32>
- 奥维 WebSocket 官方样例：<https://download.ovital.com/pub/api/omapInterfaceWeb_latest.zip>
- DSH MCP 接入：<https://deepseek-harness.github.io/deepseek-harness/en/guide/mcp-memory>
- DSH MCP 客户端：<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md>
- DSH Skill 目录：<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/skill/skill-filesystem/README.md>
- DeepSeek 视觉：<https://api-docs.deepseek.com/guides/vision>
- DeepSeek 模型和价格：<https://api-docs.deepseek.com/quick_start/pricing/>
- GRASS 流向与汇流：<https://grass.osgeo.org/grass-stable/manuals/r.watershed.html>
- GRASS 指定出口集水区：<https://grass.osgeo.org/grass-stable/manuals/r.water.outlet.html>
