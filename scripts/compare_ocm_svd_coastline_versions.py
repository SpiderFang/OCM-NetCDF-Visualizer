#!/usr/bin/env python3
"""比較原始 v1 與 coastline-corrected v2 水柱 SVD 的科學指標。

本程式不重新求解 SVD，也不修改任一結果；它讀取兩個版本保存的 cumulative
explained variance、K90、PC、raw mode/mean 與時間軸，並以同一份全臺 1 km 6 小時
產品的 source-valid/non-imputed 精確時間交集重做代表性 PC1 正/負相位視窗選取。
因此 C pilot 與四區正式比較不會把「不同缺值時間」誤當成模態差異。

表層重建仍遵循 ``mean + sum(mode_per_raw_pc * pc_raw)``；標準化 PC 只用來選取與
報告相位，不會拿來乘 raw-PC loading。輸出包含新舊 K90、前四模態累積解釋變異、
視窗起訖/中心與中心重建場速度摘要。重建場摘要是 QA 指標，不宣稱統計驗證，也
不取代正式動畫的逐幀地理遮罩檢查。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from visualize_ocm_svd_modal_context import COMMON_INTERVAL_HOURS, NANOS_PER_HOUR, WINDOW_FRAME_COUNT, parse_epoch_ns


REGIONS = {
    "A": "guishan_gongliao_northeast_taiwan_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1",
    "B": "hsinchu_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1",
    "C": "houwan_nmmba_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1",
    "D": "lienchiang_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1",
}


def _json(path: Path) -> dict[str, Any]:
    """讀取物件型 JSON；比較結果需要保留原始 metadata 可追溯性。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根節點必須是 object：{path}")
    return value


def _utc(ns: int) -> str:
    """將 epoch ns 轉成不受本機時區影響的 UTC ISO 字串。"""

    return np.datetime_as_string(np.datetime64(int(ns), "ns"), unit="h").replace("T", " ") + " UTC"


def _stats(values: np.ndarray) -> dict[str, Any]:
    """輸出有限速度值的摘要；空集合保持 null，避免把缺值當零。"""

    finite = np.asarray(values, dtype=np.float64)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return {"count": 0, "min": None, "max": None, "mean": None, "p95": None}
    return {
        "count": int(finite.size),
        "min": float(np.min(finite)),
        "max": float(np.max(finite)),
        "mean": float(np.mean(finite)),
        "p95": float(np.percentile(finite, 95)),
    }


def _common_time(svd_dir: Path, full_product_dir: Path, cache_root: Path) -> tuple[np.ndarray, np.ndarray]:
    """建立 SVD 時間軸與全臺 source-valid/non-imputed 6 小時精確交集。

    `cache_root/months/YYYYMM/time_utc_ns.npy` 只含約 17,000 個時間值，讀入記憶體很小；
    以 set 做 exact int64 equality，避免以鄰近時刻補齊原始時間缺口。
    """

    svd_time = parse_epoch_ns(np.load(svd_dir / "time_utc_ns.npy", allow_pickle=False))
    full_time = parse_epoch_ns(np.load(full_product_dir / "time_iso.npy", allow_pickle=False))
    source_valid = np.load(full_product_dir / "source_valid.npy", allow_pickle=False).astype(bool)
    imputed = np.load(full_product_dir / "imputed.npy", allow_pickle=False).astype(bool)
    allowed = {int(v) for v in full_time[source_valid & ~imputed]}
    cache_times: set[int] = set()
    for month_dir in sorted(cache_root.glob("months/20*")):
        if month_dir.is_dir() and month_dir.name.isdigit():
            path = month_dir / "time_utc_ns.npy"
            if path.is_file():
                cache_times.update(int(v) for v in parse_epoch_ns(np.load(path, allow_pickle=False)))
    selected = [int(v) for v in svd_time.tolist() if int(v) in allowed and int(v) in cache_times]
    selected.sort()
    index_by_time = {int(v): index for index, v in enumerate(svd_time.tolist())}
    return np.asarray(selected, dtype=np.int64), np.asarray([index_by_time[v] for v in selected], dtype=np.int64)


def _contiguous(time_ns: np.ndarray, start: int) -> bool:
    """檢查 28 影格候選是否每格精確相隔六小時。"""

    window = time_ns[start : start + WINDOW_FRAME_COUNT]
    return window.size == WINDOW_FRAME_COUNT and bool(np.all(np.diff(window) == COMMON_INTERVAL_HOURS * NANOS_PER_HOUR))


