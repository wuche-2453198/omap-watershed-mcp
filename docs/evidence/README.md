# 证据：DSH 真实接入与视觉链路（本机 headless）

本目录保存 **2026-10-30 本机实测** 的 DSH 接入证据。开发机为 macOS 26.6.2 arm64，
**没有 Windows 奥维客户端**，因此这里的"奥维"是项目内模拟服务器
（`scripts/run_mock_ovital.py`，自标识 `Simulated=true`）；DEM 是合成解析真值
fixture（`tests/fixtures/synthetic/single_bowl.tif`）。所有结论的证据等级仍为
`offline_simulated` / `offline_computed`，**不是**真实设备验收。

## 环境

- DSH CLI `0.1.5-rc.1`（`/opt/homebrew/bin/dsh`）
- 模型 `deepseek-flash`（DSH headless 默认路由；使用运行者本机 DSH 凭据）
- 隔离 `DSH_HOME=<repo>/.dsh-home`（不写入用户真实 `~/.dsh` 配置）
- MCP server：`python -m ovital_mcp`（stdio），日志只写 stderr
- 模拟奥维：`python scripts/run_mock_ovital.py --psk test-psk --port 0 --ready-file <file>`

> 说明：headless 会话没有交互式 workspace，项目 `.dsh/skills` 的 `project-dsh`
> 根不会自动挂载。为验证 Skill 加载，测试 patch 用官方配置项
> `skill-filesystem.customSkillDirs` 显式指向项目 `.dsh/skills`。真实 Web 会话选中
> 工作区后，project-dsh 根会自动发现同一 Skill。

## 证据文件

| 文件 | 内容 | 代码版本 |
|---|---|---|
| `dsh-headless-tool-discovery.out.txt` | DSH 真实发现并调用 `mcp__ovital__get_capabilities`，模型报告 simulated=true / connection.available / evidence_level=offline_simulated | 早期集成版 |
| `dsh-headless-skill-loading.out.txt` | DSH skill 机制真实加载 `ovital-watershed` Skill，模型复述红线与流程首尾 | 早期集成版 |
| `dsh-headless-vision-e2e-v1-pre-layout-fix.out.txt` | 首次视觉 E2E（排版修复前） | 中间版本，仅留作迭代记录 |
| `dsh-headless-vision-e2e-v2-pre-layout2.out.txt` | 视觉 E2E + F-11 bbox 修复后 | 中间版本 |
| `dsh-headless-vision-e2e-v3-pre-final-layout.out.txt` | 视觉 E2E + 第二轮排版后（仍发现竖版刻度粘连） | 中间版本 |
| **`dsh-headless-vision-e2e-final.out.txt`** | **最终代码树的视觉 E2E（vision4）** | **最终验收版本** |
| `images/final-sample-*.png` + `images/manifest.json` | vision4 任务实际返回的 5 张复核图样本（overview + seg-01..04）及其 sha256/bbox/view_bbox/layout 元数据 | 最终验收版本 |
| `dsh-headless-test.patch.yml` / `dsh-headless-test.config.yaml` | 本次 headless 测试使用的 Cordis patch 与 MCP 配置样例（POSIX 绝对路径、mock 端点、独立 data_root） | — |

### 最终视觉 E2E（`dsh-headless-vision-e2e-final.out.txt`）确认的事实

```bash
env DSH_HOME="$PWD/.dsh-home" \
  dsh --profile headless --patch "$PWD/.cache/dshtest/ovital.cordis.yml" \
  "……调用 start_watershed → get_job(include_images=true) → review_segment(1/2) → measure_geometry……"
```

模型实际收到 MCP `ImageContent` 并逐项读图，确认：

- 概览图 1011×1078、地图面板占约 73%，含标题面积、指北针、500 m 比例尺、等高线
  （10 m 等高距，140/150/160/170 可读）、蓝色 D8 河网、红/橙边界与编号分段、出口红星、
  坐标轴、颜色图例；
- 分段图 seg-01 1171×557（地图占约 61%）、seg-02（竖版）x 轴 4 个刻度可逐值阅读、
  信息面板与比例尺/指北针不遮挡；
