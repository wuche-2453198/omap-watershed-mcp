# 奥维 WebSocket V2.7 协议笔记（实施依据与证据）

> 本文件是 `ovital_mcp/protocol/` 与 `ovital_mcp/mock/` 的**唯一协议依据说明**，
> 逐条标注出处与证据等级。任何协议字段/命令号/枚举若不在本文件有出处，就不得写进代码。
>
> **证据声明（必读）**：本机是 macOS，没有 Windows 奥维客户端、没有用户奥维账号，
> 因此**真实联机未验证**。所有结论属于以下两级：
> `official_document`（官方手册明确写出）/ `official_sample`（官方样例 SDK 出现但手册未写）；
> 本机闭环验证属于 `offline_simulated`（模拟服务器回放）。
> **禁止**把本文件或回放证据表述为 `real_device_verified`。

---

## 1. 官方来源与本地副本

| 资料 | 官方 URL | 本地副本 | 用途 |
|---|---|---|---|
| WebSocket V2.7 手册 | https://www.gpsov.com/appdoc/index.php?id=32 | `.research/ovital-scratch/manual-32.md` | 命令号、字段、枚举、错误码、示例 |
| 官方样例 SDK（V2.7 配套） | https://download.ovital.com/pub/api/omapInterfaceWeb_latest.zip | `.research/ovital-scratch/zip-32/omapInterfaceWeb_latest/omapInterfaceWeb/js/` | 实际消息构造、连接/登录、响应读取路径 |
| 第三方接口介绍 | https://www.ovital.com/openapi/ | `.research/ovital-api.txt` | 能力边界（读写对象、会员要求） |
| OPEN+（V4.2，仅参考） | — | `.research/ovital-scratch/manual-42.md` | 与桌面 WebSocket 是不同入口，本版不用 |

官方样例中与协议直接相关的两个文件：

- `js/omapinterface.js`：命令号常量（文件开头 `OMAP_WIN_CMD_*` / `OMAP_API_CMD_*`）、
  连接与登录（`onopen`）、请求发送（`SendJson`）、响应分发（`onmessage`）。
- `js/omapobj.js`：消息结构体构造器 `BuildSign` / `BuildTrack` / `BuildShape` /
  `BuildTree` / `BuildGroup` / `BuildDir` / `BuildReqMapData` / `BuildReqMapFile` /
  `BuildReqGetContour` / `BuildObj` / `BuildPoint` 等。

> 手册 HTML 存在转义残留：示例中的 `?` 多为空格被转义后的痕迹（如
> `"tmModify":"2019/08/12?10:30:25"`、`errmsg:"Pre-shared?key?error"`）。
> 本实现按空格处理，不把 `?` 当字面字符。

---

## 2. 本机事实与验证边界

| 项 | 事实 | 对实现的影响 |
|---|---|---|
| 操作系统 | macOS 26.6.2 arm64 | 无法运行 Windows 奥维桌面端 |
| 奥维客户端 | 无 | 无法真实联机；只能模拟 + 官方夹具回放 |
| 奥维账号 | 无 | VIP/SVIP 权限、点数限制无法实测 |
| Python | 项目 `.venv`（3.13，websockets 17.3、mcp 2.3、pytest 9.2） | 全部命令用 `.venv/bin/python` |

因此本包的验收口径是：

1. 命令号、字段、枚举、错误码**逐条**能回到手册章节或官方样例；
2. 官方夹具能走真实编解码/解析路径（`scripts/replay_protocol.py` 第一步）；
3. 模拟服务器上完成「登录 → 写 → 回读 → 量算 → 地图 → 删除」闭环；
4. 手册未写/自相矛盾之处**显式标注待实测**，代码里不猜。

---

## 3. 传输层与帧格式

### 3.1 WebSocket 连接

- 地址：`ws://<ip>:<port>` 或 `wss://<ip>:<port>`（手册 2.1.3.1）。
- 端口由用户在奥维「系统 → 系统设置 → 高级 → 第三方接口（窗口消息+Websocket）」中
  自行设置（手册 2.1.2.1）；**7699 只是官方示例**，程序不得自造默认端口。
  本包 `OvitalClient` 要求显式 `endpoint`，仅接受 `ws://`/`wss://`。
- 子协议：官方样例 `new WebSocket("ws://"+ip, "ws")`，即子协议名 `"ws"`。
  本包客户端发送 `subprotocols=["ws"]`；真实奥维是否强制校验「待实测」。
- 帧：JSON 文本（手册 2.1.3「使用JSON格式数据进行数据交互」）。
- 单帧大小：手册未给上限；20301 返回 base64 图片可能远大于 websockets 默认 1 MiB，
  因此客户端把 `max_size` 放宽到 64 MiB（`constants.DEFAULT_MAX_MESSAGE_BYTES`，
  本项目保守值，非官方数值）。

### 3.2 请求外壳

手册 2.1.3.6 / 2.3.1.1：

```json
{"cmdid": 20006, "ret": 0, "msg": {}}
```

- `cmdid`：命令号（int，手册 2.2.1）。
- `ret`：**请求一律为 0**。
- `msg`：命令参数对象（可为空对象）。

实现：`protocol.codec.encode_request(cmdid, msg)`，紧凑 UTF-8 JSON
（与官方样例 `JSON.stringify` 一致，不转义中文）。

### 3.3 响应外壳

```json
{"cmdid": 20006, "ret": 817, "msg": {}}
```

- 成功响应**通常省略 `ret`**（手册各命令示例），此时按 0 处理；
- 20000/20006/20104 等命令成功时 `ret` 是**奥维版本号**（>0），不是 0；
- 失败响应 `ret<0`（手册 2.2.7）或 20000 的 `ret=0 + msg.errmsg`；
- 事件帧（20008/20009/20015）只有 `cmdid` + `msg`，没有 `ret`。

实现：`protocol.codec.decode_message(raw) -> OvitalResponse`。

### 3.4 响应关联（关键约束）

**协议没有请求 ID**，响应只能按 `cmdid` 关联。因此：

- 同一 `cmdid` 的请求必须串行（`OvitalClient` 每个 cmdid 一把 `asyncio.Lock`）；
- 不同 `cmdid` 可并发；
- 没有被等待的响应（事件）进事件队列（`next_event()`）；
- 断线时 pending 请求抛 `OvitalConnectionError`，**不自动重试**；重连后也不得
  自动重发写请求（由工具层写后回读确认）。

---

## 4. 登录与鉴权（20000）

### 4.1 请求

手册 2.3.1.1：

| 参数 | 必填 | 类型 | 说明 |
|---|---|---|---|
| cmdid | 是 | int | 20000 |
| ret | 是 | int | 0 |
| msg.md5psk | 是 | string | MD5 加密后的密码（16 位大写） |

算法（手册 2.3.1.1 示例 + 官方样例 `omapinterface.js` `onopen`）：

```
md5psk = md5(psk).hexdigest().upper()[:16]
```

