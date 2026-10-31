"""奥维 WebSocket V2.7 协议常量（全部来自官方手册，禁止编造）。

官方来源
========
- 手册全文：https://www.gpsov.com/appdoc/index.php?id=32 （V2.7）
  本仓库离线副本：``.research/ovital-scratch/manual-32.md``（下文用「手册 X.Y.Z」引用章节）
- 官方样例 SDK：``.research/ovital-scratch/zip-32/omapInterfaceWeb_latest/omapInterfaceWeb/js/``
  - ``omapinterface.js``：命令号常量、连接/登录、请求发送与响应分发
  - ``omapobj.js``：``BuildShape`` / ``BuildSign`` / ``BuildTree`` 等消息构造
- 官方样例下载页：https://download.ovital.com/pub/api/omapInterfaceWeb_latest.zip

证据分级（见 docs/architecture.md §9）
-------------------------------------
- ``official_document``：手册明确写出的命令号/字段/枚举。
- ``official_sample``：官方样例 SDK 里出现、但手册未写（或写法不同）的行为。
- ``offline_simulated``：本机模拟服务器回放验证。
- ``real_device_pending``：必须在 Windows 奥维实机验证（``待实测``）。

**本模块不包含任何「实测得出」的数值**；所有常量都能在手册或官方样例中定位。
"""

from __future__ import annotations

from enum import Enum
from typing import Final

from ..coordinates import CoordSemantic

# ---------------------------------------------------------------------------
# 官方来源
# ---------------------------------------------------------------------------

MANUAL_URL: Final = "https://www.gpsov.com/appdoc/index.php?id=32"
SDK_DOWNLOAD_URL: Final = "https://download.ovital.com/pub/api/omapInterfaceWeb_latest.zip"
OPENAPI_URL: Final = "https://www.ovital.com/openapi/"
MANUAL_VERSION: Final = "V2.7"

#: 官方样例要求的 WebSocket 子协议名（``new WebSocket("ws://host:port", "ws")``，
#: 见 omapinterface.js 连接代码）。真实奥维是否强制校验该子协议「待实测」。
SUBPROTOCOL: Final = "ws"

#: 手册 2.1.2.1：端口由用户在奥维「系统→系统设置→高级→第三方接口」自定义，
#: 7699 只是官方示例，**不得**作为程序默认值（配置必须显式）。
EXAMPLE_PORT: Final = 7699

#: 单帧默认上限。20301/20302 返回 base64 图片，可能远大于 websockets 默认 1 MiB，
#: 因此客户端需要放宽；具体上限取决于奥维版本与 MaxPixel（待实测）。
DEFAULT_MAX_MESSAGE_BYTES: Final = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# 命令号（手册 2.2.1「命令号」表；官方样例 omapinterface.js 顶部常量一致）
# ---------------------------------------------------------------------------

