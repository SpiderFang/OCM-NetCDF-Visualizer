#!/usr/bin/env python3
"""OCM 1 km 規則格網的精確岸線 GeoJSON 遮罩與向量陸地工具。

本模組集中實作研究報告圖與 SVD coastline-corrected v2 共同使用的岸線語意，避免
「稽核使用一套 rasterize、動畫又使用另一套 rasterize」造成格點數與畫面不一致。
輸入 GeoJSON 必須是 WGS84 經緯度座標的 Polygon/MultiPolygon；輸出陸地遮罩形狀為
``(lat, lon)``，其中 True 表示規則格網 cell 的中心、任一角點或 GeoJSON ring 頂點
接觸陸地 polygon。這是保守的 cell-overlap 近似，目的是在不改變 OCM 網格解析度下
保留龜山島、南北竿等小島，不代表 polygon 內陸地的真實比例或潮汐乾濕邊界。

本模組不會修改任何 OCM/SVD 輸入陣列；``draw_vector_land_overlay`` 只把向量陸地
加到 Matplotlib 畫面最上層。若要把 exact coastline 套用到 SVD，呼叫端必須另外
建立版本化 corrected input 或輸出目錄，不能回寫既有 v1 cache/result。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import matplotlib.patches as mpatches
import numpy as np


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """以串流方式計算岸線檔案 SHA-256，避免把 GeoJSON 另存成副本。

    ``path`` 是本機或 SERVER 上實際使用的 GeoJSON；雜湊會寫入 audit/manifest，
    讓後續能確認兩端是否使用同一份岸線版本。
    """

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _ring_to_lonlat_array(raw_ring: Any) -> np.ndarray | None:
    """把 GeoJSON ring 清理為有限的 ``(n, 2)`` `[lon, lat]` 陣列。

    GeoJSON 可能包含第三欄高程或其它維度，但本任務只做水平 WGS84 rasterize。
    ring 若少於閉合 polygon 所需的四個座標、維度不足或含 NaN，會被略過；此限制
    避免單一壞 geometry 讓整個兩年 SVD 稽核中斷，但 polygon 失效數會被記錄。
    """

    ring = np.asarray(raw_ring, dtype=np.float64)
    if ring.ndim != 2 or ring.shape[0] < 4 or ring.shape[1] < 2:
        return None
    lonlat = ring[:, :2]
    if not np.isfinite(lonlat).all():
        return None
    return lonlat


def iter_geojson_polygon_rings(geometry: dict[str, Any] | None) -> Iterable[list[np.ndarray]]:
    """遞迴列出 GeoJSON 中每個 polygon 的外環與洞環。

    輸入可為 FeatureCollection、Feature、Polygon、MultiPolygon 或
    GeometryCollection；輸出每個 polygon 的第一個 ring 是外環，後續 rings 是洞環。
    洞環只在 rasterize 時扣除，不會在向量外觀中額外填色。Point/LineString 沒有
    面積，不能定義陸地 cell，因此會被略過。
    """

    if not geometry:
        return
    geometry_type = geometry.get("type")
    if geometry_type == "FeatureCollection":
        for feature in geometry.get("features", []):
            yield from iter_geojson_polygon_rings(feature)
        return
    if geometry_type == "Feature":
        yield from iter_geojson_polygon_rings(geometry.get("geometry"))
        return
    if geometry_type == "GeometryCollection":
        for child in geometry.get("geometries", []):
            yield from iter_geojson_polygon_rings(child)
        return
    if geometry_type == "Polygon":
        rings = [_ring_to_lonlat_array(ring) for ring in geometry.get("coordinates", [])]
        valid_rings = [ring for ring in rings if ring is not None]
        if valid_rings:
            yield valid_rings
        return
    if geometry_type == "MultiPolygon":
        for polygon in geometry.get("coordinates", []):
            rings = [_ring_to_lonlat_array(ring) for ring in polygon]
            valid_rings = [ring for ring in rings if ring is not None]
            if valid_rings:
                yield valid_rings


def points_in_ring(points: np.ndarray, ring: np.ndarray) -> np.ndarray:
    """以向量化 ray-casting 判斷 `(lon,lat)` 點是否在 ring 內或邊界上。

    邊界點保守視為陸地，因為若岸線恰好穿過 cell center，保留該格點的流速會比
    移除一個鄰近海域格點更容易造成「陸地上有箭頭」的誤判。此方法適用台灣附近
    小範圍經緯度 polygon，不處理跨日期變更線的幾何。
    """

    x = points[:, 0]
    y = points[:, 1]
    ring_x = ring[:, 0]
    ring_y = ring[:, 1]
    inside = np.zeros(points.shape[0], dtype=bool)
    on_boundary = np.zeros(points.shape[0], dtype=bool)
    previous = ring.shape[0] - 1
    for current in range(ring.shape[0]):
        x0 = ring_x[previous]
        y0 = ring_y[previous]
        x1 = ring_x[current]
        y1 = ring_y[current]
        tolerance = 1.0e-10
        segment_dx = x1 - x0
        segment_dy = y1 - y0
        cross = (x - x0) * segment_dy - (y - y0) * segment_dx
        within_segment_bbox = (
            (x >= min(x0, x1) - tolerance)
            & (x <= max(x0, x1) + tolerance)
            & (y >= min(y0, y1) - tolerance)
            & (y <= max(y0, y1) + tolerance)
        )
        on_boundary |= (np.abs(cross) <= tolerance) & within_segment_bbox
        crosses_latitude = (y0 > y) != (y1 > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            crossing_lon = (segment_dx * (y - y0) / (y1 - y0)) + x0
        inside ^= crosses_latitude & (x < crossing_lon)
        previous = current
    return inside | on_boundary


def _axis_cell_bounds(axis: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """由嚴格遞增的規則軸建立每個格點 cell 的下界與上界。"""

    axis = np.asarray(axis, dtype=np.float64)
    if axis.ndim != 1 or axis.size < 2 or not np.all(np.diff(axis) > 0):
        raise ValueError("岸線 rasterize 需要至少兩個嚴格遞增的規則經緯度中心點")
    edges = np.empty(axis.size + 1, dtype=np.float64)
    edges[1:-1] = 0.5 * (axis[:-1] + axis[1:])
    edges[0] = axis[0] - 0.5 * (axis[1] - axis[0])
    edges[-1] = axis[-1] + 0.5 * (axis[-1] - axis[-2])
    return edges[:-1], edges[1:]


def _build_cell_bounds(
    lon: np.ndarray,
    lat: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """建立與 `(lat,lon)` C-order 一致的 cell bbox 與 target points。

    回傳 target points、每個 cell 的 lon_min/lon_max/lat_min/lat_max；這些陣列只用於
    rasterize，不會改變 SVD 或 surface cache 的原始座標，也不把經緯度誤當成公尺。
    """

    lon = np.asarray(lon, dtype=np.float64)
    lat = np.asarray(lat, dtype=np.float64)
    lon_grid, lat_grid = np.meshgrid(lon, lat)
    target_points = np.column_stack((lon_grid.ravel(), lat_grid.ravel()))
    lon_lower, lon_upper = _axis_cell_bounds(lon)
    lat_lower, lat_upper = _axis_cell_bounds(lat)
    cell_lon_min, cell_lat_min = np.meshgrid(lon_lower, lat_lower)
    cell_lon_max, cell_lat_max = np.meshgrid(lon_upper, lat_upper)
    return (
        target_points,
        cell_lon_min.ravel(),
        cell_lon_max.ravel(),
        cell_lat_min.ravel(),
        cell_lat_max.ravel(),
    )


def _ring_touches_cells(
    ring: np.ndarray,
    candidate_indices: np.ndarray,
    target_points: np.ndarray,
    cell_lon_min: np.ndarray,
    cell_lon_max: np.ndarray,
    cell_lat_min: np.ndarray,
    cell_lat_max: np.ndarray,
) -> np.ndarray:
    """判斷一個 ring 是否接觸候選 cell 的中心、四角或 ring vertex。"""

    if candidate_indices.size == 0:
        return np.zeros(0, dtype=bool)
    candidate_centers = target_points[candidate_indices]
    lon_min = cell_lon_min[candidate_indices]
    lon_max = cell_lon_max[candidate_indices]
    lat_min = cell_lat_min[candidate_indices]
    lat_max = cell_lat_max[candidate_indices]
    sample_sets = (
        candidate_centers,
        np.column_stack((lon_min, lat_min)),
        np.column_stack((lon_min, lat_max)),
        np.column_stack((lon_max, lat_min)),
        np.column_stack((lon_max, lat_max)),
    )
    touched = np.zeros(candidate_indices.size, dtype=bool)
    for sample_points in sample_sets:
        touched |= points_in_ring(sample_points, ring)
    nearby = (
        (ring[:, 0] >= float(np.min(lon_min)))
        & (ring[:, 0] <= float(np.max(lon_max)))
        & (ring[:, 1] >= float(np.min(lat_min)))
        & (ring[:, 1] <= float(np.max(lat_max)))
    )
    nearby_ring = ring[nearby]
    if nearby_ring.size:
        for local_index in np.flatnonzero(~touched):
            touched[local_index] = bool(
                np.any(
                    (nearby_ring[:, 0] >= lon_min[local_index])
                    & (nearby_ring[:, 0] <= lon_max[local_index])
                    & (nearby_ring[:, 1] >= lat_min[local_index])
                    & (nearby_ring[:, 1] <= lat_max[local_index])
                )
            )
    return touched


def load_coastline_geojson(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """載入 GeoJSON 並回傳 geometry 摘要與實際檔案雜湊。

    摘要中的 ``feature_count`` 是原始 FeatureCollection feature 數；
    ``polygon_count`` 是清理後可作 rasterize 的 Polygon 數，兩者同時保存避免把
    feature 數誤稱為 polygon 數。
    """

    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"找不到 coastline GeoJSON：{path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    polygon_groups = list(iter_geojson_polygon_rings(document))
    feature_count = len(document.get("features", [])) if document.get("type") == "FeatureCollection" else 1
    summary = {
        "path": str(path),
        "sha256": sha256_file(path),
        "geojson_type": document.get("type"),
        "feature_count": int(feature_count),
        "polygon_count": int(len(polygon_groups)),
        "rasterize_mode": "cell_overlap_center_corners_vertices",
        "rasterize_semantics": (
            "A grid cell is exact coastline land when its center, any corner, or any GeoJSON "
            "ring vertex touches a land polygon; holes are removed and grid resolution is unchanged."
        ),
    }
    return document, summary


def build_coastline_land_mask(lon: np.ndarray, lat: np.ndarray, geojson_path: Path) -> tuple[np.ndarray, dict[str, Any]]:
    """以既有 report-safe 規則建立 `(lat,lon)` exact coastline land mask。

    外環先加入陸地，洞環再扣回；只測試與 polygon bbox 相交的 cell，降低 1 km 網格
    對 1,905 個 polygon 的計算量。回傳 summary 會補上目標 grid shape、總 cell 數與
    命中 cell 數，供 audit、SVD input metadata 與動畫 manifest 共用。
    """

    document, summary = load_coastline_geojson(geojson_path)
    target_points, cell_lon_min, cell_lon_max, cell_lat_min, cell_lat_max = _build_cell_bounds(lon, lat)
    flat_land = np.zeros(target_points.shape[0], dtype=bool)
    skipped = 0
    candidate_tests = 0
    for rings in iter_geojson_polygon_rings(document):
        if not rings:
            skipped += 1
            continue
        exterior = rings[0]
        polygon_lon_min = float(np.min(exterior[:, 0]))
        polygon_lon_max = float(np.max(exterior[:, 0]))
        polygon_lat_min = float(np.min(exterior[:, 1]))
        polygon_lat_max = float(np.max(exterior[:, 1]))
        candidate_indices = np.flatnonzero(
            (cell_lon_max >= polygon_lon_min)
            & (cell_lon_min <= polygon_lon_max)
            & (cell_lat_max >= polygon_lat_min)
            & (cell_lat_min <= polygon_lat_max)
        )
        if candidate_indices.size == 0:
            continue
        candidate_tests += int(candidate_indices.size)
        polygon_land = _ring_touches_cells(
            exterior,
            candidate_indices,
            target_points,
            cell_lon_min,
            cell_lon_max,
            cell_lat_min,
            cell_lat_max,
        )
        for hole in rings[1:]:
            hit_indices = np.flatnonzero(polygon_land)
            if hit_indices.size == 0:
                break
            polygon_land[hit_indices] &= ~_ring_touches_cells(
                hole,
                candidate_indices[hit_indices],
                target_points,
                cell_lon_min,
                cell_lon_max,
                cell_lat_min,
                cell_lat_max,
            )
        flat_land[candidate_indices] |= polygon_land
    mask = flat_land.reshape((len(lat), len(lon)))
    summary.update(
        {
            "skipped_polygon_count": int(skipped),
            "candidate_cell_tests": int(candidate_tests),
            "grid_shape_lat_lon": [int(len(lat)), int(len(lon))],
            "total_grid_cell_count": int(mask.size),
            "land_grid_cell_count": int(np.count_nonzero(mask)),
            "land_fraction": float(np.mean(mask)),
        }
    )
    return mask, summary


def load_outer_rings(geojson_path: Path) -> list[np.ndarray]:
    """讀取向量陸地外環，供 renderer 以最高 z-order 蓋住底色與箭頭。"""

    document, _summary = load_coastline_geojson(geojson_path)
    rings: list[np.ndarray] = []
    for polygon_rings in iter_geojson_polygon_rings(document):
        if polygon_rings:
            rings.append(polygon_rings[0])
    if not rings:
        raise ValueError(f"GeoJSON 沒有可繪製的 Polygon 外環：{geojson_path}")
    return rings


def draw_vector_land_overlay(
    ax: Any,
    land_rings: list[np.ndarray],
    extent: tuple[float, float, float, float],
    *,
    facecolor: str,
    edgecolor: str,
    linewidth: float,
    zorder: int,
    antialiased: bool = True,
) -> int:
    """在 Matplotlib axes 上繪製與 extent 相交的向量陸地，回傳 patch 數。

    向量 polygon 只負責最後的視覺覆蓋，不會改變任何流速資料；它放在 quiver 之上，
    使離岸箭頭即使線段延伸進 polygon，也不會在陸地面積上留下可見白線。正式
    display-only renderer 以原始 GeoJSON ring vertex 直接填色、``edgecolor='none'``、
    ``linewidth=0`` 與抗鋸齒，讓可見輪廓來自指定高解析度 vector polygon，而不是
    conservative 1 km raster mask 的階梯邊界。raster mask 仍由呼叫端用於資料遮罩與
    audit；此函式本身只負責可見圖層，不回寫 OCM/SVD 陣列。
    """

    lon_min, lon_max, lat_min, lat_max = extent
    drawn = 0
    for ring in land_rings:
        if (
            float(np.max(ring[:, 0])) < lon_min
            or float(np.min(ring[:, 0])) > lon_max
            or float(np.max(ring[:, 1])) < lat_min
            or float(np.min(ring[:, 1])) > lat_max
        ):
            continue
        ax.add_patch(
            mpatches.Polygon(
                ring,
                closed=True,
                facecolor=facecolor,
                edgecolor=edgecolor,
                linewidth=linewidth,
                antialiased=antialiased,
                snap=False,
                zorder=zorder,
            )
        )
        drawn += 1
    return drawn