**可自证**：手册示例 `E10ADC3949BA59AB` 正是 `md5("123456")` 的大写前 16 位，
`codec.md5psk("123456") == "E10ADC3949BA59AB"` 已有测试
（`tests/test_protocol_codec.py::test_md5psk_matches_manual_example`）。

### 4.2 成功响应

手册 2.3.1.2.1：

```json
{"cmdid":20000, "ret":817, "msg":{"AppId":41}}
```

- `ret` = 奥维版本号（**不是 0**）；
- `msg.AppId`：每个 ws 连接一个，用于区分多连接（手册原文）；示例是 int，表格写 string。

### 4.3 失败响应

手册 2.3.1.2.2：

```json
{"cmdid":20000, "ret":0, "msg":{"errmsg":"Pre-shared?key?error"}}
```

- `ret=0` 且 `msg.errmsg` → 登录失败（密钥错误等）；
- 实现抛 `AuthError`，消息里**不含** PSK/md5psk。

### 4.4 本项目的模拟扩展（不是官方协议）

模拟服务器在登录成功响应里额外返回 `msg.Simulated: true`，用于让上层在
provenance 里标 `simulated`。**真实奥维不会返回该字段**，因此：

- `OvitalClient.is_simulated` = 握手 `msg.Simulated` 为真；
- 真实设备上恒为 False；
- 20006/20501 等模拟响应也带 `Simulated: true`（方便逐条判断），真实设备不会。

---

## 5. ret 语义与错误码

### 5.1 语义

| 情况 | ret | 含义 | 客户端处理 |
|---|---|---|---|
| 成功（多数命令） | 省略 / 0 | 成功 | 返回 `OvitalResponse` |
| 成功（20000/20006/20104） | >0 | 奥维版本号 | 成功，`ret` 可读作版本 |
| 登录失败 | 0 + `msg.errmsg` | 鉴权失败 | `AuthError` |
| 失败 | <0 | 手册 2.2.7 | 映射为具体异常 |

`raise_for_ret(cmdid, ret, msg)` 映射（`protocol/errors.py`）：

| ret | 手册 2.2.7 含义 | 异常 |
|---|---|---|
| -1 | 没有找到奥维地图程序窗口 | `OvitalConnectionError` |
| -2 | 向奥维地图程序注册失败 | `OvitalConnectionError` |
| -3 | 奥维地图程序版本太低 | `OvitalConnectionError` |
| -4 | 参数错误 | `OvitalRetError`（`OvitalProtocolError` 子类） |
| -5 | 当前绑定用户不是 VIP | `OvitalPrereqError` |
| -6 | 消息处理失败 | `OvitalRetError` |
| -7 | 当前绑定用户不是 SVIP | `OvitalPrereqError` |

> 手册个别命令失败表把 -7 写成「不是 VIP5 或 VIP5 以上」（2.3.29/2.3.30/2.3.31），
> 与 2.2.7 的「不是 SVIP」用词不一致；提示语同时给出两种称呼，不做二选一断言。

### 5.2 命令级「msg 内 Ret」

部分命令在 `msg` 里另有 `Ret` 字段，**不是外层 ret**：

- 20302（地图图片文件）`msg.Ret`：3=取图成功，-161=取图失败（手册 2.3.30.2）；
- 20303（等高线）`msg.Ret`：0=成功，-1..-8 各有含义（手册 2.3.31.2）。

实现：`commands.get_contours` 读取 `msg.Ret`，-1/-2 映射为 `OvitalPrereqError`，
其它负数映射为协议错误。

### 5.3 会员要求（手册 2.1.1）

- 获取地图图片数据（20301/20302）、获取等高线数据（20303）：**SVIP 及以上**；
- 其他功能：**至少 VIP**；
- 手册未给 `OmapLevel` 数值与 VIP/SVIP 名称的映射 → 本项目不猜测
  （`constants.tier_for_omap_level()` 恒返回 `unknown`），真实权限以命令 ret=-5/-7 为准。

---

## 6. 命令实现矩阵

| cmdid | 能力 | 手册 | 本包实现 | 状态 |
|---|---|---|---|---|
| 20000 | 登录 | 2.3.1 | `OvitalClient.connect` | 已实现 + 模拟验证 |
| 20006 | 获取奥维版本 | 2.3.24 | `commands.get_version` | 已实现 + 模拟验证 |
| 20008 | 奥维被关闭（事件） | 2.3.2 | `client.next_event` | 已实现 + 模拟验证 |
| 20009 | 保存按钮被点击（事件） | 2.3.3 | `client.next_event` | 已实现 + 模拟验证 |
| 20014 | 监测对象改变（注册） | 2.3.49 | `client.monitor_object_changes` | 已实现 + 模拟验证 |
| 20015 | 对象变动信息（事件） | 2.3.49 | `client.next_event` | 已实现 + 模拟验证 |
| 20016 | 屏幕中心点/左上/右下 | 2.3.58 | `commands.get_screen_corners` | 已实现 + 模拟验证（示例错位待实测） |
| 20101 | 获取收藏夹对象 | 2.3.11 | `commands.get_objects/get_shape` | 已实现 + 模拟验证 |
| 20103 | 删除收藏夹对象 | 2.3.13 | `commands.delete_objects` | 已实现（成功响应形态待实测） |
| 20104 | 获取选中对象 | 2.3.17 | `commands.get_selected` | 已实现 + 模拟验证 |
| 20107 | 查找奥维对象 | 2.3.22 | `commands.find_objects` | 已实现（不返回名称，手册明确） |
| 20110 | 选中对象树 | 2.3.18 | `commands.get_selected_tree` | 已实现 + 模拟验证（分片待实测） |
| 20112 | 设置收藏夹对象（写） | 2.3.12 | `commands.write_tree/write_objects` | 已实现 + 模拟验证（响应形态/上限待实测） |
| 20212 | 像素比例 | 2.3.28 | `commands.get_viewport` | 已实现 + 模拟验证（x/y 语义待实测） |
| 20222 | 系统坐标系参数 | 2.3.51 | `commands.get_system_crs` | 已实现 + 模拟验证 |
| 20301 | 地图图片数据 | 2.3.29 | `commands.get_map_image` | 已实现 + 模拟验证（字段名差异待实测） |
| 20303 | 等高线 | 2.3.31 | `commands.get_contours` | 已实现（**数据格式手册未定义**，只回元数据） |
| 20501 | VIP 等级 | 2.3.19 | `commands.get_vip_level` | 已实现 + 模拟验证 |
| 20730 | 窗体可显示区域 | 2.3.52 | `commands.get_viewport` | 已实现 + 模拟验证 |
| 20802 | 图形量算 | 2.3.48 | `commands.get_shape_metrics` | 已实现 + 模拟验证（字段名差异已容错） |

**本版未实现**（工具层当前不需要；不要假设支持）：

