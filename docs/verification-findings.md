# 独立验证发现（阶段 1）

> 独立验证者（Task-4 / verifier）产出。本文只记录**可复现的验证事实与文档/代码矛盾**，不代表任何功能验收通过；
> 真实 Windows 奥维联机、用户 DEM/参考边界精度验收**仍未完成**（`real_device_pending`）。
>
> - 审计时点：**2026-10-30 00:34–00:48（Asia/Shanghai）**。审计期间 protocol/GIS/文档仍在并发落盘，本文按该时点快照；
>   阶段 2（lead 通知后）会对全部落盘内容重跑审计并产出 `docs/offline-acceptance.md`。
> - 本机事实：macOS 26.6.2 arm64；**无 Windows 奥维客户端、无奥维账号、无用户 DEM、无审核参考边界、无 DSH API Key**。
> - 写作用域：`scripts/verify_*.py`、`tests/integration/**`、本文档、`docs/offline-acceptance.md`。**未修改任何业务源码/配置/其他文档。**
> - **修复复验（2026-10-30 01:10 快照）**：F-01/F-02/F-03/F-04/F-07/F-09 已由 Lead 修复并经本文件 §6 独立复验通过；
>   F-11（本审计过程中发现的复核图 bbox 口径缺陷）已由 gis-dev 修复并回归通过；新增 F-12（低）/F-13（低）待处理。
>   当前 `pytest` 342 passed（含 tests/gis）；阶段 2 闭环 harness 已就绪（`scripts/verify_mock_loop.py`，76/76 通过）。

## 0. 结论速览

| 验证面 | 结果 | 证据 |
|---|---|---|
| MCP stdio 真实握手（mcp SDK 2.3.0 Client，真实子进程） | **通过** 24/24 检查 | `scripts/verify_mcp_stdio.py` → `.cache/verify/stdio-evidence-latest.json` |
| 18 个工具 / instructions 红线 / inputSchema 可解析 | **通过** | 同上 + `scripts/verify_stdio_harness.py` |
| stdout 纯净（每行均为 JSON-RPC 2.0，无 print 污染） | **通过**（6 行响应，0 行非协议） | `.cache/verify/stdio-transcript-*.log` |
| 无奥维/无凭据时的显式结构化失败（不得崩溃/伪成功） | **通过** | 同证据 JSON 的 A6–A12、B1 |
| 启动无副作用（不自动连奥维、不建任务、不写地图） | **通过** | 同证据 JSON 的 A18–A19；`tests/integration` |
| 假 PSK 不泄漏到 stdout/stderr/协议帧 | **通过** | 同证据 JSON 的 A17、C3 |
| DSH Cordis patch 组合（隔离 `DSH_HOME` + `--dump-config`） | **通过** 11/11 检查 | `scripts/verify_dsh_patch.py` → `.cache/verify/dsh-patch-evidence-latest.json` |
| patch config 与已安装 `dsh-mcp-client` 0.1.5-rc.1 的真实 schema | **通过**（含负向控制：非法 transport 会被拒） | `.cache/verify/dsh-dump-config.txt` |
| 全量 pytest（含本阶段新增集成测试） | **通过** 255 passed in 8.11s（00:45 时点） | `.venv/bin/python -m pytest -o addopts='' -q` |
| 文档/代码矛盾审计 | **发现 8 项**（2 中/5 低/1 信息），见 §3 | 本文 |
| 模拟奥维闭环 / GIS 独立复算 / 图片内容传递 / 安全幂等 | **未验证（阶段 2）** | 见 §4 |

**一句话**：阶段 1 的可验证面（stdio 协议层、工具契约、DSH patch 组合、启动/凭据红线）在本机真实跑通且证据可复现；
文档层面存在工具参数契约与实现不同步等问题（F-01～F-08），需 Lead 在阶段 2 前修正或确认。

## 1. 本阶段新增的验证 harness（只读业务代码，不改实现）

| 文件 | 作用 |
|---|---|
| `scripts/verify_stdio_harness.py` | 以**真实子进程** `[.venv/bin/python, -m, ovital_mcp]` + mcp SDK 2.3.0 `Client` 完成真实握手；传输层逐行记录原始 stdout/stderr/双向消息；提供隔离配置（临时 data_root、随机假 PSK、指向必拒绝端口的 endpoint）。验证脚本与 pytest 共用，避免 mock 协议本身。 |
| `scripts/verify_mcp_stdio.py` | 24 项端到端检查（握手/身份/instructions/18 工具/schema/get_capabilities/错误路径/存活/stdout 纯净/stderr 无栈/凭据不泄漏/懒连接/退出码/无凭据启动/`--dump-tools`）。输出证据 JSON+transcript，失败即非零退出。 |
| `scripts/verify_dsh_patch.py` | 隔离 `DSH_HOME=<repo>/.dsh-home-verify` 跑 `dsh --profile headless --patch ... --dump-config`；不带 patch 的对照实验；用已安装插件真实 schema 校验 config（含负向控制）；校验凭据红线；记录未修改用户真实 `~/.dsh`（mtime 快照）。 |
| `tests/integration/conftest.py` | 集成测试路径/隔离目录 fixture。 |
| `tests/integration/test_stdio_client.py` | 7 个 pytest 用例，覆盖任务书阶段 1 的四项要求（启动无副作用、诊断 stdout 纯 JSON、工具调用错误结构化、真实握手的工具契约/instructions/stdout 纯净/凭据不泄漏）。 |

