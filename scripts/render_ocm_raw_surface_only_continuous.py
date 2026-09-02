#!/usr/bin/env python3
"""產生四海域「單一連續長時窗」純原始表層流場動畫。

本模組是既有 ``render_ocm_raw_surface_only.py`` 的長時窗版本，畫面仍只呈現
同源 OCM surface cache 的原始 ``u/v`` 流場；正式六層聯合水柱 SVD 僅用於時間
軸追溯及選擇一段四區共同可用的連續觀測時窗，不會重算、覆寫或替換任何 SVD
結果。與原先兩段各七日的案例不同，本版預設選取約 30 日、120 個精確 6 小時
觀測時間位置，讓影片真正增加原始觀測資料的時間範圍。

為避免每 6 小時直接切換造成明顯跳動，輸出時保留每一個實際觀測時間位置，並
對同一個連續觀測區段內的中間時間位置套用三點時間平滑（前一筆／當筆／下一筆）。
完整 2024--2025 模式也可保留全部共同實測時間位置；遇到資料缺口時不跨缺口平滑。
另可用 ``--display-frame-count`` 將完整兩年來源重採樣成固定展示影格數，例如
720 幀在 4 fps 下為精確 180 秒；正常相鄰觀測間以 u/v 線性時間內插，資料缺口
則不跨越內插而選最近有效觀測保持。這些平滑／內插值只存在於 renderer 的記憶體
與 MP4，沒有寫回 OCM cache，也不代表新的觀測或預報。manifest 會分開記錄 source
observation time slots、display frame count、內插影格與缺口保持影格，不把展示場
誤標成新增觀測。

版面直接重用已通過的 raw-only renderer：864×500、固定 0.0--0.8 m/s 色階、
0.2 m/s 刻度、精確 GeoJSON 岸線、白色流場箭頭及圖內 1 m/s 比例尺。四區共用
相同的主圖／色條矩形，適合簡報 2×2 排列；顯示縱橫比例的取捨仍只屬展示版面，
不改變經緯度範圍、原始 u/v、正式 SVD 或來源時間軸。
"""

from __future__ import annotations

import argparse
import gc
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np

try:
    import imageio.v2 as imageio
except ImportError:  # pragma: no cover - 只在缺少影片依賴的稽核環境觸發
    try:
        # SERVER 的既有 Anaconda imageio=2.9 沒有 `imageio.v2` 命名空間，但仍提供
        # 相同的 get_writer／imwrite API；使用相容 fallback 可重用既有環境，避免為
        # 3 分鐘展示版額外改動或安裝套件。兩種匯入都只影響影片 I/O，不改資料場。
        import imageio  # type: ignore[no-redef]
    except ImportError:
        imageio = None  # type: ignore[assignment]

from render_ocm_raw_surface_only import (
    RAW_DEFAULT_SPEED_TICK_STEP_MPS,
    RAW_DEFAULT_SPEED_VMAX_MPS,
    RAW_ARROW_FONT_SIZE_PT,
    RAW_AXES_RECT,
    RAW_COLORBAR_RECT,
    RAW_HEIGHT,
    RAW_TITLE_Y_FRACTION,
    RAW_WIDTH,
    _choose_raw_arrow_scale,
    _choose_raw_speed_scale,
    _configure_imageio_ffmpeg,
    _create_raw_scene,
    _ffprobe,
    _raw_ticks,
    _run_manifest_qa,
    _update_raw_scene,
    _visible_text_spec,
)
from visualize_ocm_svd_modal_context import (
    COMMON_INTERVAL_HOURS,
    DEFAULT_RENDER_DPI,
    DEFAULT_TARGET_ARROWS,
    FrameRecord,
    NANOS_PER_HOUR,
    Payload,
    _bilinear_external_frame,
    _format_time_utc,
    _linear_interpolate_field,
    _read_same_source_frame,
    _speed,
    build_region_specs,
    find_cjk_font,
    load_full_product_audit,
    load_region_dataset,
    sha256_file,
)


SCRIPT_VERSION = "1.5.0"
"""連續長時窗 raw-only renderer 的 manifest 版本；1.5.0 新增完整期間固定
展示影格數的日曆時間重採樣，並延續 1.4.0 的缺口邊界安全處理。"""

DEFAULT_SOURCE_FRAME_COUNT = 120
"""預設實際觀測時間位置數；120 個 6 小時時槽約為 30 日，對應 30 秒影片。"""

DEFAULT_FPS = 4
"""影片輸出幀率；增加長時窗時仍維持與現有簡報素材相同的播放速率。"""

INTRO_HOLD_FRAMES = 0
"""本版不另加片頭停格，讓 source frame count 與 4 fps 片長精確對應。"""

OUTRO_HOLD_FRAMES = 0
"""本版不另加片尾停格，避免 120／240 個 source frame 之外增加輸出影格。"""

TEMPORAL_SMOOTHING_WEIGHTS = (0.25, 0.50, 0.25)
"""三點時間平滑權重；只用於展示，不改變 source 時間位置或原始陣列。"""

COMMON_QUIVER_REFERENCE_MPS = 1.0
"""四區共用的比例尺參考速度；不依各區流速分布調整，只作展示尺度基準。"""

DEFAULT_COMMON_QUIVER_SCALE_MULTIPLIER = 28.0
"""固定 quiver scale 倍率；相較舊版縮短比例尺箭頭並讓 A--D 完全一致。"""


def _continuous_output_names(
    dataset: Any,
    temporal_smoothed: bool,
    display_frame_count: int,
    fps: int,
    show_title: bool,
    full_period: bool,
    display_resampled: bool,
) -> tuple[str, str, str, str, str, str]:
    """建立長時窗版本的檔名，避免與兩段七日成果混用。

    一般長時窗檔名中的 ``continuous_<days>d_<seconds>s`` 只表示本次展示影格數
    與 fps 的展示定位，不把實際起訖日期硬編碼到檔名；完整兩年版改用
    ``full_2024_2025_<seconds>s``，以免把含缺口的有效影格數誤讀成日曆連續天數。
    精確 UTC 起訖時間與 source/display frame count 會寫入 manifest。若是固定展示影格
    的日曆時間重採樣，檔名加入 ``temporal_resampled``；固定影格數的三點平滑則加入
    ``temporal_smoothed``，避免與直接播放 6 小時觀測影格的版本混淆。
    """

    suffix = (
        "_temporal_resampled"
        if display_resampled
        else ("_temporal_smoothed" if temporal_smoothed else "")
    )
    # 無標題版本必須由檔名明確區分，避免使用者在簡報後製時誤取含內嵌標題的
    # 30 秒影片。這裡只改輸出識別，不改 source frame、播放速度或資料內容。
    if not show_title:
        suffix += "_no_title"
    duration_seconds = display_frame_count / float(fps)
    if full_period:
        # 全時段版的檔名固定寫出資料涵蓋年份，避免把有效影格數誤讀成無缺口
        # 的「712 日連續觀測」；精確日曆起訖與缺口仍完整寫入 manifest。
        stem = (
            f"region_{dataset.spec.key}_{dataset.spec.short_name}_raw_surface_only_full_2024_2025_"
            f"{duration_seconds:g}s{suffix}"
        )
    else:
        coverage_days = display_frame_count * COMMON_INTERVAL_HOURS / 24.0
        day_text = f"{coverage_days:g}d"
        second_text = f"{duration_seconds:g}s"
        stem = f"region_{dataset.spec.key}_{dataset.spec.short_name}_raw_surface_only_continuous_{day_text}_{second_text}{suffix}"
    return (
        f"{stem}.mp4",
        f"{stem}_poster.png",
        f"{stem}_window_start.png",
        f"{stem}_window_middle.png",
        f"{stem}_window_end.png",
        f"{stem}_source_window_contact.png",
    )


def _common_time_values(datasets: Sequence[Any]) -> np.ndarray:
    """計算 A--D 都有精確共同時間的 UTC epoch ns 集合。

    每個 ``RegionDataset.common_time_ns`` 已先排除全臺產品的 invalid/imputed 時刻、
    不存在於正式 SVD 的時間及缺少同源 cache 的時間。這裡再取四區交集，確保單一
    長時窗在 2×2 影片中同步播放；不以鄰近時間取代缺失時刻，也不進行時間補洞。
    """

    if not datasets:
        raise ValueError("至少需要一個區域才能建立共同時間軸")
    common = {int(value) for value in np.asarray(datasets[0].common_time_ns).tolist()}
    for dataset in datasets[1:]:
        common.intersection_update(int(value) for value in np.asarray(dataset.common_time_ns).tolist())
    if not common:
        raise ValueError("A--D 沒有共同的 source-valid、非 imputed 時刻")
    return np.asarray(sorted(common), dtype=np.int64)


def _contiguous_runs(time_ns: np.ndarray) -> list[tuple[int, int]]:
    """找出共同時間軸中連續 6 小時觀測區段的 `[start, end)` 索引。

    只有相鄰 timestamp 差值精確等於 6 小時才屬於同一段；這個判定把資料缺口
    視為真正的時間邊界，避免時間平滑跨越不可觀測期間。回傳索引而非日期，
    讓後續可以用同一個 source frame count 建立候選長窗。
    """

    values = np.asarray(time_ns, dtype=np.int64)
    if values.ndim != 1 or values.size < 2:
        return []
    breaks = np.flatnonzero(np.diff(values) != COMMON_INTERVAL_HOURS * NANOS_PER_HOUR) + 1
    bounds = np.concatenate(([0], breaks, [values.size]))
    return [(int(left), int(right)) for left, right in zip(bounds[:-1], bounds[1:]) if right - left >= 2]