- 20001 获取经纬度、20002 设置临时标签、20003 地址解析、20004 照片、20005 轨迹时间点；
- 20010 清除临时对象、20011 地图跳转、20012/20013 地图中心点（20012 只作为事件入队）；
- 20100 收藏夹列表（模拟器实现，客户端未封装）、20106 设为选中、20108 移动对象；
- 20201/20202/20211 关联点、20221 设置系统坐标系；
- 20302 地图图片文件（手册 2.3.30，字段与 20301 不同）；
- 204xx 工具栏按钮、206xx 填充图案、207xx 自定义按钮/菜单/弹窗、20801 轨迹量算、
  20810/20811 显示状态、20820/20821 当前对象、20860+ 自定义地图、210xx 设备信息。

---

## 7. 逐命令协议细节与差异

### 7.1 20000 登录

见 §4。要点：`ret>0` 成功；`ret=0+errmsg` 失败；`AppId` 每连接一个。

### 7.2 20006 获取版本

- 请求（2.3.24.1）：`{"cmdid":20006,"ret":0,"msg":{}}`
- 响应（2.3.24.2）：`ret` = 版本号（示例 817），`msg` 空。
- 实现要求 `ret>0`，否则视为协议异常（不把 0 当版本）。

### 7.3 20104 获取选中对象

- 请求（2.3.17.1）：`msg.Flag`，取值见手册 2.2.3：
  1=选中对象（文件夹自动含子对象）、2=不自动含未选中子对象、3=排除文件夹本身。
- 响应（2.3.17.2）：`msg.Count` + `msg.ObjList[]`（`ObjID`/`Type`/`Name`）。
  - 表格说成功 `ret` 是**奥维版本号**，示例却省略 ret → 解析器两者都接受。
  - **ObjID 类型不一致**：20104 示例是 int，20101 是 string，20100 表格写 double。
    解析统一 `as_int()` 容错（string/int/float 均可）。

### 7.4 20110 选中对象树

- 请求（2.3.18.1）：`msg.LatlngType`（手册 2.2.2：0=系统,1=CGCS2000,2=GCJ02,3=平面,4=墨卡托）。
- 响应（2.3.18.2）：`msg.ObjItems[]`，根是容器节点（示例 `ObjID="9"`, `Name=""`），
  选中对象在 `Object.ObjectDetail.ObjChildren`，可递归多层。
- 官方样例 `GetSelObjTree` 还支持分片：请求加 `Fragment:1` + `FragmentMaxSize`，
  响应分多帧 `FragmentNum/FragmentIndex/FragmentData`。**手册未记载** →
  本包客户端支持自动重组服务端返回的分片帧，但**不主动请求分片**（待实测）。

### 7.5 20101 获取收藏夹对象

- 请求（2.3.11.1）：`msg.ObjID`（int）+ `msg.LatlngType`（手册 2.2.2）。
  读 CGCS2000 用 `LatlngType=1`（推荐，避免 GCJ-02 重复偏移）。
- 响应（2.3.11.2）：`msg.ObjItems[]`，每项：

```json
{"Type":13,"ObjID":"627700251","ParentID":0,"SrvID":"7647106613611937263",
 "tmModify":"2019/03/07?16:08:38",
 "Object":{"Name":"Shape","Type":13,"Comment":"Shape","ObjectDetail":{...}}}
```

- 说明：
  - `ObjID` 是 string（20104 是 int）；
  - 对象类型见手册 2.2.5：7 标签、8 轨迹、11 导航线路、13 图形、30 文件夹；
  - 文件夹的子对象在 `ObjectDetail.ObjChildren`（手册 2.3.11 示例只展示单对象；
    是否总带子对象「待实测」，`get_folder_children` 做了兼容）；
  - 找不到对象时手册未规定响应（模拟器返回空 `ObjItems`，属**模拟器选择**）。

### 7.6 20112 设置收藏夹对象（写）

- 请求（2.3.12.1）：`msg.ObjTree`，**外层是文件夹对象，内层是待添加对象**：
  - `Type`（30=文件夹）、`ObjID`（0=自动生成；非 0 且已存在=替换，手册表明确）、
    `SrvID`、`tmModify`、`ParentID`、`Name`、`Child`、`AllChild`、`LoadOk`、`SaveMerge`、
    `Group`、`AutoLoad`、`ShowLevel`、`ShowLevelMax`、`ShowLevelBak`、`Crypt`、`ReadOnly`、
    `NotHotId`、`Relate`、`ObjChildren[]`；
  - 子对象结构见 2.4.6（标签）/2.4.8（图形）/2.4.9（线路）等。
- 响应（2.3.12.2）：`msg.ObjItems[]`，每项 `{Type, ObjID, ParentID}`。
  - **重大差异**：手册示例是扁平数组；官方样例 `omapinterface.js` 读取路径是
    `msg.ObjItems[0].Object.ObjectDetail.ObjChildren`（树形嵌套）。
    → 两种都实现（`models.parse_write_results`），真实形态**待实测**。
  - **重大差异**：手册请求示例有 5 个子对象（含 1 个文件夹），响应示例只有 4 个
    （7/8/13/11，没有 30）→ **不能假设响应一定包含新建文件夹的 ObjID**。
    工具层写草稿时应：若结果无 type=30，用 20107 按名称查找文件夹兜底。
- 官方样例还支持大对象分片（`FragmentIndex/FragmentData/FragmentNum`），手册未记载 →
  本版**不启用**分片写；请求体超过 4 MiB（本项目保守阈值）显式报错（待实测真实上限）。
- 官方样例 `BuildTree` 的根是**合成容器**（`Object.Name=""`，`ParentID`=目标父文件夹），
  与手册示例「根即文件夹对象」写法不同。本实现按手册示例（根即草稿文件夹，有名字，
  `ParentID` 由参数决定），子对象 `ParentID` 设为该文件夹 ObjID（新建时为 0，由树结构挂接）。
  **该细节待实测**。

### 7.7 20103 删除收藏夹对象

- 请求（2.3.13.1）：`msg.NoGroup`（「不删除文件夹」）+ `msg.ListID`（对象 ID 数组）。
- 失败（2.3.13.2）：`ret` -4/-5；**成功响应形态手册未给**（只给了失败表）→ 待实测。
  实现只要求 `ret>=0`，不解析响应体。
- 官方样例 `BuildObj` 把字段写成 **`NoTroup`**（拼写错误），手册写 `NoGroup`。
  本实现按手册发 `NoGroup`；真实设备是否接受 `NoTroup`/是否忽略未知字段「待实测」。
- `NoGroup` 的取值语义手册只写「不删除文件夹」，未写 0/1 对应关系；官方样例传 1。
  模拟器按「1=跳过文件夹」实现（模拟器选择，非官方断言）。

### 7.8 20107 查找奥维对象

- 请求（2.3.22.1）：`ParentID`、`SrhOpt`（手册 2.2.4）、`ObjType`（0=任何类型）、
  `idParent`、`idShapeSelect`、`NoChild`、`MaxCnt`、`SrhTxt`、`SrhValue`、`NoCase`。
  - **字段名差异**：表格写 `idParent`/`idShapeSelect`，示例写 `ParentID`/`ShapeSelect`
    → 本实现两套都发（两处都有官方出处，真实设备接受哪个待实测）。