复现：

```bash
cd "/Users/huangsirsmacbookairm5/Public/code/project/omap mcp"
.venv/bin/python scripts/verify_mcp_stdio.py
.venv/bin/python scripts/verify_dsh_patch.py
.venv/bin/python -m pytest tests/integration -q
```

## 2. 阶段 1 通过项的关键事实（供后续复核）

- 握手：legacy initialize 成功，协商协议版本 `2025-11-25`；服务标识 `ovital-watershed` / `0.1.0`；instructions 740 字符，含"不得为了凑目标面积而修改边界""不得伪造坐标""verified_against_real_device"等红线。
- `tools/list`：恰好 18 个工具，名称与 `docs/architecture.md` §6 完全一致；每个工具 description 非空、`inputSchema.type=object`、`required ⊆ properties`。
- 无奥维时 `get_capabilities`：`status=ok`，`connection.available=false` 且 `error.code=OVITAL_CONNECTION_ERROR`，`provenance.simulated=false`、`verified_against_real_device=false`，`unverified[]` 10 项，耗时 0.031s（< 5s 契约）。
- 未配置 PSK：服务仍可启动，`get_capabilities` 返回 `CONFIG_ERROR` 而非崩溃（兼容 DSH 探测进程）。
- `get_selection`（无奥维）→ `status=error` + `OVITAL_CONNECTION_ERROR`；`inspect_dem`（不存在文件）→ `status=error` + `DATA_VALIDATION_ERROR`；之后 `get_capabilities` 仍 `ok`。
- stdout 纯净：6 行服务端输出全部为 JSON-RPC 2.0 响应；stderr 4 行日志、无 Traceback；关闭 stdin 后服务进程 exit=0。
- `--dump-tools` 诊断入口：stdout 仅 1 行 JSON（18 工具），日志全在 stderr，假 PSK 不出现。
- DSH patch：`--dump-config` 组合出 `- id: mcp-ovital`（`name: '@deepseek-ai/dsh-mcp-client'`、`serverName: ovital`、`transport: stdio`、`args: [-m, ovital_mcp]`、`toolCallTimeoutMs: 60000`、`failOnStartupError: false`、reconnect 默认展开为 `{enabled:true, initialDelayMs:500, maxDelayMs:30000, maxAttempts:10}`）；不带 `--patch` 的组合树**没有**该行（对照成立）；组合运行前后用户真实 `~/.dsh` 目录 mtime/大小不变。
- 全量测试：255 passed / 8.11s（协议 151、dsh patch 25、配置/存储/任务/服务 33、坐标 15、mock 24、集成 7，00:45 时点）。

## 3. 发现清单（文档/代码矛盾、证据与卫生问题）

> 状态：`OPEN` = 需对应负责人处理；`IN-FLIGHT` = 其他 teammate 仍在落盘，阶段 2 复查；`INFO` = 记录事实不要求动作。
> 复现命令默认在仓库根执行，`.venv/bin/python` 为项目解释器。

### F-01（中）`docs/architecture.md` §6 工具契约有 4 处输入参数与实现不一致【已修复，见 §6】

- 位置：`docs/architecture.md:252`（#6 start_watershed 写 `obj_id`）、`:255`（#9 review_segment 写 `candidate_id`/`bbox`）、`:261`（#15 finalize_draft 写 `target_folder?`）、`:262`（#16 rollback_change 写 `change_id`/`job_id`）。
- 实际 schema（`scripts/verify_mcp_stdio.py` 的 tools/list 证据或下述命令）：
  - `start_watershed` 的出口对象参数名是 **`outlet_obj_id`**（`ovital_mcp/tools/watershed.py:126` 起），没有 `obj_id`；
  - `review_segment` 只有 `job_id` / `segment_id` / `max_pixels`，**没有 `candidate_id`、`bbox`**（`ovital_mcp/tools/review.py:70`）；
  - `finalize_draft` 是 `obj_id` / `confirm` / `new_name` / `comment` / `version_note`，**没有 `target_folder`**（`ovital_mcp/tools/draft.py:589`）；
  - `rollback_change` 只有必填 `change_id`，**不接受 `job_id`**（`ovital_mcp/tools/draft.py:684`）。
- 复现（真实 stdio 会话，非 review 代码）：

  ```bash
  .venv/bin/python - <<'EOF'
  import asyncio, sys; sys.path.insert(0, "scripts")
  from verify_stdio_harness import open_stdio_session, tool_result_text, tool_result_is_error
  async def main():
      async with open_stdio_session() as s:
          for name, args in [
              ("rollback_change", {"job_id": "x"}),
              ("review_segment", {"job_id": "x", "bbox": [1, 2, 3, 4]}),
              ("start_watershed", {"dem_path": "/tmp/x.tif", "obj_id": 1}),
          ]:
              r = await s.client.call_tool(name, args)
              print(name, "is_error=", tool_result_is_error(r), "|", tool_result_text(r)[:160])
  asyncio.run(main())
  EOF
  ```

- 期望 vs 实际：按契约传参应被工具接受并按语义执行；实际 `rollback_change(job_id=…)` 被 MCP 参数校验直接拒绝（返回**纯文本**，连 `status/provenance` 结构都没有）；`review_segment(bbox=…)`/`start_watershed(obj_id=…)` 的未知参数被 SDK **静默忽略**（不报错，走默认/后续校验失败）。
- 影响：模型若按契约调用会得到误导性错误或"看起来执行了但参数被忽略"的结果 —— 与红线"失败必须显式失败、禁止伪成功"同源风险。
- 建议：以实际 schema 为准同步 `architecture.md` §6、`docs/capabilities.md:88`（"用 obj_id 作出口"应写 `outlet_obj_id`）、`docs/sample-tasks.md:20,71`（`obj_id`/`bbox`）；并考虑让工具层对未知参数显式报错。