def _select_shared_continuous_window(
    datasets: Sequence[Any],
    source_frame_count: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    """選取四區共用的單一連續長時窗。

    候選必須全部位於四區共同時間交集，且每個相鄰時間精確相隔 6 小時。為讓長時
    窗仍有可觀察的流場變化，候選評分使用正式 SVD `pc_standardized[0]` 在四區的
    平均時間標準差；這只是一個可追溯的展示選窗規則，不把 raw-only 動畫宣稱為
    氣候統計或代表全年趨勢。若分數相同，再以四區平均 `abs(pc1)` 及較早 UTC
    時間做確定性排序。PC 陣列只用於選窗，原始動畫 payload 仍直接來自 surface u/v。
    """

    if source_frame_count < 3:
        raise ValueError("連續長時窗至少需要 3 個 source observation frames")
    common_times = _common_time_values(datasets)
    runs = _contiguous_runs(common_times)
    index_maps: list[dict[int, int]] = []
    for dataset in datasets:
        index_maps.append(
            {
                int(time_value): int(svd_index)
                for time_value, svd_index in zip(
                    np.asarray(dataset.common_time_ns).tolist(),
                    np.asarray(dataset.common_svd_indices).tolist(),
                )
            }
        )

    candidates: list[dict[str, Any]] = []
    for run_start, run_end in runs:
        run_length = run_end - run_start
        if run_length < source_frame_count:
            continue
        for start in range(run_start, run_end - source_frame_count + 1):
            window_times = common_times[start : start + source_frame_count]
            pc1_rows = []
            for dataset, index_map in zip(datasets, index_maps):
                indices = [index_map[int(value)] for value in window_times.tolist()]
                pc1_rows.append(
                    np.asarray(dataset.pc_standardized[0, np.asarray(indices, dtype=np.int64)], dtype=np.float64)
                )
            pc1_matrix = np.vstack(pc1_rows)
            variability_score = float(np.mean(np.std(pc1_matrix, axis=1)))
            amplitude_score = float(np.mean(np.abs(pc1_matrix)))
            candidates.append(
                {
                    "start_index": start,
                    "end_index_exclusive": start + source_frame_count,
                    "start_time_ns": int(window_times[0]),
                    "end_time_ns": int(window_times[-1]),
                    "variability_score_pc1_std": variability_score,
                    "amplitude_score_pc1_abs_mean": amplitude_score,
                }
            )
    if not candidates:
        longest = max((right - left for left, right in runs), default=0)
        raise ValueError(
            f"四區找不到 {source_frame_count} 個連續 6 小時共同觀測影格；最長共同區段為 {longest} frames"
        )
    selected = max(
        candidates,
        key=lambda item: (
            item["variability_score_pc1_std"],
            item["amplitude_score_pc1_abs_mean"],
            -item["start_time_ns"],
        ),
    )
    start = int(selected["start_index"])
    end = int(selected["end_index_exclusive"])
    selected_times = common_times[start:end].copy()
    details = {
        "mode": "single_shared_contiguous_window",
        "selection_rule": (
            "四區共同 source-valid/non-imputed 時間交集中的連續 6 小時候選；"
            "以四區 pc_standardized[0] 視窗平均標準差最大為首要排序，"
            "平均 abs(pc1) 次排序，UTC 較早者為確定性 tie-break"
        ),
        "candidate_count": len(candidates),
        "contiguous_common_run_count": len(runs),
        "selected_candidate": selected,
        "source_frame_count": int(source_frame_count),
        "sample_interval_hours": COMMON_INTERVAL_HOURS,
        "start_utc": _format_time_utc(int(selected_times[0])),
        "end_utc": _format_time_utc(int(selected_times[-1])),
        "endpoint_span_hours": float(
            (int(selected_times[-1]) - int(selected_times[0])) / float(NANOS_PER_HOUR)
        ),
        "slot_coverage_days": float(source_frame_count * COMMON_INTERVAL_HOURS / 24.0),
        "all_regions_same_time_window": True,
        "all_frames_source_valid_non_imputed": True,
        "not_a_statistical_or_climatological_claim": True,
    }
    return selected_times, details


def _select_all_shared_observations(datasets: Sequence[Any]) -> tuple[np.ndarray, dict[str, Any]]:
    """選取 2024--2025 全部四區共同的實測時間位置，不截成單一長窗。

    ``RegionDataset.common_time_ns`` 已由 loader 以全臺產品的
    ``source_valid=True``、``imputed=False``，以及正式 SVD／同源 surface cache 的
    精確 UTC epoch-ns 交集建立。因此這裡不把全臺產品中的 imputed 時槽或任一區
    缺少同源資料的時間硬塞進影片。共同時間序列仍可能有缺口；缺口被保留在
    manifest，後續時間平滑只在兩側均為 6 小時相鄰觀測時套用。

    回傳的影格數是「實際共同 source-valid 時間位置數」，不是把日曆天數乘上四
    來推算的理論格數。這個區分對兩年完整版尤其重要：2,924 個全臺時間格中，
    四區可共同追溯且非 imputed 的實測位置目前為 2,848 個。
    """

    common_times = _common_time_values(datasets)
    if common_times.size < 2:
        raise ValueError("四區共同 source-valid、非 imputed 時間不足以建立完整時段影片")
    interval_hours = np.diff(common_times).astype(np.float64) / float(NANOS_PER_HOUR)
    gap_indices = np.flatnonzero(
        ~np.isclose(interval_hours, COMMON_INTERVAL_HOURS, rtol=0.0, atol=1.0e-9)
    )
    gap_intervals: list[dict[str, Any]] = []
    for index in gap_indices.tolist():
        delta_hours = float(interval_hours[index])
        missing_slots = max(0, int(round(delta_hours / COMMON_INTERVAL_HOURS)) - 1)
        gap_intervals.append(
            {
                "after_utc": _format_time_utc(int(common_times[index])),
                "before_utc": _format_time_utc(int(common_times[index + 1])),
                "interval_hours": delta_hours,
                "missing_6h_slots": missing_slots,
            }
        )
    details = {
        "mode": "all_shared_source_valid_non_imputed_observations_2024_2025",
        "selection_rule": (
            "取四區共同的精確 UTC epoch-ns 時間交集；全臺 source_valid=True 且 "
            "imputed=False，正式 SVD 與同源 surface cache 均須可追溯；不以 PC、"
            "鄰近時間或補值資料填補缺口"
        ),
        "candidate_count": None,
        "contiguous_common_run_count": int(gap_indices.size + 1),
        "source_frame_count": int(common_times.size),
        "sample_interval_hours": COMMON_INTERVAL_HOURS,
        "start_utc": _format_time_utc(int(common_times[0])),
        "end_utc": _format_time_utc(int(common_times[-1])),
        "endpoint_span_hours": float(
            (int(common_times[-1]) - int(common_times[0])) / float(NANOS_PER_HOUR)
        ),
        "calendar_span_days_inclusive": float(
            (int(common_times[-1]) - int(common_times[0])) / float(NANOS_PER_HOUR) / 24.0
            + COMMON_INTERVAL_HOURS / 24.0
        ),
        "observed_slot_coverage_days": float(common_times.size * COMMON_INTERVAL_HOURS / 24.0),
        "all_regions_same_time_window": True,
        "all_frames_source_valid_non_imputed": True,
        "gap_count": int(gap_indices.size),
        "gap_intervals": gap_intervals,
        "not_a_statistical_or_climatological_claim": True,
    }
    return common_times.copy(), details


def _assign_shared_records(dataset: Any, selected_times: np.ndarray, selection_details: dict[str, Any]) -> None:
    """將共同 UTC 時間轉為單區 ``FrameRecord``，保留 SVD/cache/full-product 對應。

    ``FrameRecord`` 的三個索引仍分別指向正式 SVD、全臺 QC 時間軸與同源 surface
    cache；這裡只重新組合一段連續的時間選取，不排序、不修補、不修改任何來源陣列。
    ``phase`` 使用內部 ``continuous`` 標記，因為本版不把畫面切成正／負相位案例。
    """

    common_order_by_time = {
        int(time_value): int(order)
        for order, time_value in enumerate(np.asarray(dataset.common_time_ns).tolist())
    }
    svd_index_by_time = {
        int(time_value): int(svd_index)
        for time_value, svd_index in zip(
            np.asarray(dataset.common_time_ns).tolist(),
            np.asarray(dataset.common_svd_indices).tolist(),
        )
    }
    full_index_by_time = {
        int(time_value): int(full_index)
        for time_value, full_index in zip(
            np.asarray(dataset.common_time_ns).tolist(),
            np.asarray(dataset.common_full_indices).tolist(),
        )
    }
    records: list[FrameRecord] = []
    for time_value in np.asarray(selected_times, dtype=np.int64).tolist():
        time_int = int(time_value)
        if time_int not in svd_index_by_time or time_int not in full_index_by_time:
            raise ValueError(f"{dataset.spec.key} 長時窗時間不在單區共同索引：{_format_time_utc(time_int)}")
        records.append(
            FrameRecord(
                common_order=common_order_by_time[time_int],
                time_ns=time_int,
                svd_index=svd_index_by_time[time_int],
                full_index=full_index_by_time[time_int],
                cache_ref=dataset.cache_ref_by_time_ns.get(time_int),
                phase="continuous",
            )
        )
    if len(records) != len(selected_times):
        raise RuntimeError(f"{dataset.spec.key} 長時窗 FrameRecord 數量不一致")
    dataset.selected_records = records
    dataset.selection_details = {
        **selection_details,
        "region_key": dataset.spec.key,
        "source_mode": dataset.source_mode,
    }


def _materialize_raw_payloads(dataset: Any, full_product: dict[str, Any]) -> list[Payload]:
    """只讀取長時窗原始 surface u/v，建立 raw-only payload。

    同源模式直接從每月 ``u_surface_mps.npy``/``v_surface_mps.npy`` 讀取；若資料
    網格驗證失敗而進入 fallback，才使用既有全臺產品雙線性重網格函式。為避免 raw-only
    動畫誤觸發 SVD 重建，本函式不呼叫 `mean + mode × pc`，Payload 中的 reconstruction
    欄位只放全 NaN 佔位，renderer 也不會繪製它們。每個實際 6 小時觀測只 materialize
    一次，來源 mask、缺值及單位 m/s 的語意沿用正式 loader。
    """

    payloads: list[Payload] = []
    empty_pc = np.empty(0, dtype=np.float32)
    # raw-only renderer 不會讀取三個 reconstruction 欄位；共用零長度佔位陣列可
    # 避免兩年 2,848 幀各自配置三份全網格 NaN，降低 SERVER 記憶體峰值。
    empty_field = np.empty(0, dtype=np.float32)
    for ordinal, record in enumerate(dataset.selected_records, start=1):
        if dataset.source_mode == "same_source_surface_cache":
            raw_u, raw_v = _read_same_source_frame(dataset, record, dataset.render_mask)
        else:
            raw_u, raw_v = _bilinear_external_frame(
                full_product,
                record.full_index,
                dataset.lon,
                dataset.lat,
                dataset.render_mask,
            )
        payloads.append(
            Payload(
                record=record,
                pc_standardized=empty_pc.copy(),
                raw_u=raw_u,
                raw_v=raw_v,
                raw_speed=_speed(raw_u, raw_v),
                reconstruction_u=empty_field,
                reconstruction_v=empty_field,
                reconstruction_speed=empty_field,
            )
        )
        if ordinal % 16 == 0 or ordinal == len(dataset.selected_records):
            print(f"{dataset.spec.key} materialized raw observations {ordinal}/{len(dataset.selected_records)}", flush=True)
    dataset.payloads = payloads
    return payloads


def _set_common_quiver_scale(datasets: Sequence[Any]) -> dict[str, Any]:
    """把四區資料箭頭與圖內比例尺鎖定到同一個展示尺度。

    舊版呼叫 ``_choose_raw_arrow_scale`` 後直接使用每區 u/v 的 p95，因此低流速的
    D 區會得到較小的 quiver scale，反而把相同的 1 m/s 圖例畫得特別長。本版仍
    計算並保留各區 p95 作為診斷，但不再拿它決定畫面尺度；所有區域固定使用
    ``COMMON_QUIVER_REFERENCE_MPS=1.0``。這是比例尺示意與跨區版面一致性的展示
    設定，不是對各區流速統計分布的重新正規化，原始 u/v 陣列完全不變。
    """

    diagnostics: dict[str, float] = {}
    for dataset in datasets:
        _choose_raw_arrow_scale(dataset)
        diagnostics[dataset.spec.key] = float(dataset.quiver_reference_mps)
        dataset.raw_arrow_p95_diagnostic_mps = float(dataset.quiver_reference_mps)
        dataset.quiver_reference_mps = COMMON_QUIVER_REFERENCE_MPS
    return {
        "mode": "fixed_cross_region_display_scale",
        "reference_mps": COMMON_QUIVER_REFERENCE_MPS,
        "per_region_p95_diagnostic_mps": diagnostics,
        "data_derived_scale": False,
        "note_zh": "各區 p95 僅供診斷；正式 renderer 不依各區流速分布改變 quiver scale。",
    }


def _smooth_field_same_count(
    previous: np.ndarray,
    current: np.ndarray,
    following: np.ndarray,
    valid_domain_mask: np.ndarray,
) -> np.ndarray:
    """以三點時間濾波平滑一個 u 或 v 場，並維持原本影格數。

    ``previous/current/following`` 都是相鄰 6 小時觀測的 `[lat, lon]` m/s 陣列。
    只有三筆資料同時有限、且位於 SVD renderer 有效域的格點才套用
    ``0.25*previous + 0.50*current + 0.25*following``；其他格點保留當筆觀測值，
    若當筆本來無效則維持 NaN。這是展示用低通平滑，不會填補缺值、不會跨時間缺口，
    也不改寫原始 cache。端點沒有前後完整鄰居，直接使用實際觀測值。
    """

    previous_array = np.asarray(previous, dtype=np.float32)
    current_array = np.asarray(current, dtype=np.float32)
    following_array = np.asarray(following, dtype=np.float32)
    domain_mask = np.asarray(valid_domain_mask, dtype=bool)
    if not (
        previous_array.shape == current_array.shape == following_array.shape == domain_mask.shape
    ):
        raise ValueError("三點時間平滑的 u/v 陣列與有效域遮罩 shape 不一致")
    result = current_array.copy()
    valid_triplet = (
        domain_mask
        & np.isfinite(previous_array)
        & np.isfinite(current_array)
        & np.isfinite(following_array)
    )
    result[valid_triplet] = (
        TEMPORAL_SMOOTHING_WEIGHTS[0] * previous_array[valid_triplet]
        + TEMPORAL_SMOOTHING_WEIGHTS[1] * current_array[valid_triplet]
        + TEMPORAL_SMOOTHING_WEIGHTS[2] * following_array[valid_triplet]
    )
    result[~np.isfinite(current_array)] = np.nan
    return result


def _smooth_raw_payloads_same_count(dataset: Any, source_payloads: Sequence[Payload]) -> list[Payload]:
    """對實測影格做固定影格數的三點時間平滑。

    這裡不增加、不刪除、不重新排序任何時間位置：n 個實際 6 小時觀測仍輸出 n
    個 display frames，因此 120 幀在 4 fps 就是精確 30 秒。內部影格使用鄰近觀測
    的加權場降低瞬間跳動，端點保持原始值；所有原始 source payload 仍由呼叫端
    保留在記憶體中的 `source_payloads` 並可由 manifest 的時間窗追溯。此流程不應
    被解讀為新增觀測或模型重建。完整 2024--2025 版本可能包含缺口；遇到缺口
    時保留當筆實測值，不跨越缺口計算平滑，並把略過的邊界數量寫入 manifest。
    """

    if len(source_payloads) < 3:
        raise ValueError(f"{dataset.spec.key} 至少需要 3 個觀測影格才能套用三點時間平滑")
    output: list[Payload] = [source_payloads[0]]
    smoothed_count = 0
    gap_boundary_count = 0
    empty_field = np.empty(0, dtype=np.float32)
    for index in range(1, len(source_payloads) - 1):
        previous = source_payloads[index - 1]
        current = source_payloads[index]
        following = source_payloads[index + 1]
        previous_to_current = (int(current.record.time_ns) - int(previous.record.time_ns)) / float(NANOS_PER_HOUR)
        current_to_following = (int(following.record.time_ns) - int(current.record.time_ns)) / float(NANOS_PER_HOUR)
        if previous_to_current != COMMON_INTERVAL_HOURS or current_to_following != COMMON_INTERVAL_HOURS:
            # 完整兩年版必須保留所有共同實測影格，但不能跨越資料缺口製造
            # 看似連續的場。直接沿用當筆 Payload 可保留原始值與時間位置。
            output.append(current)
            gap_boundary_count += 1
            continue
        raw_u = _smooth_field_same_count(previous.raw_u, current.raw_u, following.raw_u, dataset.render_mask)
        raw_v = _smooth_field_same_count(previous.raw_v, current.raw_v, following.raw_v, dataset.render_mask)
        output.append(
            Payload(
                record=current.record,
                pc_standardized=np.empty(0, dtype=np.float32),
                raw_u=raw_u,
                raw_v=raw_v,
                raw_speed=_speed(raw_u, raw_v),
                reconstruction_u=empty_field,
                reconstruction_v=empty_field,
                reconstruction_speed=empty_field,
            )
        )
        smoothed_count += 1
    output.append(source_payloads[-1])
    dataset.payloads = output
    # RegionDataset 的欄位名稱沿用既有 renderer，以保持共用 loader 的相容性；
    # 對外 manifest 一律輸出為 temporal_smoothing，避免誤解成增加中間觀測。
    dataset.temporal_interpolation_summary = {
        "enabled": True,
        "method": "same_count_centered_three_observation_temporal_smoothing",
        "visualization_only": True,
        "source_observation_interval_hours": COMMON_INTERVAL_HOURS,
        "display_frame_interval_hours": COMMON_INTERVAL_HOURS,
        "weights_previous_current_following": list(TEMPORAL_SMOOTHING_WEIGHTS),
        "input_observation_frame_count": int(len(source_payloads)),
        "display_payload_frame_count": int(len(output)),
        "displayed_real_observation_time_position_count": int(len(output)),
        "smoothed_interior_frame_count": int(smoothed_count),
        "virtual_frame_count": 0,
        "source_observation_time_positions_retained": True,
        "same_frame_count_as_source": len(output) == len(source_payloads),
        "preserves_source_window": True,
        "no_smoothing_across_gap": True,
        "gap_boundary_frame_count_without_smoothing": int(gap_boundary_count),
        "input_data_unchanged": True,
        "formula": "field_display(t) = 0.25*field_observed(t-6h) + 0.50*field_observed(t) + 0.25*field_observed(t+6h)",
        "mask_policy": "smooth only where all three endpoint u/v pairs are finite and render_mask is true; otherwise retain current observation or NaN",
        "note_zh": (
            "此為固定影格數的局部三點時間平滑；實際 6 小時時間位置不增加、不刪除，"
            "不跨越資料缺口、不延伸或內插整個 7 日，也不代表新增觀測。"
        ),
    }
    return output


def _resample_raw_payloads_to_count(
    dataset: Any,
    source_payloads: Sequence[Payload],
    display_frame_count: int,
) -> list[Payload]:
    """將完整兩年實測 payload 重採樣為固定數量的展示影格。

    ``source_payloads`` 是四區共同時間軸上的實際 source-valid、非 imputed 原始
    surface u/v；每個陣列維度為 ``[lat, lon]``、單位為 m/s。目標時間軸均勻覆蓋
    第一個至最後一個實測 UTC 時刻，因此 2,848 個 6 小時觀測可在 4 fps 下重採樣
    成 720 幀、精確播放 180 秒，同時保留完整 2024--2025 的時間範圍。

    正常相鄰 6 小時觀測之間，以時間比例對 u 與 v 個別做線性內插，再重新計算
    speed；這是展示用場，不是新的觀測或 SVD 重建。若目標時間落在已知資料缺口，
    不能把缺口兩側場線性連接，改採距離最近的有效 source payload 暫時保持，並在
    manifest 記錄缺口保持影格數。任一端格點缺值也不以零或鄰近值填補，沿用既有
    `_linear_interpolate_field` 的有限值規則，避免把缺值偽裝成流場。

    回傳值只有 renderer 使用的展示 payload；來源 payload 不會寫回 cache，且
    ``temporal_interpolation_summary`` 會保存來源／展示影格數、目標時間步階、內插
    與缺口保持數量，讓使用者可區分「資料取樣變稀疏」和「新增觀測」。
    """

    source_count = len(source_payloads)
    if source_count < 2:
        raise ValueError(f"{dataset.spec.key} 至少需要兩個 source payload 才能重採樣")
    if display_frame_count < 2 or display_frame_count > source_count:
        raise ValueError(
            f"{dataset.spec.key} display-frame-count 必須介於 2 與 {source_count} 之間，"
            f"收到 {display_frame_count}"
        )

    source_times = np.asarray(
        [int(payload.record.time_ns) for payload in source_payloads], dtype=np.int64
    )
    if np.any(np.diff(source_times) <= 0):
        raise ValueError(f"{dataset.spec.key} source payload 時間必須嚴格遞增")

    # 以 Python 整數計算 epoch-ns，避免先乘以索引時超過 int64；目標時間含兩個
    # 精確端點，且中間時間均勻分布於整個日曆時間範圍，不依 source frame index
    # 近似，因為完整兩年共同實測序列含有資料缺口。
    start_ns = int(source_times[0])
    end_ns = int(source_times[-1])
    duration_ns = end_ns - start_ns
    target_times = np.asarray(
        [start_ns + (duration_ns * index) // (display_frame_count - 1) for index in range(display_frame_count)],
        dtype=np.int64,
    )

    output: list[Payload] = []
    display_source_time_ns: list[int] = []
    display_source_index: list[int] = []
    interpolation_records: list[dict[str, Any]] = []
    interpolated_count = 0
    real_anchor_count = 0
    gap_hold_count = 0
    empty_pc = np.empty(0, dtype=np.float32)
    empty_field = np.empty(0, dtype=np.float32)
    expected_interval_ns = COMMON_INTERVAL_HOURS * NANOS_PER_HOUR

    for target_ns in target_times.tolist():
        target_int = int(target_ns)
        right = int(np.searchsorted(source_times, target_int, side="left"))
        if right < source_count and int(source_times[right]) == target_int:
            # 目標時間正好落在實測時刻，直接使用原始 payload；這些幀仍是觀測錨點，
            # 不重新複製陣列，也不把它們標成虛擬內插值。
            output.append(source_payloads[right])
            display_source_time_ns.append(target_int)
            display_source_index.append(right)
            real_anchor_count += 1
            continue

        if right <= 0 or right >= source_count:
            raise RuntimeError(f"{dataset.spec.key} 目標時間超出 source 時間範圍：{target_int}")

        left = right - 1
        left_ns = int(source_times[left])
        right_ns = int(source_times[right])
        interval_ns = right_ns - left_ns
        if interval_ns != expected_interval_ns:
            # 缺口兩端不可內插；選距離較近的實測值只作展示保持，並把此類幀與
            # 真正線性內插分開統計。這個策略不聲稱缺口期間的流場等於任一端。
            left_distance = target_int - left_ns
            right_distance = right_ns - target_int
            chosen = left if left_distance <= right_distance else right
            output.append(source_payloads[chosen])
            display_source_time_ns.append(int(source_times[chosen]))
            display_source_index.append(chosen)
            gap_hold_count += 1
            continue

        alpha = float(target_int - left_ns) / float(interval_ns)
        left_payload = source_payloads[left]
        right_payload = source_payloads[right]
        raw_u = _linear_interpolate_field(
            left_payload.raw_u,
            right_payload.raw_u,
            alpha=alpha,
            valid_domain_mask=dataset.render_mask,
        )
        raw_v = _linear_interpolate_field(
            left_payload.raw_v,
            right_payload.raw_v,
            alpha=alpha,
            valid_domain_mask=dataset.render_mask,
        )
        output.append(
            Payload(
                # record 保留左側 source 錨點作為來源索引；真正的展示 UTC 由摘要中
                # 的 display_time_ns 保存，避免偽造一個不存在於正式 SVD 的索引列。
                record=left_payload.record,
                pc_standardized=empty_pc.copy(),
                raw_u=raw_u,
                raw_v=raw_v,
                raw_speed=_speed(raw_u, raw_v),
                reconstruction_u=empty_field,
                reconstruction_v=empty_field,
                reconstruction_speed=empty_field,
                is_temporal_interpolated=True,
                interpolation_alpha=alpha,
                interpolation_source_times_ns=(left_ns, right_ns),
            )
        )
        display_source_time_ns.append(target_int)
        display_source_index.append(left)
        interpolation_records.append(
            {
                "display_time_utc": _format_time_utc(target_int),
                "source_left_time_utc": _format_time_utc(left_ns),
                "source_right_time_utc": _format_time_utc(right_ns),
                "alpha": alpha,
            }
        )
        interpolated_count += 1

    if len(output) != display_frame_count:
        raise RuntimeError(
            f"{dataset.spec.key} 時間重採樣後影格數錯誤：{len(output)} != {display_frame_count}"
        )

    target_interval_hours = np.diff(target_times).astype(np.float64) / float(NANOS_PER_HOUR)
    dataset.payloads = output
    dataset.temporal_interpolation_summary = {
        "enabled": True,
        "method": "piecewise_linear_calendar_time_resampling_display_only",
        "visualization_only": True,
        "source_observation_interval_hours": COMMON_INTERVAL_HOURS,
        "display_frame_interval_hours_mean": float(np.mean(target_interval_hours)),
        "display_frame_interval_hours_min": float(np.min(target_interval_hours)),
        "display_frame_interval_hours_max": float(np.max(target_interval_hours)),
        "input_observation_frame_count": int(source_count),
        "display_payload_frame_count": int(display_frame_count),
        "displayed_real_anchor_frame_count": int(real_anchor_count),
        "interpolated_frame_count": int(interpolated_count),
        "gap_hold_frame_count": int(gap_hold_count),
        "virtual_frame_count": int(interpolated_count),
        "not_used_as_display_anchor_observation_count": int(source_count - real_anchor_count),
        "preserves_source_time_span": True,
        "preserves_frame_count_and_duration": False,
        "source_observation_time_positions_retained_as_anchors": True,
        "same_frame_count_as_source": False,
        "no_interpolation_across_gap": True,
        "input_data_unchanged": True,
        "formula": "field_display(t) = (1-alpha)*field_observed(t_left) + alpha*field_observed(t_right)",
        "mask_policy": (
            "interpolate only where both endpoint u/v pairs are finite and render_mask is true; "
            "otherwise NaN; gap intervals use nearest observed payload without interpolation"
        ),
        "target_display_time_ns": [int(value) for value in target_times.tolist()],
        "target_display_start_utc": _format_time_utc(int(target_times[0])),
        "target_display_end_utc": _format_time_utc(int(target_times[-1])),
        "source_mapping_index_sample": {
            "first": int(display_source_index[0]),
            "middle": int(display_source_index[len(display_source_index) // 2]),
            "last": int(display_source_index[-1]),
        },
        "interpolation_alpha_min": (
            float(min(record["alpha"] for record in interpolation_records))
            if interpolation_records
            else None
        ),
        "interpolation_alpha_max": (
            float(max(record["alpha"] for record in interpolation_records))
            if interpolation_records
            else None
        ),
        "interpolation_record_sample": interpolation_records[:5],
        "interpolation_record_sample_last": interpolation_records[-5:],
        "note_zh": (
            "以所有共同 source-valid、非 imputed 的實測觀測作為時間錨點，將完整日曆時間"
            "範圍均勻重採樣為固定展示影格；正常 6 小時相鄰觀測間做 u/v 線性內插，"
            "資料缺口不跨越內插而採最近有效觀測保持。這是為簡報縮短播放時間的"
            "display-only 轉換，不代表新增觀測、不改寫原始 cache 或正式 SVD。"
        ),
    }
    return output


def _set_temporal_smoothing_disabled(dataset: Any) -> None:
    """記錄不做展示平滑時的精確 6 小時 payload 語意。"""

    dataset.temporal_interpolation_summary = {
        "enabled": False,
        "method": "none; exact 6-hour observed payload display",
        "visualization_only": True,
        "source_observation_interval_hours": COMMON_INTERVAL_HOURS,
        "virtual_frame_interval_hours": None,
        "alpha": None,
        "input_observation_frame_count": int(len(dataset.payloads)),
        "display_payload_frame_count": int(len(dataset.payloads)),
        "displayed_real_observation_time_position_count": int(len(dataset.payloads)),
        "virtual_frame_count": 0,
        "all_source_observations_retained_as_display_anchors": True,
        "preserves_source_window": True,
        "no_smoothing_across_gap": True,
        "input_data_unchanged": True,
        "note_zh": "未啟用時間平滑；每一幀直接使用精確 6 小時共同時間的原始觀測。",
    }


def _frame_plan(payload_count: int) -> list[int]:
    """建立與 source observation 數量完全相同的連續播放順序。

    本版刻意不加入片頭／片尾停格：任意 n 個 display frames 在 4 fps 就是 n/4 秒。
    因此兩年完整版的片長由實際共同 source-valid 時間位置數決定；poster 另行輸出，
    不需要以額外影片影格承擔靜態預覽功能。
    """

    if payload_count < 2:
        raise ValueError("長時窗影片至少需要兩個 display payload")
    return list(range(payload_count))


def _render_region(
    dataset: Any,
    *,
    output_dir: Path,
    width: int,
    height: int,
    dpi: int,
    fps: int,
    target_arrows: int,
    quiver_scale_multiplier: float,
    font: Any | None,
    temporal_display_processed: bool,
    show_title: bool,
    overwrite: bool,
) -> dict[str, Any]:
    """串流輸出單區 MP4、poster、起中末代表影格及 contact sheet。

    MP4 只在記憶體中保留目前 scene 的 RGB frame，避免長時窗的全部影像一次堆入
    RAM。poster 與 QA 圖直接由同一 scene 產生，確保版面、色階、岸線、箭頭與
    影片完全一致；``window_middle`` 對應展示時間軸中間的時間位置，若套用三點
    平滑或日曆時間重採樣，其畫面值是展示轉換結果而非原始陣列覆寫。``show_title=False``
    時不建立 figure-level 主標題，保留既有主圖框與上方安全區供簡報疊加可編輯
    標題；因此無標題版和含標題版仍共享完全相同的資料與圖框。
    """

    if imageio is None or not hasattr(imageio, "get_writer"):
        raise ImportError("輸出 MP4 需要 imageio")
    output_dir.mkdir(parents=True, exist_ok=True)
    (
        mp4_name,
        poster_name,
        start_name,
        middle_name,
        end_name,
        contact_name,
    ) = _continuous_output_names(
        dataset,
        temporal_display_processed,
        len(dataset.payloads),
        fps,
        show_title,
        full_period=dataset.selection_details.get(
            "mode"
        ) == "all_shared_source_valid_non_imputed_observations_2024_2025",
        display_resampled=bool(getattr(dataset, "display_resampled", False)),
    )
    mp4_path = output_dir / mp4_name
    poster_path = output_dir / poster_name
    start_path = output_dir / start_name
    middle_path = output_dir / middle_name
    end_path = output_dir / end_name
    targets = [mp4_path, poster_path, start_path, middle_path, end_path]
    if not overwrite:
        existing = [str(path) for path in targets if path.exists()]
        if existing:
            raise FileExistsError("輸出已存在，為避免覆寫請改用 --overwrite：" + ", ".join(existing))

    scene = _create_raw_scene(
        dataset,
        width=width,
        height=height,
        dpi=dpi,
        target_arrows=target_arrows,
        quiver_scale_multiplier=quiver_scale_multiplier,
        font=font,
        show_title=show_title,
    )
    plan = _frame_plan(len(dataset.payloads))
    first_rgb = _update_raw_scene(scene, dataset, dataset.payloads[plan[0]])
    scene.fig.savefig(poster_path, dpi=dpi, facecolor="white")
    _update_raw_scene(scene, dataset, dataset.payloads[0])
    scene.fig.savefig(start_path, dpi=dpi, facecolor="white")
    middle_index = len(dataset.payloads) // 2
    _update_raw_scene(scene, dataset, dataset.payloads[middle_index])
    scene.fig.savefig(middle_path, dpi=dpi, facecolor="white")
    _update_raw_scene(scene, dataset, dataset.payloads[-1])
    scene.fig.savefig(end_path, dpi=dpi, facecolor="white")

    contact_frames: dict[int, np.ndarray] = {0: first_rgb}
    _update_raw_scene(scene, dataset, dataset.payloads[plan[0]])
    ffmpeg_executable = _configure_imageio_ffmpeg()
    writer = imageio.get_writer(
        str(mp4_path),
        mode="I",
        fps=fps,
        codec="libx264",
        quality=10,
        macro_block_size=1,
        ffmpeg_log_level="error",
        output_params=[
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-movflags",
            "+faststart",
            "-crf",
            "16",
            "-preset",
            "slow",
        ],
    )
    try:
        for ordinal, payload_index in enumerate(plan):
            rgb = first_rgb if ordinal == 0 else _update_raw_scene(scene, dataset, dataset.payloads[payload_index])
            writer.append_data(rgb)
            if ordinal in {len(plan) // 2, len(plan) - 1}:
                contact_frames[ordinal] = rgb.copy()
            if (ordinal + 1) % 16 == 0 or ordinal + 1 == len(plan):
                print(f"{dataset.spec.key} rendered {ordinal + 1}/{len(plan)} frames", flush=True)
    finally:
        writer.close()
        import matplotlib.pyplot as plt

        plt.close(scene.fig)

    qa_dir = output_dir / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    ordered = [contact_frames[index] for index in (0, len(plan) // 2, len(plan) - 1)]
    contact = np.concatenate(ordered, axis=1)
    contact_path = qa_dir / contact_name
    imageio.imwrite(contact_path, contact)
    return {
        "mp4": {
            "filename": mp4_path.name,
            "server_path": str(mp4_path.resolve()),
            "frame_count_expected": len(plan),
            "fps_requested": fps,
            "duration_expected_seconds": len(plan) / fps,
            "codec_requested": "libx264",
            "pixel_format_requested": "yuv420p",
            "audio": False,
            "ffmpeg_executable": ffmpeg_executable,
            "sha256": sha256_file(mp4_path),
            "ffprobe": _ffprobe(mp4_path),
        },
        "poster": {
            "filename": poster_path.name,
            "server_path": str(poster_path.resolve()),
            "sha256": sha256_file(poster_path),
        },
        "qa_frames": {
            "window_start": {
                "filename": start_path.name,
                "server_path": str(start_path.resolve()),
                "sha256": sha256_file(start_path),
            },
            "window_middle": {
                "filename": middle_path.name,
                "server_path": str(middle_path.resolve()),
                "sha256": sha256_file(middle_path),
            },
            "window_end": {
                "filename": end_path.name,
                "server_path": str(end_path.resolve()),
                "sha256": sha256_file(end_path),
            },
        },
        "contact_sheet": {
            "filename": contact_path.name,
            "server_path": str(contact_path.resolve()),
            "sha256": sha256_file(contact_path),
        },
        "render_size_px": [width, height],
        "render_dpi": dpi,
        "temporal_smoothed": temporal_display_processed,
        "temporal_display_processed": temporal_display_processed,
        "display_resampled": bool(getattr(dataset, "display_resampled", False)),
        "quiver_reference_mps": float(dataset.quiver_reference_mps),
        "effective_quiver_scale": float(scene.quiver.scale),
        "raw_arrow_p95_diagnostic_mps": float(
            getattr(dataset, "raw_arrow_p95_diagnostic_mps", dataset.quiver_reference_mps)
        ),
        "frame_plan": {
            "intro_hold_frames": INTRO_HOLD_FRAMES,
            "continuous_data_frames": len(dataset.payloads),
            "outro_hold_frames": OUTRO_HOLD_FRAMES,
            "total_frames": len(plan),
        },
        "colorbar_alignment": scene.colorbar_alignment,
        "arrow_key_layout": scene.arrow_key_layout,
        "x_tick_label_layout": scene.axis_tick_layout,
        "land_patch_count": scene.land_patch_count,
    }


def _continuous_region_manifest(
    dataset: Any,
    render_summary: dict[str, Any],
    full_audit: dict[str, Any],
    selection_details: dict[str, Any],
    *,
    shared_vmax: bool,
    temporal_display_processed: bool,
    show_title: bool,
) -> dict[str, Any]:
    """建立單區長時窗 manifest，明確分離觀測時間與展示時間轉換。

    完整兩年縮短版的 source time count 與 display frame count 不同；此函式因此
    同時保存正式共同觀測的精確時間軸，以及 renderer 產生的均勻目標展示時間軸，
    避免把時間重採樣後的場誤讀為新增觀測。
    """

    bbox = [float(dataset.lon[0]), float(dataset.lon[-1]), float(dataset.lat[0]), float(dataset.lat[-1])]
    speed_vmax = float(dataset.speed_scale_vmax)
    speed_tick_step = float(getattr(dataset, "speed_tick_step_mps", RAW_DEFAULT_SPEED_TICK_STEP_MPS))
    selected_times = [int(record.time_ns) for record in dataset.selected_records]
    # manifest 只需要中間時間位置，不應在完整兩年版為了寫文件而重新依賴
    # 已釋放的全時段 payload 陣列；FrameRecord 本身已保存精確 UTC epoch-ns。
    middle_record = dataset.selected_records[len(dataset.selected_records) // 2]
    temporal_summary = dataset.temporal_interpolation_summary
    display_time_ns = temporal_summary.get("target_display_time_ns")
    if isinstance(display_time_ns, list) and display_time_ns:
        display_middle_time_ns = int(display_time_ns[len(display_time_ns) // 2])
    else:
        display_middle_time_ns = int(middle_record.time_ns)
    display_count = int(selection_details.get("display_frame_count", len(dataset.payloads)))
    return {
        "region_key": dataset.spec.key,
        "region_name_zh": dataset.spec.name_zh,
        "display_title": f"{dataset.spec.name_zh} 原始流場",
        "title_visible": bool(show_title),
        "title_removed_for_editable_ppt_overlay": not bool(show_title),
        "region_short_name": dataset.spec.short_name,
        "formal_svd_source": str(dataset.svd_dir),
        "svd_source_unchanged": True,
        "coastline_correction_scope": "visualization_only",
        "coastline": dataset.coastline_summary,
        "source_mode": dataset.source_mode,
        "surface_source": (
            "same-source published OCM surface u/v cache"
            if dataset.source_mode == "same_source_surface_cache"
            else "external full-Taiwan product remeshed to SVD grid"
        ),
        "raw_surface_only": True,
        "reconstruction_rendered": False,
        "display_extent_source": dataset.display_axis_spec["display_extent_source"],
        "display_extent": [float(value) for value in dataset.display_axis_spec["display_extent"]],
        "display_axis_spec": dataset.display_axis_spec,
        "grid": {
            "shape_lat_lon": [int(dataset.lat.size), int(dataset.lon.size)],
            "bbox_lon_min_lon_max_lat_min_lat_max": bbox,
            "lon_min": float(dataset.lon.min()),
            "lon_max": float(dataset.lon.max()),
            "lat_min": float(dataset.lat.min()),
            "lat_max": float(dataset.lat.max()),
        },
        "mask": {
            "semantic_definitions": {
                "exact_land": "GeoJSON cell-overlap center/corners/ring-vertex raster mask; audit only",
                "analysis_geometry_outside": "outside the SVD analysis geometry; not land",
                "model_static_outside": "inside analysis geometry but outside static ocean mask",
                "surface_feature_unavailable": "static ocean cell not in surface velocity feature mask",
                "temporal_invalid": "raw surface u/v invalid at this frame",
            },
            "exact_coastline_land_cell_count": int(np.count_nonzero(dataset.coastline_land_mask)),
            "exact_coastline_land_fraction": float(np.mean(dataset.coastline_land_mask)),
            "analysis_geometry_outside_cell_count": int(np.count_nonzero(~dataset.analysis_geometry_mask)),
            "render_mask_fraction": float(np.mean(dataset.render_mask)),
            "render_mask_definition": "static_ocean & analysis_geometry & surface_velocity_feature; vector coastline overlay hides visible land",
            "raster_land_background_visible": False,
        },
        "time_selection_internal": {
            **selection_details,
            "region_key": dataset.spec.key,
            "source_frame_times_utc": {
                "start": _format_time_utc(selected_times[0]),
                "end": _format_time_utc(selected_times[-1]),
            },
            "display_frame_count": display_count,
            "display_frame_times_utc": {
                "start": _format_time_utc(
                    int(display_time_ns[0]) if isinstance(display_time_ns, list) and display_time_ns else selected_times[0]
                ),
                "end": _format_time_utc(
                    int(display_time_ns[-1]) if isinstance(display_time_ns, list) and display_time_ns else selected_times[-1]
                ),
            },
            "source_valid_non_imputed_only": True,
            "interval_hours": COMMON_INTERVAL_HOURS,
            "display_middle_time_utc": _format_time_utc(display_middle_time_ns),
            "display_middle_is_temporally_processed": bool(temporal_display_processed),
        },
        "source_products": {
            "full_taiwan_1km_6h_audit_dir": str(full_audit["dir"]),
            "full_taiwan_metadata_sha256": full_audit["metadata_sha256"],
            "surface_cache_root": str(dataset.cache_root),
            "surface_cache_grid_metadata_sha256": dataset.cache_metadata.get("grid_metadata_sha256"),
            "surface_month_metadata_sha256": dataset.cache_meta_hashes,
        },
        "temporal_display_processing": temporal_summary,
        # 保留舊欄位名稱供既有 validator／閱讀工具相容；內容可為三點平滑或
        # 完整期間的日曆時間重採樣，不能僅由欄位名稱推定演算法。
        "temporal_smoothing": temporal_summary,
        "visual_spec": {
            "figure_size_px": render_summary["render_size_px"],
            "render_dpi": render_summary["render_dpi"],
            "raw_only": True,
            "title": f"{dataset.spec.name_zh} 原始流場" if show_title else None,
            "title_visible": bool(show_title),
            "title_fontsize_points": 14.0 if show_title else None,
            "title_y_fraction": float(RAW_TITLE_Y_FRACTION) if show_title else None,
            "main_axes_fraction_requested": list(RAW_AXES_RECT),
            "panel_layout": {
                "mode": "uniform_2x2_fixed_axes_rectangle",
                "display_aspect_mode": "auto_fixed_rectangle",
                "main_axes_bbox_px": render_summary["x_tick_label_layout"].get("main_axes_bbox_px"),
                "reason": "A–D 在簡報 2×2 排列時共用主圖寬高、軸線基準與底部留白",
                "scientific_scope": "presentation_only; lon/lat limits, raw u/v and formal SVD unchanged",
                "known_tradeoff": "各區地理範圍比例不同時，固定展示框可能產生有限的縱橫顯示比例差異",
            },
            "colorbar_fraction_initial": list(RAW_COLORBAR_RECT),
            "colorbar_alignment": render_summary["colorbar_alignment"],
            "colorbar_label": "流速（公尺／秒）",
            "colorbar_label_rotation_degrees": 90,
            "colorbar_label_direction": "same as latitude axis label",
            "colorbar_ticks_mps": [float(value) for value in _raw_ticks(speed_vmax, speed_tick_step)],
            "colorbar_tick_spacing_mps": speed_tick_step,
            "fixed_speed_vmin_mps": 0.0,
            "fixed_speed_vmax_mps": speed_vmax,
            "fixed_speed_scope_shared_across_regions": shared_vmax,
            "arrow_legend": {
                "label": "1 公尺／秒",
                "reference_mps": 1.0,
                "artist": "Matplotlib QuiverKey using the same raw-surface quiver artist, U=1.0",
                "inside_main_map": True,
                "layout": render_summary["arrow_key_layout"],
                "font_size_points": RAW_ARROW_FONT_SIZE_PT,
                "color": "#ffffff",
                "scale_policy": "fixed_cross_region_display_scale",
                "quiver_reference_mps": render_summary["quiver_reference_mps"],
                "effective_quiver_scale": render_summary["effective_quiver_scale"],
                "raw_arrow_p95_diagnostic_mps": render_summary["raw_arrow_p95_diagnostic_mps"],
            },
            "axis_labels": {"x": "經度（°E）", "y": "緯度（°N）"},
            "axis_ticks": {
                "x": {
                    "major_locator": "FixedLocator",
                    "major_values": [float(value) for value in dataset.display_axis_spec["x_major_values"]],
                    "major_formatter": str(dataset.display_axis_spec["x_major_formatter"]),
                    "minor_locator": "MultipleLocator(0.2)",
                    "minor_labels": False,
                    "label_bbox_qa": render_summary["x_tick_label_layout"],
                },
                "y": {
                    "major_locator": "FixedLocator",
                    "major_values": [float(value) for value in dataset.display_axis_spec["y_major_values"]],
                    "major_formatter": str(dataset.display_axis_spec["y_major_formatter"]),
                },
            },
            "land_overlay": {
                "source": dataset.coastline_summary["path"],
                "sha256": dataset.coastline_summary["sha256"],
                "facecolor": "#a29d93",
                "edgecolor": "none",
                "linewidth": 0.0,
                "antialiased": True,
                "visible_source": "high-resolution GeoJSON vector polygon fill",
                "raster_land_background_visible": False,
            },
            "display_text": _visible_text_spec(dataset, show_title=show_title),
        },
        "outputs": render_summary,
        "limitations": [
            "本版只顯示同源 OCM 原始表層流場，不顯示 SVD 模態重建；正式 SVD 未被改寫。",
            (
                "本版使用四區共同 source-valid、非 imputed 的全部 2024--2025 實測時間位置；"
                "保留資料缺口，不以補值影格取代。"
                if selection_details.get("mode") == "all_shared_source_valid_non_imputed_observations_2024_2025"
                else "長時窗由四區共同 source-valid、非 imputed 的連續 6 小時時間交集選取；選窗評分沿用正式 SVD 第一時間係數，僅供展示選段，不是統計代表性結論。"
            ),
            "時間平滑只使用相鄰 6 小時觀測；所有觀測時間位置保留，不跨缺口，也不代表新增觀測。",
            "圖內箭頭為視覺抽樣，pcolormesh 仍保留規則網格；箭頭比例尺 U=1.0。",
            "exact coastline 只在展示階段遮蔽陸地色塊與箭頭並疊加向量 polygon，不改變原始 cache 或正式 SVD。",
        ],
    }


def _build_manifest(
    args: argparse.Namespace,
    datasets: Sequence[Any],
    summaries: Sequence[dict[str, Any]],
    full_audit: dict[str, Any],
    selection_details: dict[str, Any],
    shared_vmax: float,
    arrow_scale_details: dict[str, Any],
) -> dict[str, Any]:
    """組合連續長時窗版本的單一 manifest。"""

    # 主程式在四區逐一渲染後會釋放大型 payload；因此 display count 必須在選取
    # 階段寫入 selection_details，而不能到這裡再依賴 dataset.payloads 的長度。
    # 完整版若未指定重採樣，display count 等於 source count；3 分鐘版則為 720。
    display_count = int(selection_details.get("display_frame_count", selection_details["source_frame_count"]))
    total_frames = INTRO_HOLD_FRAMES + display_count + OUTRO_HOLD_FRAMES
    temporal_summary = datasets[0].temporal_interpolation_summary if datasets else {}
    return {
        "schema_name": "ocm_raw_surface_only_continuous_animation_manifest",
        "schema_version": SCRIPT_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "renderer": str(Path(__file__).resolve()),
        "purpose": "四個海域單一連續長時窗純原始表層流場動畫；供簡報 2×2 版面使用",
        "raw_surface_only": True,
        "reconstruction_rendered": False,
        "svd_source_unchanged": True,
        "coastline_correction_scope": "visualization_only",
        "formal_svd_source": str(args.svd_base.resolve()),
        "formal_svd_source_policy": "只讀既有 2026-08-13 water-column SVD；不執行 SVD 重算",
        "academic_semantics_manifest_only": "六層聯合 SVD 模態之表層分量；本版畫面只展示原始表層流場",
        "coastline": datasets[0].coastline_summary if datasets else None,
        "coastline_usage": "exact coastline 只在 renderer 階段遮蔽可見陸地流速／箭頭並疊加高解析度向量 polygon",
        "render_scope": [dataset.spec.key for dataset in datasets],
        "time_window": selection_details,
        "render_policy": {
            "width_px": args.width,
            "height_px": args.height,
            "fps": args.fps,
            "raster_dpi": args.dpi,
            "expected_duration_seconds": total_frames / args.fps,
            "expected_frame_count": total_frames,
            "no_audio": True,
            "title_visible": bool(args.show_title),
            "title_removed_for_editable_ppt_overlay": not bool(args.show_title),
            "codec": "libx264",
            "pixel_format": "yuv420p",
            "h264_crf": 16,
            "h264_preset": "slow",
            "target_arrows": args.target_arrows,
            "quiver_scale_multiplier": args.quiver_scale_multiplier,
            "quiver_scale_policy": {
                **arrow_scale_details,
                "scale_multiplier": float(args.quiver_scale_multiplier),
                "effective_quiver_scale": float(
                    COMMON_QUIVER_REFERENCE_MPS * args.quiver_scale_multiplier
                ),
                "legend_font_size_points": RAW_ARROW_FONT_SIZE_PT,
            },
            "panel_layout": {
                "mode": "uniform_2x2_fixed_axes_rectangle",
                "main_axes_fraction": list(RAW_AXES_RECT),
                "title_safe_overlay_height_px": int(
                    round((1.0 - RAW_AXES_RECT[1] - RAW_AXES_RECT[3]) * args.height)
                ),
                "title_overlay_y_fraction": float(RAW_TITLE_Y_FRACTION),
                "title_safe_overlay_scope": (
                    "PPTX editable title area; renderer title hidden in this variant"
                    if not args.show_title
                    else "renderer title occupies the top title band; no PPTX overlay required"
                ),
                "display_aspect_mode": "auto_fixed_rectangle",
                "all_regions_same_main_axes_bbox_required": True,
                "all_regions_same_colorbar_bbox_required": True,
                "scientific_scope": "presentation_only; no data/SVD modification",
            },
            "fixed_speed_vmin_mps": 0.0,
            "fixed_speed_vmax_mps": float(shared_vmax),
            "fixed_speed_ticks_mps": [float(value) for value in _raw_ticks(shared_vmax, args.speed_tick_step)],
            "fixed_speed_tick_spacing_mps": float(args.speed_tick_step),
            "fixed_speed_scale_display_only": True,
            "values_above_vmax_are_saturated": True,
            "source_observation_frame_count": int(selection_details["source_frame_count"]),
            "display_data_frame_count": int(display_count),
            "display_resampling_enabled": bool(selection_details.get("display_resampling_enabled", False)),
            "display_frame_target_count": (
                int(selection_details["display_frame_target_count"])
                if selection_details.get("display_frame_target_count") is not None
                else None
            ),
            "intro_hold_frames": INTRO_HOLD_FRAMES,
            "outro_hold_frames": OUTRO_HOLD_FRAMES,
        },
        # 這個欄位名稱沿用歷史 manifest schema，以免既有檢視器失效；實際方法與
        # 內插／平滑數量完整取自 dataset 的 summary，另提供語意較清楚的新欄位。
        "temporal_smoothing": temporal_summary,
        "temporal_display_processing": temporal_summary,
        "source_audit": {
            "full_taiwan_product_dir": str(full_audit["dir"]),
            "full_taiwan_metadata_sha256": full_audit["metadata_sha256"],
            "full_taiwan_time_count": int(full_audit["time_count"]),
            "source_valid_count": int(np.count_nonzero(full_audit["source_valid"])),
            "imputed_count": int(np.count_nonzero(full_audit["imputed"])),
            "source_valid_non_imputed_count": int(
                np.count_nonzero(full_audit["source_valid"] & ~full_audit["imputed"])
            ),
        },
        "visible_text_policy": {
            "audience_visible_scope": (
                "title + axes + fixed speed colorbar + in-map 1 m/s scale only"
                if args.show_title
                else "axes + fixed speed colorbar + in-map 1 m/s scale only; title reserved for PPT overlay"
            ),
            "main_title_visible": bool(args.show_title),
            "main_title_removed_for_editable_ppt_overlay": not bool(args.show_title),
            "phase_utc_line_visible": False,
            "panel_caption_visible": False,
            "pc_values_visible": False,
            "k_symbols_visible": False,
            "forbidden_tokens": ["PC", "PC1", "PC2", "PC3", "PC4", "K", "K90", "解釋變異"],
        },
        "regions": [
            _continuous_region_manifest(
                dataset,
                summary,
                full_audit,
                selection_details,
                shared_vmax=True,
                temporal_display_processed=bool(
                    dataset.temporal_interpolation_summary.get("enabled", False)
                ),
                show_title=args.show_title,
            )
            for dataset, summary in zip(datasets, summaries)
        ],
    }


def _arrow_scale_consistency_qa(summaries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """檢查 A--D 是否真的使用同一個 quiver scale 與相同比例尺群組尺寸。

    這個 QA 不以各區流速 p95 作為通過條件，因為本版刻意不讓資料分布決定圖例
    大小；它只比較 renderer 實際寫入 scene 的參考量、有效 scale 與圖例 bbox。
    因而可以抓出 D 區因局地低流速而意外被放大的回歸問題。所有差異均以像素或
    浮點絕對差記錄，供本機 validator 與人工 2×2 檢查共同使用。
    """

    references = [float(item.get("quiver_reference_mps", np.nan)) for item in summaries]
    effective_scales = [float(item.get("effective_quiver_scale", np.nan)) for item in summaries]
    widths = []
    for item in summaries:
        bbox = item.get("arrow_key_layout", {}).get("group_bbox_px")
        if isinstance(bbox, list) and len(bbox) == 4:
            widths.append(float(bbox[2]) - float(bbox[0]))
    reference_spread = float(np.ptp(references)) if references else None
    scale_spread = float(np.ptp(effective_scales)) if effective_scales else None
    width_spread = float(np.ptp(widths)) if widths else None
    passed = bool(
        len(references) == len(summaries)
        and len(effective_scales) == len(summaries)
        and len(widths) == len(summaries)
        and reference_spread is not None
        and scale_spread is not None
        and width_spread is not None
        and reference_spread <= 1.0e-9
        and scale_spread <= 1.0e-9
        and width_spread <= 1.0
    )
    return {
        "mode": "fixed_cross_region_display_scale",
        "reference_mps": references,
        "effective_quiver_scale": effective_scales,
        "legend_group_width_px": widths,
        "reference_spread_mps": reference_spread,
        "effective_scale_spread": scale_spread,
        "legend_group_width_spread_px": width_spread,
        "tolerance_reference_mps": 1.0e-9,
        "tolerance_effective_scale": 1.0e-9,
        "tolerance_legend_group_width_px": 1.0,
        "passed": passed,
    }


def _write_readme(path: Path, manifest: dict[str, Any]) -> None:
    """寫出長時窗成果的繁體中文資料與限制說明。"""

    policy = manifest["render_policy"]
    window = manifest["time_window"]
    temporal = manifest["temporal_smoothing"]
    full_period = window.get("mode") == "all_shared_source_valid_non_imputed_observations_2024_2025"
    display_resampled = temporal.get("method") == "piecewise_linear_calendar_time_resampling_display_only"
    display_count = int(policy["display_data_frame_count"])
    ticks = ", ".join(f"{value:.1f}" for value in policy["fixed_speed_ticks_mps"])
    title_visible = bool(policy.get("title_visible", True))
    title_safe_height_px = int(
        round((1.0 - RAW_AXES_RECT[1] - RAW_AXES_RECT[3]) * policy["height_px"])
    )
    lines = [
        (
            "# 四海域純原始表層流場動畫—2024–2025 全期間 3 分鐘重採樣版"
            if display_resampled
            else "# 四海域純原始表層流場動畫—單一連續長時窗版"
        )
        + ("（無內嵌主標題）" if not title_visible else ""),
        "",
        "本目錄只提供 A、B、C、D 四個海域的原始 OCM 表層流場，不包含模態重建面板。",
        "版面沿用已核對的 raw-only 2×2 簡報比例；正式水柱 SVD 僅用於時間軸追溯與",
        "長時窗選取，不會被重算或覆寫。",
        "",
        "## 時間範圍與影格語意",
        "",
        (
            f"- 四區使用 2024--2025 全部共同 source-valid、非 imputed 的實測時間位置："
            f"`{window['start_utc']}` 至 `{window['end_utc']}`。"
            if full_period
            else f"- 四區使用同一段連續 UTC 時窗：`{window['start_utc']}` 至 `{window['end_utc']}`。"
        ),
        (
            f"- source observation：{window['source_frame_count']} 個精確 6 小時觀測時間位置；"
            f"日曆涵蓋約 {window.get('calendar_span_days_inclusive', window.get('slot_coverage_days', 0.0)):.2f} 日，"
            f"實測時槽覆蓋約 {window.get('observed_slot_coverage_days', window.get('slot_coverage_days', 0.0)):.2f} 日。"
            if full_period
            else f"- source observation：{window['source_frame_count']} 個精確 6 小時觀測時間位置；"
            f"  timestamp 端點差約 {window['endpoint_span_hours']:.2f} 小時，時槽覆蓋約 {window['slot_coverage_days']:.2f} 日。"
        ),
        (
            f"- 共同時間序列保留 {window.get('gap_count', 0)} 個資料缺口，不跨缺口平滑；缺口不是以補值影格取代。"
            if full_period
            else "- 選窗要求四區共同 source-valid、非 imputed、相鄰 6 小時無缺口；選窗分數"
            "  使用既有正式 SVD 的第一時間係數視窗變異，只是展示選段規則，不是統計或氣候結論。"
        ),
        f"- temporal display processing enabled：`{temporal['enabled']}`；method：`{temporal['method']}`。",
        (
            f"- source frames={window['source_frame_count']}；display frames={display_count}；"
            f"線性內插影格={temporal.get('interpolated_frame_count', 0)}；"
            f"資料缺口保持影格={temporal.get('gap_hold_frame_count', 0)}。"
            if display_resampled
            else f"- 啟用時在相鄰 6 小時觀測位置套用三點時間平滑；source frames 與 display frames 均為"
            f"  {temporal['display_payload_frame_count']}，平滑內部影格數={temporal.get('smoothed_interior_frame_count', 0)}。"
        ),
        (
            "  目標展示時間均勻覆蓋完整起訖日期；正常 6 小時相鄰觀測間做 u/v 線性內插，"
            "資料缺口不跨越內插而採最近有效觀測保持。這不是新增觀測，不改寫原始 cache。"
            if display_resampled
            else "  這不是七日內插、不代表新增觀測、不跨越資料缺口，且不改寫原始 cache。"
        ),
        f"- 影片為 {policy['expected_frame_count']} 幀、{policy['fps']} fps、約",
        f"  {policy['expected_duration_seconds']:.2f} 秒；不另加片頭／片尾影格。",
        "",
        "## 畫面與資料規格",
        "",
        "- 四區標題為 `海域 A（東北角） 原始流場`、`海域 B（新竹外海） 原始流場`、",
        "  `海域 C（後灣海域） 原始流場`、`海域 D（連江海域） 原始流場`。",
        "- 固定流速色階 0.0--0.8 m/s，刻度為 `" + ticks + "`；超過上限的畫面色彩飽和，",
        "  不修改原始速度數值。",
        (
            f"- 本版移除影片內嵌主標題；保留約 {title_safe_height_px} px 上方安全區，"
            "建議在 PPTX 以可編輯文字疊加各海域名稱。"
            if not title_visible
            else "- 影片內嵌各海域主標題；若需簡報後製，可改用同時間窗的無內嵌主標題版本。"
        ),
        "- 色條標籤為 `流速（公尺／秒）`，與緯度軸同方向；圖內 `1 公尺／秒` 比例尺",
        "  使用同一個 raw-surface QuiverKey、U=1.0，不是手繪圖示。四區固定使用同一",
        f"  quiver scale（參考速度 {COMMON_QUIVER_REFERENCE_MPS:.1f} m/s、倍率 {policy['quiver_scale_multiplier']:.1f}）；",
        "  各區 p95 僅列為診斷，不會改變比例尺大小。",
        "- 高解析度 exact coastline vector polygon 只在展示階段覆蓋陸地色塊與箭頭；",
        "  保守 raster land mask 只供 audit，不繪製階梯狀可見海岸線。",
        "- 為使影片在簡報 2×2 排列時一致，四區共用固定主圖／色條矩形；這是展示版面",
        "  取捨，不應用於從影片像素量測地理距離。原始 lon/lat 與各區 display extent 未改變。",
        "",
        "## 科學範圍與限制",
        "",
        f"- 正式 SVD 根目錄：`{manifest['formal_svd_source']}`；`svd_source_unchanged=true`。",
        "- `coastline_correction_scope=visualization_only`；exact coastline 不重新定義",
        "  既有 SVD 有效域，只阻止真實陸地上的速度色塊與箭頭出現在觀眾畫面。",
        "- 原始流場優先讀取與正式 SVD 同源的 `preprocessed/ocm_surface` cache；實際",
        "  source mode、cache metadata 雜湊與完整 6 小時產品 QC 摘要寫入 manifest。",
        (
            "- 本版日曆時間重採樣是局部展示轉換；展示影格不是逐一播放所有 6 小時觀測，"
            "短於展示時間步階的真實變化不一定被保留，因此不可把內插場當成原始觀測值。"
            if display_resampled
            else "- 本版三點時間平滑是局部展示轉換；短於 6 小時的真實變化不一定被保留，"
            "  因此不可把平滑後的場當成原始觀測值或新的分析結果。"
        ),
        "",
        "## 輸出與 QA",
        "",
        "- 每區包含 MP4、poster、window start/middle/end QA 幀及 `qa/*contact.png`。",
        "- `animation_manifest.json` 記錄實際時間窗、source/display/virtual frame count、",
        "  formal SVD、exact coastline SHA-256、固定色階、共用 quiver scale、版面 bbox、編碼與雜湊。",
        "- `qa/local_validation.json` 為本機 ffprobe、PNG 尺寸、文字／版面 metadata、",
        "  岸線旗標與四區圖框一致性的綜合驗證；自動 QA 仍不能取代簡報 2×2 人工觀看。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    """解析連續長時窗 renderer 的 server/local 共用參數。"""

    parser = argparse.ArgumentParser(description="Render four continuous raw surface-current-only OCM animations.")
    parser.add_argument("--svd-base", type=Path, required=True, help="formal water-column SVD parent directory")
    parser.add_argument("--surface-cache-base", type=Path, required=True, help="same-source preprocessed/ocm_surface parent directory")
    parser.add_argument("--full-product-dir", type=Path, required=True, help="full Taiwan 1 km 6-hour product for time/QC audit")
    parser.add_argument("--coastline-geojson", type=Path, required=True, help="exact coastline GeoJSON used at render time")
    parser.add_argument("--output-dir", type=Path, required=True, help="new versioned output directory")
    parser.add_argument("--regions", default="A,B,C,D", help="comma-separated A/B/C/D")
    parser.add_argument("--source-frame-count", type=int, default=DEFAULT_SOURCE_FRAME_COUNT, help="number of actual 6-hour observations in the shared continuous window")
    parser.add_argument(
        "--all-source-valid",
        action="store_true",
        help="render all 2024-2025 four-region common source-valid/non-imputed observations; preserve gaps",
    )
    parser.add_argument(
        "--display-frame-count",
        type=int,
        default=None,
        help=(
            "固定完整時段的展示影格數；僅搭配 --all-source-valid 使用，"
            "正常相鄰觀測間做日曆時間 u/v 線性內插"
        ),
    )
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS, help="video fps")
    parser.add_argument("--width", type=int, default=RAW_WIDTH, help="video width")
    parser.add_argument("--height", type=int, default=RAW_HEIGHT, help="video height")
    parser.add_argument("--dpi", type=int, default=DEFAULT_RENDER_DPI, help="Matplotlib raster dpi")
    parser.add_argument("--target-arrows", type=int, default=DEFAULT_TARGET_ARROWS, help="approximate arrows per map")
    parser.add_argument(
        "--quiver-scale-multiplier",
        type=float,
        default=DEFAULT_COMMON_QUIVER_SCALE_MULTIPLIER,
        help="fixed cross-region quiver scale multiplier; larger means shorter arrows",
    )
    parser.add_argument("--fixed-speed-vmax", type=float, default=RAW_DEFAULT_SPEED_VMAX_MPS, help="fixed cross-region colorbar upper bound in m/s")
    parser.add_argument("--speed-tick-step", type=float, default=RAW_DEFAULT_SPEED_TICK_STEP_MPS, help="fixed colorbar major tick spacing in m/s")
    parser.add_argument("--font-path", type=Path, default=None, help="CJK font path")
    parser.add_argument(
        "--temporal-smoothing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="apply same-count centered three-observation temporal smoothing (default: enabled)",
    )
    parser.add_argument(
        "--show-title",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否在影片內嵌 figure-level 主標題；使用 --no-show-title 產生簡報後製版",
    )
    parser.add_argument("--overwrite", action="store_true", help="overwrite only files in explicitly supplied output directory")
    return parser.parse_args()


def main() -> None:
    """載入正式資料、選取共同長時窗、輸出四支 raw-only 動畫並寫入 QA。"""

    args = parse_args()
    if args.source_frame_count < 3 or args.fps <= 0 or args.width <= 0 or args.height <= 0 or args.dpi <= 0 or args.target_arrows <= 0 or args.quiver_scale_multiplier <= 0:
        raise ValueError("source-frame-count 至少 3，fps/width/height/dpi/target-arrows/quiver-scale-multiplier 必須為正值")
    if args.display_frame_count is not None:
        if not args.all_source_valid:
            raise ValueError("display-frame-count 只允許搭配 --all-source-valid，避免誤把長時段重採樣套到選窗模式")
        if args.display_frame_count < 2:
            raise ValueError("display-frame-count 至少需要 2 幀，才能保留完整起訖時間")
    if not np.isfinite(args.fixed_speed_vmax) or args.fixed_speed_vmax <= 0.0:
        raise ValueError("fixed-speed-vmax 必須為正有限值")
    if not np.isfinite(args.speed_tick_step) or args.speed_tick_step <= 0.0 or args.speed_tick_step > args.fixed_speed_vmax:
        raise ValueError("speed-tick-step 必須為正值且不可大於 fixed-speed-vmax")
    if args.width % 2 or args.height % 2:
        raise ValueError("H.264 yuv420p 輸出要求 width/height 為偶數")

    specs = build_region_specs(args.regions.split(","))
    full_audit = load_full_product_audit(args.full_product_dir)
    font = find_cjk_font(args.font_path)
    print(f"font={args.font_path if args.font_path else 'auto'} available={font is not None}", flush=True)

    datasets = []
    for spec in specs:
        dataset = load_region_dataset(
            spec,
            svd_base=args.svd_base,
            surface_cache_base=args.surface_cache_base,
            full_audit=full_audit,
            coastline_geojson=args.coastline_geojson,
            svd_directory_suffix="",
        )
        datasets.append(dataset)

    if args.all_source_valid:
        # 完整版不以 PC1 或單一代表窗口選段，而是直接使用四區共同可追溯的
        # 全部實測時間；後續平滑函式會在缺口邊界保留原始值。
        selected_times, selection_details = _select_all_shared_observations(datasets)
    else:
        selected_times, selection_details = _select_shared_continuous_window(datasets, args.source_frame_count)
    print(
        f"selected shared timeline {selection_details['start_utc']} -> {selection_details['end_utc']} "
        f"source_frames={selection_details['source_frame_count']} "
        f"mode={selection_details['mode']}",
        flush=True,
    )

    source_frame_count = int(selection_details["source_frame_count"])
    display_frame_count = (
        int(args.display_frame_count) if args.display_frame_count is not None else source_frame_count
    )
    if display_frame_count > source_frame_count:
        raise ValueError(
            f"display-frame-count={display_frame_count} 不可大於 source observation count={source_frame_count}"
        )
    # 讓 render、manifest 與 README 使用同一個顯示影格契約；source count 仍保持
    # 為實際觀測位置數，不能因 3 分鐘版而被覆寫成 720。
    selection_details = {
        **selection_details,
        "display_frame_count": display_frame_count,
        "display_resampling_enabled": bool(
            args.display_frame_count is not None and display_frame_count != source_frame_count
        ),
        "display_frame_target_count": (
            int(args.display_frame_count) if args.display_frame_count is not None else None
        ),
    }

    for dataset in datasets:
        _assign_shared_records(dataset, selected_times, selection_details)
        source_payloads = _materialize_raw_payloads(dataset, full_audit)
        dataset.display_resampled = False
        if args.display_frame_count is not None and display_frame_count != source_frame_count:
            # 3 分鐘完整期間版只做一次日曆時間重採樣；線性內插本身已把 6 小時
            # source 間的轉換變成均勻展示時間，不再疊加三點平滑以免過度模糊兩年趨勢。
            _resample_raw_payloads_to_count(dataset, source_payloads, display_frame_count)
            dataset.display_resampled = True
            source_payloads = []
        elif args.temporal_smoothing:
            _smooth_raw_payloads_same_count(dataset, source_payloads)
            source_payloads = []
        else:
            _set_temporal_smoothing_disabled(dataset)
            source_payloads = []
        _choose_raw_speed_scale(dataset)
        print(
            f"{dataset.spec.key} source_mode={dataset.source_mode} raw_p995={dataset.speed_scale_p995:.3f} "
            f"display_frames={len(dataset.payloads)}",
            flush=True,
        )

    # 比例尺只是四區共同的展示示意，不應因 D 區流速較低而被 renderer 放大；
    # p95 不再參與 scale 計算，只保留在本函式回傳的診斷欄位中。固定參考量配合
    # 同一 quiver_scale_multiplier，會讓四區的資料箭頭與 U=1.0 圖例使用同一幾何尺。
    arrow_scale_details = _set_common_quiver_scale(datasets)
    print(
        f"fixed shared quiver reference={COMMON_QUIVER_REFERENCE_MPS:.1f} m/s; "
        f"scale multiplier={args.quiver_scale_multiplier:.1f}",
        flush=True,
    )

    # 色階必須跨 A--D 固定，不能因長時窗或單區極端值而每區跳動；p99.5 只保留
    # 為 renderer 診斷，正式展示仍使用使用者已核對的 0.0--0.8 m/s 規格。
    auto_p995 = max(float(dataset.speed_scale_p995) for dataset in datasets)
    shared_vmax = float(args.fixed_speed_vmax)
    for dataset in datasets:
        dataset.speed_scale_vmax = shared_vmax
        dataset.speed_tick_step_mps = float(args.speed_tick_step)
    print(
        f"raw p99.5 diagnostic max={auto_p995:.3f} m/s; fixed display vmax={shared_vmax:.1f} m/s; "
        f"ticks every {args.speed_tick_step:.1f} m/s",
        flush=True,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for dataset in datasets:
        summaries.append(
            _render_region(
                dataset,
                output_dir=args.output_dir,
                width=args.width,
                height=args.height,
                dpi=args.dpi,
                fps=args.fps,
                target_arrows=args.target_arrows,
                quiver_scale_multiplier=args.quiver_scale_multiplier,
                font=font,
                temporal_display_processed=bool(
                    dataset.temporal_interpolation_summary.get("enabled", False)
                ),
                show_title=args.show_title,
                overwrite=args.overwrite,
            )
        )
        # 完整兩年版每區可能暫存約數千張規則格點場；render_region 已完成影片、
        # poster 與 contact sheet 後即可釋放該區 payload，避免 A--D 累積佔用多 GB
        # 記憶體。FrameRecord、選窗細節與 manifest 所需 metadata 仍保留。
        dataset.payloads = []
        gc.collect()

    manifest = _build_manifest(
        args,
        datasets,
        summaries,
        full_audit,
        selection_details,
        shared_vmax,
        arrow_scale_details,
    )
    manifest["qa"] = _run_manifest_qa(manifest, args.output_dir)
    manifest["qa"]["arrow_scale_consistency"] = _arrow_scale_consistency_qa(summaries)
    manifest["qa"]["all_passed"] = bool(
        manifest["qa"].get("all_passed") and manifest["qa"]["arrow_scale_consistency"]["passed"]
    )
    manifest_path = args.output_dir / "animation_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_readme(args.output_dir / "README.md", manifest)
    (args.output_dir / "RENDER_COMPLETE").write_text(
        (
            "A–D 單一連續長時窗純原始表層流場動畫已完成；"
            + ("本版移除內嵌主標題，供簡報後製疊加；" if not args.show_title else "")
            + "正式 SVD 未改寫，coastline 修正僅限 visualization-only。\n"
        ),
        encoding="utf-8",
    )
    print(f"manifest={manifest_path}", flush=True)
    print(json.dumps(manifest["qa"], ensure_ascii=False, indent=2), flush=True)
    # SERVER 可能沒有 ffprobe；此時保留 manifest 的工具不可用狀態，待同步回本機
    # 由本機 validator 重新執行完整技術 QA，不因工具缺少而刪除已完成的影像成果。


if __name__ == "__main__":
    main()