- 响应（2.3.22.2）：`msg.ListID[] = [{ObjID, ObjType}]`，**不含名称**。
  需要名称/坐标必须再对每个 ID 调 20101。
  - 示例里 `ObjID` 是 int，表格写 string → 解析容错。
  - **实现要点（维护者必读）**：`commands.find_objects()` 返回的 `ObjNode.name`
    **恒为 `None`**，这不是 bug，而是手册 2.3.22.2 的响应本身就不含名称。
    任何「按名称确认文件夹/对象」的逻辑（如 `write_draft` 的草稿文件夹复用、
    `finalize_draft` 的对象确认）都必须：
    `find_objects(name)` 拿到候选 `ObjID`/`ObjType` → 对每个候选再调
    `get_objects(obj_id)`（20101）读取 `Object.Name` 与 `Type` 后比对。
    禁止把 `find_objects` 的返回直接当作「名称已匹配」的证据。
    工具层 `ovital_mcp/tools/draft.py::_match_folder_by_name` 即按此实现。

### 7.9 20802 图形对象基本信息（量算）

- 请求（2.3.48.1）：`msg.ObjID`。
- 响应（2.3.48.2）：

| 手册表格字段 | 示例/官方样例字段 | 含义 |
|---|---|---|
| `idObj` | `ObjID` | 对象 ID；0 表示没找到图形对象 |
| `iPointCnt` | `PointCnt` | 顶点个数 |
| `iAreaCnt` | `AreaCnt` | 区域个数 |
| `dPerimeter` | `Perimeter` | 周长（**km**） |
| `dAreaSize` | `AreaSize` | 面积（**㎡**） |

- 官方样例 `omapinterface.js` 读取的是无前缀字段（`ObjID/PointCnt/AreaCnt/Perimeter/AreaSize`），
  与示例一致 → 本实现两种都接受，以示例字段名为发送/模拟口径。
- 单位必须记录：**面积 ㎡、周长 km**；面积不是「亩」也不是「平方公里」。
- 真实奥维的面积算法（投影口径）手册未说明 → 模拟器用局部 UTM + shapely 计算，
  与项目 GIS 口径一致；真实设备数值需对账（待实测）。

### 7.10 20301 获取地图图片数据

- 请求（2.3.29.1）：`msg.ReqMapDatas[]`，元素：
  `x_lng1/y_lat1`（左下角）、`x_lng2/y_lat2`（右上角）、`CoordType`（手册 2.2.2 顶层枚举）、
  `MaxLevel`（地图级别）、`MaxPixel`（最大像素）。
  - **字段名差异**：手册表写 `MaxLevel`；官方样例 `omapobj.js` `BuildReqMapData` 实际发
    `MapLevel`（响应示例又用 `MaxLevel`）→ 本实现两个字段都发（待实测）。
- 响应（2.3.29.2）：`msg.Data` 为 **base64 PNG**（示例以 `iVBORw0KGgo` 开头）。
  - **结构差异**：表格说响应在 `ReqMapDatas[]` 里，示例却是 msg 顶层扁平字段
    （`x_lng1` 等 + `Data`）→ 解析器两种都接受。
  - 失败：-4 参数错误、-6 消息处理失败、-7 不是 SVIP（手册 2.3.29.2）。
- 坐标：示例用 `CoordType=2`（当前坐标系）且给的是平面坐标；要经纬度用 `CoordType=0`。
  本项目默认 `coord_type=0`（CGCS2000）。
- 权限：手册 2.1.1 明确地图图片需 SVIP；`ret=-7` 时工具层必须明确报「需要 SVIP」，
  并建议用本地 DEM/复核图替代，**不得返回空白图或伪造地图**。

### 7.11 20303 获取等高线

- 请求（2.3.31.1）：`msg.ReqMapDatas[]`（手册表）/ `msg.ReqContours[]`（示例与官方样例
  `BuildReqGetContour`），元素：`MapID`、`Value`、bbox、`CoordType`、`MapLevel`、
  `ElevValue`、`CheckFlag`。本实现两个数组名都发。
- 响应（2.3.31.2）：`msg.Ret`（0 成功，-1..-8 含义见手册）、`MapID`、`Value`、bbox、
  `CoordType`、`MapLevel`、`EvevValue`（**手册拼写错误**，请求字段是 `ElevValue`）、
  `NeedVip`、`CheckFlag`、`CheckResult`、`Part`。
- **关键限制**：手册**没有给出等高线几何/高程数组的数据格式**。因此本包
  `get_contours` 只返回元数据并显式标 `geometry_format_unverified=True`；
  **禁止**编造等高线几何。需要等高线做复核时应使用本地 DEM 生成。

### 7.12 20016 获取屏幕中心点/左上角/右下角

- 请求（2.3.58.1）：`msg.ReqCoordType`——**独立枚举**：
  0=根据奥维决定、1=真实经纬度、2=GCJ-02。
  （注意：0 在这里**不是** CGCS2000，与顶层 CoordType 完全不同！）
- 响应（2.3.58.2）：`latc/lngc`（中心）、`latlt/lnglt`（左上）、`latrb/lngrb`（右下）、
  `ReqCoordType`、`RealLl`、`MapLevel`、`MapType`。
- **手册示例数值错位**：示例 `latlt=30.59536557, lnglt=27.15203275,
  latrb=111.41784668, lngrb=119.09729004`——纬度字段出现 111/119，经度字段出现 27/30，
  明显不是同一量级的合理组合。因此：
  - 不能拿该示例推断轴序；
  - 解析器对不合理值给 `warnings`，不静默纠正；
  - 真实字段含义/轴序**待实测**。
- 用途边界：仅辅助视图取证；不能用角点保证任意三维视图的像素映射。

### 7.13 20730 获取奥维可显示区域

- 请求（2.3.52.1）：`msg` 可空（表格写 `AppID`，示例空对象；本实现发空对象）。
- 响应（2.3.52.2）：`Left/Right/Top/Bottom`（距离主窗体边界），
  **值 <=0 表示窗体最小化，无法正常获取区域参数**。

### 7.14 20212 像素比例

- 请求（2.3.28.1）：`msg.x`、`msg.y`（手册只写「x坐标/y坐标」，语义未说明）。
- 响应（2.3.28.2）：`msg.MeterX[]`、`msg.MeterY[]`（各级别下每像素米数）。
- 本实现 `get_viewport` 传窗体区域中心像素；**x/y 语义待实测**。

### 7.15 20222 获取系统坐标系参数

- 请求（2.3.51.1）：空 `msg`。
- 响应（2.3.51.2）：`CoordType`——**第 6 套枚举**：2=经纬度、4=横轴墨卡托；
  `iPrjType`：0=WGS84、1=Xian80、2=Krasovsky_1940、3=UTM、4=CGCS2000、5=CustomType；
  以及 `FalseEasting/FalseNorthing/ScaleFactor/CentralMeridian/AngularUnit/LinearUnit/
  SemimajorAxis/InverseFlattening` 等。
- 用途：解释 `LatlngType=0`（系统设置的坐标系）到底是什么；本项目默认仍用
  `LatlngType=1`（CGCS2000）读写，避免坐标系不确定。