### F-02（中）`docs/capabilities.md:88`、`docs/sample-tasks.md:20,71` 沿用 F-01 的旧参数名【已修复，见 §6】

- `docs/capabilities.md:88`："用 obj_id 作出口时需 ≥VIP" → 实际参数为 `outlet_obj_id`。
- `docs/sample-tasks.md:20`："`dem_path` + `obj_id` 或 `outlet_lonlat`"；`:71`："`review_segment`（按 `segment_id` 或北侧 `bbox`）" → `bbox` 参数不存在（见 F-01）。
- 期望：文档中的工具参数名可直接复制使用；实际：按文档传参会被忽略或报错。建议随 F-01 一并修正。

### F-03（低）`docs/architecture.md:29` 仍写"工具注册（17 个工具）"【已修复，见 §6】

- 同文件 §6（`:243`）已明确"共 18 个工具（17 + `list_jobs`）"，`tools/list` 实测 18 个。
- 期望：契约文档内部一致；实际：§1 目录注释为陈旧数字。建议改为 18（或"18 个（§6）"）。

### F-04（中）README 快速开始第 2 步在干净仓库会失败（`config/` 目录不存在）【已修复，见 §6】

- 位置：`README.md:61`：`cp ovital_mcp/dsh/config.example.yaml config/ovital.yaml`。
- 复现：`ls config` → `No such file or directory`（仓库无 `config/` 目录，也不在交付物中）；`cp` 因目标目录缺失直接失败。
- 期望：按 README 三步能启动；实际：第二步报错，用户需自行猜测 `mkdir -p config` 或改用默认候选路径之一（`ovital.yaml` / `.ovital-mcp/config.yaml`，见 `ovital_mcp/config.py:31-35`）。
- 建议：README 增加 `mkdir -p config`，或改为 `cp ... .ovital-mcp/config.yaml`（目录由服务运行期创建）。

### F-05（中）证据分级诚实性：`docs/capabilities.md` §3.1 的模拟闭环数值仍为"实现方自证"，尚未被独立复验

- 位置：`docs/capabilities.md:62-74`（如"登录 20000 返回 ret=817""20301 返回 21,586 字节 PNG""回读顶点 delta=0"）。
- 文档自身已标注"由实现/验证方提供，verifier 会独立复验"，**态度诚实**；但在独立复验完成前，这些数字不能作为验收结论被引用。
- 本阶段状态：`IN-FLIGHT` —— 阶段 2 必须用模拟服务器 + MCP 工具链独立复现这些数值，并核对 PNG 内容（magic/尺寸/可解码）与坐标往返。
- 类似地，`docs/version-record.md:70` 的 GRASS 手工链证据目录 `.cache/grasstest/` 中 `run.log`/`run2.log` 是失败尝试（`ERROR: Raster map <dem> not found`），最终成功在 `run3.log`（`r.watershed`/`r.water.outlet`/`r.to.vect`/`v.out.ogr complete`）+ `basin.geojson` 有效。结论可成立，但证据目录混有失败日志，建议清理或在报告中说明；阶段 2 会独立重跑 GRASS 链。

### F-06（低）`docs/protocol-notes.md`、`docs/gis-notes.md`、`docs/offline-acceptance.md` 被多处引用但尚未落盘；`docs/protocol-samples/` 为空目录

- 引用位置：`README.md:116,118,119`；`docs/capabilities.md:62`；`docs/version-record.md:110,125,126`；`docs/architecture.md:60,68,99`。
- 状态：`IN-FLIGHT`（A 的 protocol-notes/protocol-samples、B 的 gis-notes、D 的 offline-acceptance 尚在阶段 2 前落盘）。阶段 2 终稿前必须复查所有链接可达，否则交付物断链。
- 另外 `docs/version-record.md:126` 已提前引用本文 `docs/verification-findings.md`（本文件，阶段 1 版）。

### F-07（低）弱测试：`tests/test_server.py:107` 名不副实【已修复，见 §6】

- 测试名 `test_no_ovital_connection_needed_for_gis_tool_errors` 声称验证"缺口工具在无奥维时显式报错"，但函数体注释写明"不真正调用（需要事件循环与协议包）"，只断言 `ctx._ovital is None`。
- 期望：测试名与断言范围一致（或真正通过 stdio 调用工具）；实际：给人"已覆盖工具错误路径"的错觉。阶段 1 已用 `tests/integration/test_stdio_client.py` 覆盖真实错误路径；建议重命名该用例或改为复用集成 harness。

### F-08（低/卫生）仓库根 `.dsh-home/.credentials.yaml` 是指向用户真实凭据的符号链接

- 现场：`ls -la .dsh-home/.credentials.yaml` → `-> /Users/huangsirsmacbookairm5/.dsh/.credentials.yaml`（00:33 创建，非本次验证产物；本次隔离验证只写 `.dsh-home-verify/`，其中无凭据）。
- 风险：该目录若随仓库打包/共享，会暴露真实凭据文件路径，且任何以 `DSH_HOME=<repo>/.dsh-home` 启动的进程会读取用户真实凭据。
- 建议：删除该 symlink 或把 `.dsh-home/` 明确列为本地临时目录（交付包排除）；本机验证统一用 `.dsh-home-verify/`（本脚本已如此）。
- 复现：`ls -la .dsh-home/.credentials.yaml`；`DSH_HOME=$PWD/.dsh-home-verify .venv/bin/python scripts/verify_dsh_patch.py`（隔离证据）。

