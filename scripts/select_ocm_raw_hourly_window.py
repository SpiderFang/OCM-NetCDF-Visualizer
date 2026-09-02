#!/usr/bin/env python3
"""從 2024–2025 原始表層產品中選取四區共同三日小時觀測時窗。

本模組只負責「選窗」，不會建立新的流場值，也不會修改既有 `.npy` 或 NetCDF。
目前 SERVER 已有的正式全臺產品是每 6 小時一格，因此本工具先使用該產品的
source-valid、非 imputed 時間與原始表層 `u_surface/v_surface` 做候選評分，再把
候選起始日期交給後續的 hourly NetCDF 前處理。真正交付的 72 個動畫影格仍由
原始日 NetCDF 的 72 個 hourly `hvel` time index 產生，並不使用這裡的 6 小時
欄位內插。

選窗語意如下：

* 每個候選從每日 OCM 第一個標準時間 `01:00 UTC` 開始，連續涵蓋三個原始日檔，
  取 `01:00–24:00 UTC` 的 72 個實際小時觀測影格。
* 候選必須在四區共同的全臺 6 小時 source-valid、非 imputed 時間中保留 12 個
  6 小時錨點，且三個原始日檔都存在；後續前處理還會再次驗證每檔 24 個小時步。
* 「代表性較高」以四區平均流速、東西／南北流速在 12 個 6 小時抽樣中的相對
  變化評分。這是展示選段規則，不是潮汐分離、潮汐調和分析或全年統計結論。

輸出的 JSON 只保存候選分數、時間與檔案存在性，供前處理、renderer 與 manifest
追溯；不寫入原始產品目錄，也不覆寫既有選窗檔案。
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np


NANOS_PER_HOUR = 3_600_000_000_000
"""一小時的 epoch 奈秒數；選窗時只使用整點 UTC 時間。"""

SOURCE_INTERVAL_HOURS = 6
"""既有全臺選窗產品的時間間隔；不是本次動畫的 hourly 輸出間隔。"""

HOURLY_FRAME_COUNT = 72
"""三個完整原始日檔的 24×3 個實際小時觀測影格。"""

SOURCE_ANCHOR_COUNT = 12
"""每個三日候選中的 6 小時 source anchor 數量，用於選窗評分與有效性檢查。"""

REGION_READ_CHUNK = 24
"""選窗讀取 6 小時產品時的時間區塊大小，避免把兩年完整網格放入記憶體。"""

REGION_DISPLAY_EXTENTS = {
    "A": (121.3, 122.8, 24.6, 25.5),
    "B": (119.7, 121.2, 24.3, 25.2),
    "C": (120.2, 121.6, 21.6, 22.4),
    "D": (119.2, 120.7, 25.8, 26.6),
}
"""四區沿用簡報第 6–9 頁的展示範圍，只用於選窗的空間統計。"""

REGION_ORDER = ("A", "B", "C", "D")
"""固定四區排序，確保同一候選在不同執行環境得到相同輸出順序。"""


def _parse_epoch_ns(value: Any) -> int:
    """將全臺產品的 ISO UTC 時間轉為 epoch ns。

    `time_iso.npy` 是前處理後的標準時間軸；這裡只解析既有字串，不修正、排序或
    補洞。若時間無法解析就立即失敗，避免把錯誤時間拿來挑選 raw NetCDF 日檔。
    """

    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return int(np.datetime64(text, "ns").astype("int64"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"無法解析全臺產品時間 {value!r}") from exc


def _format_utc(time_ns: int) -> str:
    """以固定 UTC 格式輸出時間，供 JSON 與人工選窗檢查使用。"""

    value = datetime.fromtimestamp(int(time_ns) / 1_000_000_000.0, tz=timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _sha256_file(path: Path) -> str:
    """以串流方式計算輸入產品 metadata 的 SHA-256，避免複製大型陣列。"""

    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _crop_indices(lon: np.ndarray, lat: np.ndarray, extent: tuple[float, float, float, float]) -> tuple[np.ndarray, np.ndarray]:
    """取得覆蓋簡報 display extent 的規則格點索引。

    選窗統計不需要重新內插；只從全臺 1 km 規則格點取每隔數格的樣本。額外保留
    一個格點邊界的空間緩衝，避免候選分數因端點剛好落在格點中心外而少算一列。
    """

    lon_values = np.asarray(lon, dtype=np.float64)
    lat_values = np.asarray(lat, dtype=np.float64)
    if lon_values.ndim != 1 or lat_values.ndim != 1 or lon_values.size < 2 or lat_values.size < 2:
        raise ValueError(f"全臺 lon/lat 必須是一維且至少兩格：lon={lon_values.shape}, lat={lat_values.shape}")
    lon_step = float(np.median(np.diff(lon_values)))
    lat_step = float(np.median(np.diff(lat_values)))
    if not math.isfinite(lon_step) or not math.isfinite(lat_step) or lon_step <= 0.0 or lat_step <= 0.0:
        raise ValueError("全臺 lon/lat 必須嚴格遞增")
    lon_min, lon_max, lat_min, lat_max = extent
    lon_indices = np.flatnonzero((lon_values >= lon_min - lon_step) & (lon_values <= lon_max + lon_step))
    lat_indices = np.flatnonzero((lat_values >= lat_min - lat_step) & (lat_values <= lat_max + lat_step))
    if lon_indices.size < 2 or lat_indices.size < 2:
        raise ValueError(f"展示範圍沒有足夠全臺格點：extent={extent}")
    return lon_indices, lat_indices


def _daily_file_paths(source_root: Path, start_time_ns: int) -> list[Path]:
    """建立候選三日對應的原始 NetCDF 日檔路徑。

    檔名日期是 OCM 原始資料修復時間偏移時的權威日期來源；這裡只檢查檔案存在，
    每檔的 24 個 hourly time 維度仍由真正前處理流程重新讀取與驗證。
    """

    start = datetime.fromtimestamp(int(start_time_ns) / 1_000_000_000.0, tz=timezone.utc).date()
    return [
        source_root / str(current.year) / f"{current:%Y%m%d}_schout.nc"
        for current in (start + timedelta(days=offset) for offset in range(3))
    ]


def _candidate_anchor_indices(
    time_ns: np.ndarray,
    valid_non_imputed: np.ndarray,
    start_index: int,
    time_to_index: dict[int, int] | None = None,
) -> list[int] | None:
    """檢查候選是否含 12 個精確 6 小時有效錨點。

    `time_to_index` 由呼叫端共用，避免候選逐一建立相同的時間索引字典；這對兩年
    產品的數千個候選只影響選窗效能，不會改變候選的時間定義或有效性判定。
    """

    start_ns = int(time_ns[start_index])
    indices: list[int] = []
    if time_to_index is None:
        time_to_index = {int(value): index for index, value in enumerate(time_ns.tolist())}
    for anchor in range(SOURCE_ANCHOR_COUNT):
        index = time_to_index.get(start_ns + anchor * SOURCE_INTERVAL_HOURS * NANOS_PER_HOUR)
        if index is None or not bool(valid_non_imputed[index]):
            return None
        indices.append(int(index))
    return indices


def _preload_region_series(
    u_source: np.ndarray,
    v_source: np.ndarray,
    static_mask: np.ndarray,
    lon_indices: np.ndarray,
    lat_indices: np.ndarray,
    spatial_stride: int,

) -> dict[str, np.ndarray]:
    """以小型時間區塊預先計算單區抽樣統計，避免反覆觸發大型 memmap I/O。

    選窗只需要四區 display extent 內每隔數格的 1 km 原始 `u/v`。若直接把
    兩年所有抽樣格點存成陣列，某些 NumPy／memmap 組合仍可能讓作業系統保留
    過大的檔案頁面；因此改為每次只讀 24 個時間步，立即壓縮成每時刻統計量。
    這些統計只服務「候選評分」，不會寫回產品，也不會拿來產生動畫影格。

    回傳欄位的維度與單位如下：
    * `mean_u`、`mean_v`、`mean_speed`：`(time,)`，分別為 m/s 的區域平均。
    * `finite_fraction`：`(time,)`，抽樣格點中同時具有限 u/v 的比例。
    * `spatial_speed_std`：`(time,)`，每個時間步的區域速度空間標準差，單位 m/s。
    """

    sampled_lon = lon_indices[:: max(1, int(spatial_stride))]
    sampled_lat = lat_indices[:: max(1, int(spatial_stride))]
    if sampled_lon.size < 2 or sampled_lat.size < 2:
        raise ValueError("選窗空間抽樣後格點不足")

    mask = np.asarray(static_mask[np.ix_(sampled_lat, sampled_lon)], dtype=bool)
    time_count = int(u_source.shape[0])
    mean_u = np.full(time_count, np.nan, dtype=np.float64)
    mean_v = np.full(time_count, np.nan, dtype=np.float64)
    mean_speed = np.full(time_count, np.nan, dtype=np.float64)
    finite_fraction = np.zeros(time_count, dtype=np.float64)
    spatial_speed_std = np.full(time_count, np.nan, dtype=np.float64)

    # 逐小區塊讀取能保留連續 I/O 的效率，同時把峰值記憶體限制在數十個
    # 抽樣影格；影片實際 72 個 raw hourly frame 仍會由後續前處理重新讀 NetCDF。
    for start in range(0, time_count, REGION_READ_CHUNK):
        stop = min(start + REGION_READ_CHUNK, time_count)
        u_values = np.asarray(
            u_source[start:stop, sampled_lat[:, None], sampled_lon],
            dtype=np.float32,
        )
        v_values = np.asarray(
            v_source[start:stop, sampled_lat[:, None], sampled_lon],
            dtype=np.float32,
        )
        valid = mask[None, :, :] & np.isfinite(u_values) & np.isfinite(v_values)
        u_values = np.where(valid, u_values, np.nan)
        v_values = np.where(valid, v_values, np.nan)
        speed_values = np.hypot(u_values, v_values)
        valid_count = np.count_nonzero(valid, axis=(1, 2)).astype(np.float64)
        mean_u[start:stop] = np.divide(
            np.nansum(u_values, axis=(1, 2), dtype=np.float64),
            valid_count,
            out=np.full(valid_count.shape, np.nan, dtype=np.float64),
            where=valid_count > 0,
        )
        mean_v[start:stop] = np.divide(
            np.nansum(v_values, axis=(1, 2), dtype=np.float64),
            valid_count,
            out=np.full(valid_count.shape, np.nan, dtype=np.float64),
            where=valid_count > 0,
        )
        mean_speed[start:stop] = np.divide(
            np.nansum(speed_values, axis=(1, 2), dtype=np.float64),
            valid_count,
            out=np.full(valid_count.shape, np.nan, dtype=np.float64),
            where=valid_count > 0,
        )
        finite_fraction[start:stop] = valid_count / float(valid.shape[1] * valid.shape[2])
        with np.errstate(invalid="ignore", divide="ignore"):
            spatial_speed_std[start:stop] = np.nanstd(speed_values, axis=(1, 2))
    return {
        "mean_u": mean_u,
        "mean_v": mean_v,
        "mean_speed": mean_speed,
        "finite_fraction": finite_fraction,
        "spatial_speed_std": spatial_speed_std,
    }


def _region_candidate_metric(
    series: dict[str, np.ndarray],
    anchor_indices: list[int],
) -> dict[str, float]:
    """由預先讀取的單區時間序列計算候選相對流場變化指標。

    `mean_u/v/speed` 描述區域平均流場，`spatial_speed_std` 保留每個錨點的區域
    空間差異。指標僅用來挑選 PI 可檢查的代表時窗，不是潮汐調和分析、全年統計，
    也不是後續影片的平滑或內插資料。
    """

    indices = np.asarray(anchor_indices, dtype=np.int64)
    mean_u = np.asarray(series["mean_u"][indices], dtype=np.float64)
    mean_v = np.asarray(series["mean_v"][indices], dtype=np.float64)
    mean_speed = np.asarray(series["mean_speed"][indices], dtype=np.float64)
    spatial_speed_std_values = np.asarray(series["spatial_speed_std"][indices], dtype=np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        background_speed = float(np.nanmean(mean_speed))
        vector_variation = float(np.hypot(np.nanstd(mean_u), np.nanstd(mean_v)))
        speed_variation = float(np.nanstd(mean_speed))
        spatial_temporal_speed_std = float(np.nanmean(spatial_speed_std_values))
        relative_variation = (
            vector_variation + speed_variation + 0.5 * spatial_temporal_speed_std
        ) / max(background_speed, 0.05)
    finite_fraction = float(np.mean(series["finite_fraction"][indices]))
    values = (relative_variation, background_speed, finite_fraction)
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"候選流場統計包含非有限值：{values}")
    return {
        "relative_variation_index": relative_variation,
        "mean_speed_mps": background_speed,
        "finite_fraction": finite_fraction,
        "mean_u_std_mps": float(np.nanstd(mean_u)),
        "mean_v_std_mps": float(np.nanstd(mean_v)),
        "mean_speed_std_mps": speed_variation,
        "spatial_temporal_speed_std_mps": spatial_temporal_speed_std,
    }


def select_window(
    full_product_dir: Path,
    source_root: Path,
    output_path: Path,
    spatial_stride: int = 4,
) -> dict[str, Any]:
    """選取並寫出四區共同三日實際 hourly NetCDF 時窗。"""

    metadata_path = full_product_dir / "metadata.json"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"找不到全臺產品 metadata：{metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    time_step = metadata.get("time_axis", {}).get("time_step_hours")
    if time_step != SOURCE_INTERVAL_HOURS:
        raise ValueError(f"選窗輸入必須是既有 6 小時全臺產品，收到 time_step_hours={time_step!r}")

    time_ns = np.asarray([_parse_epoch_ns(value) for value in np.load(full_product_dir / "time_iso.npy", allow_pickle=False)], dtype=np.int64)
    source_valid = np.load(full_product_dir / "source_valid.npy", allow_pickle=False).astype(bool)
    imputed = np.load(full_product_dir / "imputed.npy", allow_pickle=False).astype(bool)
    valid_non_imputed = source_valid & ~imputed
    u_source = np.load(full_product_dir / "u_surface.npy", mmap_mode="r")
    v_source = np.load(full_product_dir / "v_surface.npy", mmap_mode="r")
    static_mask = np.load(full_product_dir / "mask.npy", allow_pickle=False).astype(bool)
    lon = np.load(full_product_dir / "lon.npy", allow_pickle=False).astype(np.float64)
    lat = np.load(full_product_dir / "lat.npy", allow_pickle=False).astype(np.float64)
    expected = (time_ns.size, lat.size, lon.size)
    if tuple(u_source.shape) != expected or tuple(v_source.shape) != expected or static_mask.shape != (lat.size, lon.size):
        raise ValueError(
            f"全臺產品陣列 shape 不一致：u={u_source.shape}, v={v_source.shape}, "
            f"mask={static_mask.shape}, expected={expected}"
        )

    region_indices = {
        key: _crop_indices(lon, lat, REGION_DISPLAY_EXTENTS[key])
        for key in REGION_ORDER
    }
    time_to_index = {int(value): index for index, value in enumerate(time_ns.tolist())}

    # 先把四區抽樣時間序列讀入記憶體。原先逐候選讀取 12 個影格會讓兩年
    # memmap 產生大量隨機 I/O；快取後的評分仍使用完全相同的 mask、空間抽樣與
    # 指標公式，但執行時間較穩定，且不改變選窗結果的科學語意。
    region_series = {
        key: _preload_region_series(
            u_source,
            v_source,
            static_mask,
            region_indices[key][0],
            region_indices[key][1],
            spatial_stride,
        )
        for key in REGION_ORDER
    }
    candidates: list[dict[str, Any]] = []
    for index, start_ns in enumerate(time_ns.tolist()):
        dt = datetime.fromtimestamp(int(start_ns) / 1_000_000_000.0, tz=timezone.utc)
        if dt.hour != 1 or dt.minute != 0 or dt.second != 0:
            continue
        anchor_indices = _candidate_anchor_indices(
            time_ns,
            valid_non_imputed,
            index,
            time_to_index,
        )
        if anchor_indices is None:
            continue
        daily_files = _daily_file_paths(source_root, int(start_ns))
        if not all(path.is_file() for path in daily_files):
            continue
        region_metrics: dict[str, dict[str, float]] = {}
        for key in REGION_ORDER:
            region_metrics[key] = _region_candidate_metric(region_series[key], anchor_indices)
        combined_score = float(
            np.mean([region_metrics[key]["relative_variation_index"] for key in REGION_ORDER])
        )
        mean_speed_score = float(np.mean([region_metrics[key]["mean_speed_mps"] for key in REGION_ORDER]))
        candidates.append(
            {
                "start_time_ns": int(start_ns),
                "start_utc": _format_utc(int(start_ns)),
                "end_display_time_ns": int(start_ns + (HOURLY_FRAME_COUNT - 1) * NANOS_PER_HOUR),
                "end_display_utc": _format_utc(int(start_ns + (HOURLY_FRAME_COUNT - 1) * NANOS_PER_HOUR)),
                "source_anchor_start_index": int(anchor_indices[0]),
                "source_anchor_end_index": int(anchor_indices[-1]),
                "source_anchor_indices": anchor_indices,
                "daily_files": [str(path) for path in daily_files],
                "combined_relative_variation_score": combined_score,
                "combined_mean_speed_mps": mean_speed_score,
                "regions": region_metrics,
            }
        )

    if not candidates:
        raise RuntimeError("找不到四區共同、三日連續且具 12 個有效 6 小時錨點的候選時窗")
    selected = max(
        candidates,
        key=lambda item: (
            item["combined_relative_variation_score"],
            item["combined_mean_speed_mps"],
            -item["start_time_ns"],
        ),
    )
    selected_start_ns = int(selected["start_time_ns"])
    selected_start_index = time_to_index[selected_start_ns]
    selected_anchor_indices = [int(value) for value in selected["source_anchor_indices"]]
    expected_hourly_times = [selected_start_ns + offset * NANOS_PER_HOUR for offset in range(HOURLY_FRAME_COUNT)]
    output = {
        "schema_name": "ocm_raw_surface_hourly_three_day_window_selection",
        "schema_version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "selection_scope": "four_region_shared_three_calendar_day_window",
        "selection_rule": (
            "候選從每日 01:00 UTC 開始，要求四區共同 6 小時 source-valid/non-imputed "
            "12-anchor 時序與三個原始日 NetCDF；首要排序為四區平均區域相對流場變化，"
            "次排序為四區平均流速，最後取較早 UTC。這是展示選窗規則，不是潮汐調和分析。"
        ),
        "source_full_product": {
            "directory": str(full_product_dir.resolve()),
            "metadata_sha256": _sha256_file(metadata_path),
            "time_step_hours": SOURCE_INTERVAL_HOURS,
            "source_valid_count": int(np.count_nonzero(source_valid)),
            "imputed_count": int(np.count_nonzero(imputed)),
            "valid_non_imputed_count": int(np.count_nonzero(valid_non_imputed)),
        },
        "raw_netcdf_source": {
            "root": str(source_root.resolve()),
            "daily_file_pattern": "YYYYMMDD_schout.nc",
            "expected_time_count_per_file": 24,
            "expected_time_interval_hours": 1,
            "time_label_convention": "filename date + source hourly index 0..23 => 01:00..24:00 UTC",
        },
        "target_animation_window": {
            "definition": "72 consecutive actual hourly observations across three source daily files",
            "frame_count": HOURLY_FRAME_COUNT,
            "display_interval_hours": 1,
            "start_utc": selected["start_utc"],
            "end_utc": selected["end_display_utc"],
            "endpoint_span_hours": HOURLY_FRAME_COUNT - 1,
            "calendar_source_day_count": 3,
            "expected_time_ns": expected_hourly_times,
            "source_6h_anchor_count": SOURCE_ANCHOR_COUNT,
            "source_6h_anchor_start_utc": _format_utc(int(time_ns[selected_anchor_indices[0]])),
            "source_6h_anchor_end_utc": _format_utc(int(time_ns[selected_anchor_indices[-1]])),
        },
        "selected_candidate": selected,
        "candidate_count": len(candidates),
        "candidate_start_index_in_full_6h_product": int(selected_start_index),
        "spatial_sampling": {
            "grid_source": str(full_product_dir / "lon.npy"),
            "spatial_stride": int(spatial_stride),
            "region_display_extents": {key: list(REGION_DISPLAY_EXTENTS[key]) for key in REGION_ORDER},
        },
        "verification": {
            "selected_6h_anchors_all_source_valid_non_imputed": True,
            "selected_daily_files_exist": all(Path(path).is_file() for path in selected["daily_files"]),
            "hourly_values_not_created_by_this_script": True,
            "requires_netCDF_hourly_dimension_validation_before_render": True,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def parse_args() -> argparse.Namespace:
    """解析選窗工具的唯讀輸入與 JSON 輸出路徑。"""

    parser = argparse.ArgumentParser(description="Select a shared three-day actual-hourly OCM raw-current window.")
    parser.add_argument("--full-product-dir", type=Path, required=True, help="existing 6-hour full Taiwan product")
    parser.add_argument("--source-root", type=Path, required=True, help="raw NetCDF root such as /CWA-OCM")
    parser.add_argument("--output", type=Path, required=True, help="new selection JSON path")
    parser.add_argument("--spatial-stride", type=int, default=4, help="grid stride used only for candidate scoring")
    return parser.parse_args()


def main() -> None:
    """執行唯讀選窗並輸出可追溯的候選摘要。"""

    args = parse_args()
    if args.spatial_stride <= 0:
        raise ValueError("spatial-stride 必須為正整數")
    result = select_window(
        full_product_dir=args.full_product_dir,
        source_root=args.source_root,
        output_path=args.output,
        spatial_stride=args.spatial_stride,
    )
    print(json.dumps({
        "output": str(args.output.resolve()),
        "selected_start_utc": result["target_animation_window"]["start_utc"],
        "selected_end_utc": result["target_animation_window"]["end_utc"],
        "candidate_count": result["candidate_count"],
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