### 7.16 20501 获取 VIP 等级

- 请求（2.3.19.1）：空 `msg`。
- 响应（2.3.19.2）：`msg.OmapLevel`（账号 VIP 等级，int）+ `msg.LoadOK`（是否正常使用）。
- 手册**未给** OmapLevel 数值与 VIP/SVIP 名称的映射 → 本项目只透传数值，
  等级判断以具体命令 ret=-5/-7 为准（见 §5.3）。
- 20503（试用 SVIP / 判断是否 SVIP，手册 2.3.20）：`msg.Send` 0/1，
  响应 `msg.ReqVip5Result`（1=之前请求过，2=可以发送请求）。本版未实现 20503 请求封装，
  仅常量与枚举（`constants.SVIP_TRIAL_RESULT_NAMES`）。

### 7.17 20014 / 20015 对象变动监听

- 20014 请求（2.3.49.1）：`msg.AppID`：0=停止监听、-1=监听所有、>0=指定 AppID、-100=查询。
  响应示例 `ret:-1`（手册示例本身如此，说明失败形态）。
- 20015 事件（2.3.49.2）：`msg.Version`（0 接口版本）、`msg.Flag`（0=添加或修改，1=删除）、
  `msg.ListID[]`（被修改对象 ID）。
- 本包：`client.monitor_object_changes(app_id=0)` 发送 20014；20015 进事件队列。
  模拟器在写入/删除后主动推 20015（模拟行为）。

### 7.18 20008 / 20009 事件

- 20008（2.3.2）：奥维被关闭，`msg.msg = "Omap is Closed! "`。
- 20009（2.3.3）：保存按钮被点击，`msg.msg = "The save button was pressed! "`。
- 无 ret，进事件队列。

---

## 8. 坐标契约（最容易出错的部分）

### 8.1 四套互不相同的协议枚举

| 出处 | 枚举名 | 取值 | 语义 |
|---|---|---|---|
| 手册 2.2.2 | 顶层 `CoordType` | 0 / 1 / 2 | CGCS2000 / GCJ-02 / 当前坐标系 |
| 手册 2.2.2（2.3.11/2.3.18 同） | `LatlngType` | 0 / 1 / 2 / 3 / 4 | 系统设置 / CGCS2000 / GCJ-02 / 平面 / 墨卡托 xy |
| 手册 2.4.1 | `OmapLatlng.CoordType` | 0 / 1 / 2 | **GCJ-02 / CGCS2000 / 平面**（与顶层 0/1 相反！） |
| 手册 2.4.8 | `Shape.ObjectDetail.Gcj02` | 0 / 1 / 2 | CGCS2000 / GCJ-02 / 平面 |

另有两套额外枚举（协议层单独解析，不在架构契约 §3 的四套里）：

| 出处 | 枚举名 | 取值 | 语义 |
|---|---|---|---|
| 手册 2.3.58 | 20016 `ReqCoordType` | 0 / 1 / 2 | 根据奥维决定 / 真实经纬度 / GCJ-02 |
| 手册 2.3.51 | 20222 `CoordType` | 2 / 4 | 经纬度 / 横轴墨卡托 |

以及 20222 的 `iPrjType`：0=WGS84、1=Xian80、2=Krasovsky_1940、3=UTM、4=CGCS2000、5=CustomType。

> **纪律**：解析逻辑只在 `ovital_mcp/coordinates.py`（Lead 所有）与
> `protocol/constants.py`（两套额外枚举）里；协议层任何地方都不得把数字直接当语义用。

### 8.2 轴序

- `Shape.Latlng`：**扁平数组，纬度在前**：
  手册 2.4.8 示例 `[39.01064751, 106.57287598, 38.52668162, 107.10571289]`，
  39.01 是北纬、106.57 是东经 → 顺序是 `lat, lng, lat, lng, ...`。
- `OmapLatlng`（手册 2.4.1）：命名字段 `lat` / `lng`（无扁平数组轴序问题）。
- `Shape.Latlng` 的 `Mtp` = 顶点个数；示例 2 点 → `Mtp=2`。
- 对外统一 `(lon, lat)`（EPSG:4490 语义）；所有解析/构建函数 docstring 写明轴序。
- 本项目用**往返验证**固定这一点：写 `(lon,lat)` → 协议 `lat,lng` → 回读 `(lon,lat)`，
  断言 `max_delta_deg ≤ 1e-7`（约 1.1 cm），并用「经纬度互换后的偏差是否更小」
  检测轴序颠倒（`commands.compare_vertices().swap_detected`）。

### 8.3 两个点的图形 ≠ 多边形

手册 2.4.8 明确：「当图形为两个点时，通过起始角度与终止角度表示圆、扇形、矩形」。
因此 `len(Latlng) == 4`（2 个点）的图形**不是**多边形，不能当集水区边界。
本包 `ShapeObject.two_point_special=True` 显式标记；工具层必须拒绝把它当面积对象。
两个点图形的面积/周长语义（角度如何影响几何）手册未给 → 待实测。

### 8.4 多区域与孔洞

手册只给了 `OuterRgnIdx`（外部 Rgn 索引，0 为全部都是外部）与 `AreaCnt`（20802 区域个数），
**没有**说明 `Mtp + Latlng + OuterRgnIdx` 如何组合表示孔洞/多区域。
→ 本版**不写**孔洞/多区域；读到的 `AreaCnt>1` 需人工核实（待实测）。

### 8.5 坐标系选择建议（写草稿）

- 读：`LatlngType=1`（CGCS2000），避免 GCJ-02 重复偏移；
- 写：`Shape.ObjectDetail.Gcj02=0`（CGCS2000），本项目 `build_shape_object` 默认；
- 若必须用 GCJ-02：显式 `allow_gcj02_conversion=True`，并记录「公开近似算法」；
- 平面坐标（LatlngType=3 / Gcj02=2）：需要奥维系统坐标系参数，本版直接报错，不猜。

---

## 9. 分片传输（官方样例有、手册未写 → 待实测）

官方样例 `omapinterface.js`：

- 20110 请求可带 `Fragment:1` + `FragmentMaxSize:N`；响应分多帧：
  `{"msg":{"FragmentNum":N,"FragmentIndex":i,"FragmentData":"<base64>"}}`，
  `FragmentData` 是 **JSON 文本片段**的 base64；SDK 按 `FragmentIndex==FragmentNum-1`
  判断收齐（隐含顺序到达）。
- 20112 大对象请求会按 `FragmentIndex/FragmentData/FragmentNum` 分片发送
  （`FragmentData` 是 JSON 文本切片，未 base64）。

本项目处理：

- 客户端**自动重组**服务端分片响应（`client.FragmentAccumulator`，容忍乱序）；
- 客户端**不主动请求**分片（20110 不带 Fragment 字段）；
- 写入**不启用**分片；超过 `DEFAULT_WRITE_MAX_BYTES=4 MiB` 显式报错；
- base64 变体：SDK `Base64ToStr` 用 `'-'→'/'; '_'→'+'`（与标准 URL-safe 相反，
  疑似样例 bug）。`codec.b64decode_variants()` 三种变体都尝试，但 `-`/`_` 同时出现时
  存在歧义 → 依赖后续 JSON 解析做最终校验，失败即显式报错，不静默返回坏数据。