### F-09（中/伪成功通道）provenance 的 `verified_against_real_device=true` 判定偏乐观【已修复，见 §6】

- 位置：`ovital_mcp/tools/common.py:101-108`：`ovital_provenance()` 在"客户端已连接且 `is_simulated=False`"时直接返回 `verified_against_real_device=True`。
- 风险：真实设备验证应基于**真实设备证据**，而不是"没看到模拟标记"。任何能按协议握手、但不返回 `Simulated` 扩展字段的 WebSocket 服务端（本地桥接/第三方 mock）都会被标成"已验证真实设备"。
- 当前本机无真实/伪真实服务端，未观察到伪成功；模拟服务器文档称所有成功响应带 `Simulated: true`，阶段 2 会独立验证该标记是否真的透传到工具层（若模拟连接被标成 `verified_against_real_device=true`，则升级为高优先级缺陷）。
- 建议：改为显式配置/证明（如 `ovital.attested_real_device=true` 才允许 True），或仅在有真实设备验证记录（控制点/往返证据）时置 True。

### F-10（信息）文档在审计期间仍在被并发修改

- 审计时点哈希（sha256 前 16 位 / mtime）：`docs/architecture.md 89e0d3a7830c236c 00:38`、`docs/capabilities.md 55ea1af047d49d35 00:46`、`docs/version-record.md e1826cb28763831b 00:46`、`docs/install-windows.md 3fbed633c969329f 00:46`、`README.md ebdd0020c07895ce 00:34`、`SKILL.md 9747fcacc3f02626 00:45`。
- 因此上述行号/结论以该快照为准；阶段 2 会重跑 §3 的全部检查（脚本可直接复用），并更新本文"阶段 2 结论"。

## 4. 本阶段**未验证**（如实声明，不得用 mock/文档顶替）

以下属 task-4 阶段 2（lead 通知后）内容，本阶段**没有**得出任何结论：

1. 模拟奥维服务器完整闭环（登录→选中→读对象→写草稿→回读→回滚）与 `simulated=true` 透传；
2. `start_watershed` 持久任务全链路（合成 DEM → job 轮询 → `get_job include_images` 返回真实 PNG → `review_segment` 图片）；
3. GIS 独立复算（合成解析真值 DEM 的掩膜/面积、引擎 provenance 是否真实 GRASS、复核图可解码/比例尺/指北针/文字）；
4. 图片内容真实传递断言（ImageContent + PNG magic）；**DSH 视觉模型实际收到图片 = 未完成**（本机无 API Key，需 Lead 安排）;
5. 安全/幂等/取消/断线路径（PSK 全仓扫描、幂等键重复写、faults 注入、取消子进程、错误码语义）；
6. 真实 Windows 奥维联机、目标账号 VIP/SVIP、用户 DEM 与审核边界精度 —— **real_device_pending，未完成**；
7. `docs/offline-acceptance.md`（阶段 2 汇总，含证据分级映射与剩余风险）。

## 5. 证据文件索引（本机，非交付物；`.cache/` 不入库）

| 文件 | 内容 |
|---|---|
| `.cache/verify/stdio-evidence-latest.json` | MCP stdio 24 项检查 + tools/schema/instructions 摘要 + 用量 |
| `.cache/verify/stdio-transcript-*.log` | 双向真实协议 transcript（客户端请求 / 服务端 stdout / stderr） |
| `.cache/verify/dsh-patch-evidence-latest.json` | DSH patch 11 项检查（含 schema 负向控制、对照实验、~/.dsh 未变） |
| `.cache/verify/dsh-dump-config.txt` | 隔离 DSH_HOME 下 `--dump-config` 完整组合树（含 mcp-ovital 行） |
| `.cache/verify/runs/` | 每次 stdio 运行的隔离配置/数据目录（测试假 PSK，非真实凭据） |

## 6. 修复复验与阶段 2 前置观察（2026-10-30 01:10 快照）

> 快照情况：Lead 修完 F-01/02/03/04/07/09；gis-dev 已落盘 `gis/grass.py`、`gis/review.py` 并修复 F-11；
> `pytest` 342 passed（含 `tests/gis`）。docs/gis-notes.md 与复核图排版仍在收尾，**阶段 2 门禁尚未开放**。

### 6.1 已复验通过的修复