CMD_LOGIN: Final = 20000  # 登录（手册 2.3.1）
CMD_GET_LATLNG: Final = 20001  # 获取经纬度（2.3.4）
CMD_SET_TEMP_SIGN: Final = 20002  # 设置临时标签（2.3.5）
CMD_GEOCODE: Final = 20003  # 地址解析（2.3.8）
CMD_SET_PICTURES: Final = 20004  # 设置照片列表（2.3.7）
CMD_TIME_TO_LATLNG: Final = 20005  # 根据时间获取轨迹点经纬度（2.3.9）
CMD_GET_VERSION: Final = 20006  # 获取奥维版本（2.3.24）
CMD_OMAP_CLOSED: Final = 20008  # 奥维被关闭（事件，2.3.2）
CMD_SAVE_CLICKED: Final = 20009  # 奥维保存按钮被点击（事件，2.3.3）
CMD_CLEAN_TEMP_OBJ: Final = 20010  # 清除临时对象（2.3.15）
CMD_SET_MAP_LOCATION: Final = 20011  # 地图跳转/设置地图位置（2.3.6）
CMD_GET_MAP_INFO: Final = 20012  # 当前屏幕中心点坐标/类型/级别/地图类型（2.3.23）
CMD_FOLLOW_MAP_INFO: Final = 20013  # 自动返回奥维中心点信息（2.3.57）
CMD_MONITOR_OBJ_CHANGE: Final = 20014  # 监测奥维对象改变（2.3.49）
CMD_OBJ_CHANGED: Final = 20015  # 奥维对象变动信息（事件，2.3.49）
CMD_GET_SCREEN_COORDS: Final = 20016  # 获取屏幕中心点/左上角/右下角坐标（2.3.58）
CMD_GET_FAV_LIST: Final = 20100  # 获取收藏夹列表（2.3.10）
CMD_GET_OBJECT: Final = 20101  # 获取收藏夹对象（2.3.11）
CMD_DELETE_OBJECT: Final = 20103  # 删除收藏夹对象（2.3.13）
CMD_GET_SELECTED: Final = 20104  # 获取收藏夹选中对象（2.3.17）
CMD_SET_SELECTED_BY_ID: Final = 20106  # 根据ID将对象设为选中状态（2.3.59）
CMD_SEARCH_OBJECT: Final = 20107  # 查找奥维对象（2.3.22）
CMD_MOVE_OBJECT: Final = 20108  # 移动奥维对象（2.3.60）
CMD_GET_SELECTED_TREE: Final = 20110  # 获取选中对象树形结构（2.3.18）
CMD_WRITE_OBJECT_TREE: Final = 20112  # 设置/添加收藏夹对象（2.3.12）
CMD_SET_SYS_CRS: Final = 20221  # 设置系统坐标系参数（2.3.50）
CMD_GET_SYS_CRS: Final = 20222  # 获取系统坐标系参数（2.3.51）
CMD_GET_MAP_DATA: Final = 20301  # 获取地图图片数据（2.3.29）
CMD_GET_MAP_FILE: Final = 20302  # 获取地图图片文件（2.3.30）
CMD_GET_CONTOUR: Final = 20303  # 获取等高线（2.3.31）
CMD_GET_VIP_LEVEL: Final = 20501  # 获取VIP等级（2.3.19）
CMD_GET_SVIP_TRIAL: Final = 20503  # 判断是否为SVIP/试用SVIP（2.3.20，命令号表 20503）
CMD_GET_PIXEL_RATIO: Final = 20212  # 获取当前点在地图下像素比例（2.3.28）
CMD_GET_OMAP_WND_RECT: Final = 20730  # 获取奥维窗体可显示区域（2.3.52）
CMD_GET_SHAPE_METRICS: Final = 20802  # 获取图形对象基本信息（2.3.48）
CMD_GET_TRACK_METRICS: Final = 20801  # 获取轨迹对象基本信息（2.3.47）
CMD_GET_WS_CONFIG: Final = 20850  # 获取奥维 websocket 配置（2.3.74）
CMD_GET_PLUGIN_VERSION: Final = 20204  # 获取插件版本（2.3.63）


# ---------------------------------------------------------------------------
# 事件命令号
# ---------------------------------------------------------------------------

#: 手册明确是「奥维主动推送」的命令号。客户端在没有 pending 请求时收到这些
#: cmdid 的消息，一律进事件队列（docs/architecture.md §4.2）。
#: - 20008 奥维被关闭（2.3.2）
#: - 20009 保存按钮被点击（2.3.3）
#: - 20015 对象变动信息（2.3.49，需先用 20014 注册监听）
EVENT_CMDS: Final[frozenset[int]] = frozenset(
    {
        CMD_OMAP_CLOSED,
        CMD_SAVE_CLICKED,
        CMD_OBJ_CHANGED,
    }
)

#: 既能作为请求响应、也能作为订阅推送的命令号（例如 20012 地图中心点）。
#: 客户端统一按「有 pending 请求 → 响应；否则 → 事件队列」处理，此集合只用于文档
#: 与 ``next_event`` 的默认过滤提示。
DUAL_USE_CMDS: Final[frozenset[int]] = frozenset({CMD_GET_MAP_INFO, CMD_FOLLOW_MAP_INFO})


# ---------------------------------------------------------------------------
# 对象类型（手册 2.2.5「奥维对象类型」）
# ---------------------------------------------------------------------------

OBJ_TYPE_SIGN: Final = 7  # 标签
OBJ_TYPE_TRACK: Final = 8  # 轨迹
OBJ_TYPE_DIRECTION: Final = 11  # 导航线路
OBJ_TYPE_SHAPE: Final = 13  # 图形
OBJ_TYPE_GROUP: Final = 30  # 文件夹