---

## 10. 模拟服务器（`ovital_mcp/mock/`）

### 10.1 定位与自标识

- 本机无法真实联机，模拟服务器提供可重复的离线闭环；
- 所有成功响应带 `msg.Simulated: true`（本项目扩展；真实奥维不会返回）；
- 20301 返回的 PNG：画面有 `SIMULATED MAP` 水印 + `NOT REAL OVITAL DATA` +
  范围/经纬网文字；PNG 元数据（tEXt）写 `Description=SIMULATED MAP - NOT REAL OVITAL DATA`、
  `Simulated=True`、`verified_against_real_device=False`、bbox 等；
- 回放证据一律标 `simulated=true`、`verified_against_real_device=false`、
  `evidence_level=offline_simulated`。

### 10.2 世界模型（`mock/world.py`）

- 内存对象树：文件夹/标签/图形（轨迹/线路不建模）；
- 20112：`ObjID=0` 自动生成（确定性计数器，同一操作序列 → 同一 ID），
  非 0 且存在 → 替换（手册 2.3.12.1）；子对象按树结构挂到根文件夹；
- 20101：按 `LatlngType` 转换坐标（0/1 按 CGCS2000，2 用公开近似算法转 GCJ-02；
  3/4 未实现 → ret=-4）；
- 20802：用局部 UTM（`coordinates.metric_crs_for`）+ shapely **真算**面积/周长；
  两点图形返回 0 并带 `_note`（模拟器未实现圆/扇形/矩形几何）；
- 20107：按手册 2.2.4 的 SrhOpt 0/1/2/3/4/5/6 做名称/备注匹配；11/12/21/22/23 需要
  样式属性 → ret=-4（模拟器未实现）；
- 20103：`NoGroup=1` 跳过文件夹（按字面「不删除文件夹」），`NoGroup=0` 递归删除；
- 模拟器**不强制**「必须先 20000 登录」的调用顺序（真实奥维是否强制「待实测」）；
  客户端本身只在 `connect()` 成功后才允许 `request()`，因此正常路径不会乱序。
- 20301：按 bbox 长宽比渲染，保证 `width*height ≤ MaxPixel`；只支持 `CoordType=0/1`，
  平面坐标返回 -4（模拟器未实现投影参数）；
- 20303：只回手册元数据字段，**不编造几何**；
- 20016/20730/20212/20222/20501/20006：返回手册示例量级的模拟值。

### 10.3 故障注入（`mock/faults.py`）

| 注入 | 行为 | 验证目标 |
|---|---|---|
| `drop_on_write` | 收到 20112 立即断开连接 | 断线时 pending 失败、不自动重发创建 |
| `timeout_on(cmdid)` | 指定 cmdid 不回应 | 客户端超时显式失败、不自动重试 |
| `duplicate_20112` | 20112 响应发两次 | 重复响应不被当第二次写入结果 |
| `delay_on(cmdid)` | 响应延迟 | 同 cmdid 串行、不同 cmdid 并发 |
| `fail_on(cmdid, ret)` | 强制返回 ret | -5/-7 权限映射、-4 参数错误 |
| `fragment_on(cmdid)` | 响应分片发送 | 客户端分片重组 |
| `emit_change_events` | 写入/删除后推 20015 | 事件队列 |

### 10.4 独立进程启动

```bash
.venv/bin/python scripts/run_mock_ovital.py --port 0 --psk-file /tmp/mock-psk.txt \
    --fault drop-on-write --fault duplicate-20112 --ready-file /tmp/ovital-ready.json
```

- stdout 一行 JSON：`{"url":"ws://127.0.0.1:<port>","simulated":true,...}`；
- `--ready-file` 写同样内容（原子替换），供集成测试等待；
- SIGINT/SIGTERM 优雅关闭并关闭所有连接；
- PSK 优先级：`--psk-file` > `--psk` > `OVITAL_MCP_MOCK_PSK` > 内置测试值；
  **绝不读取真实奥维凭据**。

---

## 11. 包结构与公开 API（给工具层）

```
ovital_mcp/protocol/
  __init__.py     # 公开导出
  constants.py    # 命令号/对象类型/枚举/错误码/待实测清单（全部带手册出处）
  errors.py       # ProtocolDecodeError / OvitalRetError / OvitalTimeoutError / UnsupportedCommandError / raise_for_ret
  codec.py        # 外壳编解码、md5psk、脱敏、base64、分片、请求体构造
  models.py       # 数据类 + 响应解析（字段差异容错、轴序、raw 保留）
  objects.py      # build_shape_object / build_sign_object / build_write_tree / object_point_lonlat
  client.py       # OvitalClient（连接/登录/按 cmdid 串行/事件队列/断线失败）
  commands.py     # 高层命令封装 + probe_capabilities + compare_vertices
ovital_mcp/mock/
  world.py server.py faults.py fixtures/*.json
```

关键签名（与 `docs/architecture.md` §4 一致；带 `*` 的为 keyword-only 附加参数，不破坏契约调用）：

```python
OvitalClient(endpoint, psk, *, connect_timeout_s=5.0, request_timeout_s=10.0, logger=None)
client.connected / is_simulated / server_version / version / app_id
await client.connect() -> OvitalVersion        # ret=版本号；ret=0 → AuthError
await client.request(cmdid, msg=None, *, timeout=None) -> OvitalResponse
await client.next_event(cmdid=None, timeout=None) -> OvitalResponse
await client.close()

await commands.get_version(client) -> int
await commands.get_vip_level(client) -> dict
await commands.get_selected(client, flag=1) -> list[SelectedObject]
await commands.get_selected_tree(client, latlng_type=1) -> list[ObjNode]
await commands.get_objects(client, obj_id, latlng_type=1) -> list[ObjNode]
await commands.get_folder_children(client, folder_id, latlng_type=1) -> list[ObjNode]
await commands.find_objects(client, name, *, isrh_opt=5, latlng_type=1) -> list[ObjNode]
await commands.get_shape(client, obj_id, *, latlng_type=1, allow_gcj02_conversion=False) -> ShapeObject
await commands.get_shape_metrics(client, obj_id) -> ShapeMetrics
await commands.write_tree(client, tree) -> list[ObjectWriteResult]
await commands.write_objects(client, *, parent_id, name, children) -> list[ObjectWriteResult]
await commands.delete_objects(client, obj_ids, *, no_group=1) -> None
await commands.get_map_image(client, *, bbox_lonlat, max_level, max_pixel, coord_type=0) -> bytes
await commands.get_contours(client, *, bbox_lonlat, coord_type=0, **kwargs) -> dict
await commands.get_viewport(client) -> dict
await commands.get_screen_corners(client) -> dict
await commands.get_system_crs(client) -> dict
await commands.probe_capabilities(client) -> dict
commands.compare_vertices(local_lonlat, readback_lonlat, *, tolerance_deg=1e-7) -> dict
```

