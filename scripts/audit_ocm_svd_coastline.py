#!/usr/bin/env python3
"""稽核 exact coastline 與四區水柱 SVD 表層特徵的地理一致性。

本程式執行在能讀取 SERVER SVD 結果與 ``preprocessed/ocm_surface`` 的環境，先以
``coastline_utils.build_coastline_land_mask`` 對每一區 SVD 規則格網建立保守的
cell-overlap 陸地遮罩，再把它與原始 static ocean、analysis geometry 及表層
velocity feature mask 逐格交集。接著逐月掃描同源 surface cache 的所有 u/v 與
``valid_mask_surface`` 時間列，計算 exact-land 格點上的有限值出現率、有效時間數與
流速統計；不以單一影格判定污染。

若 exact-land cell 同時被原始 SVD surface feature mask 納入，且同源 cache 在這些
cell 有有限 u/v，便判定為上游表層特徵污染候選。程式另外以
``regression_u/v_mps_per_pc_std.npy`` 計算所有已保存模態對表層特徵的「模態表示變異
proxy」；它不是重新讀取已刪除的 raw feature matrix，因此 manifest 會明確標註其
限制，不把 proxy 誤稱為原始資料總變異。

輸出 JSON/CSV 只寫入新指定的 versioned audit 目錄；本程式不修改既有 SVD run、原始
surface cache、PPTX 或任何 v1 動畫。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from coastline_utils import build_coastline_land_mask, sha256_file


REGIONS: dict[str, dict[str, str]] = {
    "A": {
        "name_zh": "臺灣東北",
        "svd_dir": "guishan_gongliao_northeast_taiwan_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1",
        "flow_domain_id": "northeast_taiwan_common_cache_v3",
    },
    "B": {
        "name_zh": "新竹",
        "svd_dir": "hsinchu_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1",
        "flow_domain_id": "hsinchu_cache_v3",
    },
    "C": {
        "name_zh": "後灣",
        "svd_dir": "houwan_nmmba_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1",
        "flow_domain_id": "houwan_nmmba_cache_v3",
    },
    "D": {
        "name_zh": "連江",
        "svd_dir": "lienchiang_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1",
        "flow_domain_id": "lienchiang_common_cache_v3",
    },
}


def _json(path: Path) -> dict[str, Any]:
    """讀取物件型 JSON，將格式錯誤轉成可定位的 audit 失敗。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 必須是 object：{path}")
    return value


def _utc(ns: int) -> str:
    """把 UTC epoch ns 轉成 manifest 可讀的 ISO-8601 字串。"""

    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _stats(values: np.ndarray, *, quantile_sample: np.ndarray | None = None) -> dict[str, Any]:
    """回傳有限值基本統計；空集合以 null 表示而不是以零代替缺值。"""

    finite = np.asarray(values, dtype=np.float64)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return {"count": 0, "min": None, "max": None, "mean": None, "std": None, "p50": None, "p95": None, "p99": None}
    sample = finite if quantile_sample is None else np.asarray(quantile_sample, dtype=np.float64)
    sample = sample[np.isfinite(sample)]
    return {
        "count": int(finite.size),
        "min": float(np.min(finite)),
        "max": float(np.max(finite)),
        "mean": float(np.mean(finite)),
        "std": float(np.std(finite)),
        "p50": float(np.percentile(sample, 50)),
        "p95": float(np.percentile(sample, 95)),
        "p99": float(np.percentile(sample, 99)),
    }


def _append_quantile_sample(sample_parts: list[np.ndarray], values: np.ndarray, limit: int = 200_000) -> None:
    """以固定上限保留每月有限流速值的 deterministic sample。

    exact-land 可能包含數千格點、17,000 個小時；若把全部速度值串接，稽核會無謂
    佔用數百 MB RAM。min/max/mean/std 仍以每月完整值累積，百分位數只使用每月
    均勻抽樣且總量受 limit 控制，manifest 會標明此量化限制。
    """

    finite = np.asarray(values, dtype=np.float64)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return
    stride = max(1, int(math.ceil(finite.size / limit)))
    sample_parts.append(finite[::stride])