OBJ_TYPE_NAMES: Final[dict[int, str]] = {
    OBJ_TYPE_SIGN: "sign",
    OBJ_TYPE_TRACK: "track",
    OBJ_TYPE_DIRECTION: "direction",
    OBJ_TYPE_SHAPE: "shape",
    OBJ_TYPE_GROUP: "group",
}

#: 与 ``OBJ_TYPE_NAMES`` 同一内容（工具层约定的公开名字，手册 2.2.5）。
OBJECT_TYPE_NAMES: Final[dict[int, str]] = dict(OBJ_TYPE_NAMES)

#: 手册 2.2.6：临时对象节点 ID（0=根节点，202=临时对象分组节点）。
OBJ_GROUP_ROOT_ID: Final = 0
OBJ_GROUP_TEMP_ID: Final = 202

#: 手册 2.2.3：20104 请求的 iFlag 取值。
SEL_FLAG_GROUP_JOIN: Final = 1  # 选中对象，选中的文件夹自动包含所有子对象
SEL_FLAG_SEL_ONLY: Final = 2  # 选中对象，选中的文件夹不自动包括非选中子对象
SEL_FLAG_NO_GROUP: Final = 3  # 如果选中的是文件夹，则排除文件夹本身去选中子结点

SEL_FLAG_NAMES: Final[dict[int, str]] = {
    SEL_FLAG_GROUP_JOIN: "group_join",
    SEL_FLAG_SEL_ONLY: "selected_only",
    SEL_FLAG_NO_GROUP: "no_group",
}


# ---------------------------------------------------------------------------
# 坐标枚举（四套互不相同；解析函数在 ovital_mcp.coordinates，唯一实现）
# ---------------------------------------------------------------------------
#
# 本文件只引用枚举「数值 -> 语义」的映射并注明出处，解析逻辑一律调用
# ``ovital_mcp.coordinates.semantic_for_*``，禁止在协议层另写一套。

#: 手册 2.2.2 顶层 ``CoordType``：0=CGCS2000, 1=GCJ-02, 2=当前坐标系。
TOP_COORD_TYPE_DOC = "手册 2.2.2 CoordType"

#: 手册 2.2.2 / 2.3.11 / 2.3.18 ``LatlngType``：
#: 0=系统设置的坐标系, 1=CGCS2000, 2=GCJ-02, 3=平面坐标系, 4=墨卡托全球投影 xy。
LATLNG_TYPE_DOC = "手册 2.2.2 LatlngType"

#: 手册 2.4.1 ``OmapLatlng.CoordType``：**0=GCJ-02, 1=CGCS2000, 2=平面坐标**
#: （与顶层 CoordType 的 0/1 含义相反，是手册中最容易踩的陷阱）。
OMAP_LATLNG_COORD_TYPE_DOC = "手册 2.4.1 OmapLatlng.CoordType"

#: 手册 2.4.8 ``Shape.ObjectDetail.Gcj02``：0=CGCS2000, 1=GCJ-02, 2=平面坐标系。
SHAPE_GCJ02_DOC = "手册 2.4.8 Shape.ObjectDetail.Gcj02"

#: 手册 2.3.58 ``20016.ReqCoordType``：**0=根据奥维决定, 1=真实经纬度, 2=GCJ-02**。
#: 这是第 5 套枚举：0 在这里表示「系统决定」，不是 CGCS2000！``coordinates.py``
#: 没有这套映射（它只覆盖架构契约列出的四套），因此在协议层单独解析。
SCREEN_COORD_REQ_TYPE: Final[dict[int, CoordSemantic]] = {
    0: CoordSemantic.SYSTEM,
    1: CoordSemantic.CGCS2000,
    2: CoordSemantic.GCJ02,
}
SCREEN_COORD_REQ_TYPE_DOC = "手册 2.3.58 20016 ReqCoordType"

#: 手册 2.3.51 ``20222.CoordType``：2=经纬度, 4=横轴墨卡托。
#: 注意这是第 6 套枚举，与顶层 CoordType（0/1/2）完全不同。
SYSTEM_CRS_COORD_TYPE: Final[dict[int, str]] = {
    2: "geographic",  # 经纬度
    4: "transverse_mercator",  # 横轴墨卡托
}
SYSTEM_CRS_COORD_TYPE_DOC = "手册 2.3.51 20222.CoordType"