### 11.1 `probe_capabilities` 返回结构（供 `get_capabilities` 工具）

```jsonc
{
  "connected": true,
  "simulated": true,                       // 模拟服务器；真实奥维 false
  "verified_against_real_device": false,   // 恒 false（本机无实机）
  "evidence_level": "offline_simulated",   // 或 official_document_only
  "server_version": 817, "app_id": "42", "endpoint": "ws://...",
  "status": "ok",                          // 有单项失败时 "partial"
  "errors": [{"probe": "...", "code": "...", "message": "..."}],
  "commands": [{"probe": "20006_version", "ok": true}],
  "version": {"ok": true, "value": 817, "source": "20006（手册 2.3.24）"},
  "vip": {"ok": true, "omap_level": 9, "load_ok": 1, "tier": "unknown", "tier_note": "..."},
  "system_crs": {"ok": true, "coord_type": 4, "coord_type_meaning": "transverse_mercator",
                 "prj_type": 2, "prj_type_meaning": "Krasovsky_1940", "...": "..."},
  "selection": {"ok": true, "count": 0, "objects": [], "flag": 1},
  "viewport": {"status": "ok", "window_rect": {...}, "screen": {...},
               "pixel_ratio": {...}, "errors": [], "warnings": []},
  "permissions": {
    "map_image": {"ok": true, "evidence": "20301 调用成功（手册 2.1.1：需 SVIP）"},
    "contour":   {"ok": true, "evidence": "20303 调用成功；数据格式待实测"},
    "object_read_write": {"ok": true, "evidence": "...", "note": "探测不写地图"}
  },
  "limits": {"documented": {...}, "project_conservative": {...}, "unverified": [...]},
  "unverified": ["...待实测项..."]
}
```

单项失败只记录（`ok=false` + `errors[]`），不抛异常；只有连接不可用才抛
`OvitalConnectionError`。探测只发只读命令，**绝不写地图**（有测试断言）。

### 11.2 工具层注意事项

1. **写草稿**：`objects.build_write_tree(folder_id=0, folder_name=..., children=[shape,sign])`
   → `commands.write_tree`。返回结果里按 `type_id` 找 30/13/7；
   若没有 30（手册示例响应确实可能不含文件夹），用 `find_objects(folder_name)` 兜底。
2. **写后回读**：`get_shape` + `compare_vertices` 做往返证据；
   `get_shape_metrics` 与本地 GIS 量算对账（面积 ㎡、周长 km）。
3. **超时/断线**：不要盲目重发 20112；先 `get_objects`/`find_objects` 回读确认。
4. **两点图形**：`ShapeObject.two_point_special=True` → 拒绝当边界。
5. **等高线**：`geometry_format_unverified=True` → 不得声称拿到等高线几何。
6. **权限**：`OvitalPrereqError` → 明确报「需要 VIP/SVIP」，给本地替代方案。
7. **模拟 vs 真实**：`client.is_simulated` 与 `probe_capabilities()["simulated"]`
   必须写进 provenance；`verified_against_real_device` 恒 false。
8. **20107 无名称**：`find_objects()` 返回的 `ObjNode.name is None` 是手册行为，
   按名称确认对象必须再调 20101 读取（见 §7.8「实现要点」）。
9. **20112 替换语义**：`ObjTree`/子节点 `ObjID != 0` 且已存在时是**替换**而不是新增
   （手册 2.3.12.1）。`finalize_draft` 等「改名/改备注」操作依赖这一点：
   必须先 20101 读出完整 raw → 改 Name/Comment → 用 `build_write_tree(folder_id=父ID,
   children=[修改后的 raw 节点])` 写回，并在写前保存快照。真实设备的替换是否会
   丢弃未提交字段「待实测」，因此写后必须回读核对。
   mock 层已覆盖「ObjID 非 0 即替换」测试：
   `tests/test_mock_server.py::test_20112_replace_semantics_for_existing_obj_id`。

---

## 12. 测试与回放证据

### 12.1 测试命令

```bash
cd "/Users/huangsirsmacbookairm5/Public/code/project/omap mcp"
.venv/bin/python -m pytest tests/test_protocol_*.py tests/test_mock_*.py -q
```

覆盖矩阵（与任务书 §8 对应）：

| 要求 | 测试位置 |
|---|---|
| 登录成功/失败 | `test_protocol_client.py::test_login_*`、`test_mock_server.py::test_login_failure_*` |
| ret=版本号语义 | `test_protocol_client.py::test_ret_version_number_is_success_not_error`、`test_protocol_models.py::test_parse_version_*` |
| 未知 cmdid | `test_protocol_client.py::test_unknown_cmdid_maps_to_protocol_error_with_ret` |
| 同 cmdid 并发串行 | `test_protocol_client.py::test_same_cmdid_requests_are_serialized` |
| 不同 cmdid 并发 | `test_protocol_client.py::test_different_cmdids_run_concurrently` |
| 事件队列 | `test_protocol_client.py::test_unsolicited_event_goes_to_queue`、`test_next_event_*` |
| 断线 pending 失败 | `test_protocol_client.py::test_disconnect_fails_pending_and_never_auto_retries_write` |
| 重连不重放写 | 同上（断言服务端只收到 1 次 20112）+ `test_mock_server.py::test_fault_drop_on_write_closes_connection` |
| 写后回读坐标往返 ≤1e-7 | `test_protocol_commands.py::test_shape_write_read_roundtrip_within_tolerance` |
| 轴序 lat-first | 同上 + `test_protocol_models.py::test_parse_shape_*` |
| 四套枚举映射 | `test_protocol_models.py::test_four_coordinate_enums_are_distinct` |
| 20802 字段差异 | `test_protocol_models.py::test_parse_shape_metrics_*` |
| 四套/额外坐标枚举契约夹具 | `test_protocol_models.py::test_coordinate_enum_contract_fixture_matches_implementation` |
| PSK 不泄露 | `test_protocol_client.py::test_psk_never_appears_in_logs`、`test_login_failure_*` |
| 20112 替换语义（finalize_draft 依赖） | `test_mock_server.py::test_20112_replace_semantics_for_existing_obj_id`、`test_20112_repeated_identical_write_is_idempotent_on_existing_ids`、`test_protocol_commands.py::test_finalize_like_replace_updates_object_in_place` |
| 官方夹具可回放 | `test_protocol_codec.py::test_every_fixture_has_official_source`、`test_protocol_commands.py::test_official_fixtures_parse_through_models` |
| 模拟服务器自标识/水印/独立进程 | `test_mock_server.py::test_handshake_self_identifies_as_simulated`、`test_map_image_wire_level_has_watermark_and_metadata`、`test_standalone_script_starts_and_stops` |

### 12.2 端到端回放

```bash
.venv/bin/python scripts/replay_protocol.py
# 退出码 0 = 全部通过；证据默认写到 docs/protocol-samples/
```

回放内容：