| Finding | 复验方式（可复现） | 结果 |
|---|---|---|
| F-01 | 读 `docs/architecture.md` §6 四行 + live `tools/list` 比对；`start_watershed(obj_id=…)` 别名已在 `watershed.py:132-137` 实现（同时给 `outlet_obj_id` 且不一致会显式报错） | **通过**：契约四行已按实现修正，并注明 candidate_id/bbox/target_folder 待实现 |
| F-02 | `grep -n 'obj_id 作出口\|或北侧 `bbox`\|`obj_id` 或 `outlet_lonlat`' docs/capabilities.md docs/sample-tasks.md` | **通过**：0 命中 |
| F-03 | `sed -n 29p docs/architecture.md` | **通过**：已写 18 个工具 |
| F-04 | `sed -n 57,64p README.md`；干净目录执行 `mkdir -p config && cp …` | **通过**：已含 `mkdir -p config` |
| F-07 | `tests/test_server.py::test_ovital_dependent_tool_returns_structured_error_when_unreachable` 真实调用工具并断言结构化 error | **通过**（7 passed：`pytest tests/test_tools_safety.py tests/test_server.py`） |
| F-09 | ① mock 正常连接 → `simulated=true/verified=false`；② 伪造 `ovital.real_device_verified=true` + mock → 仍 false（simulated 优先）；③ 单元级伪造"非模拟已连接客户端"：未设 flag→false、显式 flag=true→true；④ 代码三处统一走 `common.effective_real_device_verified()` | **通过**（证据：`scripts/verify_mock_loop.py` A3/A8/A15 + 手工单元级） |
| F-11 | 合成任务 `review_segment`：`figure.bbox_lonlat == segment.bbox_lonlat`，均为 EPSG:4490 经纬度且在合法范围；in-image "范围" 行不再印米制值 | **通过**（`scripts/verify_mock_loop.py` C10b） |

### 6.2 新发现（阶段 2 前，逐项跟踪）

#### F-12（低）engine 不可用错误码在 job 层丢失具体性【已修复，见 §7】
- 现象：`start_watershed(engine="grass", grass_cmd="/nonexistent/...")` → 任务显式失败（正确），但 `job.error.code = "ENGINE_FAILED"`，具体原因只出现在 `message`：`"[ENGINE_UNAVAILABLE] GRASS 引擎不可用…"`；而 `EngineUnavailableError.code = "ENGINE_UNAVAILABLE"`，架构契约要求"两者都不可用则报 EngineUnavailableError"。
- 位置：`ovital_mcp/worker.py:247`（engine 返回 failed result 时硬编码 `"code": "ENGINE_FAILED"`；同文件顶层异常路径反而用 `getattr(exc, "code", "WORKER_ERROR")` 透传）。
- 复现：`HOME=$PWD/.cache/grasshome .venv/bin/python scripts/verify_mock_loop.py --groups grass`（D9/D9b）。
- 影响：按错误码分支的模型/客户端会把"GRASS 未安装（可恢复，可切 native）"误判为一般引擎失败；不构成伪成功。
- 建议：failed result 分支透传原 error code。**已修复（见 §7）**：`worker.py` 改为 `getattr(result, "error_code", None)`，实测 `job.error.code=ENGINE_UNAVAILABLE`。

#### F-13（低）capabilities 文档两处数值口径不够精确
- `docs/capabilities.md:67`：写"能力探测 `20006/20501/20222/20104/20730/20016/20212/20301/20303` 全部返回 ok"；实测 `get_capabilities.connection.commands` 是 **7 个探测组**（`viewport(20730+20016+20212)` 合并为 1 项），覆盖上述 9 条命令。建议写明"覆盖 9 条命令（7 个探测组）"。
- `docs/capabilities.md:70`：写"20301 返回 21,586 字节 PNG"；实测 `get_scene(bbox=116.99,39.74,117.0,39.75, max_level=12, max_pixel=500000)` 返回 **18,513 字节** PNG（707×706，PNG tEXt 含 `Description=SIMULATED MAP - NOT REAL OVITAL DATA`、`Simulated=True`）。图片字节数取决于请求范围/层级/像素上限，建议标注"示例请求下"并附请求参数，不要当作固定值。
- 复现：`scripts/verify_mock_loop.py`（A4/A6 证据）或见 `.cache/verify/loop/mock-loop-evidence-latest.json`。

### 6.3 DSH 视觉证据独立复核结论（Lead 提供 transcript，本机 artifact 交叉验证）
- `.cache/dshtest/vision1.err` 显示 DSH headless 真实拉起 MCP（日志注册 18 工具）、连接模拟服务器（`simulated=True version=817`）、任务 `j-20261029-165310-83a96e` 面积 1,512,900.0 m²（= single_bowl 解析真值 1681×900，误差 <1e-9）。
- `vision1.out` 声称的 2 个 sha256（`58d510dd…`、`fc21a98a…`）**已在本机 DSH attachment 库找到对应对象**，内容哈希自洽，且解码尺寸 870×859 / 1086×561 与源 PNG 一致 → 证明模型收到的是**像素图片**而非路径。
- **口径修正**：这两个 sha256 是 DSH attachment（原始 PNG 被转码为 WebP）的内容哈希，**不是**原始 PNG 文件 SHA256（原始为 `5af1f0db…` / `bbe4cf42…`）。阶段 2 报告必须按此口径表述，不能写"PNG sha256=58d5…"。
- verifier 已用 `read_image` 独立查看同源 PNG：模型对概览图（标题/信息面板/等高线 100–180/蓝色河网/红色星形出口/指北针 N 网格北/500m 比例尺/分段编号 2/3/4）的描述与实际一致；seg-01 的"图幅压扁、纵轴与标题重叠、下方大片空白、比例尺文字被信息面板遮挡"缺陷也属实（Lead 已知，gis-dev 正在修）。
- **诚实边界**：视觉回合由 Lead 执行；verifier 无 API Key，**未重跑模型**；证据等级记"Lead 提供的 agent 执行证据 + verifier artifact 复核"，不写成 verifier 独立复跑。

