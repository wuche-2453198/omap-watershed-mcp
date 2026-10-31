"""协议编解码：消息外壳、登录摘要、字段别名、base64 与分片。

设计要点
========
1. **协议没有请求 ID**，响应只能按 ``cmdid`` 关联（docs/architecture.md §4.2）。
   本模块只负责单帧编解码；串行化/事件队列在 ``client.py``。
2. 登录：``md5(psk).upper()[:16]``（手册 2.3.1.1 + 官方样例 omapinterface.js）。
   本模块提供 ``md5psk()``，但**永不**把结果写日志；``redact_msg()`` 供日志使用。
3. 字段名容错：手册表格与示例/官方样例存在差异（见 ``constants`` 与
   ``docs/protocol-notes.md``），本模块集中放别名表，避免各处硬编码。
4. 分片（Fragment*）只有官方样例证据、手册未记载 → 默认不启用，标「待实测」。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from . import constants as C
from .errors import ProtocolDecodeError
from .models import OvitalResponse

__all__ = [
    "json_dumps",
    "encode_request",
    "decode_message",
    "md5psk",
    "redact_msg",
    "redact_text",
    "safe_msg_summary",
    "b64encode",
    "b64decode_tolerant",
    "b64decode_variants",
    "is_fragment_message",
    "split_text",
    "encode_fragment_requests",
    "FragmentAccumulator",
    "build_search_request",
    "build_delete_request",
    "build_map_data_request",
    "build_contour_request",
    "build_obj_list_request",
    "build_obj_request",
    "build_selected_request",
    "build_selected_tree_request",
    "SENSITIVE_KEYS",
]

#: 需要在日志/异常里脱敏的字段名（大小写不敏感比较）。
SENSITIVE_KEYS: frozenset[str] = frozenset({"md5psk", "psk", "password", "passwd", "key"})

#: 手册 2.3.1.1：md5psk 是 MD5 大写后前 16 个字符。
_MD5PSK_RE = re.compile(r"\b[0-9A-F]{16,32}\b")


def json_dumps(obj: Any) -> str:
    """序列化为 WebSocket 文本帧。

    官方样例用 ``JSON.stringify``：紧凑、UTF-8 原文（不转义中文）。
    """

    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def encode_request(cmdid: int, msg: Mapping[str, Any] | None = None) -> str:
    """构造请求帧。

    手册 2.1.3.6 / 2.3.1.1：``{"cmdid":<int>,"ret":0,"msg":{...}}``，
    且「请求一律为 ret=0」。
    """

    if not isinstance(cmdid, int) or isinstance(cmdid, bool):
        raise ProtocolDecodeError(f"cmdid 必须是整数，收到 {cmdid!r}")
    if msg is not None and not isinstance(msg, Mapping):
        raise ProtocolDecodeError(f"msg 必须是 JSON 对象，收到 {type(msg).__name__}")
    return json_dumps({"cmdid": cmdid, "ret": C.REQUEST_RET, "msg": dict(msg or {})})


def decode_message(raw: str | bytes) -> OvitalResponse:
    """解析服务端帧。

    官方响应外壳：``{"cmdid":int, "ret":int?, "msg":{...}}``。成功响应通常省略
    ``ret``（手册各命令示例），失败响应带 ``ret<0``；20000/20006/20104/20110
    成功时 ``ret`` 是版本号（>0）。
    """

    if isinstance(raw, (bytes, bytearray)):
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolDecodeError(f"帧不是合法 UTF-8：{exc}") from exc
    elif isinstance(raw, str):
        text = raw
    else:
        raise ProtocolDecodeError(f"帧类型不支持：{type(raw).__name__}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        preview = text[:200] if len(text) <= 200 else text[:200] + "…"
        raise ProtocolDecodeError(
            f"帧不是合法 JSON：{exc}",
            details={"preview": preview, "length": len(text)},
        ) from exc

    if not isinstance(data, dict):
        raise ProtocolDecodeError("帧顶层必须是 JSON 对象", raw=data)
    if "cmdid" not in data:
        raise ProtocolDecodeError("帧缺少 cmdid", raw=data)
    try:
        cmdid = int(data["cmdid"])
    except (TypeError, ValueError) as exc:
        raise ProtocolDecodeError(f"cmdid 不是整数：{data.get('cmdid')!r}", raw=data) from exc

    ret_raw = data.get("ret", 0)
    try:
        ret = int(ret_raw)
    except (TypeError, ValueError) as exc:
        raise ProtocolDecodeError(f"ret 不是整数：{ret_raw!r}", raw=data) from exc

    msg_raw = data.get("msg", {})
    if msg_raw is None:
        msg: dict[str, Any] = {}
    elif isinstance(msg_raw, dict):
        msg = msg_raw
    else:
        # 20008/20009 等事件手册示例是 {"msg":{"msg":"..."}}，仍应是对象；
        # 若服务端返回字符串则包装起来，不丢数据。
        msg = {"_raw": msg_raw}

    fragments = 0
    if C.FRAGMENT_NUM_FIELD in msg:
        try:
            fragments = int(msg[C.FRAGMENT_NUM_FIELD])
        except (TypeError, ValueError):
            fragments = 0

    return OvitalResponse(cmdid=cmdid, ret=ret, msg=msg, raw=data, fragments=fragments)


# ---------------------------------------------------------------------------
# 登录摘要与脱敏
# ---------------------------------------------------------------------------


def md5psk(psk: str) -> str:
    """计算登录用 ``md5psk``：``md5(psk).hexdigest().upper()[:16]``。

    手册 2.3.1.1（``MD5加密密码``，示例 ``E10ADC3949BA59AB`` 为 16 位大写）
    与官方样例 omapinterface.js（``$.md5(key).toUpperCase().substr(0,16)``）。

    **调用方不得把返回值写日志/异常/工具返回。**
    """

    if not isinstance(psk, str) or not psk:
        raise ProtocolDecodeError("PSK 不能为空")
    digest = hashlib.md5(psk.encode("utf-8")).hexdigest().upper()
    return digest[: C.MD5PSK_LENGTH]


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """把文本中的敏感值替换为 ``***REDACTED***``。

    除调用方显式提供的 secret（明文 PSK、md5psk）外，还兜底屏蔽形如 16/32 位
    大写十六进制串的 md5psk（避免日志里意外出现）。
    """

    redacted = text
    for secret in secrets:
        if secret and len(secret) >= 4:
            redacted = redacted.replace(secret, "***REDACTED***")
    redacted = _MD5PSK_RE.sub("***REDACTED***", redacted)
    return redacted


def redact_msg(msg: Any, secrets: Iterable[str] = ()) -> Any:
    """深度复制 msg 并把敏感字段值替换为 ``***``（用于日志/证据）。"""

    secret_list = [s for s in secrets if s]

    def walk(value: Any) -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
                    result[key] = "***"
                else:
                    result[key] = walk(item)
            return result
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, str):
            return redact_text(value, secret_list)
        return value

    return walk(msg)


def safe_msg_summary(msg: Any, *, limit: int = 200, secrets: Iterable[str] = ()) -> str:
    """生成一行脱敏的 msg 摘要（日志用）。"""

    try:
        text = json_dumps(redact_msg(msg, secrets))
    except (TypeError, ValueError):  # pragma: no cover - 极端防御
        text = repr(msg)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


# ---------------------------------------------------------------------------
# base64 与分片
# ---------------------------------------------------------------------------


def b64encode(data: bytes) -> str:
    """标准 base64（20301/20302 的 ``Data`` 用标准 base64，示例以 ``iVBOR`` 开头）。"""

    return base64.b64encode(data).decode("ascii")


def b64decode_variants(data: str) -> list[tuple[str, bytes]]:
    """返回所有能成功解码的 (变体名, 字节) 列表，按优先级排序。

    变体：
    - ``standard``：标准 base64（``+/``）；
    - ``urlsafe``：URL-safe base64（``-_``）；
    - ``sdk_reversed``：官方样例 omapinterface.js ``Base64ToStr`` 的映射
      ``'-'->'/'; '_'->'+'``（与 URL-safe **相反**，疑似样例 bug，待实测）。

    注意：``-``/``_`` 同时出现时不同变体可能都能解码出**不同**字节，属于
    base64 本身的歧义；调用方（分片重组）应通过后续 JSON 解析做最终校验，
    解析失败必须显式报错，不能把歧义结果当成功。
    """

    if not isinstance(data, str):
        raise ProtocolDecodeError(f"base64 数据必须是字符串，收到 {type(data).__name__}")
    cleaned = re.sub(r"\s+", "", data)
    if not cleaned:
        return [("standard", b"")]
    padded = cleaned + "=" * (-len(cleaned) % 4)
    candidates = (
        ("standard", padded),
        ("urlsafe", padded.replace("-", "+").replace("_", "/")),
        ("sdk_reversed", padded.replace("-", "/").replace("_", "+")),
    )
    results: list[tuple[str, bytes]] = []
    seen: set[bytes] = set()
    for name, candidate in candidates:
        try:
            decoded = base64.b64decode(candidate, validate=True)
        except Exception:  # noqa: BLE001 - 逐个尝试
            continue
        if decoded in seen:
            continue
        seen.add(decoded)
        results.append((name, decoded))
    return results


def b64decode_tolerant(data: str) -> bytes:
    """按优先级解码 base64（standard → urlsafe → sdk_reversed）。

    全部失败抛 ``ProtocolDecodeError``；歧义见 ``b64decode_variants``。
    """

    variants = b64decode_variants(data)
    if not variants:
        raise ProtocolDecodeError(
            "base64 解码失败（已尝试标准/URL-safe/官方样例映射三种变体）"
        )
    return variants[0][1]


def is_fragment_message(msg: Mapping[str, Any]) -> bool:
    """判断一条响应是否是分片帧（官方样例字段，手册未写）。"""

    return (
        C.FRAGMENT_NUM_FIELD in msg
        and C.FRAGMENT_INDEX_FIELD in msg
        and C.FRAGMENT_DATA_FIELD in msg
    )


def split_text(text: str, max_chars: int) -> list[str]:
    """按字符数切分文本（官方样例 omapinterface.js ``splitStr`` 同语义）。"""

    if max_chars <= 0:
        raise ProtocolDecodeError("max_chars 必须为正整数")
    if len(text) <= max_chars:
        return [text]
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


def encode_fragment_requests(cmdid: int, msg: Mapping[str, Any], max_chars: int) -> list[str]:
    """把一个大 msg 编码为若干分片请求帧。

    官方样例 omapinterface.js：20112 请求体超过 ``FragmentMaxSize`` 时，
    发送 ``msg.FragmentIndex/FragmentData/FragmentNum`` 多帧，每帧
    ``FragmentData`` 是 JSON 文本的片段。手册未记载 → 「待实测」，默认不启用。
    """

    text = json_dumps(msg)
    parts = split_text(text, max_chars)
    frames: list[str] = []
    for index, part in enumerate(parts):
        frames.append(
            encode_request(
                cmdid,
                {
                    C.FRAGMENT_INDEX_FIELD: index,
                    C.FRAGMENT_DATA_FIELD: part,
                    C.FRAGMENT_NUM_FIELD: len(parts),
                },
            )
        )
    return frames


class FragmentAccumulator:
    """收集同一 cmdid 的分片，拼回完整 JSON 文本。

    分片可能乱序到达（官方样例按 ``FragmentIndex == FragmentNum-1`` 判断结束，
    隐含顺序到达）；本实现按收到数量判断，容忍乱序。
    """

    def __init__(self) -> None:
        self._parts: dict[int, str] = {}
        self._num: int | None = None

    def reset(self) -> None:
        self._parts.clear()
        self._num = None

    @property
    def expected(self) -> int | None:
        return self._num

    def add(self, msg: Mapping[str, Any]) -> str | None:
        """加入一个分片；收齐时返回拼接后的文本，否则返回 None。"""

        try:
            num = int(msg[C.FRAGMENT_NUM_FIELD])
            index = int(msg[C.FRAGMENT_INDEX_FIELD])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolDecodeError(
                "分片帧缺少合法的 FragmentNum/FragmentIndex", raw=dict(msg)
            ) from exc
        if num <= 0 or not (0 <= index < num):
            raise ProtocolDecodeError(
                f"分片索引非法：index={index}, num={num}", raw=dict(msg)
            )
        if self._num is None:
            self._num = num
        elif self._num != num:
            raise ProtocolDecodeError(
                f"同一响应分片总数不一致：{self._num} -> {num}", raw=dict(msg)
            )
        data = msg.get(C.FRAGMENT_DATA_FIELD)
        if not isinstance(data, str):
            raise ProtocolDecodeError("FragmentData 必须是字符串", raw=dict(msg))
        # FragmentData 是 base64 编码的 JSON 文本片段（官方样例 Base64ToStr）。
        self._parts[index] = b64decode_tolerant(data).decode("utf-8", errors="strict")
        if len(self._parts) == self._num:
            text = "".join(self._parts[i] for i in range(self._num))
            self.reset()
            return text
        return None


# ---------------------------------------------------------------------------
# 常用请求体构造（字段全部来自手册，章节见 docstring）
# ---------------------------------------------------------------------------


def _coerce_obj_id(value: Any, *, field: str = "ObjID") -> int:
    from .models import as_int

    return as_int(value, field_name=field)


def build_obj_list_request(parent_id: int = C.OBJ_GROUP_ROOT_ID) -> dict[str, Any]:
    """20100 获取收藏夹列表（手册 2.3.10.1）：``{"ParentID": 0}``。"""

    return {"ParentID": _coerce_obj_id(parent_id, field="ParentID")}


def build_obj_request(obj_id: int, latlng_type: int = 1) -> dict[str, Any]:
    """20101 获取收藏夹对象（手册 2.3.11.1）：``{"ObjID": ..., "LatlngType": ...}``。

    ``LatlngType`` 枚举见手册 2.2.2；默认 1=CGCS2000（避免 GCJ-02 重复偏移）。
    """

    return {
        "ObjID": _coerce_obj_id(obj_id),
        "LatlngType": int(latlng_type),
    }


def build_selected_request(flag: int = C.SEL_FLAG_GROUP_JOIN) -> dict[str, Any]:
    """20104 获取选中对象（手册 2.3.17.1）：``{"Flag": 1|2|3}``。"""

    if flag not in C.SEL_FLAG_NAMES:
        raise ProtocolDecodeError(f"20104 Flag 必须是 1/2/3（手册 2.2.3），收到 {flag!r}")
    return {"Flag": int(flag)}


def build_selected_tree_request(latlng_type: int = 1) -> dict[str, Any]:
    """20110 获取选中对象树（手册 2.3.18.1）：``{"LatlngType": 1}``。"""

    return {"LatlngType": int(latlng_type)}


def build_search_request(
    *,
    name: str,
    isrh_opt: int = 5,
    parent_id: int = 0,
    obj_type: int = 0,
    no_child: int = 0,
    max_cnt: int = 0,
    no_case: int = 1,
    shape_select: int = 0,
    srh_value: int = 0,
) -> dict[str, Any]:
    """20107 查找奥维对象（手册 2.3.22.1）。

    手册表格与示例字段名不一致，这里**两套都发**（``ParentID``/``idParent``、
    ``ShapeSelect``/``idShapeSelect``），两处都有官方出处；真实设备接受哪个
    「待实测」。
    """

    if isrh_opt not in C.SEARCH_OPT_NAMES:
        raise ProtocolDecodeError(
            f"SrhOpt={isrh_opt} 不在手册 2.2.4 枚举内（{sorted(C.SEARCH_OPT_NAMES)}）"
        )
    msg: dict[str, Any] = {
        "SrhOpt": int(isrh_opt),
        "SrhTxt": str(name),
        "ObjType": int(obj_type),
        "NoChild": int(no_child),
        "MaxCnt": int(max_cnt),
        "SrhValue": int(srh_value),
        "NoCase": int(no_case),
    }
    for field in C.SEARCH_PARENT_FIELDS:
        msg[field] = int(parent_id)
    for field in C.SEARCH_SHAPE_SELECT_FIELDS:
        msg[field] = int(shape_select)
    return msg


def build_delete_request(obj_ids: Iterable[int], *, no_group: int = 1) -> dict[str, Any]:
    """20103 删除收藏夹对象（手册 2.3.13.1）：``{"NoGroup": 1, "ListID": [...]}``。

    注意：官方样例 omapobj.js ``BuildObj`` 把字段写成 ``NoTroup``（拼写错误），
    手册写 ``NoGroup``。这里以**手册**为准发送 ``NoGroup``；该差异与真实设备
    是否接受 ``NoTroup`` 一并列入「待实测」。成功响应形态手册未给（待实测）。
    """

    ids = [_coerce_obj_id(obj_id) for obj_id in obj_ids]
    if not ids:
        raise ProtocolDecodeError("20103 删除对象列表不能为空")
    return {"NoGroup": int(no_group), "ListID": ids}


def build_map_data_request(
    *,
    bbox_lonlat: tuple[float, float, float, float],
    max_level: int,
    max_pixel: int,
    coord_type: int = 0,
) -> dict[str, Any]:
    """20301 获取地图图片数据（手册 2.3.29.1）。

    ``bbox_lonlat`` 是 ``(min_lon, min_lat, max_lon, max_lat)``，映射为
    ``x_lng1/y_lat1``（左下角）与 ``x_lng2/y_lat2``（右上角）。

    地图级别字段：手册表写 ``MaxLevel``，官方样例 ``BuildReqMapData`` 发
    ``MapLevel`` → 两个都发（见 ``constants.MAP_LEVEL_FIELDS``，待实测）。
    """

    min_lon, min_lat, max_lon, max_lat = bbox_lonlat
    if not (min_lon < max_lon and min_lat < max_lat):
        raise ProtocolDecodeError(
            "bbox_lonlat 必须是 (min_lon,min_lat,max_lon,max_lat) 且 min<max"
        )
    item: dict[str, Any] = {
        "x_lng1": float(min_lon),
        "y_lat1": float(min_lat),
        "x_lng2": float(max_lon),
        "y_lat2": float(max_lat),
        "CoordType": int(coord_type),
        "MaxPixel": int(max_pixel),
    }
    for field in C.MAP_LEVEL_FIELDS:
        item[field] = int(max_level)
    return {"ReqMapDatas": [item]}


def build_contour_request(
    *,
    bbox_lonlat: tuple[float, float, float, float],
    coord_type: int = 0,
    map_id: int = 1,
    value: int = 1,
    map_level: int = 12,
    elev_value: int = 30,
    check_flag: int = 0,
) -> dict[str, Any]:
    """20303 获取等高线（手册 2.3.31.1）。

    请求数组字段名：手册表写 ``ReqMapDatas``，示例写 ``ReqContours``
    （官方样例 ``BuildReqGetContour`` 也是 ``ReqContours``）。两个都发。
    """

    min_lon, min_lat, max_lon, max_lat = bbox_lonlat
    if not (min_lon < max_lon and min_lat < max_lat):
        raise ProtocolDecodeError(
            "bbox_lonlat 必须是 (min_lon,min_lat,max_lon,max_lat) 且 min<max"
        )
    item = {
        "MapID": int(map_id),
        "Value": int(value),
        "x_lng1": float(min_lon),
        "y_lat1": float(min_lat),
        "x_lng2": float(max_lon),
        "y_lat2": float(max_lat),
        "CoordType": int(coord_type),
        "MapLevel": int(map_level),
        "ElevValue": int(elev_value),
        "CheckFlag": int(check_flag),
    }
    return {"ReqMapDatas": [item], "ReqContours": [item]}
