"""下载 Copernicus GLO-30 公开 DEM tile（AWS S3，无需认证）。

Copernicus DEM 由 ESA/TanDEM-X 任务生产，官方定义为 **DSM**（含建筑/植被
地表），GLO-30 为全球约 30 m；水平基准 WGS84（EPSG:4326），垂直基准
EGM2008（正高），单位米；单 tile 为 1°×1° 经纬度。

公开下载地址（已实测 2026-10 可访问，无需 AWS 账号）::

    https://copernicus-dem-30m.s3.amazonaws.com/
        Copernicus_DSM_COG_10_N39_00_E116_00_DEM/
        Copernicus_DSM_COG_10_N39_00_E116_00_DEM.tif

用法::

    .venv/bin/python scripts/fetch_copernicus_dem.py --dest-dir .cache/copernicus-dem
    .venv/bin/python scripts/fetch_copernicus_dem.py --north 39 --east 116

注意：本脚本只负责下载与基本完整性检查；**不**提供独立审核边界。用它跑
出的集水区只能做"可复现性/合理性"检查，不能当作工程精度证据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

#: S3 公共前缀（Copernicus DEM GLO-30 COG 10 版）。
S3_PREFIX = "https://copernicus-dem-30m.s3.amazonaws.com"
#: 默认 tile（北京西北方向，含山地，便于做真实地形汇流试验）。
DEFAULT_NORTH = 39
DEFAULT_EAST = 116


def tile_name(north: int, east: int) -> str:
    """返回 Copernicus tile 名，例如 ``Copernicus_DSM_COG_10_N39_00_E116_00_DEM``。"""

    ns = "N" if north >= 0 else "S"
    ew = "E" if east >= 0 else "W"
    return (
        f"Copernicus_DSM_COG_10_{ns}{abs(int(north)):02d}_00_"
        f"{ew}{abs(int(east)):03d}_00_DEM"
    )


def tile_url(north: int, east: int) -> str:
    name = tile_name(north, east)
    return f"{S3_PREFIX}/{name}/{name}.tif"


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_tile(
    dest_dir: str | Path,
    *,
    north: int = DEFAULT_NORTH,
    east: int = DEFAULT_EAST,
    timeout_s: float = 600.0,
    overwrite: bool = False,
) -> Path:
    """下载单个 tile 到 ``dest_dir``，返回本地路径（含完整性检查）。"""

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = tile_name(north, east)
    target = dest_dir / f"{name}.tif"
    url = tile_url(north, east)
    if target.is_file() and not overwrite and target.stat().st_size > 0:
        print(f"已存在，跳过下载：{target}（{target.stat().st_size / 1e6:.1f} MB）")
        return target

    print(f"下载 {url}")
    tmp = target.with_suffix(".tif.part")
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            total = int(response.headers.get("Content-Length") or 0)
            downloaded = 0
            with open(tmp, "wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        percent = downloaded * 100.0 / total
                        print(f"\r  {percent:5.1f}%  {downloaded / 1e6:6.1f}/{total / 1e6:.1f} MB", end="")
        print()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"下载失败：{exc}") from exc
    if tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise SystemExit("下载失败：文件为空")
    tmp.replace(target)
    print(f"完成：{target}（{target.stat().st_size / 1e6:.1f} MB）")
    return target


def inspect_tile(path: Path) -> dict:
    """用 rasterio 做基本检查（不依赖项目代码，脚本可独立运行）。"""

    try:
        import rasterio
    except ImportError as exc:  # pragma: no cover - 仅环境缺失时
        raise SystemExit(f"需要 rasterio 才能检查 tile：{exc}") from exc
    with rasterio.open(path) as ds:
        info = {
            "path": str(path),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
            "crs": str(ds.crs),
            "width": ds.width,
            "height": ds.height,
            "res": [abs(float(ds.res[0])), abs(float(ds.res[1]))],
            "bounds": [float(v) for v in ds.bounds],
            "nodata": ds.nodata,
            "dtype": str(ds.dtypes[0]),
        }
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dest-dir",
        default=str(Path(__file__).resolve().parents[1] / ".cache" / "copernicus-dem"),
        help="下载目录（默认 .cache/copernicus-dem）",
    )
    parser.add_argument("--north", type=int, default=DEFAULT_NORTH, help="tile 北边界纬度（整数）")
    parser.add_argument("--east", type=int, default=DEFAULT_EAST, help="tile 西边界经度（整数）")
    parser.add_argument("--overwrite", action="store_true", help="强制重新下载")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出检查结果")
    args = parser.parse_args(argv)

    path = download_tile(
        args.dest_dir,
        north=args.north,
        east=args.east,
        overwrite=args.overwrite,
    )
    info = inspect_tile(path)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print(
            f"CRS={info['crs']} {info['width']}×{info['height']} "
            f"res={info['res']} dtype={info['dtype']} nodata={info['nodata']}"
        )
        print(f"SHA256={info['sha256']}")
    print(
        "数据说明：Copernicus GLO-30 是 DSM（含建筑/植被），垂直基准 EGM2008，"
        "无独立人工审核边界；仅可用于可复现性/合理性检查。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