### 6.4 阶段 2 harness 就绪声明
`scripts/verify_mock_loop.py`（+ `scripts/verify_mock_ovital.py` mock 进程管理）已就绪，当前树全跑 **76/76 checks 通过（9.0s）**，覆盖任务书阶段 2 的 6 类证据：模拟奥维闭环、GIS 独立复算（合成解析真值逐像元）、GRASS 真实调用（provenance 三阶段命令/返回码）、图片真实传递（PNG magic/尺寸/非空白/DSH attachment）、安全与幂等/取消/断线/超时、DSH patch/工具发现/Skill/视觉证据复核。
**待门禁开放后**：重跑全部证据 → 产出 `docs/offline-acceptance.md`（<300 行，含证据分级映射与 real_device_pending 清单）→ complete task-4。

## 7. 最终状态（2026-10-30 01:35，最终代码树）

| 项 | 最终结果 | 证据 |
|---|---|---|
| 阶段 2 全量证据脚本 | **83/83 checks PASS，0 失败**（A 模拟闭环 / B 故障幂等 / C GIS 解析真值 / D GRASS 真实性 / E DSH 证据复核 / F 安全与图片） | `.cache/verify/loop/mock-loop-evidence-latest.json`（sha256 `3f2b4683…`） |
| MCP stdio 协议层 | 24/24 PASS | `.cache/verify/stdio-evidence-latest.json`（sha256 `2ad95ad6…`） |
| DSH patch 组合/schema | 11/11 PASS | `.cache/verify/dsh-patch-evidence-latest.json`（sha256 `d6f898c0…`） |
| 全量测试 | 348 passed（`tests/gis` 74 passed、GRASS 无 skip；`tests/integration` 10 passed） | `.venv/bin/python -m pytest -o addopts='' -q` |
| F-12 | **已修复**：`engine=grass + 无效 grass_cmd` → `job.error.code=ENGINE_UNAVAILABLE`（原 `ENGINE_FAILED`） | loop D9 |
| F-11 | **已修复**：`figure.bbox_lonlat` 与 `segment.bbox_lonlat` 逐值一致且为 EPSG:4490 | loop C10b |
| R-01（排版） | **已修复**：外扩地形上下文 + 信息面板移出图外；`layout.mode=outside`、分段面板占比 46.5%–72.7%、纵横比 1.02–2.10、`view_bbox_lonlat ⊇ bbox_lonlat`、caption 注明外扩 | loop C10c/C10d/F6；verifier 目视同源 PNG |
| F-13（文档口径） | 仍为文档建议（不阻塞）：探测项 7 组/9 命令、PNG 字节数随请求参数变化 | — |
| DSH 视觉证据 | Lead headless + 模拟奥维 + 合成 DEM；verifier 复核 attachment 哈希自洽、尺寸与任务 figure 一致、transcript 与 result.json 一致；**口径：attachment WebP 哈希 ≠ 原始 PNG SHA256** | `docs/evidence/dsh-headless-vision-e2e-*.out.txt` |
| 验证有效性说明 | `start_watershed` 自动幂等键会让"相同输入"复用历史任务；本轮所有任务均使用**本次运行唯一 idempotency_key**，确保验证的是最终代码树新产物（早期草稿运行曾命中旧任务，已修正并在 §7 说明） | `scripts/verify_mock_loop.py` RUNTAG |

**剩余未完成（不得表述为已验收）**：真实 Windows 奥维联机/权限/轴序/替换语义/20802 口径；用户 DEM 与独立审核边界精度；
真实 SVIP 地图/等高线；DSH Web GUI 自动 Skill 发现与 Windows 实机视觉；Copernicus 真实 DEM 未跑通。
最终汇总见 `docs/offline-acceptance.md`。

## 8. 最终定稿状态（2026-10-30 02:00，layout final v2 + vision4）

| 项 | 最终结果 | 证据 |
|---|---|---|
| 阶段 2 全量证据脚本 | **84/84 checks PASS，0 失败**（A 模拟闭环 / B 故障幂等 / C GIS 解析真值+排版 / D GRASS 真实性 / E DSH 复核 / F 安全与图片） | `.cache/verify/loop/mock-loop-evidence-latest.json`（sha256 `94fb178b…`） |
| MCP stdio 协议层 | 24/24 PASS | `.cache/verify/stdio-evidence-latest.json`（sha256 `bbf7daf2…`） |
| DSH patch 组合/schema | 11/11 PASS | `.cache/verify/dsh-patch-evidence-latest.json`（sha256 `f55654b4…`） |
| 全量测试 | **349 passed**（26.1s；`tests/gis` 75 passed、GRASS 无 skip；`tests/integration` 10 passed） | `pytest -o addopts='' -q` |
| F-12 | **已修复**：`job.error.code=ENGINE_UNAVAILABLE`（原 `ENGINE_FAILED`） | loop D9 |
| F-11 | **已修复**：`figure.bbox_lonlat` 与 `segment.bbox_lonlat` 逐值一致且为 EPSG:4490 | loop C10b |
| R-01（排版，v1→v4 四轮） | **已修复/定稿**：`figure.bbox_lonlat`=真实段 bbox、`view_bbox_lonlat`=外扩显示范围且 ⊇ bbox；`stack`/`side` 自适应布局；窄图刻度限制+旋转；信息面板在图外不遮挡；像素统计（地图面板宽/高占比、连续白带、刻度簇间距）全部通过 | loop C10c/C10d/C16/F6；vision4（`docs/evidence/dsh-headless-vision-e2e-final.out.txt`） |
| C16（竖版综合可读性，Lead 裁定口径） | **PASS**：地图面板宽≥400px、高度占比≥55%、刻度间距≥55px 或旋转无重叠、互不遮挡、无>15%纯白带（side 布局右侧留白按"地图面板外"口径记录，不判失败） | loop C16 证据 |
| DSH 视觉证据 | vision4（最终代码树）：模型真实收到图片并描述像素内容；attachment 哈希自洽；bbox/面积/simulated 口径与 result.json 一致；**口径：attachment WebP 哈希 ≠ 源 PNG SHA256** | `docs/evidence/*.out.txt`、`images/manifest.json`（5 张，sha256 已复核） |
| 已知外观局限（不作失败项） | ①竖版 side 布局右侧信息栏下方约 20–22% 留白；②方形 DEM overview 信息卡叠地图；③seg-01 图注字号偏小；④`x_major_tick_count` 元数据 ±1；⑤极端宽扁 overview 纵横比可 >2.2 | 详见 `docs/offline-acceptance.md` §6.2 |