def _select_windows(time_ns: np.ndarray, pc1: np.ndarray) -> dict[str, Any]:
    """以 renderer 同一確定性規則重新選取不重疊正/負相位案例。"""

    candidates = {"positive": [], "negative": []}
    center_offset = WINDOW_FRAME_COUNT // 2
    for start in range(max(0, time_ns.size - WINDOW_FRAME_COUNT + 1)):
        if not _contiguous(time_ns, start):
            continue
        center = start + center_offset
        value = float(pc1[center])
        phase = "positive" if value > 0 else "negative" if value < 0 else "zero"
        if phase != "zero":
            candidates[phase].append(
                {
                    "start": start,
                    "end": start + WINDOW_FRAME_COUNT - 1,
                    "center": center,
                    "pc1_standardized": value,
                    "abs_pc1_standardized": abs(value),
                }
            )
    for phase in candidates:
        candidates[phase].sort(key=lambda item: (-item["abs_pc1_standardized"], item["center"]))
    if not candidates["positive"] or not candidates["negative"]:
        raise ValueError("找不到完整正/負 PC1 7 日候選視窗")
    positive = candidates["positive"][0]
    negative = next(
        (item for item in candidates["negative"] if item["end"] < positive["start"] or item["start"] > positive["end"]),
        None,
    )
    if negative is None:
        raise ValueError("正/負候選視窗無法排成互不重疊的兩段")
    return {
        "rule": "complete 28-frame contiguous 6-hour window; PC1 abs priority; positive first; negative non-overlap",
        "candidate_count_positive": len(candidates["positive"]),
        "candidate_count_negative": len(candidates["negative"]),
        "windows": {
            "positive": positive,
            "negative": negative,
        },
    }


def _window_report(selection: dict[str, Any], time_ns: np.ndarray, pc1: np.ndarray) -> list[dict[str, Any]]:
    """把候選索引轉成起訖 UTC 與中心 PC1 的可讀報告。"""

    result = []
    for phase in ("positive", "negative"):
        item = selection["windows"][phase]
        result.append(
            {
                "phase": phase,
                "start_utc": _utc(int(time_ns[item["start"]])),
                "end_utc": _utc(int(time_ns[item["end"]])),
                "center_utc": _utc(int(time_ns[item["center"]])),
                "center_pc1_standardized": float(pc1[item["center"]]),
                "frame_count": WINDOW_FRAME_COUNT,
            }
        )
    return result


def _load_version_metrics(svd_dir: Path, time_ns: np.ndarray, common_svd_indices: np.ndarray) -> dict[str, Any]:
    """讀取一個版本的 K90/EV/PC 並計算共同視窗中心的 surface reconstruction stats。"""

    cumulative = np.load(svd_dir / "cumulative_explained_variance.npy", allow_pickle=False).astype(np.float64)
    pc_std = np.load(svd_dir / "pc_standardized.npy", mmap_mode="r", allow_pickle=False)
    pc_raw = np.load(svd_dir / "pc.npy", mmap_mode="r", allow_pickle=False)
    mean_u = np.asarray(np.load(svd_dir / "mean_u_mps.npy", mmap_mode="r", allow_pickle=False)[0], dtype=np.float32)
    mean_v = np.asarray(np.load(svd_dir / "mean_v_mps.npy", mmap_mode="r", allow_pickle=False)[0], dtype=np.float32)
    mode_u = np.load(svd_dir / "mode_u_mps_per_raw_pc.npy", mmap_mode="r", allow_pickle=False)
    mode_v = np.load(svd_dir / "mode_v_mps_per_raw_pc.npy", mmap_mode="r", allow_pickle=False)
    k90_indices = np.flatnonzero(cumulative >= 0.9)
    if not k90_indices.size:
        raise ValueError(f"{svd_dir} cumulative EV 未達 90%")
    k90 = int(k90_indices[0] + 1)
    # 先用自身 PC1 選視窗；呼叫端再把新舊 window 同時報告，便能辨識相位案例是否改變。
    selection = _select_windows(time_ns, np.asarray(pc_std[0, common_svd_indices], dtype=np.float64))
    center_stats = []
    for phase in ("positive", "negative"):
        center = selection["windows"][phase]["center"]
        svd_index = int(common_svd_indices[center])
        coefficients = np.asarray(pc_raw[:k90, svd_index], dtype=np.float32)
        u = mean_u + np.einsum("k,kyx->yx", coefficients, np.asarray(mode_u[:k90, 0], dtype=np.float32))
        v = mean_v + np.einsum("k,kyx->yx", coefficients, np.asarray(mode_v[:k90, 0], dtype=np.float32))
        center_stats.append({"phase": phase, "utc": _utc(int(time_ns[center])), "speed_mps": _stats(np.hypot(u, v))})
    return {
        "svd_dir": str(svd_dir),
        "time_count": int(np.load(svd_dir / "time_utc_ns.npy", mmap_mode="r", allow_pickle=False).size),
        "mode_count": int(cumulative.size),
        "k90": k90,
        "cumulative_explained_variance_first4": [float(v) for v in cumulative[:4]],
        "cumulative_explained_variance_first4_percent": float(cumulative[3] * 100.0),
        "selection": selection,
        "windows": _window_report(selection, time_ns, np.asarray(pc_std[0, common_svd_indices], dtype=np.float64)),
        "center_reconstruction_speed_stats": center_stats,
    }