def audit_surface_cache_land_values(
    cache_root: Path,
    land_mask: np.ndarray,
    svd_lon: np.ndarray,
    svd_lat: np.ndarray,
) -> dict[str, Any]:
    """逐月掃描同源表層 cache 在 exact-land cell 的有限 u/v 與速度。

    ``u_surface_mps``、``v_surface_mps``、``valid_mask_surface`` 的維度應為
    ``(time, lat, lon)``；只有同時 finite 且 valid 的 u/v 才算物理有效 pair。函式
    同時保留未套用 valid mask 的 finite count，便於辨識「陣列有數值但 QC 標記無效」
    的情況。每月處理完即釋放 view，避免把兩年 cache 載入 RAM。
    """

    grid_dir = cache_root / "grid"
    cache_lon = np.load(grid_dir / "lon.npy", allow_pickle=False).astype(np.float64)
    cache_lat = np.load(grid_dir / "lat.npy", allow_pickle=False).astype(np.float64)
    grid_match = (
        cache_lon.shape == svd_lon.shape
        and cache_lat.shape == svd_lat.shape
        and np.allclose(cache_lon, svd_lon, rtol=0.0, atol=1.0e-9)
        and np.allclose(cache_lat, svd_lat, rtol=0.0, atol=1.0e-9)
    )
    if not grid_match:
        raise ValueError(f"同源 surface cache grid 與 SVD grid 不一致：{cache_root}")

    land_count = int(np.count_nonzero(land_mask))
    total_cell_time = 0
    finite_u_count = 0
    finite_v_count = 0
    valid_pair_count = 0
    valid_mask_count = 0
    time_steps_with_any_pair = 0
    cell_valid_counts = np.zeros(land_count, dtype=np.int64)
    speed_stats_count = 0
    speed_sum = 0.0
    speed_sum_sq = 0.0
    speed_min = float("inf")
    speed_max = float("-inf")
    speed_samples: list[np.ndarray] = []
    month_records: list[dict[str, Any]] = []
    month_dirs = sorted(
        path for path in cache_root.glob("months/20*") if path.is_dir() and path.name.isdigit() and 202401 <= int(path.name) <= 202512
    )
    if not month_dirs:
        raise FileNotFoundError(f"找不到 2024–2025 surface cache 月份：{cache_root}")

    for month_dir in month_dirs:
        month_id = month_dir.name
        metadata_path = month_dir / "metadata.json"
        time_ns = np.load(month_dir / "time_utc_ns.npy", allow_pickle=False).astype(np.int64, copy=False)
        u_array = np.load(month_dir / "u_surface_mps.npy", mmap_mode="r", allow_pickle=False)
        v_array = np.load(month_dir / "v_surface_mps.npy", mmap_mode="r", allow_pickle=False)
        valid_array = np.load(month_dir / "valid_mask_surface.npy", mmap_mode="r", allow_pickle=False)
        expected_shape = (time_ns.size, len(svd_lat), len(svd_lon))
        if u_array.shape != expected_shape or v_array.shape != expected_shape or valid_array.shape != expected_shape:
            raise ValueError(f"{month_id} surface cache shape 不一致：u={u_array.shape}, v={v_array.shape}, valid={valid_array.shape}, expected={expected_shape}")
        u_land = np.asarray(u_array[:, land_mask], dtype=np.float64)
        v_land = np.asarray(v_array[:, land_mask], dtype=np.float64)
        valid_land = np.asarray(valid_array[:, land_mask], dtype=bool)
        finite_u = np.isfinite(u_land)
        finite_v = np.isfinite(v_land)
        pair = finite_u & finite_v
        usable = pair & valid_land
        speed = np.hypot(u_land, v_land)
        usable_speed = speed[usable]
        total_cell_time += int(usable.size)
        finite_u_count += int(np.count_nonzero(finite_u))
        finite_v_count += int(np.count_nonzero(finite_v))
        valid_mask_count += int(np.count_nonzero(valid_land))
        valid_pair_count += int(np.count_nonzero(usable))
        time_steps_with_any_pair += int(np.count_nonzero(np.any(usable, axis=1)))
        cell_valid_counts += np.count_nonzero(usable, axis=0)
        if usable_speed.size:
            speed_stats_count += int(usable_speed.size)
            speed_sum += float(np.sum(usable_speed, dtype=np.float64))
            speed_sum_sq += float(np.sum(np.square(usable_speed), dtype=np.float64))
            speed_min = min(speed_min, float(np.min(usable_speed)))
            speed_max = max(speed_max, float(np.max(usable_speed)))
            _append_quantile_sample(speed_samples, usable_speed)
        month_records.append(
            {
                "month": month_id,
                "time_count": int(time_ns.size),
                "first_utc": _utc(int(time_ns[0])) if time_ns.size else None,
                "last_utc": _utc(int(time_ns[-1])) if time_ns.size else None,
                "finite_u_count_on_exact_land": int(np.count_nonzero(finite_u)),
                "finite_v_count_on_exact_land": int(np.count_nonzero(finite_v)),
                "valid_finite_uv_pair_count_on_exact_land": int(np.count_nonzero(usable)),
                "metadata_sha256": sha256_file(metadata_path) if metadata_path.is_file() else None,
            }
        )
    if speed_stats_count:
        sample = np.concatenate(speed_samples) if speed_samples else np.empty(0, dtype=np.float64)
        speed_mean = speed_sum / speed_stats_count
        speed_variance = max(0.0, speed_sum_sq / speed_stats_count - speed_mean * speed_mean)
        speed_summary = {
            "count": speed_stats_count,
            "min": speed_min,
            "max": speed_max,
            "mean": speed_mean,
            "std": math.sqrt(speed_variance),
            "p50": float(np.percentile(sample, 50)) if sample.size else None,
            "p95": float(np.percentile(sample, 95)) if sample.size else None,
            "p99": float(np.percentile(sample, 99)) if sample.size else None,
            "quantile_method": "deterministic per-month evenly spaced sample; exact count/min/max/mean/std",
        }
    else:
        speed_summary = _stats(np.empty(0, dtype=np.float64))
        speed_summary["quantile_method"] = "empty"
    return {
        "cache_root": str(cache_root),
        "grid_match_to_svd": bool(grid_match),
        "grid_metadata_sha256": sha256_file(grid_dir / "metadata.json") if (grid_dir / "metadata.json").is_file() else None,
        "month_count": len(month_records),
        "time_count": int(total_cell_time // max(land_count, 1)),
        "exact_land_cell_count": land_count,
        "land_cell_time_count": int(total_cell_time),
        "finite_u_count": finite_u_count,
        "finite_v_count": finite_v_count,
        "finite_u_rate": float(finite_u_count / total_cell_time) if total_cell_time else None,
        "finite_v_rate": float(finite_v_count / total_cell_time) if total_cell_time else None,
        "valid_mask_count": valid_mask_count,
        "valid_finite_uv_pair_count": valid_pair_count,
        "valid_finite_uv_pair_rate": float(valid_pair_count / total_cell_time) if total_cell_time else None,
        "time_steps_with_any_valid_finite_uv_pair": time_steps_with_any_pair,
        "cell_valid_time_count_stats": _stats(cell_valid_counts.astype(np.float64)),
        "speed_mps_stats_on_valid_finite_uv_pairs": speed_summary,
        "months": month_records,
    }


def _variance_proxy(
    svd_dir: Path,
    surface_feature_mask: np.ndarray,
    land_mask: np.ndarray,
    cell_area: np.ndarray,
    surface_weight_m: float,
) -> dict[str, Any]:
    """計算 SVD 已保存模態在表層 u/v 的 land variance proxy。

    ``regression_*_per_pc_std`` 是每一個標準化 PC 單位對物理 u/v 的 loading；跨
    mode 平方和可作為已保存模態表示的逐 feature 變異 proxy。它不等於原始 raw
    feature matrix 的總變異，因為 raw matrix 在正式 run 完成後已移除；因此結果只作
    污染量級與重算前後比較，不作獨立統計結論。
    """

    u_path = svd_dir / "regression_u_mps_per_pc_std.npy"
    v_path = svd_dir / "regression_v_mps_per_pc_std.npy"
    if not u_path.is_file() or not v_path.is_file():
        return {"available": False, "reason": "regression_u/v arrays missing"}
    regression_u = np.load(u_path, mmap_mode="r", allow_pickle=False)
    regression_v = np.load(v_path, mmap_mode="r", allow_pickle=False)
    u_surface = np.asarray(regression_u[:, 0], dtype=np.float64)
    v_surface = np.asarray(regression_v[:, 0], dtype=np.float64)
    u_var = np.nansum(np.square(u_surface), axis=0)
    v_var = np.nansum(np.square(v_surface), axis=0)
    total_feature = surface_feature_mask & np.isfinite(u_var) & np.isfinite(v_var)
    land_feature = land_mask & total_feature
    unweighted_total = float(np.sum(u_var[total_feature] + v_var[total_feature]))
    unweighted_land = float(np.sum(u_var[land_feature] + v_var[land_feature]))
    weighted = cell_area * float(surface_weight_m)
    weighted_total = float(np.sum((u_var[total_feature] + v_var[total_feature]) * weighted[total_feature]))
    weighted_land = float(np.sum((u_var[land_feature] + v_var[land_feature]) * weighted[land_feature]))
    return {
        "available": True,
        "mode_count": int(u_surface.shape[0]),
        "definition": "sum over saved modes of regression_u/v_mps_per_pc_std squared; represented-mode variance proxy, not raw total variance",
        "unweighted_physical_variance_proxy_total": unweighted_total,
        "unweighted_physical_variance_proxy_exact_land": unweighted_land,
        "unweighted_physical_variance_proxy_exact_land_fraction": float(unweighted_land / unweighted_total) if unweighted_total > 0 else None,
        "area_weighted_surface_variance_proxy_total": weighted_total,
        "area_weighted_surface_variance_proxy_exact_land": weighted_land,
        "area_weighted_surface_variance_proxy_exact_land_fraction": float(weighted_land / weighted_total) if weighted_total > 0 else None,
    }


def audit_region(
    key: str,
    spec: dict[str, str],
    *,
    svd_base: Path,
    surface_cache_base: Path,
    coastline_path: Path,
) -> dict[str, Any]:
    """完成單區格網、遮罩、cache 有限值與 SVD 變異 proxy 稽核。"""

    svd_dir = svd_base / spec["svd_dir"]
    cache_root = surface_cache_base / spec["flow_domain_id"]
    config = _json(svd_dir / "config.json")
    metadata = _json(svd_dir / "metadata.json")
    lon = np.load(svd_dir / "lon.npy", allow_pickle=False).astype(np.float64)
    lat = np.load(svd_dir / "lat.npy", allow_pickle=False).astype(np.float64)
    static = np.load(svd_dir / "static_ocean_mask.npy", allow_pickle=False).astype(bool)
    geometry = np.load(svd_dir / "analysis_geometry_mask.npy", allow_pickle=False).astype(bool)
    velocity_all = np.load(svd_dir / "velocity_feature_mask.npy", allow_pickle=False).astype(bool)
    surface_feature = velocity_all[0]
    expected_shape = (len(lat), len(lon))
    if static.shape != expected_shape or geometry.shape != expected_shape or surface_feature.shape != expected_shape:
        raise ValueError(f"{key} mask shape 不一致：static={static.shape}, geometry={geometry.shape}, surface={surface_feature.shape}, expected={expected_shape}")
    land, coastline_summary = build_coastline_land_mask(lon, lat, coastline_path)
    cache_result = audit_surface_cache_land_values(cache_root, land, lon, lat)
    surface_weight = float(config.get("vertical_sampling", {}).get("vertical_quadrature_weights_m", [5.0])[0])
    variance_proxy = _variance_proxy(
        svd_dir,
        surface_feature,
        land,
        np.load(svd_dir / "cell_area_m2.npy", allow_pickle=False).astype(np.float64),
        surface_weight,
    )
    exact_land_surface_feature_cells = int(np.count_nonzero(land & surface_feature))
    total_surface_feature_cells = int(np.count_nonzero(surface_feature))
    finite_land_pair = int(cache_result["valid_finite_uv_pair_count"])
    return {
        "region_key": key,
        "region_name_zh": spec["name_zh"],
        "svd_run_dir": str(svd_dir),
        "source_cache_root": str(cache_root),
        "grid": {
            "shape_lat_lon": [int(len(lat)), int(len(lon))],
            "bbox_lonlat": [float(lon[0]), float(lon[-1]), float(lat[0]), float(lat[-1])],
            "static_ocean_cell_count": int(np.count_nonzero(static)),
            "analysis_geometry_cell_count": int(np.count_nonzero(geometry)),
            "surface_velocity_feature_cell_count": total_surface_feature_cells,
        },
        "coastline": coastline_summary,
        "mask_intersections": {
            "exact_land_cell_count": int(np.count_nonzero(land)),
            "exact_land_fraction": float(np.mean(land)),
            "exact_land_and_static_ocean_cell_count": int(np.count_nonzero(land & static)),
            "exact_land_and_analysis_geometry_cell_count": int(np.count_nonzero(land & geometry)),
            "exact_land_and_surface_velocity_feature_cell_count": exact_land_surface_feature_cells,
            "exact_land_in_surface_velocity_feature_fraction": float(exact_land_surface_feature_cells / total_surface_feature_cells) if total_surface_feature_cells else None,
            "semantic_cell_counts": {
                "real_exact_land": int(np.count_nonzero(land)),
                "analysis_geometry_outside": int(np.count_nonzero(~geometry)),
                "model_static_outside_inside_analysis_geometry": int(np.count_nonzero(geometry & ~static)),
                "surface_feature_unavailable_inside_geometry_static": int(np.count_nonzero(geometry & static & ~surface_feature)),
                "usable_surface_feature_after_exact_land_correction": int(np.count_nonzero(geometry & static & surface_feature & ~land)),
            },
        },
        "source_cache_exact_land_values": cache_result,
        "svd_surface_feature_pollution": {
            "surface_velocity_feature_count_including_u_v": total_surface_feature_cells * 2,
            "exact_land_surface_velocity_feature_count_including_u_v": exact_land_surface_feature_cells * 2,
            "finite_valid_uv_pair_count_on_exact_land": finite_land_pair,
            "exact_land_has_finite_valid_uv": bool(finite_land_pair > 0),
            "exact_land_in_original_svd_surface_feature_mask": bool(exact_land_surface_feature_cells > 0),
            "pollution_decision": "upstream_surface_feature_pollution" if exact_land_surface_feature_cells > 0 and finite_land_pair > 0 else "no_finite_included_surface_pollution_detected",
            "variance_proxy": variance_proxy,
        },
        "svd_arrays": {
            "metadata_analysis_label": metadata.get("analysis_label"),
            "metadata_science_provenance_sha256": metadata.get("science_provenance_sha256"),
            "pc_time_count": int(np.load(svd_dir / "pc.npy", mmap_mode="r", allow_pickle=False).shape[1]),
            "mode_count": int(np.load(svd_dir / "pc.npy", mmap_mode="r", allow_pickle=False).shape[0]),
            "cumulative_explained_variance_first4_percent": float(np.load(svd_dir / "cumulative_explained_variance.npy", allow_pickle=False)[3] * 100.0),
        },
    }


def write_csv(path: Path, regions: list[dict[str, Any]]) -> None:
    """寫出每區一列的扁平 CSV 摘要，方便研究者在試算表中篩查污染指標。"""

    fields = [
        "region_key",
        "region_name_zh",
        "exact_land_cell_count",
        "exact_land_fraction",
        "exact_land_and_static_ocean_cell_count",
        "exact_land_and_analysis_geometry_cell_count",
        "exact_land_and_surface_velocity_feature_cell_count",
        "exact_land_in_surface_velocity_feature_fraction",
        "finite_valid_uv_pair_count_on_exact_land",
        "exact_land_has_finite_valid_uv",
        "pollution_decision",
        "surface_velocity_feature_count_including_u_v",
        "exact_land_surface_velocity_feature_count_including_u_v",
        "valid_finite_uv_pair_rate_on_exact_land",
        "time_steps_with_any_valid_finite_uv_pair",
        "speed_p95_mps_on_exact_land",
        "speed_max_mps_on_exact_land",
        "area_weighted_land_variance_proxy_fraction",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for region in regions:
            mask = region["mask_intersections"]
            cache = region["source_cache_exact_land_values"]
            pollution = region["svd_surface_feature_pollution"]
            speed = cache["speed_mps_stats_on_valid_finite_uv_pairs"]
            variance = pollution["variance_proxy"]
            writer.writerow(
                {
                    "region_key": region["region_key"],
                    "region_name_zh": region["region_name_zh"],
                    "exact_land_cell_count": mask["exact_land_cell_count"],
                    "exact_land_fraction": mask["exact_land_fraction"],
                    "exact_land_and_static_ocean_cell_count": mask["exact_land_and_static_ocean_cell_count"],
                    "exact_land_and_analysis_geometry_cell_count": mask["exact_land_and_analysis_geometry_cell_count"],
                    "exact_land_and_surface_velocity_feature_cell_count": mask["exact_land_and_surface_velocity_feature_cell_count"],
                    "exact_land_in_surface_velocity_feature_fraction": mask["exact_land_in_surface_velocity_feature_fraction"],
                    "finite_valid_uv_pair_count_on_exact_land": pollution["finite_valid_uv_pair_count_on_exact_land"],
                    "exact_land_has_finite_valid_uv": pollution["exact_land_has_finite_valid_uv"],
                    "pollution_decision": pollution["pollution_decision"],
                    "surface_velocity_feature_count_including_u_v": pollution["surface_velocity_feature_count_including_u_v"],
                    "exact_land_surface_velocity_feature_count_including_u_v": pollution["exact_land_surface_velocity_feature_count_including_u_v"],
                    "valid_finite_uv_pair_rate_on_exact_land": cache["valid_finite_uv_pair_rate"],
                    "time_steps_with_any_valid_finite_uv_pair": cache["time_steps_with_any_valid_finite_uv_pair"],
                    "speed_p95_mps_on_exact_land": speed["p95"],
                    "speed_max_mps_on_exact_land": speed["max"],
                    "area_weighted_land_variance_proxy_fraction": variance.get("area_weighted_surface_variance_proxy_exact_land_fraction") if variance.get("available") else None,
                }
            )


def main() -> None:
    """執行四區 audit，並把污染判定與全部中間指標寫成 JSON/CSV。"""

    parser = argparse.ArgumentParser(description="Audit exact coastline land cells against OCM SVD surface features.")
    parser.add_argument("--svd-base", type=Path, required=True)
    parser.add_argument("--surface-cache-base", type=Path, required=True)
    parser.add_argument("--coastline-geojson", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--regions", default="A,B,C,D")
    args = parser.parse_args()
    selected = [item.strip().upper() for item in args.regions.split(",") if item.strip()]
    unknown = [item for item in selected if item not in REGIONS]
    if unknown:
        raise ValueError(f"未知區域：{unknown}")
    _document, coastline_summary = __import__("coastline_utils").load_coastline_geojson(args.coastline_geojson)
    regions = [
        audit_region(
            key,
            REGIONS[key],
            svd_base=args.svd_base,
            surface_cache_base=args.surface_cache_base,
            coastline_path=args.coastline_geojson,
        )
        for key in selected
    ]
    decision_values = [region["svd_surface_feature_pollution"]["pollution_decision"] for region in regions]
    manifest = {
        "schema_name": "ocm_svd_coastline_svd_land_audit",
        "schema_version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "coastline_source": coastline_summary,
        "svd_base": str(args.svd_base.resolve()),
        "surface_cache_base": str(args.surface_cache_base.resolve()),
        "regions": regions,
        "decision": {
            "any_upstream_surface_feature_pollution": any(value == "upstream_surface_feature_pollution" for value in decision_values),
            "region_decisions": {region["region_key"]: region["svd_surface_feature_pollution"]["pollution_decision"] for region in regions},
            "rule": "exact-land cell is upstream pollution candidate only when it is in original surface feature mask and same-source cache has finite valid u/v on exact-land cells",
        },
        "limitations": [
            "exact coastline mask uses conservative cell-center/corners/ring-vertex contact, not polygon area fraction",
            "surface cache speed quantiles use deterministic capped samples; count/min/max/mean/std are accumulated from all valid values",
            "regression variance is a saved-mode representation proxy because the original raw feature matrix is not retained in the SVD result",
            "analysis geometry outside, static model outside, feature unavailable and exact real land are reported as separate semantic masks",
        ],
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "coastline_svd_land_audit.json"
    csv_path = args.output_dir / "coastline_svd_land_audit.csv"
    json_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(csv_path, regions)
    print(json.dumps({"json": str(json_path), "csv": str(csv_path), "decision": manifest["decision"]}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