**最终未完成（不得表述为已验收）**：真实 Windows 奥维联机/权限/轴序/替换语义/20802 口径；用户 DEM 与独立审核边界精度；
真实 SVIP 地图/等高线；DSH Web GUI 自动 Skill 发现与 Windows 实机视觉；Copernicus 真实 DEM 未跑通。
汇总与证据分级映射见 `docs/offline-acceptance.md`；真实设备待办见 `docs/待核验清单.md`。

## 9. 补充复核：真实公开 DEM（SRTM）链路 + export 报告修复（2026-10-30 02:02–02:08）

### 9.1 独立复核 Lead 的 SRTM 链路样例（不是工程精度验收）
Lead 用真实公开 DEM（SRTM 1″ `N39E115`，AWS elevation-tiles-prod，`.hgt`→GeoTIFF EPSG:4326，
SHA256 `f4bd6149…`）跑通 native 链路并留下成果；verifier 独立复核（不采信其结论，直接读交付产物）：

| 复核项 | 方法 | 结果 |
|---|---|---|
| 边界面积 | GeoJSON(EPSG:4490) → pyproj→EPSG:32650 → shapely 复算 | **19,673,902.93 m²**（与声称相对差 0.00） |
| 周长/几何 | shapely `length`/`is_valid` | 28,797.63 m；有效、单部件、0 洞、608 唯一顶点（609 坐标含闭合点） |
| DEM 文件 | 本地 `.cache/realdem/beijing_west_srtm.tif` SHA256 | 与声称 `f4bd6149…` 一致 |
| 任务字段（清理前核对） | `.cache/leadtest/.../result.json` | `truncated=false`、`snap_distance_m=16.126`、`flow_accum_cells=25857`、`engine_used=native`、`metric_crs=EPSG:32650`、分辨率 27.58 m、像元面积 760.87 m² |
| 图件 | 9 张（overview+seg-01..08）manifest SHA256/bytes + PIL 解码 | **全部一致**，dims 874×1080…1179×929 |
| 结论口径 | — | 未算面积差/边界距离/正确率；**无独立审核参考边界**；SRTM 雷达地表模型、垂直基准未声明 → 只能证明链路可运行 |

### 9.2 F-14（低，证据一致性）：`export_result` 报告复核图清单
- Lead 发现并修复：报告原从内层引擎成果取 review 导致清单为空；修复后从 worker 外层 `result.review` 读取。
- verifier 独立复测（合成任务、当前代码树）：`export_result(format=report)` 生成报告，`## 3. 复核图` 列出
  `overview`、`seg-01`… 路径（修复生效）。
- **残留证据问题**：已交付的 `docs/evidence/real-srtm-report.md` 是修复前产物（§3 仍写"无（复核图生成失败或未启用）"），
  与 `docs/real-dem-run.md` §4 声称生成报告不一致 → **建议用修复后代码重新生成该报告**；不影响验收结论，但影响证据自洽性。

### 9.3 证据保留限制（如实记录）
- Lead 清理了 `.cache/dshtest`（DSH 原始日志/job result.json）与仓库内 `.dsh-home` 附件库；verifier 在清理前已完成
  5/5 attachment 校验（记录于 01:58 证据 JSON），清理后仅 2/5 claimed attachment 仍可复核（`58d510dd…`、`fc21a98a…`），
  另有 vision4 时间窗 attachment 与交付图尺寸一致（overview 1011×1078、seg-02 859×929）。E 组已改为只依赖交付证据，
  并对"18 工具注册日志/任务 result.json 已不可复现"做降级标注，不假装独立复核。
- 原始 SRTM 文件（约 26 MB）未入库；交付物保留边界 GeoJSON/报告/9 张图+manifest，可复核到面积/几何/哈希层面。

### 9.4 刷新后的最终证据（02:07，最终代码树，含 export 修复）
| 证据 | 结果 | SHA256 |
|---|---|---|
| 阶段 2 loop | **84/84 PASS**（26.6s） | `5d948666f27b7aa3289a93adcbed80181e36ed6738589da16cc0c8c47d37acaa` |
| MCP stdio | 24/24 PASS | `66d64f1a16161e52ded495b64c52c22983749b5d569bcafb81b6fca86c2a8389` |
| DSH patch | 11/11 PASS | `7531dbdc50e2876a479e826a10073960b6bfc97555003d99e7e3a4ad47cc5794` |
| pytest / gis / integration | 349 passed / 75 passed / 10 passed | — |
| 真实 SRTM 边界 GeoJSON | 面积独立复算一致 | `8d7916698d267fd58883998daa4923950768487b1dda157afb585caf022afdd5` |
| 真实 SRTM 图件 manifest（9 张） | SHA256 全部一致 | `453f4ab61cc8fa3082692dcf1c1fd8d15443ab65df23ce68ea605bf0b6b0be87` |