#: 手册 2.3.50/2.3.51 ``iPrjType``。
SYSTEM_CRS_PRJ_TYPE: Final[dict[int, str]] = {
    0: "WGS84",
    1: "Xian80",
    2: "Krasovsky_1940",
    3: "UTM",
    4: "CGCS2000",
    5: "CustomType",
}
SYSTEM_CRS_PRJ_TYPE_DOC = "手册 2.3.50/2.3.51 iPrjType"

#: 手册 2.4.8 ``Shape.ObjectDetail`` 中「两个点表示圆/扇形/矩形」的说明：
#: 「当图形为两个点时，通过起始角度与终止角度表示圆、扇形、矩形」。
#: 因此 ``len(Latlng)==4``（2 个点）的图形**不是**普通多边形，工具层不得当作
#: 集水区边界处理。孔洞/多区域表示手册未明确说明（待实测）。
SHAPE_TWO_POINT_NOTE = "手册 2.4.8：图形为两个点时用起始/终止角度表示圆、扇形、矩形"


# ---------------------------------------------------------------------------
# 图形量算（手册 2.3.48）
# ---------------------------------------------------------------------------

#: 手册 2.3.48 表：``dPerimeter`` 单位 km，``dAreaSize`` 单位 ㎡；官方样例
#: omapinterface.js 读取的字段名却是 ``Perimeter`` / ``AreaSize``（无前缀），
#: 且示例响应用无前缀字段。解析时两种都要接受（见 codec.FIELD_ALIASES）。
SHAPE_METRICS_UNITS = {"area": "m2", "perimeter": "km"}
SHAPE_METRICS_FIELD_DIFF_DOC = "手册 2.3.48 表 vs 示例/官方样例字段名差异"


# ---------------------------------------------------------------------------
# 请求/响应外壳
# ---------------------------------------------------------------------------

#: 手册 2.1.3.6/2.3.1.1：请求外壳 ``{cmdid, ret, msg}``，且「请求一律为 ret=0」。
REQUEST_ENVELOPE_KEYS: Final = ("cmdid", "ret", "msg")
REQUEST_RET: Final = 0

#: 登录（手册 2.3.1.1）：``md5psk`` = MD5(预共享密钥) 转大写后取前 16 个字符。
#: 官方样例 omapinterface.js：``$.md5(key).toUpperCase().substr(0,16)``。
MD5PSK_FIELD: Final = "md5psk"
MD5PSK_LENGTH: Final = 16
LOGIN_APPID_FIELD: Final = "AppId"
LOGIN_ERRMSG_FIELD: Final = "errmsg"

#: 模拟服务器自标识字段（真实奥维不会返回，属于本项目模拟协议扩展）。
#: 任务书要求握手响应 msg.Simulated=True；客户端据此把 provenance 标 simulated。
SIMULATED_FIELD: Final = "Simulated"


# ---------------------------------------------------------------------------
# 分段传输（官方样例有、手册未写 → official_sample + 待实测）
# ---------------------------------------------------------------------------

#: omapinterface.js：20110 请求可带 ``Fragment:1`` + ``FragmentMaxSize``，
#: 响应分多帧返回 ``FragmentNum/FragmentIndex/FragmentData``（FragmentData 是
#: base64 编码的 JSON 文本分片）。20112 大对象请求也会按
#: ``FragmentIndex/FragmentData/FragmentNum`` 分片发送。
#: 手册 2.3.12/2.3.18 均未记载这些字段 → 实现保留，但默认不启用，标「待实测」。
FRAGMENT_CMDS: Final[frozenset[int]] = frozenset({CMD_GET_SELECTED_TREE, CMD_WRITE_OBJECT_TREE})
FRAGMENT_NUM_FIELD: Final = "FragmentNum"
FRAGMENT_INDEX_FIELD: Final = "FragmentIndex"
FRAGMENT_DATA_FIELD: Final = "FragmentData"
FRAGMENT_FLAG_FIELD: Final = "Fragment"
FRAGMENT_MAX_SIZE_FIELD: Final = "FragmentMaxSize"

