# 协议回放证据（模拟环境）

> **本目录由 `scripts/replay_protocol.py` 生成。**
> `simulated: true`、`verified_against_real_device: false`、
> `evidence_level: offline_simulated`。本机没有 Windows 奥维客户端，
> 因此这里是「官方文档级实现 + 官方夹具回放 + 模拟服务器闭环」，
> **不是**真实奥维联机记录。真实设备待核验项见 `replay-evidence.json` 的 `unverified`。

## 内容

| 文件/目录 | 说明 |
|---|---|
| `replay-evidence.json` | 完整证据：步骤、耗时、坐标往返、量算对账、故障场景、待实测清单 |
| `samples/*.json` | 脱敏的请求/响应样例，每条带 `_source`（手册章节 + 官方 URL） |
| `official-fixtures/*.json` | 官方手册/样例 SDK 摘录夹具（带 `_source`） |
| `simulated-map-20301.png` | 模拟 20301 地图图片（带 SIMULATED MAP 水印与元数据） |

## 关键结果

- 登录：ret=817（手册 2.3.1.2.1：成功 ret 是版本号，不是 0）；模拟自标识 simulated=True
- 写草稿对象 ID：文件夹 1000000001、图形 1000000002、出口标签 1000000003
- 坐标往返：max_delta_deg=0.000e+00 （阈值 1e-07 度），轴序检查 axis_order=lon,lat，swap_detected=False
- 20802 量算对账：模拟 961157.679906 ㎡ vs 独立 GIS 961157.679906 ㎡，差 4.474e-07 ㎡（EPSG:32648）
- 20301 模拟地图：500x500px，sha256=4e8d9e5e14b9d762…，水印校验=True
- 可重复性：True（4 次运行核心摘要一致，digest=f05b71f294fbfecf…）
- 20112 响应形态：测试了 manual_flat, sdk_nested 两种官方写法

## 故障注入验证

| 场景 | 通过 | 说明 |
|---|---|---|
| drop_on_write | True | 断线时 pending 请求显式失败；不自动重发创建（防重复对象） |
| timeout_on | True | 超时显式失败且不自动重试；写请求超时后应回读确认而非重写 |
| duplicate_20112 | True | 重复响应未被当成第二次写入结果；真实设备是否重发未知（待实测） |
| permission_denied_20301 | True | ret=-7 映射为权限不足（OvitalPrereqError），不伪装成空图片 |

## 证据分级

- `official_document`：手册明确写到的命令/字段（见 `official-fixtures/` 与 `samples/` 的 `_source`）。
- `offline_simulated`：本目录的回放结果（模拟服务器 + 官方夹具）。
- `real_device_pending`：必须在 Windows 奥维实机验证（见 `replay-evidence.json` 的 `unverified`）。

**禁止把本目录结果表述为真实设备验证结果。**