def compare_region(
    key: str,
    *,
    old_svd_base: Path,
    new_svd_base: Path,
    new_suffix: str,
    full_product_dir: Path,
    old_cache_root: Path,
) -> dict[str, Any]:
    """比較單區 v1 與 corrected v2 指標，包含精確共同時間與視窗變化。"""

    old_dir = old_svd_base / REGIONS[key]
    new_dir = new_svd_base / f"{REGIONS[key]}{new_suffix}"
    common_time, old_indices = _common_time(old_dir, full_product_dir, old_cache_root)
    # 新舊 run 應保留相同 canonical UTC；仍重新映射 index，不假設 PROPACK 輸出順序。
    new_time = parse_epoch_ns(np.load(new_dir / "time_utc_ns.npy", allow_pickle=False))
    new_index_by_time = {int(v): i for i, v in enumerate(new_time.tolist())}
    shared = [(int(t), int(old_i), int(new_index_by_time[int(t)])) for t, old_i in zip(common_time.tolist(), old_indices.tolist()) if int(t) in new_index_by_time]
    if len(shared) < WINDOW_FRAME_COUNT * 2:
        raise ValueError(f"{key} 新舊 run 精確共同 6 小時時間不足：{len(shared)}")
    times = np.asarray([item[0] for item in shared], dtype=np.int64)
    old_idx = np.asarray([item[1] for item in shared], dtype=np.int64)
    new_idx = np.asarray([item[2] for item in shared], dtype=np.int64)
    old = _load_version_metrics(old_dir, times, old_idx)
    new = _load_version_metrics(new_dir, times, new_idx)
    return {
        "region_key": key,
        "exact_common_source_valid_non_imputed_count": int(times.size),
        "old_v1": old,
        "new_coastline_corrected_v2": new,
        "delta": {
            "k90": int(new["k90"] - old["k90"]),
            "cumulative_explained_variance_first4_percentage_points": float(new["cumulative_explained_variance_first4_percent"] - old["cumulative_explained_variance_first4_percent"]),
            "positive_window_center_changed": old["windows"][0]["center_utc"] != new["windows"][0]["center_utc"],
            "negative_window_center_changed": old["windows"][1]["center_utc"] != new["windows"][1]["center_utc"],
        },
    }


def main() -> None:
    """執行 A–D 或指定單區版本比較並寫出 JSON。"""

    parser = argparse.ArgumentParser(description="Compare OCM SVD v1 and coastline-corrected v2 metrics.")
    parser.add_argument("--old-svd-base", type=Path, required=True)
    parser.add_argument("--new-svd-base", type=Path, required=True)
    parser.add_argument("--new-suffix", default="_coastline_corrected_v2")
    parser.add_argument("--full-product-dir", type=Path, required=True)
    parser.add_argument("--surface-cache-base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--regions", default="A,B,C,D")
    args = parser.parse_args()
    selected = [item.strip().upper() for item in args.regions.split(",") if item.strip()]
    unknown = [item for item in selected if item not in REGIONS]
    if unknown:
        raise ValueError(f"未知區域：{unknown}")
    cache_ids = {"A": "northeast_taiwan_common_cache_v3", "B": "hsinchu_cache_v3", "C": "houwan_nmmba_cache_v3", "D": "lienchiang_common_cache_v3"}
    regions = [
        compare_region(
            key,
            old_svd_base=args.old_svd_base,
            new_svd_base=args.new_svd_base,
            new_suffix=args.new_suffix,
            full_product_dir=args.full_product_dir,
            old_cache_root=args.surface_cache_base / cache_ids[key],
        )
        for key in selected
    ]
    output = {
        "schema_name": "ocm_svd_v1_v2_coastline_comparison",
        "schema_version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "old_svd_base": str(args.old_svd_base.resolve()),
        "new_svd_base": str(args.new_svd_base.resolve()),
        "new_suffix": args.new_suffix,
        "full_product_dir": str(args.full_product_dir.resolve()),
        "surface_cache_base": str(args.surface_cache_base.resolve()),
        "selection_definition": "full product source_valid=True and imputed=False, exact SVD/cache UTC equality, complete non-overlapping 28-frame 6-hour windows, PC1 abs priority",
        "regions": regions,
        "limitations": [
            "代表性視窗是相位案例，不是全年統計驗證。",
            "中心重建速度摘要未替代正式動畫 exact-land mask/向量地理 QA。",
            "新舊 SVD 的 PC 符號慣例若因 anchor loading 改變，正負相位名稱需搭配 loading 方向解讀。",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "regions": selected}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