> 说明：task-4 已于 02:00 complete；本节为交付后的**补充独立复核**，不改变"通过/未完成"结论，只补强真实 DEM 链路证据并
> 记录 F-14/证据保留限制。最终汇总见 `docs/offline-acceptance.md`（298 行，§3.4b/§6.1/§8 已同步）。

## 10. F-14 修复复核（02:16，复现运行产物尚在时）

Lead 用同一 SRTM DEM/参数复现运行 `j-20261029-181428-9c0ac0` 并重新生成报告；verifier 在 `.cache/leadtest/data-regen` 尚存时独立复核：

| 复核项 | 方法/结果 |
|---|---|
| 修复后报告 `docs/evidence/real-srtm-report.md` | SHA256 `daab42541401761c492d3bbc82eb2edd2154684e89ea7feff6f09032a6438800`（与 Lead 声称一致）；§3 列出 overview+seg-01..08 共 9 张复核图 |
| job 摘要归档 `docs/evidence/real-srtm-job-result.json` | SHA256 `116987abb09a647e769227d88ae6b70e487f7b5d1521f377b3dd22bdb28bc93f`（8731 B）；`exports.report_sha256` = 报告哈希，`exports.geojson_sha256` = `f53b3ea3…` 且能在复现运行 exports 目录找到同哈希文件 |
| 交叉复现（两次运行） | 复现任务 `boundary.geojson` 与归档首次运行文件 **逐字节相同**（均 `8d791669…`）；坐标数组逐值相同；复算面积 19,673,902.93 m²、周长 28,797.63 m、609 坐标/608 唯一顶点、0 洞；9 张图与 `docs/evidence/images/real-srtm-*.png` **逐字节一致**；DEM SHA256 相同 |
| 几何坐标哈希 `e66dda7b…` | 复现成功：`sha256(json.dumps(geometry, sort_keys=True))` = `e66dda7ba8ccbe3f…`（方法已记录，供他人复算） |
| `export_result` 导出 geojson | `f53b3ea3…`，其 properties 含 `job_id`/`task_id`，geometry 与归档边界逐值相同（故整文件哈希与任务产物 `boundary.geojson` 不同） |

### 10.1 F-15（低，文档措辞）：**已修复并复核**（2026-10-30 02:23）
- 原 `docs/real-dem-run.md` §5 与 `docs/evidence/README.md` 写"`boundary.geojson` 整体文件哈希不同仅因属性含 `job_id`/时间"；
  实测任务产物 boundary 两次运行**逐字节相同**（`8d791669…`），整文件哈希不同的是 `export_result` 导出 geojson（`f53b3ea3…`，含 job_id/task_id）。
- **Lead 已按实测改正**：现文档明确"任务产物 `artifacts/boundary.geojson` 逐字节相同、坐标逐值一致、面积/周长/图件/DEM SHA256 相同；
  只有 `export_result` 导出的 GeoJSON 整文件哈希不同（properties 含 job_id/task_id/时间，几何一致）"；
  并加注"报告 §3 图路径为复现运行临时目录（已清理），等价归档见 `docs/evidence/images/real-srtm-*.png` + manifest（逐字节一致）"。
- verifier 复核通过：`docs/real-dem-run.md` SHA256 `102422d1…`、`docs/evidence/README.md` SHA256 `9efe7503…`；`.cache/realdem`/`.cache/leadtest` 已清理（`.cache/verify` 保留）。

### 10.2 更新后的证据哈希（最终树，业务代码自 02:07 无改动）
| 文件 | SHA256 |
|---|---|
| `docs/evidence/real-srtm-report.md`（修复后） | `daab42541401761c492d3bbc82eb2edd2154684e89ea7feff6f09032a6438800` |
| `docs/evidence/real-srtm-job-result.json`（新增归档） | `116987abb09a647e769227d88ae6b70e487f7b5d1521f377b3dd22bdb28bc93f` |
| `docs/real-dem-run.md`（F-15 修正后） | `102422d15fc95a4ae6acabe3d2d6c9a72dc1b8d42cb49edf46f37836084c21a2` |
| `docs/evidence/README.md`（F-15 修正后） | `9efe75031bfcd6069b8be11c4790c766614f48ccfd008e06c5aa5dfa47a878ba` |
| `docs/evidence/real-srtm-boundary.geojson`（归档首次运行，未替换） | `8d7916698d267fd58883998daa4923950768487b1dda157afb585caf022afdd5` |
| `docs/evidence/images/real-srtm-manifest.json`（未替换） | `453f4ab61cc8fa3082692dcf1c1fd8d15443ab65df23ce68ea605bf0b6b0be87` |

> task-4 保持 complete；本节仅为 F-14 修复后的补充复核，不改变通过/未完成结论。报告 `docs/offline-acceptance.md`（299 行）已同步
> §3.4b/§6.1 F-14/F-15/§8 哈希（F-15 修复后哈希已更新）。复现运行产物 `.cache/leadtest` 与 `.cache/realdem` 按 Lead 说明随后清理，
> 本报告只引用 `docs/evidence/` 归档哈希。