1. 27 个官方夹具（手册/官方样例）走真实编解码/解析路径；
2. 模拟服务器闭环：登录 → 能力探测 → 写草稿（文件夹+图形+出口标签）→ 回读 →
   坐标往返（max_delta_deg=0）→ 20802 与独立 GIS 对账（area_diff≈4.5e-7 ㎡）→
   20301 模拟地图（水印校验）→ 20303 元数据 → 20016/20730/20212 视图 →
   20107 查找 → 20103 删除 → 回读确认为空；
3. 故障场景：drop_on_write / timeout_on / duplicate_20112 / 20301 -7 权限；
4. 两种 20112 响应形态（manual_flat / sdk_nested）各跑 2 轮，核心摘要 digest 一致
   → `repeatable: true`；
5. 20112 替换语义（`ObjID != 0` 且已存在 → 替换，不新增对象）由
   `tests/test_mock_server.py::test_20112_replace_semantics_for_existing_obj_id` 等
   测试固定，供 `finalize_draft` 依赖；真实设备替换行为仍「待实测」；
5. 输出：
   - `docs/protocol-samples/replay-evidence.json`（完整证据 + 待实测清单）
   - `docs/protocol-samples/samples/*.json`（脱敏请求/响应，每条带 `_source`）
   - `docs/protocol-samples/official-fixtures/*.json`（官方夹具）
   - `docs/protocol-samples/simulated-map-20301.png`（带水印模拟地图）
   - `docs/protocol-samples/README.md`

---

## 13. 待实测清单（Windows 奥维实机 + 真实账号）

> 这些项目**不能**由本机模拟/回放证明。每条给出「为什么不能定论」与「怎么验证」。

| # | 项 | 现状与风险 | 实机验证方法 |
|---|---|---|---|
| 1 | 20000 握手：真实奥维是否返回 `Simulated` | 预期不返回；`is_simulated` 依赖该字段 | 实机登录，打印原始响应；确认为 False |
| 2 | 登录失败 `ret=0 + msg.errmsg` 的确切文本/字段名 | 手册示例有 HTML 转义残留 | 用错误密钥登录，记录原始帧 |
| 3 | 20112 成功响应形态：扁平 `ObjItems[]` vs 嵌套 `ObjChildren` | 手册与官方样例冲突；本包两种都解析 | 实机写 1 文件夹+1 图形，保存原始响应 |
| 4 | 20112 响应是否包含新建文件夹（Type=30）的 ObjID | 手册示例响应缺 30 | 同上；若缺，验证 `find_objects` 兜底 |
| 5 | 20112 单次写入顶点数/字节数上限；`Fragment*` 分片是否被接受 | 手册未写；本包 4 MiB 保守阈值、不启用分片 | 递增顶点数写入，记录成功/失败阈值 |
| 6 | 20103 成功响应体；`NoGroup` 字段名与取值语义；`NoTroup` 是否被接受 | 手册只给失败表；官方样例拼写错误 | 删除草稿对象，保存原始响应 |
| 7 | 20802 字段名（带前缀 vs 无前缀）与面积/周长算法口径 | 本包两种都解析；算法未知 | 写已知多边形，对比本地投影面积 |
| 8 | 20301 地图级别字段名 `MaxLevel` vs `MapLevel`；响应扁平 vs `ReqMapDatas[]` | 手册表 vs 官方样例冲突；本包两个都发、两种都解析 | 实机取图，保存原始请求/响应 |
| 9 | 20303 等高线数据格式（几何/高程数组） | 手册**未定义**；本包只回元数据 | 实机取等高线，保存原始响应结构 |
| 10 | 20016 字段轴序/含义（手册示例数值错位） | 示例不可信；本包给 warnings | 实机在已知中心点取角点，核对 latc/lngc/latlt/lnglt/latrb/lngrb |
| 11 | 20016 `ReqCoordType=0`（根据奥维决定）的实际语义 | 未知 | 分别用 0/1/2 请求并比较 |
| 12 | 20110 分片响应字段与 base64 变体（SDK `Base64ToStr` 映射与 URL-safe 相反） | 手册未记载；歧义可能导致分片解坏 | 选中大量对象，启用 FragmentMaxSize，抓原始帧 |
| 13 | Shape 多区域/孔洞表示（`OuterRgnIdx` + `Mtp` + `Latlng` 规则） | 手册未说明；本版不写孔洞 | 实机读一个带孔图形，分析 `AreaCnt>1` 时字段 |
| 14 | 两个点图形的圆/扇形/矩形语义（起止角、点顺序） | 手册只给一句话 | 实机分别画圆/扇形/矩形，读回字段 |
| 15 | 颜色字段编码（`LineClr`/`AreaClr` 是 0xRRGGBB / 0xAARRGGBB / 0xBBGGRR） | 手册只给示例整数；本包原样透传 | 写不同颜色值，在奥维里看实际颜色 |
| 16 | 20104/20101 的 ObjID 类型是否稳定（int vs string） | 手册表格自相矛盾 | 实机抓原始帧确认 |
| 17 | 20107 是否真的不返回名称（本包按手册只取 ID/类型） | 手册明确只给 ID/类型 | 实机抓响应确认 |
| 18 | OmapLevel 数值与 VIP/SVIP 名称映射 | 手册未给；本包不猜 | 用不同会员账号读 20501，并对比 20301/20303 的 ret |
| 19 | 20302 地图图片文件命令（本版未实现） | 手册 2.3.30 字段与 20301 不同 | 需要时实现并实机验证 |
| 20 | 20112 官方样例 `BuildTree` 根容器语义（`Name=""`、`ParentID`=父文件夹） | 与手册示例不同；本包按手册示例 | 实机用两种写法各写一次，看树结构 |

---

## 14. 已知限制

- **真实联机未验证**：本机无 Windows 奥维，无真实账号；`verified_against_real_device=false`。
- 20302 地图图片文件、20221 设置坐标系、20106 设为选中、20108 移动对象、
  20801 轨迹量算等未实现（工具层当前不需要）。
- 模拟器不建模轨迹/线路/自定义地图/工具栏/弹窗；20303 不返回几何。
- 模拟器 20101 找不到对象返回空 `ObjItems`（手册未规定，属模拟器选择）。
- 模拟器不支持 `LatlngType=3/4`、20301 `CoordType=2`（需要投影参数，模拟器未实现）。
- 写入不支持孔洞/多区域；两点图形面积量算未实现（模拟器返回 0）。
- 分片写入未启用；分片读取仅客户端自动重组。
- base64 `-`/`_` 歧义无法完全消解，依赖后续 JSON 解析显式失败。

---

## 15. 参考 URL

- 奥维 WebSocket V2.7 手册：<https://www.gpsov.com/appdoc/index.php?id=32>
- 官方样例 SDK 下载：<https://download.ovital.com/pub/api/omapInterfaceWeb_latest.zip>
- 奥维第三方接口介绍：<https://www.ovital.com/openapi/>
- 项目内实施契约：`docs/architecture.md` §3（坐标）/ §4（协议包）
- 项目内实施任务书：`DSH_实施任务书.md` A/B 节
- 可行性方案：`OMAP_MCP_可行性与实施方案.md` 第 2/6/7 节