- `figure.bbox_lonlat` 与 `segment.bbox_lonlat` 逐值一致（EPSG:4490 经纬度）；
  `figure.view_bbox_lonlat` 为外扩显示范围且包含段 bbox；
- 图上面积与 `measure_geometry` 一致（1,512,900 m² = 1681 × 900 m²，EPSG:32650 平面面积）；
- `simulated=true`（奥维连接层）/ `offline_computed`（GIS 计算层）/ `verified_against_real_device=false`；
- 模型主动指出："边界覆盖整个 41×41 DEM、`truncated=true`、出口无出流方向，这是
  数据边缘而不是独立山脊分水岭，不能当作真实工程边界"——说明模型没有被模拟数据误导。

### 已知残余视觉问题（如实记录，不影响"图片真实到达/边界口径正确"的结论）

1. 竖版分段图（seg-02）右侧信息栏下方仍有约 20% 空白（信息栏比地图面板短）；
2. overview 信息面板叠加在地图左上角，遮住少量等高线/河网（UI 元素互不遮挡）；
3. seg-01 底部说明文字字号偏小（约 5–6 px）；
4. 竖版窄图渲染的 x 主刻度实际 4 个，`layout.x_major_tick_count` 报 5（元数据与图面
   数量差 1，属统计口径问题，不影响可读性）。

以上作为 gis 复核图的已知外观局限记录；它们不改变图片内容真实性、bbox/面积口径与
模拟/离线证据分级。

## 真实公开 DEM（SRTM）链路证据

| 文件 | 内容 |
|---|---|
| [`docs/real-dem-run.md`](../real-dem-run.md) | 运行记录与口径（真实公开 DEM、无独立审核参考边界） |
| `real-srtm-boundary.geojson` | 集水区边界（EPSG:4490，608 唯一顶点；verifier 独立复算面积 19,673,902.93 m²） |
| `real-srtm-report.md` | 由修复后的 `export_result` 复现运行 `j-20261029-181428-9c0ac0` 生成（F-14：旧报告早于 export 修复，复核图清单为空，已重新生成） |
| `real-srtm-job-result.json` | 复现运行的任务/成果摘要：参数、面积/周长/顶点、DEM SHA256、复核图清单与 SHA256、导出哈希 |
| `images/real-srtm-*.png` + `images/real-srtm-manifest.json` | overview + seg-01..08 图件与 SHA256/bytes（两次独立运行图件逐字节一致） |

> 真实 DEM 链路的图像与 boundary 由**首次运行**归档；正式报告由**复现运行**生成。
> 两次运行实测：**任务产物 `artifacts/boundary.geojson` 逐字节相同（`8d791669…`）、
> 几何坐标逐值一致、面积/周长逐位相同、9 张图件 PNG 逐字节相同、DEM SHA256 相同**；
> 只有 `export_result` 导出的 GeoJSON 整文件哈希不同（`f53b3ea3…`，其 properties 含
> job_id/task_id/时间字段，几何与 boundary 一致）。
> `real-srtm-report.md` §3 的图路径来自复现运行临时工作目录（已清理），归档等价物为
> `images/real-srtm-*.png` + `images/real-srtm-manifest.json`（逐字节一致）。
> 无人工审核参考边界，不得引用本组证据声称工程精度。

## 明确未验证（不要引用本目录证明以下事项）

- 真实 Windows 奥维客户端的连接、鉴权、VIP/SVIP 权限、对象读写与面积口径；
- 真实用户 DEM、坐标系统与经人工审核的参照边界精度；
- 真实 SVIP 地图图片/等高线接口的返回格式与视觉复核效果；
- Windows 实机/Web GUI 工作区自动发现 Skill（本机 headless 用 `customSkillDirs` 显式挂载）；
- 带真实奥维数据的 DSH 会话（本目录中的奥维是模拟服务器）。

## 复现说明

- 复现时替换 patch/config 中的绝对路径为本机仓库路径，并先启动模拟奥维服务器；
- DSH 模型凭据由运行者自己的 DSH 环境提供，本仓库不包含、不记录任何 API Key；
- 模型看到的图片经 DSH attachment 层转为 WebP 后进入上下文，因此 transcript 中的
  attachment sha256 与源 PNG sha256 不同；源 PNG 哈希见 `images/manifest.json`。