#: 官方样例 Base64ToStr 的映射是 ``'-'->'/'; '_'->'+'``（与标准 URL-safe
#: base64 相反）。解码时两种映射都要尝试，否则真实设备的分片可能解不出来。
#: 证据：omapinterface.js ``Base64ToStr``；待实测。
FRAGMENT_B64_NOTE = "官方样例 Base64ToStr 使用 '-'→'/'、'_'→'+'（与 URL-safe 相反），待实测"


# ---------------------------------------------------------------------------
# 20301 / 20303 请求字段差异（手册 vs 官方样例）
# ---------------------------------------------------------------------------

#: 手册 2.3.29 请求表字段是 ``MaxLevel``；官方样例 omapobj.js ``BuildReqMapData``
#: 实际发送 ``MapLevel``（响应示例又用 ``MaxLevel``）。为兼容两种解释，请求里
#: **同时**发送两个字段（多一个未知字段不会改变语义，且两处都有官方出处），
#: 真实设备接受哪个「待实测」。
MAP_LEVEL_FIELDS: Final = ("MaxLevel", "MapLevel")

#: 手册 2.3.31 响应表把海拔字段写成 ``EvevValue``（拼写错误，示例也是），
#: 而请求字段是 ``ElevValue``。解析时两种都接受。
CONTOUR_ELEV_FIELDS: Final = ("EvevValue", "ElevValue")

#: 手册 2.3.30/2.3.31 响应里 ``Ret`` 是 msg 内的状态字段（不是外层 ret）：
#: 20302: 3=取图成功, -161=取图失败；20303: 0=成功, -1..-8 各有含义。
#: 详见手册 2.3.30.2 / 2.3.31.2。
MAP_FILE_RET_OK: Final = 3
CONTOUR_RET_MESSAGES: Final[dict[int, str]] = {
    0: "获取成功",
    -1: "奥维VIP级别不够",
    -2: "需要SVIP",
    -3: "坐标系错误",
    -4: "参数错误",
    -5: "内存异常",
    -6: "未知错误",
    -7: "与等高线请求相关",
    -8: "内存不足而终止",
}


# ---------------------------------------------------------------------------
# 20107 查找对象（手册 2.3.22）
# ---------------------------------------------------------------------------

#: 手册 2.3.22.1 ``SrhOpt``（同 2.2.4 iSrhOpt）。
SEARCH_OPT_NAMES: Final[dict[int, str]] = {
    0: "name_fuzzy",
    1: "name_prefix",
    2: "comment_fuzzy",
    3: "comment_prefix",
    4: "duplicate",
    5: "name_exact",
    6: "comment_exact",
    11: "sign_style",
    12: "sign_icon_id",
    21: "track_line_type",
    22: "track_style",
    23: "track_class",
}

#: 手册 2.3.22 请求表与示例字段名不一致：表写 ``idParent`` / ``idShapeSelect``，
#: 示例写 ``ParentID`` / ``ShapeSelect``。两套都发（见 docs/protocol-notes.md）。
SEARCH_PARENT_FIELDS: Final = ("ParentID", "idParent")
SEARCH_SHAPE_SELECT_FIELDS: Final = ("ShapeSelect", "idShapeSelect")


# ---------------------------------------------------------------------------
# 默认（文档级）限制与待实测项
# ---------------------------------------------------------------------------

#: 手册**没有**给出 20112 单次写入的顶点数/字节数上限，也没有说明如何表示
#: 多区域与孔洞。因此：
#: - ``write_objects`` 默认不启用分片，超过 ``DEFAULT_WRITE_MAX_BYTES`` 时显式报错；
#: - 孔洞/多区域写入选为「待实测」，v1 不写。
DEFAULT_WRITE_MAX_BYTES: Final = 4 * 1024 * 1024
WRITE_LIMIT_NOTE = "手册未给 20112 顶点数/字节数上限，4 MiB 为本项目保守阈值（待实测）"

