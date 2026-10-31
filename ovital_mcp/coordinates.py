"""坐标语义、轴序与 CRS 工具（协议与 GIS 共用的唯一实现）。

此模块的核心目的是把奥维 V2.7 协议里**四套互不相同的坐标枚举**分开解析，
避免出现"数字 0 一律当 WGS84/CGCS2000"这类静默错误。

官方依据（WebSocket V2.7 手册 https://www.gpsov.com/appdoc/index.php?id=32）：

- 手册 2.2.2 顶层 ``CoordType``：0=CGCS2000 地理坐标, 1=GCJ-02 经纬度,
  2=当前坐标系(奥维中使用的坐标系)。
- 手册 2.2.2 ``LatlngType``：0=系统设置的坐标系, 1=CGCS2000 地理坐标,
  2=GCJ-02 经纬度, 3=平面坐标系, 4=墨卡托全球投影 xy 坐标。
- 手册 2.4.1 ``OmapLatlng.CoordType``：**0=GCJ-02 经纬度, 1=CGCS2000 地理坐标,
  2=平面坐标**（与顶层 CoordType 的 0/1 含义相反！）。
- 手册 2.4.8 ``Shape.ObjectDetail.Gcj02``：0=CGCS2000 地理坐标,
  1=GCJ-02 经纬度, 2=平面坐标系。

轴序（手册 2.4.8 示例：``"Latlng":[39.01064751, 106.57287598, ...]``，
39.01 为北纬、106.57 为东经）：``Shape.Latlng`` 扁平数组是 **纬度在前
(lat, lng, lat, lng, ...)**；``OmapLatlng`` 是命名字段 ``lat``/``lng``。
本模块对外统一使用 ``(lon, lat)`` 顺序。

坐标系选择：
- 内部规范坐标：``CANONICAL_CRS = EPSG:4490``（CGCS2000 地理坐标，经度在前）。
  注意 EPSG:4490 的官方轴序是 lat,lon，但本项目在所有内存结构里固定
  ``(lon, lat)``，与 pyproj 交互时通过 ``always_xy=True`` 保证。
- CGCS2000 与 WGS84 的差异是厘米级（对于 30 m DEM 可忽略），但报告中必须
  写明；需要严格 WGS84 时显式用 pyproj 转换并记录。
- 面积/周长/缓冲必须投影到米制 CRS，禁止直接在地理坐标上算面积。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from enum import Enum
from typing import Any

from .errors import CoordinateSemanticError, DataValidationError

CANONICAL_CRS = "EPSG:4490"


class CoordSemantic(str, Enum):
    """坐标语义（与具体协议枚举解耦）。"""

    SYSTEM = "system"
    CGCS2000 = "cgcs2000"
    GCJ02 = "gcj02"
    PLANE = "plane"
    MERCATOR = "mercator"


# ---------------------------------------------------------------------------
# 四套协议枚举（禁止合并、禁止随手用数字）
# ---------------------------------------------------------------------------

#: 手册 2.2.2 顶层 ``CoordType``。
COORD_TYPE_TOP: dict[int, CoordSemantic] = {
    0: CoordSemantic.CGCS2000,
    1: CoordSemantic.GCJ02,
    2: CoordSemantic.SYSTEM,
}

#: 手册 2.2.2 / 2.3.11 / 2.3.18 ``LatlngType``。
LATLNG_TYPE: dict[int, CoordSemantic] = {
    0: CoordSemantic.SYSTEM,
    1: CoordSemantic.CGCS2000,
    2: CoordSemantic.GCJ02,
    3: CoordSemantic.PLANE,
    4: CoordSemantic.MERCATOR,
}

#: 手册 2.4.1 ``OmapLatlng.CoordType`` —— 注意 0/1 与顶层 CoordType 相反。
OMAP_LATLNG_COORD_TYPE: dict[int, CoordSemantic] = {
    0: CoordSemantic.GCJ02,
    1: CoordSemantic.CGCS2000,
    2: CoordSemantic.PLANE,
}

#: 手册 2.4.8 ``Shape.ObjectDetail.Gcj02``。
SHAPE_GCJ02: dict[int, CoordSemantic] = {
    0: CoordSemantic.CGCS2000,
    1: CoordSemantic.GCJ02,
    2: CoordSemantic.PLANE,
}


def _lookup(mapping: dict[int, CoordSemantic], value: Any, name: str) -> CoordSemantic:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoordinateSemanticError(
            f"{name} 需要整数枚举值，收到 {value!r}",
            details={"field": name, "value": repr(value)},
        )
    try:
        return mapping[value]
    except KeyError as exc:  # noqa: TRY003 - 明确报错而不是静默猜测
        raise CoordinateSemanticError(
            f"{name} 未知枚举值 {value}；允许值 {sorted(mapping)}。"
            " 不得把它当作 WGS84/CGCS2000 处理。",
            details={"field": name, "value": value, "allowed": sorted(mapping)},
        ) from exc


def semantic_for_top_coord_type(value: int) -> CoordSemantic:
    """解析顶层 ``CoordType``（手册 2.2.2：0=CGCS2000, 1=GCJ02, 2=当前坐标系）。"""

    return _lookup(COORD_TYPE_TOP, value, "CoordType(top)")


def semantic_for_latlng_type(value: int) -> CoordSemantic:
    """解析 ``LatlngType``（手册 2.2.2：0=系统, 1=CGCS2000, 2=GCJ02, 3=平面, 4=墨卡托）。"""

    return _lookup(LATLNG_TYPE, value, "LatlngType")


def semantic_for_omap_latlng_coord_type(value: int) -> CoordSemantic:
    """解析 ``OmapLatlng.CoordType``（手册 2.4.1：0=GCJ02, 1=CGCS2000, 2=平面）。"""

    return _lookup(OMAP_LATLNG_COORD_TYPE, value, "OmapLatlng.CoordType")


def semantic_for_shape_gcj02(value: int) -> CoordSemantic:
    """解析 ``Shape.ObjectDetail.Gcj02``（手册 2.4.8：0=CGCS2000, 1=GCJ02, 2=平面）。"""

    return _lookup(SHAPE_GCJ02, value, "Shape.Gcj02")


# ---------------------------------------------------------------------------
# GCJ-02 <-> CGCS2000（公开标准算法；近似，仅作显式转换用）
# ---------------------------------------------------------------------------

_A = 6378245.0  # Krasovsky 1940 长半轴
_EE = 0.00669342162296594323  # 偏心率平方


def _out_of_china(lon: float, lat: float) -> bool:
    """GCJ-02 算法在中国境外不偏移（粗略矩形，公开算法通用判断）。"""

    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret = (
        -100.0
        + 2.0 * x
        + 3.0 * y
        + 0.2 * y * y
        + 0.1 * x * y
        + 0.2 * math.sqrt(abs(x))
    )
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = (
        300.0
        + x
        + 2.0 * y
        + 0.1 * x * x
        + 0.1 * x * y
        + 0.1 * math.sqrt(abs(x))
    )
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def cgcs2000_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    """CGCS2000/WGS84 经纬度 -> GCJ-02 经纬度。

    这是公开的近似算法（Krasovsky 椭球参数），在中国大陆范围内的偏移量为
    数百米级，通常与商业底图对齐使用；**不能当作精确测绘变换**。返回
    ``(lon, lat)``。不在中国境内时原样返回。
    """

    lon, lat = verify_canonical_lonlat(lon, lat)
    if _out_of_china(lon, lat):
        return lon, lat
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlng = _transform_lng(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = 1 - _EE * math.sin(radlat) ** 2
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lon + dlng, lat + dlat


def gcj02_to_cgcs2000(lon: float, lat: float) -> tuple[float, float]:
    """GCJ-02 经纬度 -> CGCS2000/WGS84 经纬度（迭代反解，近似）。"""

    lon, lat = verify_canonical_lonlat(lon, lat)
    if _out_of_china(lon, lat):
        return lon, lat
    # 迭代反解：以 CGCS2000 作为初值，反复用正变换逼近。
    cg_lon, cg_lat = lon, lat
    for _ in range(10):
        fwd_lon, fwd_lat = cgcs2000_to_gcj02(cg_lon, cg_lat)
        d_lon = fwd_lon - lon
        d_lat = fwd_lat - lat
        if abs(d_lon) < 1e-11 and abs(d_lat) < 1e-11:
            break
        cg_lon -= d_lon
        cg_lat -= d_lat
    return cg_lon, cg_lat


# ---------------------------------------------------------------------------
# LatLng 结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LatLng:
    """一个带语义的经纬度点。

    注意：``lat`` 在前只是字段名顺序，不代表协议数组顺序。
    """

    lat: float
    lng: float
    semantic: CoordSemantic = CoordSemantic.CGCS2000

    def as_tuple(self) -> tuple[float, float]:
        return (self.lat, self.lng)


def verify_canonical_lonlat(lon: float, lat: float) -> tuple[float, float]:
    """校验内部规范坐标 ``(lon, lat)`` 的数值合理性。

    防御常见的轴序颠倒（把 39.9 的纬度当经度等）。只做合理性检查，不做
    猜测性纠正；超出合理范围直接报错。
    """

    if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
        raise DataValidationError(f"经纬度必须是数字，收到 {type(lon)!r}/{type(lat)!r}")
    if math.isnan(lon) or math.isnan(lat) or math.isinf(lon) or math.isinf(lat):
        raise DataValidationError(f"经纬度不能是 NaN/Inf，收到 ({lon!r}, {lat!r})")
    if not (-180.0 <= lon <= 180.0):
        raise DataValidationError(
            f"经度 {lon} 超出 [-180,180]，疑似轴序颠倒或坐标语义错误"
        )
    if not (-90.0 <= lat <= 90.0):
        raise DataValidationError(
            f"纬度 {lat} 超出 [-90,90]，疑似轴序颠倒或坐标语义错误"
        )
    return float(lon), float(lat)


def latlng_to_canonical(
    latlng: LatLng,
    *,
    allow_gcj02_conversion: bool = False,
) -> tuple[float, float]:
    """把带语义的 ``LatLng`` 转成内部规范 ``(lon, lat)``（EPSG:4490 语义）。

    - ``CGCS2000``：直接返回（CGCS2000 与 WGS84 厘米级差异，见模块 docstring）。
    - ``GCJ02``：仅当 ``allow_gcj02_conversion=True`` 时用公开近似算法反解；
      否则抛错，提示优先用 ``LatlngType=1`` 读取 CGCS2000。
    - ``SYSTEM``：语义不明确，抛错（必须先用 20222 查询系统坐标系后显式解释）。
    - ``PLANE``/``MERCATOR``：需要外部投影参数，抛错。
    """

    semantic = latlng.semantic
    if semantic == CoordSemantic.CGCS2000:
        return verify_canonical_lonlat(latlng.lng, latlng.lat)
    if semantic == CoordSemantic.GCJ02:
        if not allow_gcj02_conversion:
            raise CoordinateSemanticError(
                "对象坐标是 GCJ-02 经纬度。为避免重复偏移，默认不自动转换；"
                "请改用 LatlngType=1 读取 CGCS2000，或在配置中显式开启 "
                "allow_gcj02_conversion 并知悉其为公开近似算法。",
                details={"semantic": semantic.value},
            )
        lon, lat = gcj02_to_cgcs2000(latlng.lng, latlng.lat)
        return verify_canonical_lonlat(lon, lat)
    if semantic == CoordSemantic.SYSTEM:
        raise CoordinateSemanticError(
            "坐标语义为“系统设置的坐标系”，无法安全解释；请先用 20222 获取系统坐标系参数",
            details={"semantic": semantic.value},
        )
    raise CoordinateSemanticError(
        f"坐标语义 {semantic.value} 需要投影参数才能转换，适配器未实现该转换",
        details={"semantic": semantic.value},
    )


def canonical_to_latlng(
    lon: float,
    lat: float,
    semantic: CoordSemantic,
    *,
    allow_gcj02_conversion: bool = False,
) -> LatLng:
    """内部规范 ``(lon, lat)`` -> 带语义 ``LatLng``（写协议时使用）。"""

    lon, lat = verify_canonical_lonlat(lon, lat)
    if semantic == CoordSemantic.CGCS2000:
        return LatLng(lat=lat, lng=lon, semantic=semantic)
    if semantic == CoordSemantic.GCJ02:
        if not allow_gcj02_conversion:
            raise CoordinateSemanticError(
                "目标坐标语义为 GCJ-02，但未开启 allow_gcj02_conversion；"
                "默认应写 CGCS2000（LatlngType=1 / Gcj02=0）。",
                details={"semantic": semantic.value},
            )
        g_lon, g_lat = cgcs2000_to_gcj02(lon, lat)
        return LatLng(lat=g_lat, lng=g_lon, semantic=semantic)
    raise CoordinateSemanticError(
        f"目标坐标语义 {semantic.value} 需要投影参数，适配器未实现该转换",
        details={"semantic": semantic.value},
    )


# ---------------------------------------------------------------------------
# 投影与几何
# ---------------------------------------------------------------------------


def metric_crs_for(lon: float, lat: float, *, kind: str = "utm") -> str:
    """给定经纬度选择一个米制投影 CRS（字符串）。

    - ``kind="utm"``：自动 UTM 分带，EPSG:326xx（北半球）/ 327xx（南半球）。
    - ``kind="gk"``：CGCS2000 3 度带高斯-克吕格（EPSG:4491-4554 中的对应带），
      返回形如 ``EPSG:4547`` 的字符串；仅在中国范围有意义。

    投影仅用于距离/面积/缓冲计算；成果边界仍以 EPSG:4490 经纬度保存并记录。
    """

    lon, lat = verify_canonical_lonlat(lon, lat)
    if kind == "utm":
        zone = int((lon + 180.0) // 6.0) + 1
        if zone < 1:
            zone = 1
        if zone > 60:
            zone = 60
        epsg = (32600 if lat >= 0 else 32700) + zone
        return f"EPSG:{epsg}"
    if kind == "gk":
        if not (73.0 <= lon <= 136.0):
            raise DataValidationError(f"经度 {lon} 不在 CGCS2000 高斯-克吕格分带适用范围内")
        # CGCS2000 3-degree GK: EPSG:4513-4533 为 25-45 带（中央经线 75E-135E），
        # 因存在两种带号命名，这里使用明确的 CGCS2000 / 3-degree Gauss-Kruger
        # zone XX（EPSG:4513 + (zone-25)）。
        zone = int(round((lon - 1.5) / 3.0)) + 1
        zone = min(max(zone, 25), 45)
        return f"EPSG:{4513 + (zone - 25)}"
    raise DataValidationError(f"未知的米制投影类型 kind={kind!r}，只支持 'utm' / 'gk'")


def crs_axis_order(crs: Any) -> str:
    """返回 CRS 的轴序描述：``lonlat`` / ``latlon`` / ``easting_northing``。

    用于读取栅格/矢量时显式处理轴序，避免 EPSG 官方轴序与 GIS 惯例不一致。
    """

    from pyproj import CRS  # 延迟导入：coordinates 模块被协议层轻量引用

    parsed = CRS.from_user_input(crs)
    if parsed.is_geographic:
        axes = [ax.name.lower() for ax in parsed.axis_info]
        first = axes[0] if axes else "lon"
        return "lonlat" if first.startswith(("lon", "east", "geodetic longitude")) else "latlon"
    return "easting_northing"


@lru_cache(maxsize=64)
def _cached_transformer(src_crs: str, dst_crs: str):
    """缓存 pyproj Transformer 创建（创建成本 ~毫秒级，逐要素创建会拖慢矢量化）。"""

    from pyproj import Transformer

    return Transformer.from_crs(src_crs, dst_crs, always_xy=True)


def transform_geometry(geom: Any, src_crs: Any, dst_crs: Any) -> Any:
    """用 pyproj 显式转换 shapely 几何（始终 ``always_xy=True``）。

    Transformer 按 (src, dst) 缓存；批量转换请优先复用同一对 CRS。
    """

    from shapely.ops import transform as shapely_transform

    src_key = str(src_crs)
    dst_key = str(dst_crs)
    if src_key == dst_key:
        return geom
    transformer = _cached_transformer(src_key, dst_key)
    return shapely_transform(transformer.transform, geom)


def area_m2(geom: Any, crs: Any) -> float:
    """在给定米制 CRS 下计算水平面积（平方米）。

    地理坐标（度）下算面积没有工程意义，这里会拒绝。
    """

    from pyproj import CRS

    parsed = CRS.from_user_input(crs)
    if parsed.is_geographic:
        raise DataValidationError(
            f"CRS {crs} 是地理坐标，不能直接计算面积；请先投影到米制 CRS"
        )
    return float(geom.area)