#: 必须在 Windows 奥维实机核验的协议项（docs/protocol-notes.md「待实测」同步维护）。
UNVERIFIED_ITEMS: Final[tuple[str, ...]] = (
    "20000 握手：真实奥维是否返回 Simulated 字段（预期不返回）；AppId 类型",
    "登录失败时 ret=0 + msg.errmsg 的确切文本与字段名",
    "20112 写入成功响应形态：手册示例是扁平 msg.ObjItems[]，官方样例按 "
    "msg.ObjItems[0].Object.ObjectDetail.ObjChildren 读取，二者需实测确认",
    "20112 单次写入的顶点数/字节数上限，以及官方样例的分片字段"
    "（FragmentIndex/FragmentData/FragmentNum）是否被服务端接受",
    "20103 删除成功的响应体（手册只写了失败形态）与 NoGroup 字段名"
    "（官方样例 BuildObj 误写成 NoTroup）",
    "20802 响应字段名：手册表 idObj/iPointCnt/iAreaCnt/dPerimeter/dAreaSize "
    "vs 示例与官方样例 ObjID/PointCnt/AreaCnt/Perimeter/AreaSize",
    "20301 请求地图级别字段名：手册表 MaxLevel vs 官方样例 BuildReqMapData 发送 MapLevel",
    "20301 响应是否为扁平字段（示例）还是 ReqMapDatas 数组（表）",
    "20303 等高线数据的具体数据格式（手册只给元数据字段，未给几何/高程数组结构）",
    "20016 返回示例的经纬度字段值明显错位（latlt=30.59 / lnglt=27.15 等），"
    "需实测确认 latlt/lnglt/latrb/lngrb 的轴序与含义",
    "20016 ReqCoordType=0 时「根据奥维决定」的实际语义",
    "20110 分片响应字段与 base64 变体（官方样例 Base64ToStr 映射与 URL-safe 相反）",
    "Shape 多区域/孔洞的表示（OuterRgnIdx + Mtp + Latlng 的确切规则）",
    "两个点图形的圆/扇形/矩形语义（起始角/终止角、点顺序）",
    "颜色字段编码（LineClr/AreaClr 是 0xRRGGBB、0xAARRGGBB 还是 0xBBGGRR）",
    "20104/20101 响应 ObjID 类型不一致（int vs string）是否稳定",
    "20107 查找对象只返回 ObjID/ObjType，不返回名称；名称需再查 20101",
    "会员等级数值（OmapLevel）与 SVIP/VIP5 权限的对应关系",
    "20302 地图图片文件命令与 20301 的差异（本版未实现 20302）",
)


# ---------------------------------------------------------------------------
# 命令号 -> 名称（手册 2.2.1「命令号」表；工具层展示/日志用）
# ---------------------------------------------------------------------------

CMD_NAMES: Final[dict[int, str]] = {
    CMD_LOGIN: "登录",
    CMD_GET_LATLNG: "获取经纬度",
    CMD_SET_TEMP_SIGN: "设置临时标签",
    CMD_GEOCODE: "地址解析",
    CMD_SET_PICTURES: "设置照片列表",
    CMD_TIME_TO_LATLNG: "根据时间获取轨迹点经纬度",
    CMD_GET_VERSION: "获取奥维版本",
    CMD_OMAP_CLOSED: "奥维被关闭",
    CMD_SAVE_CLICKED: "奥维保存按钮被点击",
    CMD_CLEAN_TEMP_OBJ: "清除临时对象",
    CMD_SET_MAP_LOCATION: "地图跳转",
    CMD_GET_MAP_INFO: "当前屏幕中心点坐标/类型/级别/地图类型",
    CMD_FOLLOW_MAP_INFO: "自动返回奥维中心点信息",
    CMD_MONITOR_OBJ_CHANGE: "监测奥维对象改变",
    CMD_OBJ_CHANGED: "奥维对象变动信息",
    CMD_GET_SCREEN_COORDS: "获取屏幕中心点/左上角/右下角坐标",
    CMD_GET_FAV_LIST: "获取收藏夹列表",
    CMD_GET_OBJECT: "获取收藏夹对象",
    CMD_DELETE_OBJECT: "删除收藏夹对象",
    CMD_GET_SELECTED: "获取收藏夹选中对象",
    CMD_SET_SELECTED_BY_ID: "根据ID将对象设为选中状态",
    CMD_SEARCH_OBJECT: "查找奥维对象",
    CMD_MOVE_OBJECT: "移动奥维对象",
    CMD_GET_SELECTED_TREE: "获取收藏夹选中对象树形结构",
    CMD_WRITE_OBJECT_TREE: "设置收藏夹对象",
    CMD_SET_SYS_CRS: "设置系统坐标系参数",
    CMD_GET_SYS_CRS: "获取系统坐标系参数",
    CMD_GET_MAP_DATA: "获取地图图片数据",
    CMD_GET_MAP_FILE: "获取地图图片文件",
    CMD_GET_CONTOUR: "获取地图等高线",
    CMD_GET_VIP_LEVEL: "获取VIP等级",
    CMD_GET_SVIP_TRIAL: "判断是否为SVIP/试用SVIP",
    CMD_GET_PIXEL_RATIO: "获取当前点在地图下像素比例",
    CMD_GET_OMAP_WND_RECT: "获取奥维窗体可显示区域",
    CMD_GET_SHAPE_METRICS: "获取图形对象基本信息",
    CMD_GET_TRACK_METRICS: "获取轨迹对象基本信息",
    CMD_GET_WS_CONFIG: "获取奥维websocket配置",
    CMD_GET_PLUGIN_VERSION: "获取插件版本",
}


#: 手册 2.2.7「奥维错误ID」的符号名（工具层展示用）。
RET_ERROR_NAMES: Final[dict[int, str]] = {
    -1: "no_omap_window",
    -2: "register_failed",
    -3: "omap_version_too_low",
    -4: "bad_parameter",
    -5: "not_vip",
    -6: "message_handle_failed",
    -7: "not_svip",
}


# ---------------------------------------------------------------------------
# 会员等级（手册 2.1.1 / 2.3.19 / 2.3.20）
# ---------------------------------------------------------------------------
#
# 手册 2.1.1：获取地图图片数据、获取等高线数据功能需要 SVIP 及以上；其他功能
# 至少需要 VIP。
# 手册 2.3.19：20501 返回 OmapLevel（int，账号VIP等级）+ LoadOK（是否正常使用）。
# 手册 2.3.20：20503 返回 ReqVip5Result（1=之前已经请求过，2=可以发送请求）。
#
# **手册没有给出 OmapLevel 数值与 VIP/SVIP 名称的对应表**，因此这里不编造
# 阈值；真实权限一律以命令返回 ret=-5/-7 为准（见 errors.raise_for_ret）。


class VipTier(str, Enum):
    """会员层级（手册 2.1.1 的两种说法）。"""

    VIP = "VIP"
    SVIP = "SVIP"
    UNKNOWN = "unknown"


#: 手册 2.1.1：各功能要求的最低会员层级。
FEATURE_MIN_TIER: Final[dict[str, VipTier]] = {
    "map_image": VipTier.SVIP,  # 20301/20302 获取地图图片数据/文件
    "contour": VipTier.SVIP,  # 20303 获取等高线
    "object_read_write": VipTier.VIP,  # 20101/20104/20110/20112/20103/20802 等
    "other": VipTier.VIP,
}

#: 手册 2.3.20：20503 返回的 ReqVip5Result。
SVIP_TRIAL_RESULT_NAMES: Final[dict[int, str]] = {
    1: "之前已经请求过（试用机会已用完）",
    2: "可以发送请求",
}

VIP_LEVEL_FIELDS: Final[tuple[str, str]] = ("OmapLevel", "LoadOK")
VIP_LEVEL_NOTE = (
    "手册 2.3.19 只给 OmapLevel 数值，未给 VIP/SVIP 名称映射；"
    "权限以命令返回 ret=-5（需VIP）/-7（需SVIP）实测为准"
)


def tier_for_omap_level(omap_level: int | None) -> VipTier:
    """把 20501 的 ``OmapLevel`` 映射为会员层级。

    手册 2.3.19 没有给出数值→名称的对应关系，因此这里**不猜测**：一律返回
    ``VipTier.UNKNOWN``，并建议上层用具体命令的 ret（-5/-7）判定权限。
    该函数存在的意义是固定「已知边界」：不要因为 OmapLevel=9 就断言是 SVIP。
    """

    del omap_level  # 明确不参与判断，避免被误用为阈值
    return VipTier.UNKNOWN


def feature_min_tier(feature: str) -> VipTier:
    """返回手册 2.1.1 规定的最低会员层级；未知功能按 other（VIP）。"""

    return FEATURE_MIN_TIER.get(feature, FEATURE_MIN_TIER["other"])
