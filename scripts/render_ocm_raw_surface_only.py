#!/usr/bin/env python3
"""產生四海域「純原始表層流場」動畫。

本模組沿用既有正式六層聯合水柱 SVD renderer 的資料追溯與岸線繪圖邏輯，
但畫面只繪製同源 OCM surface cache 的原始 ``u/v`` 流場，不繪製模態重建面板、
相位文字或 UTC 文字。SVD 仍只用於追溯正式資料來源及選取既有的正／負時間係數
代表視窗；不會重算、覆寫或替換任何 SVD 結果。

輸出採 864×500 的緊湊單圖版面，適合四張影片在簡報中以 2×2 配置排列：四區共用
相同的主圖框、色條框、標題基線與底部軸線位置；主圖使用固定的 display rectangle，
因此不會再因各區經緯度比例不同而產生不同上下空白。標題位於主圖上方，`1 公尺／秒`
比例尺由同一個 Matplotlib quiver artist 以 ``U=1.0`` 放在主圖右下角，右側完整
色條與主圖實際 axes bbox 對齊。exact coastline 的
高解析度 GeoJSON polygon 只在展示階段覆蓋流速色塊與箭頭；保守的 1 km raster
land mask 仍保留作為資料／地理稽核語意，不把階梯 raster 邊界畫成可見海岸線。

時間播放預設可啟用 `--temporal-interpolation`：仍輸出原先兩段各 28 格、4 fps、
約 16 秒的動畫，但以相鄰 12 小時錨點建立中間的 6 小時展示場，降低原始 6 小時
影格直接切換時的視覺跳動。這是 presentation-only 的線性轉換，不是新增觀測、
預報或 SVD 更新；相關限制與每區選窗均寫入 manifest/README。
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np

from coastline_utils import draw_vector_land_overlay
from visualize_ocm_svd_modal_context import (
    ANALYSIS_OUTSIDE_COLOR,
    AXIS_FONT_SIZE_PT,
    COLORBAR_LABEL,
    COMMON_INTERVAL_HOURS,
    DEFAULT_CROSS_REGION_VMAX_MPS,
    DEFAULT_RENDER_DPI,
    DEFAULT_SPEED_ROUNDING_MPS,
    DEFAULT_TARGET_ARROWS,
    DEFAULT_QUIVER_SCALE_MULTIPLIER,
    DISPLAY_FORBIDDEN_TOKENS,
    DYNAMIC_MISSING_COLOR,
    FEATURE_UNAVAILABLE_COLOR,
    FONT_CANDIDATES,
    LAND_ANTIALIASED,
    LAND_COLOR,
    LAND_EDGE_COLOR,
    LAND_EDGE_WIDTH,
    MODEL_OUTSIDE_COLOR,
    NANOS_PER_HOUR,
    OUTRO_HOLD_FRAMES,
    PANEL_BORDER_COLOR,
    QUIVER_COLOR,
    QUIVER_SHADOW_COLOR,
    SEMANTIC_BACKGROUND_COLORS,
    TITLE_FONT_SIZE_PT,
    WINDOW_FRAME_COUNT,
    _apply_geographic_axis_style,
    _display_title,
    _format_time_utc,
    _make_masked_speed,
    _measure_x_tick_label_layout,
    _quiver_key_group_bbox_px,
    _region_manifest,
    _selection_manifest,
    _semantic_background_mesh,
    _speed_ticks,
    _configure_imageio_ffmpeg,
    apply_temporal_interpolation,
    build_region_specs,
    choose_global_vmax,
    choose_quiver_step,
    find_cjk_font,
    font_with_size,
    load_full_product_audit,
    load_region_dataset,
    materialize_payloads,
    read_json,
    select_phase_windows,
    set_temporal_interpolation_disabled,
    sha256_file,
)

try:
    import imageio.v2 as imageio
except ImportError:  # pragma: no cover - 只在沒有影片依賴的稽核環境觸發
    imageio = None  # type: ignore[assignment]


SCRIPT_VERSION = "1.3.1"
"""純原始流場 renderer 的 manifest 版本；與雙面板成果目錄分開管理。

1.3.1 修正 SERVER Matplotlib 3.5 對 ``QuiverKey(zorder=...)`` 的版本相容行為：
比例尺容器在第一次 draw 後明確提升到高於向量陸地的圖層，避免 B 區右下陸地
遮住白色 1 m/s 箭頭與文字；此修正只影響圖例可見性，不改資料或比例尺數值。
1.3.0 新增每小時實測展示模式的動態時間標題契約：啟用
`dynamic_time_title=True` 時，畫面上方只顯示中央 UTC 日期時間，並使用較小字級
與較高位置，將海域名稱／原始流場標籤留給簡報端後製；舊版預設行為不變。
1.2.5 將含標題版的 figure-level 主標題上移，避免與向上延伸後的主圖上緣刻度重疊；
1.2.4 將固定主圖框上緣再向上延伸，壓縮無標題版上方空白但保留簡報標題安全區；
1.2.2 在固定四區 raw-only 主圖框的同時，縮小圖內比例尺文字以配合 2×2 簡報配置；
1.2.1 保留經度軸名稱的安全下緣與標題／主圖
間距，避免固定框過度上移而造成標題與上緣刻度視覺貼合。1.2.0 固定四區 raw-only
影片的主圖框為同一個 display rectangle，避免在 2×2
簡報版面中因 A–D 經緯度範圍比例不同而產生不一致的上下空白；這只改變展示階段
的 axes 幾何，不改變原始流場資料、時間平滑或正式 SVD 結果。1.1.0 的固定
0.0--0.8 m/s 色階與 0.2 m/s 刻度規格仍維持。
"""

RAW_WIDTH = 864
"""四區 2×2 簡報版面的單支影片寬度（像素）。"""

RAW_HEIGHT = 500
"""依 A 區版面確認圖採用的緊湊影片高度（像素）。"""

RAW_FPS = 4
"""簡報播放幀率；每一資料幀仍代表 6 小時觀測或展示平滑後的 6 小時位置。"""

RAW_ARROW_LABEL = "1 公尺／秒"
"""主圖內比例尺文字；依使用者要求移除括號，單位不拆行。"""

RAW_TITLE_FONT_SIZE_PT = 14.0
"""單圖版主標題字級；比雙面板版本小，以配合四張 2×2 簡報配置。"""

RAW_TITLE_Y_FRACTION = 0.985
"""含標題版主標題的 figure fraction 上緣位置；配合主圖 top=0.94 避免重疊。"""

RAW_DYNAMIC_TITLE_FONT_SIZE_PT = 11.0
"""每小時實測版只顯示 UTC 時間時使用的較小字級，讓時間與主圖保留清楚間距。"""

RAW_DYNAMIC_TITLE_Y_FRACTION = 0.995
"""每小時實測版 UTC 時間的 figure fraction 上緣位置；比主標題更靠近畫布頂端。"""

RAW_ARROW_FONT_SIZE_PT = 8.0
"""主圖內比例尺字級；縮小比例尺群組後仍保留簡報縮放時的辨識度。"""

RAW_VECTOR_LAND_UNDERLAY_ZORDER = 0
"""高解析度向量陸地底墊圖層；只填補透明 raster 在 polygon 內可能露出的白底。"""

RAW_VECTOR_LAND_TOP_ZORDER = 30
"""高解析度向量陸地最終遮罩圖層；必須蓋過流速底圖與資料箭頭。"""

RAW_ARROW_KEY_ZORDER = 40
"""圖內 1 m/s 比例尺的最終圖層；刻意高於向量陸地，避免 B 區右下陸地遮住圖例。"""

RAW_DEFAULT_SPEED_VMAX_MPS = float(DEFAULT_CROSS_REGION_VMAX_MPS)
"""未指定參數時沿用既有 2.2 m/s 跨區色階上限，維持舊命令的相容性。"""

RAW_DEFAULT_SPEED_TICK_STEP_MPS = 0.4
"""未指定參數時沿用既有每 0.4 m/s 一格的色條刻度間距。"""

RAW_AXES_RECT = (0.10, 0.14, 0.75, 0.80)
"""四區共用的固定主圖框 figure fraction；壓縮上方空白並保留標題安全區。

主圖寬度仍鎖定 0.75，不改變已核對的左／右邊界；下界 0.14 為經度軸名稱保留
足夠的畫布安全區，避免文字下緣被裁切；上界 0.94 仍留下約 6% 畫高作為
簡報端可放置可編輯標題的安全區。raw-only 2×2 版面會把四區都填入同一個固定
矩形，因此標題安全區、經度軸、色條與比例尺的外部留白不會因各區經緯度範圍比例
不同而漂移。這是展示版面調整，不改變 raw u/v、座標範圍或正式 SVD。
"""

RAW_COLORBAR_RECT = (0.875, 0.14, 0.025, 0.80)
"""右側色條初始 figure fraction；draw 後以主圖實際 bbox 重設 y/height。"""

INTRO_HOLD_FRAMES = 4
"""4 fps 下的片頭停留，約 1 秒。"""


@dataclass
class RawScene:
    """單一原始流場畫面的可更新 Matplotlib artists。

    陣列均為 `(lat, lon)`、速度單位 m/s；`mesh` 只承載原始 surface speed，
    `quiver` 只承載原始 surface u/v。`background_mesh` 將分析域外、靜態模型域外、
    特徵未納入與逐時缺值分開繪製。真實陸地由 `land_patches` 對應的 GeoJSON vector
    polygon 在最高 z-order 覆蓋，因此不把展示用 raster land mask 誤作海岸線。
    """

    fig: Any
    ax: Any
    background_mesh: Any
    mesh: Any
    quiver: Any
    colorbar: Any
    colorbar_axis: Any
    arrow_key: Any
    title_text: Any
    dynamic_time_title: bool
    axis_tick_layout: dict[str, Any]
    colorbar_alignment: dict[str, Any]
    arrow_key_layout: dict[str, Any]
    quiver_step_yx: tuple[int, int]
    land_patch_count: int


def _raw_ticks(vmax: float, tick_step: float = RAW_DEFAULT_SPEED_TICK_STEP_MPS) -> np.ndarray:
    """建立固定流速色條刻度，並保留指定色階上限作為最後端點。

    ``vmax`` 與 ``tick_step`` 都是 renderer 的展示參數，不從單一影格或資料分布
    動態推導。此次簡報一致版以 0.0--0.8 m/s、0.2 m/s 間距呼叫本函式，得到
    `0.0, 0.2, 0.4, 0.6, 0.8`；預設值仍保留舊版 0.0--2.2 m/s、0.4 m/s 間距，
    以免其他既有命令在未指定新參數時意外改變。超過 ``vmax`` 的原始速度會在
    pcolormesh 中飽和於色階頂端，但原始陣列與箭頭的速度尺度不會被截斷或修改。
    """

    vmax = float(vmax)
    tick_step = float(tick_step)
    if not np.isfinite(vmax) or vmax < 0.0:
        raise ValueError(f"固定流速色階上限必須為非負有限值，收到 {vmax!r}")
    if not np.isfinite(tick_step) or tick_step <= 0.0:
        raise ValueError(f"固定流速色階刻度間距必須為正有限值，收到 {tick_step!r}")
    if vmax == 0.0:
        return np.asarray([0.0], dtype=np.float64)
    ticks = np.arange(0.0, vmax + 1.0e-9, tick_step, dtype=np.float64)
    ticks = ticks[ticks <= vmax + 1.0e-8]
    if ticks.size == 0 or not np.isclose(ticks[-1], vmax, rtol=0.0, atol=1.0e-8):
        ticks = np.append(ticks, vmax)
    return np.unique(np.round(ticks, 8))


def _choose_raw_speed_scale(dataset: Any) -> None:
    """只以原始 surface payload 決定單區 p99.5 診斷值與初始色階。

    雖然共用資料 loader 也 materialize 了既有 SVD reconstruction 供時間窗流程相容，
    本函式刻意只讀 `payload.raw_speed`。因此純原始版的色階不會被未繪製的重建場
    極端值影響；最後由命令列的固定展示參數在 A--D 間統一上限。這些統計只用於
    展示比例，不是 OCM 物理結論；使用者指定的固定色階不會被本函式的 p99.5
    診斷值覆蓋。
    """

    values = [payload.raw_speed[np.isfinite(payload.raw_speed)] for payload in dataset.payloads]
    if not values:
        raise ValueError(f"{dataset.spec.key} 原始表層 payload 沒有有限流速")
    finite = np.concatenate(values).astype(np.float64, copy=False)
    p995 = float(np.nanpercentile(finite, 99.5))
    vmax = max(
        DEFAULT_SPEED_ROUNDING_MPS,
        math.ceil(max(p995, 0.05) / DEFAULT_SPEED_ROUNDING_MPS) * DEFAULT_SPEED_ROUNDING_MPS,
    )
    dataset.speed_scale_p995 = p995
    dataset.speed_scale_vmax = float(vmax)


def _choose_raw_arrow_scale(dataset: Any) -> None:
    """只以原始 surface u/v 的 p95 大小設定畫面箭頭參考量。"""

    values = []
    for payload in dataset.payloads:
        valid = np.isfinite(payload.raw_u) & np.isfinite(payload.raw_v)
        if np.any(valid):
            values.append(np.hypot(payload.raw_u[valid], payload.raw_v[valid]).astype(np.float64, copy=False))
    if not values:
        raise ValueError(f"{dataset.spec.key} 原始表層 payload 沒有有限向量")
    dataset.quiver_reference_mps = float(max(np.nanpercentile(np.concatenate(values), 95.0), 0.05))


def _shift_array_without_wrap(array: np.ndarray, dy: int, dx: int, fill_value: Any) -> np.ndarray:
    """平移二維陣列但不讓邊界值從另一側繞回來。

    近岸顯示填補需要在規則格網上尋找最近的有限海水格點。直接使用
    ``numpy.roll`` 會把左邊的值錯誤帶到右邊，造成海域邊界的跨側污染，因此這裡
    以明確的來源／目的切片實作不循環平移。``array`` 的維度必須是
    ``(lat, lon)``；布林陣列以 False 填補，浮點陣列通常以 NaN 填補。
    """

    source = np.asarray(array)
    if source.ndim != 2:
        raise ValueError(f"平移輔助函式只接受二維陣列，收到 shape={source.shape}")
    result = np.full_like(source, fill_value)
    height, width = source.shape
    source_y0 = max(0, -int(dy))
    source_y1 = min(height, height - int(dy))
    source_x0 = max(0, -int(dx))
    source_x1 = min(width, width - int(dx))
    dest_y0 = max(0, int(dy))
    dest_y1 = min(height, height + int(dy))
    dest_x0 = max(0, int(dx))
    dest_x1 = min(width, width + int(dx))
    if source_y0 < source_y1 and source_x0 < source_x1:
        result[dest_y0:dest_y1, dest_x0:dest_x1] = source[source_y0:source_y1, source_x0:source_x1]
    return result


def _coastline_safe_display_speed(dataset: Any, speed: np.ndarray) -> np.ndarray:
    """建立不改動原始資料的近岸顯示速度，消除向量岸線外側的白色縫帶。

    目前 hourly raw-only 產品把 hourly 場重網格到正式 1 km 參考格點；在真實海岸
    與保守 ``cell_overlap_center_corners_vertices`` 陸地格點不完全重合的邊界 cell，
    ``raw_speed`` 可能是 NaN，原 renderer 便會露出近白色的逐時缺值底色；同樣的
    問題也可能發生在貼著 exact land 的模型域外 cell。這不是新觀測值，也不是資料
    修復：本函式只在繪圖階段，將保守 ``coastline_land_mask`` 及其一格 8-neighbour
    接縫範圍內的缺值 cell，以同一影格最近的「非陸地且有限」海水速度填色。高解析度
    GeoJSON polygon 隨後以最高 z-order 覆蓋真實陸地，故 polygon 內仍完全不可見，
    polygon 外的部分只呈現與鄰近海水連續的 viridis 色階，不再形成 raster/vector
    交界的白色階梯邊界。

    回傳陣列的 shape 與輸入相同，單位仍為 m/s；輸入 ``speed`` 本身不會被改寫。
    箭頭仍另以原始有限 ``u/v`` 與 ``~coastline_land_mask`` 判定，不會因這個速度
    填色而捏造陸地向量。若極端情況找不到任何有限海水種子，才使用同影格可用速度
    的中位數作為最後顯示色；該 fallback 會寫入 dataset 的 manifest 摘要，提醒這是
    視覺接縫處理而非科學數值。
    """

    source_speed = np.asarray(speed, dtype=np.float32)
    land = np.asarray(dataset.coastline_land_mask, dtype=bool)
    render_mask = np.asarray(dataset.render_mask, dtype=bool)
    if source_speed.ndim != 2 or source_speed.shape != land.shape or source_speed.shape != render_mask.shape:
        raise ValueError(
            "近岸顯示速度的陣列維度不一致："
            f"speed={source_speed.shape}, coastline_land={land.shape}, render_mask={render_mask.shape}"
        )

    # raster land mask 是保守的「與 polygon 接觸」近似，與向量 polygon 之間可能
    # 留下一格模型域外或逐時 NaN。將 land mask 擴張一格只用於找出這個顯示接縫；
    # 有限的非陸地海水 cell 不會被覆蓋，真正的流場值仍直接使用原始值。
    expanded_land = land.copy()
    directions = (
        (-1, -1), (-1, 0), (-1, 1),
        (0, -1),           (0, 1),
        (1, -1),  (1, 0),  (1, 1),
    )
    for dy, dx in directions:
        expanded_land |= _shift_array_without_wrap(land, dy, dx, False)

    target = land | (expanded_land & ~np.isfinite(source_speed))
    seed = np.isfinite(source_speed) & render_mask & ~land
    display_speed = source_speed.copy()
    pending = target.copy()
    known = seed.copy()
    # 以同步的 BFS 層次向外傳播，讓每個保守陸地 cell 取得最近海水 cell 的速度。
    # 只在 target 內寫入，遠離海岸的模型域外／逐時缺值不會被此顯示補值誤覆蓋。
    while np.any(pending):
        newly_filled = np.zeros_like(pending)
        for dy, dx in directions:
            neighbour_known = _shift_array_without_wrap(known, dy, dx, False)
            neighbour_speed = _shift_array_without_wrap(display_speed, dy, dx, np.nan)
            candidate = pending & neighbour_known & ~newly_filled
            if np.any(candidate):
                display_speed[candidate] = neighbour_speed[candidate]
                newly_filled[candidate] = True
        if not np.any(newly_filled):
            break
        known |= newly_filled
        pending &= ~newly_filled

    fallback_count = int(np.count_nonzero(pending))
    if fallback_count:
        fallback_values = source_speed[seed]
        if fallback_values.size == 0:
            fallback_values = source_speed[np.isfinite(source_speed) & render_mask]
        fallback_speed = float(np.nanmedian(fallback_values)) if fallback_values.size else 0.0
        display_speed[pending] = fallback_speed

    fill_summary = {
        "enabled": bool(np.any(target)),
        "method": "nearest_finite_non_land_speed_bfs_for_display_only",
        "conservative_land_cell_count": int(np.count_nonzero(land)),
        "expanded_coastal_seam_cell_count": int(np.count_nonzero(expanded_land & ~land)),
        "target_display_fill_cell_count": int(np.count_nonzero(target)),
        "finite_ocean_seed_cell_count": int(np.count_nonzero(seed)),
        "filled_cell_count": int(np.count_nonzero(target & np.isfinite(display_speed))),
        "fallback_cell_count": fallback_count,
        "does_not_modify_raw_speed": True,
        "does_not_create_quiver_vectors": True,
    }
    # Dataset 是 renderer 的暫存承載物；只保存固定的顯示政策與第一個影格的摘要，
    # 不把任何補值回寫到 payload 或 source cache。重複更新影格時保留相同政策名稱，
    # 讓 hourly manifest 不會因為 poster/contact sheet 多次呼叫而累計假 frame 數。
    if not hasattr(dataset, "coastline_display_fill_summary"):
        setattr(dataset, "coastline_display_fill_summary", fill_summary)
    return display_speed


def _measure_colorbar_alignment(fig: Any, ax: Any, colorbar_axis: Any) -> dict[str, Any]:
    """以最終 renderer bbox 驗證色條高度與主圖完全對齊。

    Matplotlib 的 `set_aspect("equal")` 可能在第一次 draw 後調整 axes 高度；因此
    不能只比較初始 figure fraction。本函式使用畫布像素 bbox，將色條 y/height
    的差異寫入 manifest，並讓 QA 可以區分真正對齊與僅數值上看似相同的版面。
    """

    renderer = fig.canvas.get_renderer()
    ax_box = ax.get_window_extent(renderer)
    cbar_box = colorbar_axis.get_window_extent(renderer)
    height_diff = float(cbar_box.height - ax_box.height)
    bottom_diff = float(cbar_box.y0 - ax_box.y0)
    return {
        "main_axes_bbox_px": [float(ax_box.x0), float(ax_box.y0), float(ax_box.x1), float(ax_box.y1)],
        "colorbar_bbox_px": [float(cbar_box.x0), float(cbar_box.y0), float(cbar_box.x1), float(cbar_box.y1)],
        "height_difference_px": height_diff,
        "bottom_difference_px": bottom_diff,
        "tolerance_px": 1.0,
        "aligned": abs(height_diff) <= 1.0 and abs(bottom_diff) <= 1.0,
    }


def _measure_arrow_key_inside_axes(fig: Any, ax: Any, key: Any) -> dict[str, Any]:
    """量測主圖內 QuiverKey 群組，確認箭頭與文字未越出地圖圖框。

    比例尺仍由同一個 quiver artist、同一個 `scale` 與 `U=1.0` 建立；此函式只
    量測其最終 pixel bbox，不以手畫箭頭或以文字替代流速比例。若字型變化使群組
    伸出圖框，render 會 fail，避免把不可辨識的比例尺交付到簡報。
    """

    renderer = fig.canvas.get_renderer()
    group = _quiver_key_group_bbox_px(key, renderer)
    axes_box = ax.get_window_extent(renderer)
    margin = 1.0
    inside = (
        group[0] >= axes_box.x0 - margin
        and group[1] >= axes_box.y0 - margin
        and group[2] <= axes_box.x1 + margin
        and group[3] <= axes_box.y1 + margin
    )
    vector_artist = getattr(key, "vector", None)
    text_zorder = float(key.text.get_zorder())
    vector_zorder = float(vector_artist.get_zorder()) if vector_artist is not None else None
    return {
        "group_bbox_px": [float(value) for value in group],
        "main_axes_bbox_px": [float(axes_box.x0), float(axes_box.y0), float(axes_box.x1), float(axes_box.y1)],
        "inside_main_axes": bool(inside),
        "reference_mps": float(key.U),
        "label": RAW_ARROW_LABEL,
        "coordinates": "axes",
        "anchor_axes_fraction": [float(key.X), float(key.Y)],
        # SERVER 使用的 Matplotlib 3.5 會把建構子傳入的 ``zorder`` 當作內部
        # PolyCollection 參數，QuiverKey 容器本身仍維持 quiver+0.1。這會使比例尺
        # 在 z=30 的真實陸地 polygon 下方消失。此處保留三個實際圖層值，讓輸出
        # manifest 能稽核 B 區等「比例尺錨點落在陸地」的畫面是否仍可見。
        "key_artist_zorder": float(key.get_zorder()),
        "vector_artist_zorder": vector_zorder,
        "text_artist_zorder": text_zorder,
        "above_vector_land_top_overlay": bool(
            key.get_zorder() > RAW_VECTOR_LAND_TOP_ZORDER
            and (vector_zorder is None or vector_zorder > RAW_VECTOR_LAND_TOP_ZORDER)
            and text_zorder > RAW_VECTOR_LAND_TOP_ZORDER
        ),
        # 比例尺是畫面內的定量圖例；與資料箭頭可有陰影以提高辨識度不同，比例尺
        # 本身不再加黑／灰描邊，避免觀眾把外框誤認為另一組流場線條。
        "outline_removed": not key.text.get_path_effects()
        and (vector_artist is None or not vector_artist.get_path_effects()),
    }


def _raise_arrow_key_above_vector_land(key: Any) -> None:
    """明確將 QuiverKey 容器與已初始化子 artist 提升到真實陸地之上。

    Matplotlib 3.5 的 ``Axes.quiverkey`` 不把 ``zorder`` 視為 ``QuiverKey`` 本身的
    參數，而會留在其內部 vector 的 keyword 中。因此即使呼叫端傳入較高圖層，
    容器仍以資料 quiver 的 ``zorder + 0.1`` 繪製，落在 z=30 的向量陸地之下。
    新版 Matplotlib 已修正這個 API 行為，但 SERVER 保持 3.5 以確保既有研究環境
    可重現。本函式必須在首次 ``canvas.draw()`` 後呼叫，因為該版本的 ``vector``
    PolyCollection 到此時才建立；它只調整比例尺可見性與移除外框效果，不改 U=1.0、
    同一 quiver scale、原始流速、箭頭資料或任何空間遮罩。
    """

    key.set_zorder(RAW_ARROW_KEY_ZORDER)
    # 文字在所有版本一開始就存在；提高其 z-order 可避免 label 單獨被陸地遮掉。
    key.text.set_zorder(RAW_ARROW_KEY_ZORDER)
    key.text.set_path_effects([])
    # Matplotlib 3.5/3.10 都在第一個 draw 後以 ``vector`` 暴露比例尺箭頭；保留
    # 容錯分支是為了兼容極舊版本可能使用的 ``poly`` 名稱。這裡不手畫替代箭頭，
    # 仍是同一個 QuiverKey、U=1.0 的定量比例尺。
    for vector_name in ("vector", "poly"):
        vector_artist = getattr(key, vector_name, None)
        if vector_artist is not None:
            vector_artist.set_zorder(RAW_ARROW_KEY_ZORDER)
            if hasattr(vector_artist, "set_path_effects"):
                vector_artist.set_path_effects([])


def _augment_x_axis_layout_qa(fig: Any, ax: Any, base_layout: dict[str, Any]) -> dict[str, Any]:
    """補充經度軸與固定主圖框的像素級版面稽核。

    `_measure_x_tick_label_layout` 已檢查 major tick 文字的相鄰間距與畫布裁切；
    本函式再量測 X 軸名稱及主圖框本身。這兩者必須一起檢查，因為 2×2 簡報版面
    的一致性不是只靠刻度數值：若某區仍因 equal aspect 自動縮短 axes，經度軸名稱
    和底部白邊就會與其他區不同。回傳的 bbox 均為最終 renderer 的像素座標，供
    manifest 的跨區共同框線 QA 使用；函式不修改資料、座標範圍或色彩正規化。
    """

    renderer = fig.canvas.get_renderer()
    figure_bbox = fig.bbox
    axes_box = ax.get_window_extent(renderer)
    axis_label = ax.xaxis.label
    label_box = axis_label.get_window_extent(renderer) if axis_label.get_visible() else None
    label_bbox = None
    label_clipped = False
    if label_box is not None:
        label_bbox = [float(label_box.x0), float(label_box.y0), float(label_box.x1), float(label_box.y1)]
        label_clipped = (
            label_box.x0 < figure_bbox.x0
            or label_box.x1 > figure_bbox.x1
            or label_box.y0 < figure_bbox.y0
            or label_box.y1 > figure_bbox.y1
        )

    layout = dict(base_layout)
    layout.update(
        {
            "main_axes_bbox_px": [float(axes_box.x0), float(axes_box.y0), float(axes_box.x1), float(axes_box.y1)],
            "main_axes_bottom_px": float(axes_box.y0),
            "main_axes_top_px": float(axes_box.y1),
            "x_axis_label": str(axis_label.get_text()),
            "x_axis_label_bbox_px": label_bbox,
            "x_axis_label_clipped": bool(label_clipped),
            "x_axis_label_visible": bool(axis_label.get_visible()),
        }
    )
    layout["passed"] = bool(layout.get("passed", False) and not label_clipped and axis_label.get_visible())
    return layout


def _create_raw_scene(
    dataset: Any,
    *,
    width: int,
    height: int,
    dpi: int,
    target_arrows: int,
    quiver_scale_multiplier: float,
    font: Any | None,
    show_title: bool = True,
    dynamic_time_title: bool = False,
) -> RawScene:
    """建立單一 raw surface panel、同高 colorbar 與圖內 1 m/s 比例尺。

    版面使用 `[0.10, 0.14, 0.75, 0.74]` 固定矩形，使四區影片在 2×2 投影片中
    擁有相同的主圖寬高、X 軸基線與底部留白。正式水柱 SVD 的資料仍以各區 display
    extent 與固定刻度繪製；raw-only 展示為了跨區版面一致，將共用的 geographic
    equal-aspect 預設覆寫為固定 axes rectangle 的 display aspect。這是刻意的
    presentation layout decision：不重新取樣資料、不改變 lon/lat 的軸範圍，也不
    修改 u/v、速度單位或任何 SVD 結果；其限制是各區地圖的縱橫顯示比例不再完全
    依經緯度範圍自然縮放，故本版定位為簡報 2×2 視覺展示。建立後將 cbar axis 的
    y/height 同步到主圖，保證色條不再延伸到主圖以外。``show_title=False``
    時只隱藏 figure-level 主標題，保留既有主圖、色條、座標軸及上方安全留白，
    讓簡報端可以在同一位置疊加可編輯的海域名稱；不會裁切資料圖或改變資料座標。
    """

    vmax = float(dataset.speed_scale_vmax)
    norm = mcolors.Normalize(vmin=0.0, vmax=vmax, clip=True)
    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad((0.0, 0.0, 0.0, 0.0))
    semantic_cmap = mcolors.ListedColormap(SEMANTIC_BACKGROUND_COLORS, name="ocm_raw_mask_semantics")
    semantic_norm = mcolors.BoundaryNorm(np.arange(-0.5, 5.5, 1.0), semantic_cmap.N)
    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi, facecolor="white")
    ax = fig.add_axes(list(RAW_AXES_RECT))
    colorbar_axis = fig.add_axes(list(RAW_COLORBAR_RECT))
    ax.set_facecolor(ANALYSIS_OUTSIDE_COLOR)

    first = dataset.payloads[0]
    # `first.raw_speed` 是原始 hourly／6 小時 payload 的科學值；近岸顯示速度只
    # 是為了讓保守 raster land cell 與 exact vector coastline 之間沒有白色縫帶，
    # 因此僅將它傳給畫面 mesh，不會改動 payload 或後續箭頭的資料來源。
    first_display_speed = _coastline_safe_display_speed(dataset, first.raw_speed)
    background_mesh = _semantic_background_mesh(ax, dataset, first_display_speed, semantic_cmap, semantic_norm)
    mesh = ax.pcolormesh(
        dataset.lon,
        dataset.lat,
        _make_masked_speed(first_display_speed, vmax),
        shading="auto",
        cmap=cmap,
        norm=norm,
        zorder=2,
    )
    step_y, step_x = choose_quiver_step(dataset.lon.size, dataset.lat.size, target_arrows)
    # exact-land raster mask 在動畫階段只限制箭頭 anchor，不當作可見階梯海岸線。
    # 即使原始模型在部分陸域 cell 留有有限 u/v，也不能讓箭頭從向量 polygon 外側
    # 回露；底層速度則由上面的 display-only 鄰近海水填色保持視覺連續。
    valid = np.isfinite(first.raw_u) & np.isfinite(first.raw_v) & dataset.render_mask & ~dataset.coastline_land_mask
    sampled_u = np.ma.masked_where(~valid[::step_y, ::step_x], first.raw_u[::step_y, ::step_x])
    sampled_v = np.ma.masked_where(~valid[::step_y, ::step_x], first.raw_v[::step_y, ::step_x])
    quiver = ax.quiver(
        dataset.lon[::step_x],
        dataset.lat[::step_y],
        sampled_u,
        sampled_v,
        color=QUIVER_COLOR,
        scale=max(quiver_scale_multiplier * dataset.quiver_reference_mps, 0.1),
        width=0.0023,
        headwidth=3.1,
        headlength=4.2,
        headaxislength=3.5,
        alpha=0.94,
        zorder=10,
    )
    # 舊版 SERVER 的 Matplotlib 3.5 搭配目前 NumPy 在 Quiver 的 path-effect
    # collection 繪製階段可能觸發 C 層 `getargs` 錯誤；而本系列簡報版已要求
    # 白色箭頭、比例尺不帶外框，資料箭頭也不需要依賴陰影才能表達方向。直接
    # 清空 path effects 可跨版本穩定輸出，保留 Quiver 的白色本體、U/V 數值與
    # 固定尺度；這只改變展示外觀，不改變原始流場。
    quiver.set_path_effects([])
    # 先放置一層與真實陸地同色的向量底墊，讓 exact coastline polygon 內的
    # transparent／masked raster 像素不會回露到白色 figure 背景。這一層仍使用
    # GeoJSON 原始高解析度頂點，沒有把 1 km conservative raster mask 擴張成可見
    # 海岸線；後面的同一組 polygon 會在最高 z-order 再蓋一次，確保 raw 色塊與
    # quiver 箭頭完全不穿入陸地。底墊是消除 raster/vector 接縫的繪圖技術，並不
    # 改變 render_mask、原始 u/v 或任何資料稽核結果。
    draw_vector_land_overlay(
        ax,
        dataset.land_rings,
        tuple(float(value) for value in dataset.display_axis_spec["display_extent"]),
        facecolor=LAND_COLOR,
        edgecolor=LAND_EDGE_COLOR,
        linewidth=LAND_EDGE_WIDTH,
        antialiased=LAND_ANTIALIASED,
        zorder=RAW_VECTOR_LAND_UNDERLAY_ZORDER,
    )
    land_patch_count = draw_vector_land_overlay(
        ax,
        dataset.land_rings,
        tuple(float(value) for value in dataset.display_axis_spec["display_extent"]),
        facecolor=LAND_COLOR,
        edgecolor=LAND_EDGE_COLOR,
        linewidth=LAND_EDGE_WIDTH,
        antialiased=LAND_ANTIALIASED,
        zorder=RAW_VECTOR_LAND_TOP_ZORDER,
    )
    _apply_geographic_axis_style(
        ax,
        dataset.lon,
        dataset.lat,
        font=font,
        show_x_labels=True,
        display_axis_spec=dataset.display_axis_spec,
    )
    # `_apply_geographic_axis_style` 共用既有 renderer 的 equal-aspect 預設；但本版
    # 是四支 raw-only 影片並排成 2×2。A–D 的經緯度範圍比例不同，若保留 equal aspect，
    # Matplotlib 會在固定 figure rectangle 內縮短 C/D 的 axes，導致主圖、色條、經度
    # 軸與下方白邊不一致。這裡只在展示 renderer 覆寫為固定矩形，讓四區外框完全相同；
    # 原始座標、major tick 與 polygon land overlay 的資料位置仍由同一組 xlim/ylim
    # 決定。這不會對 raw u/v 陣列或正式 SVD 做任何科學轉換。
    ax.set_aspect("auto")
    for spine in ax.spines.values():
        spine.set_color(PANEL_BORDER_COLOR)
        spine.set_linewidth(0.7)

    colorbar = fig.colorbar(mesh, cax=colorbar_axis)
    # 色階刻度必須跟固定展示上限一起寫入，不能在每幀或每區重新取樣；此次
    # 簡報一致版為 0.0--0.8 m/s、每 0.2 m/s 一格。刻度參數掛在 dataset 上，
    # 讓 scene、region manifest 與整體 render policy 使用完全相同的來源。
    speed_tick_step = float(getattr(dataset, "speed_tick_step_mps", RAW_DEFAULT_SPEED_TICK_STEP_MPS))
    colorbar.set_ticks(_raw_ticks(vmax, speed_tick_step))
    colorbar.ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f"))
    colorbar.ax.tick_params(
        labelsize=AXIS_FONT_SIZE_PT,
        labelcolor="#000000",
        colors="#000000",
        pad=3,
        length=2.5,
    )
    colorbar.outline.set_linewidth(0.55)
    colorbar.ax.yaxis.set_label_position("right")
    colorbar.set_label(
        COLORBAR_LABEL,
        rotation=90,
        rotation_mode="anchor",
        labelpad=12,
        fontproperties=font_with_size(font, AXIS_FONT_SIZE_PT),
        color="#000000",
    )
    colorbar.ax.yaxis.label.set_va("center")
    colorbar.ax.yaxis.label.set_ha("center")

    # 主標題是簡報後製最常需要調整的觀眾可見文字。無標題版只移除這個
    # figure-level artist，不動主圖的 axes fraction，因而保留可供 PPTX 疊加
    # 編輯文字的上方安全區，也避免同一 30 秒資料因版面重排而無法直接對照。
    # `dynamic_time_title` 是原始每小時觀測版的專用選項：啟用後，figure-level
    # 文字只顯示實際 payload 的 UTC 時間，不再把海域名稱或「原始流場」寫入畫面；
    # 這樣簡報端可另行放置可編輯的四區標籤。時間文字在每次更新 scene 時改寫，
    # 不加入模態、PC、相位或任何由 SVD 重建推導的資訊。預設為 False 以保持既有
    # 30/60 日與全期間成果的畫面完全不變。
    title_text = None
    if show_title:
        first_time_ns = int(
            getattr(first, "display_time_ns", None)
            or getattr(first.record, "time_ns")
        )
        if dynamic_time_title:
            title = _format_time_utc(first_time_ns)
            title_x = 0.50
            title_ha = "center"
        else:
            title = f"{_display_title(dataset.spec.key)} 原始流場"
            title_x = 0.10
            title_ha = "left"
        title_text = fig.text(
            title_x,
            RAW_DYNAMIC_TITLE_Y_FRACTION if dynamic_time_title else RAW_TITLE_Y_FRACTION,
            title,
            ha=title_ha,
            va="top",
            fontproperties=font_with_size(
                font,
                RAW_DYNAMIC_TITLE_FONT_SIZE_PT if dynamic_time_title else RAW_TITLE_FONT_SIZE_PT,
            ),
            color="#000000",
        )
    # 依參考靜態圖把比例尺放在主圖右下方。`coordinates="axes"` 讓它跟著主圖
    # bbox 移動；使用同一個 quiver artist 且 U=1.0，確保顯示長度真實對應 1 m/s。
    arrow_key = ax.quiverkey(
        quiver,
        X=0.78,
        Y=0.08,
        U=1.0,
        label=RAW_ARROW_LABEL,
        labelpos="E",
        coordinates="axes",
        color=QUIVER_COLOR,
        labelcolor=QUIVER_COLOR,
        labelsep=0.012,
        fontproperties=font_with_size(font, RAW_ARROW_FONT_SIZE_PT),
        # 新版 Matplotlib 會直接採用此容器層級；SERVER 3.5 則在首次 draw 後
        # 由 `_raise_arrow_key_above_vector_land` 再明確設定，避免 B 區右下陸地
        # 把整組比例尺遮住。
        zorder=RAW_ARROW_KEY_ZORDER,
    )
    # SERVER 的 QuiverKey vector 在首次 draw 後才建立，因此先初始化，再把整組
    # 比例尺（容器、箭頭、文字）提升到 z=40 並清除外框效果。第二次 draw 才是
    # 寫入 poster／MP4 的最終圖層順序，確保即使比例尺位於 B 區陸地上仍可見。
    fig.canvas.draw()
    _raise_arrow_key_above_vector_land(arrow_key)
    # 第一個 draw 後才取得固定矩形 axes 的實際位置；色條只需放在同一 y/height，
    # x 位置維持右側欄位，因而不會改變使用者已確認的左／右版面邊界。
    ax_position = ax.get_position()
    colorbar_axis.set_position([RAW_COLORBAR_RECT[0], ax_position.y0, RAW_COLORBAR_RECT[2], ax_position.height])
    fig.canvas.draw()
    colorbar_alignment = _measure_colorbar_alignment(fig, ax, colorbar_axis)
    if not colorbar_alignment["aligned"]:
        raise RuntimeError(f"{dataset.spec.key} 色條與主圖未對齊：{colorbar_alignment}")
    arrow_key_layout = _measure_arrow_key_inside_axes(fig, ax, arrow_key)
    if not arrow_key_layout["inside_main_axes"]:
        raise RuntimeError(f"{dataset.spec.key} 圖內比例尺超出主圖：{arrow_key_layout}")
    axis_tick_layout = _augment_x_axis_layout_qa(fig, ax, _measure_x_tick_label_layout(fig, ax))
    return RawScene(
        fig=fig,
        ax=ax,
        background_mesh=background_mesh,
        mesh=mesh,
        quiver=quiver,
        colorbar=colorbar,
        colorbar_axis=colorbar_axis,
        arrow_key=arrow_key,
        title_text=title_text,
        dynamic_time_title=bool(dynamic_time_title),
        axis_tick_layout=axis_tick_layout,
        colorbar_alignment=colorbar_alignment,
        arrow_key_layout=arrow_key_layout,
        quiver_step_yx=(step_y, step_x),
        land_patch_count=land_patch_count,
    )


def _update_raw_scene(scene: RawScene, dataset: Any, payload: Any) -> np.ndarray:
    """更新單一 raw panel 並回傳 RGB uint8 畫面。

    每幀只更新原始表層速度、u/v 與逐時語意背景；標題、色條、axes、向量陸地及
    圖內比例尺皆固定。`payload.raw_*` 已在 materialization 階段套用 SVD 有效域，
    vector polygon 仍在其上方覆蓋真實陸地，故不會以逐時 NaN 顏色冒充海岸線。
    """

    speed = payload.raw_speed
    u = payload.raw_u
    v = payload.raw_v
    # 速度填補只服務 raster/vector 海岸接縫；raw speed、u/v、時間索引與 source
    # validity 均保持原值，避免把展示修補誤當成觀測資料修復。
    display_speed = _coastline_safe_display_speed(dataset, speed)
    scene.background_mesh.set_array(
        __import__("visualize_ocm_svd_modal_context")._semantic_background(dataset, display_speed).ravel()
    )
    scene.mesh.set_array(_make_masked_speed(display_speed, dataset.speed_scale_vmax).ravel())
    step_y, step_x = scene.quiver_step_yx
    # 與建立 scene 時使用同一個 exact-land anchor 規則；這不改動原始 u/v，只讓
    # quiver 不會在向量陸地內產生可見箭頭，亦不會因 display-only 速度填色增加箭頭。
    valid = np.isfinite(u) & np.isfinite(v) & dataset.render_mask & ~dataset.coastline_land_mask
    scene.quiver.set_UVC(
        np.ma.masked_where(~valid[::step_y, ::step_x], u[::step_y, ::step_x]),
        np.ma.masked_where(~valid[::step_y, ::step_x], v[::step_y, ::step_x]),
    )
    if scene.dynamic_time_title and scene.title_text is not None:
        # payload.record.time_ns 是 renderer 對每一幀保存的實際 UTC epoch-ns；
        # 若未來某個展示 payload 另外提供 display_time_ns，優先使用它，但不
        # 允許以本機時區或人工遞增的 frame number 代替原始時間戳。每小時 raw-only
        # 版本的觀眾可見標題只放這個時間，不混入海域名稱或資料型態文字。
        display_time_ns = getattr(payload, "display_time_ns", None)
        if display_time_ns is None:
            display_time_ns = getattr(payload.record, "time_ns")
        scene.title_text.set_text(
            _format_time_utc(int(display_time_ns))
        )
    scene.fig.canvas.draw()
    rgba = np.asarray(scene.fig.canvas.buffer_rgba(), dtype=np.uint8)
    width, height = scene.fig.canvas.get_width_height()
    if rgba.shape[:2] != (height, width):
        raise RuntimeError(f"Matplotlib canvas shape={rgba.shape} != {(height, width)}")
    return rgba[:, :, :3].copy()


def _frame_plan() -> list[int]:
    """建立 4 fps、約 16 秒的片頭／56 資料幀／片尾播放順序。"""

    last = WINDOW_FRAME_COUNT * 2 - 1
    return [0] * INTRO_HOLD_FRAMES + list(range(WINDOW_FRAME_COUNT * 2)) + [last] * OUTRO_HOLD_FRAMES


def _output_names(dataset: Any, temporal_interpolated: bool) -> tuple[str, str, str, str]:
    """建立不含 modal-context 混淆字樣的純原始成果檔名。"""

    suffix = "_temporal_interpolated" if temporal_interpolated else ""
    stem = f"region_{dataset.spec.key}_{dataset.spec.short_name}_raw_surface_only{suffix}"
    return f"{stem}.mp4", f"{stem}_poster.png", f"{stem}_positive_window.png", f"{stem}_negative_window.png"


def _ffprobe(path: Path) -> dict[str, Any]:
    """讀取影片 stream/format 編碼資訊，供 manifest 技術 QA 使用。"""

    executable = shutil.which("ffprobe")
    if not executable:
        return {"available": False, "error": "ffprobe_not_found"}
    command = [
        executable,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_name,codec_type,pix_fmt,width,height,r_frame_rate,nb_frames",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    value = json.loads(result.stdout)
    streams = value.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = [item for item in streams if item.get("codec_type") == "audio"]
    return {
        "available": True,
        "video": video,
        "format": value.get("format", {}),
        "audio_stream_count": len(audio),
    }


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
    temporal_interpolated: bool,
    show_title: bool,
    overwrite: bool,
) -> dict[str, Any]:
    """串流輸出單區 raw-only MP4、poster、相位 QA 圖與首中末 contact sheet。"""

    if imageio is None or not hasattr(imageio, "get_writer"):
        raise ImportError("輸出 MP4 需要 imageio")
    output_dir.mkdir(parents=True, exist_ok=True)
    mp4_name, poster_name, positive_name, negative_name = _output_names(dataset, temporal_interpolated)
    mp4_path = output_dir / mp4_name
    poster_path = output_dir / poster_name
    positive_path = output_dir / positive_name
    negative_path = output_dir / negative_name
    targets = [mp4_path, poster_path, positive_path, negative_path]
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
    plan = _frame_plan()
    first_rgb = _update_raw_scene(scene, dataset, dataset.payloads[plan[0]])
    scene.fig.savefig(poster_path, dpi=dpi, facecolor="white")
    _update_raw_scene(scene, dataset, dataset.payloads[0])
    scene.fig.savefig(positive_path, dpi=dpi, facecolor="white")
    _update_raw_scene(scene, dataset, dataset.payloads[WINDOW_FRAME_COUNT])
    scene.fig.savefig(negative_path, dpi=dpi, facecolor="white")

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
            if (ordinal + 1) % 8 == 0 or ordinal + 1 == len(plan):
                print(f"{dataset.spec.key} rendered {ordinal + 1}/{len(plan)} frames", flush=True)
    finally:
        writer.close()
        plt.close(scene.fig)

    qa_dir = output_dir / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    ordered = [contact_frames[index] for index in (0, len(plan) // 2, len(plan) - 1)]
    contact = np.concatenate(ordered, axis=1)
    contact_path = qa_dir / f"region_{dataset.spec.key}_first_middle_last_contact.png"
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
        "poster": {"filename": poster_path.name, "server_path": str(poster_path.resolve()), "sha256": sha256_file(poster_path)},
        "qa_frames": {
            "positive_window": {"filename": positive_path.name, "server_path": str(positive_path.resolve()), "sha256": sha256_file(positive_path)},
            "negative_window": {"filename": negative_path.name, "server_path": str(negative_path.resolve()), "sha256": sha256_file(negative_path)},
        },
        "contact_sheet": {"filename": contact_path.name, "server_path": str(contact_path.resolve()), "sha256": sha256_file(contact_path)},
        "render_size_px": [width, height],
        "render_dpi": dpi,
        "temporal_interpolated": temporal_interpolated,
        "frame_plan": {
            "intro_hold_frames": INTRO_HOLD_FRAMES,
            "positive_data_frames": WINDOW_FRAME_COUNT,
            "negative_data_frames": WINDOW_FRAME_COUNT,
            "outro_hold_frames": OUTRO_HOLD_FRAMES,
            "total_frames": len(plan),
        },
        "colorbar_alignment": scene.colorbar_alignment,
        "arrow_key_layout": scene.arrow_key_layout,
        "x_tick_label_layout": scene.axis_tick_layout,
        "land_patch_count": scene.land_patch_count,
    }


def _visible_text_spec(dataset: Any, *, show_title: bool = True) -> dict[str, Any]:
    """建立 raw-only 畫面 allowlist/denylist；phase/UTC 與重建 caption 均不顯示。

    ``show_title=False`` 是供簡報後製的展示變體：主標題不進入影像像素，
    但仍在 manifest 保留其原本候選文字與可見狀態，讓稽核者能分辨「刻意移除」
    與「renderer 遺漏標題」兩種情形。其餘座標、色條及比例尺文字照常稽核。
    """

    title = f"{_display_title(dataset.spec.key)} 原始流場"
    strings = {
        "main_title": title if show_title else None,
        "arrow_legend": RAW_ARROW_LABEL,
        "colorbar_label": COLORBAR_LABEL,
        "x_axis_label": "經度（°E）",
        "y_axis_label": "緯度（°N）",
    }
    forbidden = {
        token: [name for name, value in strings.items() if value is not None and token in str(value)]
        for token in (*DISPLAY_FORBIDDEN_TOKENS, "模態 1 時間係數", "累積流場變異百分比達 90%")
    }
    found = {token: names for token, names in forbidden.items() if names}
    if found:
        raise ValueError(f"raw-only 觀眾可見文字違反 denylist：{found}")
    return {
        "strings": strings,
        "visible": True,
        "main_title_visible": bool(show_title),
        "main_title_removed_for_editable_overlay": not bool(show_title),
        "phase_utc_line_visible": False,
        "panel_caption_visible": False,
        "reconstruction_panel_visible": False,
        "forbidden_tokens": [*DISPLAY_FORBIDDEN_TOKENS, "模態 1 時間係數", "累積流場變異百分比達 90%"],
        "forbidden_tokens_found": found,
        "passed": not found,
        "font_and_color": {
            "visible_text_color": "#000000",
            "title_fontsize_points": RAW_TITLE_FONT_SIZE_PT if show_title else None,
            "axis_fontsize_points": AXIS_FONT_SIZE_PT,
            "arrow_legend_fontsize_points": RAW_ARROW_FONT_SIZE_PT,
        },
    }


def _raw_region_manifest(
    dataset: Any,
    render_summary: dict[str, Any],
    full_audit: dict[str, Any],
    *,
    shared_vmax: bool,
    temporal_interpolated: bool,
    show_title: bool,
) -> dict[str, Any]:
    """建立純原始單區 manifest；保留內部 SVD 選窗但明確標記未繪製重建場。"""

    bbox = [float(dataset.lon[0]), float(dataset.lon[-1]), float(dataset.lat[0]), float(dataset.lat[-1])]
    # dataset.speed_scale_vmax 與 speed_tick_step_mps 在 main() 中由跨區固定展示
    # 參數設定；此處再次取值可讓每區 visual_spec 與實際 colorbar artist 對得上，
    # 避免只更新總 manifest 而遺漏區域層級的色階稽核欄位。
    speed_vmax = float(dataset.speed_scale_vmax)
    speed_tick_step = float(getattr(dataset, "speed_tick_step_mps", RAW_DEFAULT_SPEED_TICK_STEP_MPS))
    return {
        "region_key": dataset.spec.key,
        "region_name_zh": dataset.spec.name_zh,
        "display_title": f"{_display_title(dataset.spec.key)} 原始流場",
        "region_short_name": dataset.spec.short_name,
        "formal_svd_source": str(dataset.svd_dir),
        "svd_source_unchanged": True,
        "coastline_correction_scope": "visualization_only",
        "coastline": dataset.coastline_summary,
        "source_mode": dataset.source_mode,
        "surface_source": "same-source published OCM surface u/v cache" if dataset.source_mode == "same_source_surface_cache" else "external full-Taiwan product remeshed to SVD grid",
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
            "selection_basis": "pc_standardized[0] only for existing representative positive/negative windows; phase/UTC hidden from frame",
            "phase_windows": _selection_manifest(dataset),
            "common_time_count": int(dataset.common_time_ns.size),
            "source_valid_non_imputed_only": True,
            "interval_hours": COMMON_INTERVAL_HOURS,
        },
        "source_products": {
            "full_taiwan_1km_6h_audit_dir": str(full_audit["dir"]),
            "full_taiwan_metadata_sha256": full_audit["metadata_sha256"],
            "surface_cache_root": str(dataset.cache_root),
            "surface_cache_grid_metadata_sha256": dataset.cache_metadata.get("grid_metadata_sha256"),
            "surface_month_metadata_sha256": dataset.cache_meta_hashes,
        },
        "temporal_interpolation": dataset.temporal_interpolation_summary,
        "visual_spec": {
            "figure_size_px": render_summary["render_size_px"],
            "render_dpi": render_summary["render_dpi"],
            "raw_only": True,
            "title": f"{_display_title(dataset.spec.key)} 原始流場" if show_title else None,
            "title_visible": bool(show_title),
            "title_fontsize_points": RAW_TITLE_FONT_SIZE_PT if show_title else None,
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
            "colorbar_label": COLORBAR_LABEL,
            "colorbar_label_rotation_degrees": 90,
            "colorbar_label_direction": "same as latitude axis label",
            "colorbar_ticks_mps": [float(value) for value in _raw_ticks(speed_vmax, speed_tick_step)],
            "colorbar_tick_spacing_mps": speed_tick_step,
            "fixed_speed_vmin_mps": 0.0,
            "fixed_speed_vmax_mps": speed_vmax,
            "fixed_speed_scope_shared_across_regions": shared_vmax,
            "arrow_legend": {
                "label": RAW_ARROW_LABEL,
                "reference_mps": 1.0,
                "artist": "Matplotlib QuiverKey using the same raw-surface quiver artist, U=1.0",
                "inside_main_map": True,
                "layout": render_summary["arrow_key_layout"],
                "font_size_points": RAW_ARROW_FONT_SIZE_PT,
                "color": QUIVER_COLOR,
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
                "facecolor": LAND_COLOR,
                "edgecolor": LAND_EDGE_COLOR,
                "linewidth": LAND_EDGE_WIDTH,
                "antialiased": LAND_ANTIALIASED,
                "visible_source": "high-resolution GeoJSON vector polygon fill",
                "raster_land_background_visible": False,
            },
            "display_text": _visible_text_spec(dataset, show_title=show_title),
        },
        "outputs": render_summary,
        "limitations": [
            "本版只顯示同源 OCM 原始表層流場，不顯示 SVD 模態重建；正式 SVD 仍僅作時間窗追溯，沒有被改寫。",
            "兩段播放視窗沿用既有第一模態時間係數正／負案例選取，但 phase/UTC 不放入觀眾可見畫面；這不是全年統計驗證。",
            "圖內箭頭為視覺抽樣，pcolormesh 仍保留規則網格；箭頭長度由同一 quiver scale 計算，比例尺 U=1.0。",
            "exact coastline 只在展示階段遮蔽陸地色塊與箭頭並疊加向量 polygon，不改變 SVD 或原始 cache 的科學遮罩。",
            *(["時間內插是 presentation-only 的 12 小時錨點線性轉換，部分 6 小時畫面為虛擬顯示場，不代表新增觀測。"] if temporal_interpolated else []),
        ],
    }


def _write_readme(path: Path, manifest: dict[str, Any]) -> None:
    """寫出純原始版輸出目錄的繁體中文追溯說明。"""

    temporal = manifest.get("temporal_interpolation", {})
    render_policy = manifest.get("render_policy", {})
    fixed_vmax = float(render_policy.get("fixed_speed_vmax_mps", RAW_DEFAULT_SPEED_VMAX_MPS))
    fixed_tick_step = float(
        render_policy.get("fixed_speed_tick_spacing_mps", RAW_DEFAULT_SPEED_TICK_STEP_MPS)
    )
    fixed_ticks = ", ".join(f"{value:.1f}" for value in render_policy.get("fixed_speed_ticks_mps", []))
    lines = [
        "# 四海域純原始表層流場動畫—緊湊 2×2 版",
        "",
        "本目錄只提供四個海域的原始 OCM 表層流場動畫，不包含模態重建面板。版面依",
        "使用者確認的 A 區預覽，採 864×500，適合四支影片在簡報中以 2×2 配置排列；",
        "四區共用相同的主圖固定矩形、色條位置、經度軸基線與底部留白，避免 A–D",
        "的經緯度範圍比例不同而在 2×2 排列時出現不同高度的白邊；",
        "不修改 PPTX，也不覆寫既有 v3/v4 雙面板成果。",
        "",
        "## 畫面規格",
        "",
        "- 主標題為 `海域 A（東北角） 原始流場`、`海域 B（新竹外海） 原始流場`、",
        "  `海域 C（後灣海域） 原始流場`、`海域 D（連江海域） 原始流場`。",
        "- 畫面移除相位、UTC、模態重建 caption 與所有 PC/K/K90 等內部術語；只保留",
        "  海域標題、經緯度軸、固定色條及主圖內 `1 公尺／秒` 比例尺。",
        "- 比例尺由同一個 raw-surface Matplotlib QuiverKey 以 `U=1.0` 產生，不是手畫",
        "  圖示；色條完整標籤 `流速（公尺／秒）` 與主圖實際高度對齊。色條固定刻度為",
        f"  `{fixed_ticks}` m/s，每 {fixed_tick_step:.1f} m/s 一格，固定上限 {fixed_vmax:.1f} m/s。",
        "  此為展示正規化；超過上限的原始速度會飽和為色階頂端，原始資料與箭頭尺度不變。",
        "- 比例尺箭頭與文字均不使用黑／灰色外框；流場箭頭的輕微陰影只用於提升資料箭頭",
        "  在底圖上的辨識度，兩者在 manifest 中分開記錄。",
        "- 為維持 2×2 視覺一致，raw-only 主圖採固定 axes rectangle 的 display aspect；",
        "  這是 presentation-only 版面配置，不改變各區 xlim/ylim、原始 u/v、速度單位或 SVD。",
        "  代價是不同經緯度範圍的地理縱橫顯示比例可能有有限差異，故不應用此版面作量測。",
        "- 色條單位採與 Y 軸緯度標籤相同的旋轉方向；高解析度 GeoJSON polygon 置於",
        "  流速色塊與箭頭上方，raster land mask 只供資料／地理 audit，不作可見階梯海岸線。",
        "",
        "## 資料與科學範圍",
        "",
        f"- 正式 SVD 追溯根目錄：`{manifest.get('formal_svd_source')}`；`svd_source_unchanged=true`。",
        "- `coastline_correction_scope=visualization_only`：exact coastline 只阻止陸地上的原始流速色塊／箭頭被展示，並疊加真實向量陸地；不重新定義既有 SVD。",
        "- 原始流場優先讀取同源 `preprocessed/ocm_surface/<flow_domain_id>` surface cache；每區 source mode 與 cache 雜湊均記於 manifest。",
        "- 既有 `pc.npy`/`pc_standardized.npy` 只用於沿用已核對的代表時間窗；它們不是本版的觀眾可見文字，也不被拿來重建或修改原始場。",
        "",
        "## 時間播放",
        "",
        f"- temporal interpolation enabled：`{temporal.get('enabled')}`；method：`{temporal.get('method')}`。",
        "- 每支影片 4 fps、64 幀、約 16 秒、H.264/yuv420p、無音訊；片頭／片尾各停留約 1 秒。",
        "- 啟用內插時，部分 6 小時畫面由相鄰 12 小時真實錨點線性估計，只改善固定片長下的視覺轉換，不代表新的觀測或預報結果。",
        "",
        "## 輸出與 QA",
        "",
        "- 每區含 MP4、poster、正／負時間窗代表圖與 `qa/region_*_first_middle_last_contact.png`。",
        "- `animation_manifest.json` 記錄正式 SVD、exact coastline SHA-256、資料 cache、顯示範圍、固定色階、色條／比例尺 bbox 與 ffprobe。",
        "- 交付前應以 manifest 的 `qa` 檢查影片編碼、尺寸、影格數、無音訊、主圖／色條等高、比例尺在主圖內、經緯度 tick 未裁切，以及四區主圖／色條 bbox 一致；仍建議在簡報縮放至 2×2 後人工觀看。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _build_manifest(args: argparse.Namespace, datasets: Sequence[Any], summaries: Sequence[dict[str, Any]], full_audit: dict[str, Any], shared_vmax: float) -> dict[str, Any]:
    """組合單一 raw-only manifest，讓成果與既有雙面板 manifest 完全隔離。"""

    return {
        "schema_name": "ocm_raw_surface_only_animation_manifest",
        "schema_version": SCRIPT_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "renderer": str(Path(__file__).resolve()),
        "purpose": "四個海域純原始表層流場動畫；供簡報 2×2 版面使用，不取代模態研究圖",
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
        "render_policy": {
            "width_px": args.width,
            "height_px": args.height,
            "fps": args.fps,
            "raster_dpi": args.dpi,
            "expected_duration_seconds": (INTRO_HOLD_FRAMES + WINDOW_FRAME_COUNT * 2 + OUTRO_HOLD_FRAMES) / args.fps,
            "expected_frame_count": INTRO_HOLD_FRAMES + WINDOW_FRAME_COUNT * 2 + OUTRO_HOLD_FRAMES,
            "no_audio": True,
            "title_visible": bool(args.show_title),
            "title_removed_for_editable_ppt_overlay": not bool(args.show_title),
            "codec": "libx264",
            "pixel_format": "yuv420p",
            "h264_crf": 16,
            "h264_preset": "slow",
            "target_arrows": args.target_arrows,
            "quiver_scale_multiplier": args.quiver_scale_multiplier,
            "panel_layout": {
                "mode": "uniform_2x2_fixed_axes_rectangle",
                "main_axes_fraction": list(RAW_AXES_RECT),
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
        },
        "temporal_interpolation": {
            "enabled": bool(args.temporal_interpolation),
            "method": "piecewise_linear_display_only" if args.temporal_interpolation else "none; exact 6-hour observed payload display",
            "source_observation_interval_hours": COMMON_INTERVAL_HOURS,
            "anchor_interval_hours": COMMON_INTERVAL_HOURS * 2 if args.temporal_interpolation else COMMON_INTERVAL_HOURS,
            "scope": "raw surface display payload only; formal SVD/source cache/coastline unchanged",
        },
        "source_audit": {
            "full_taiwan_product_dir": str(full_audit["dir"]),
            "full_taiwan_metadata_sha256": full_audit["metadata_sha256"],
            "full_taiwan_time_count": int(full_audit["time_count"]),
            "source_valid_count": int(np.count_nonzero(full_audit["source_valid"])),
            "imputed_count": int(np.count_nonzero(full_audit["imputed"])),
            "source_valid_non_imputed_count": int(np.count_nonzero(full_audit["source_valid"] & ~full_audit["imputed"])),
        },
        "visible_text_policy": {
            "audience_visible_scope": (
                "title + axes + fixed speed colorbar + in-map 1 m/s scale only"
                if args.show_title
                else "axes + fixed speed colorbar + in-map 1 m/s scale only; title reserved for PPT overlay"
            ),
            "main_title_visible": bool(args.show_title),
            "phase_utc_line_visible": False,
            "panel_caption_visible": False,
            "pc_values_visible": False,
            "k_symbols_visible": False,
            "forbidden_tokens": [*DISPLAY_FORBIDDEN_TOKENS, "模態 1 時間係數", "累積流場變異百分比達 90%"],
        },
        "regions": [
            _raw_region_manifest(
                dataset,
                summary,
                full_audit,
                shared_vmax=True,
                temporal_interpolated=args.temporal_interpolation,
                show_title=args.show_title,
            )
            for dataset, summary in zip(datasets, summaries)
        ],
    }


def _run_manifest_qa(manifest: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """執行不依賴 OCR 的技術、版面與觀眾文字 QA，並回傳可序列化摘要。

    除了逐區檢查色條、比例尺與文字外，本函式會把四區最終主圖／色條 bbox
    放在同一個像素座標系比較。這是 2×2 簡報一致性的必要條件；若只驗證每區
    自己「合法」，仍可能出現 A/B 與 C/D 的底部白邊不一致。
    """

    region_results = []
    for region in manifest.get("regions", []):
        outputs = region.get("outputs", {})
        video = Path(outputs.get("mp4", {}).get("server_path", ""))
        probe = outputs.get("mp4", {}).get("ffprobe", {})
        stream = probe.get("video", {}) if isinstance(probe, dict) else {}
        render = manifest.get("render_policy", {})
        visual = region.get("visual_spec", {})
        text = visual.get("display_text", {})
        arrow = visual.get("arrow_legend", {}).get("layout", {})
        cbar = visual.get("colorbar_alignment", {})
        axis_layout = visual.get("axis_ticks", {}).get("x", {}).get("label_bbox_qa", {})
        checks = {
            "video_exists": video.is_file(),
            "codec_h264": stream.get("codec_name") == "h264",
            "pixel_format_yuv420p": stream.get("pix_fmt") == "yuv420p",
            "width_ok": int(stream.get("width", -1)) == int(render.get("width_px", -2)),
            "height_ok": int(stream.get("height", -1)) == int(render.get("height_px", -2)),
            "fps_ok": stream.get("r_frame_rate") == f"{int(render.get('fps', 0))}/1",
            "frame_count_ok": int(stream.get("nb_frames", -1)) == int(render.get("expected_frame_count", -2)),
            "no_audio": int(outputs.get("mp4", {}).get("ffprobe", {}).get("audio_stream_count", -1)) == 0,
            "colorbar_same_height": cbar.get("aligned") is True,
            "arrow_inside_main_axes": arrow.get("inside_main_axes") is True and arrow.get("reference_mps") == 1.0,
            "arrow_outline_removed": arrow.get("outline_removed") is True,
            "title_and_raw_only_text_passed": text.get("passed") is True,
            "no_forbidden_visible_tokens": not text.get("forbidden_tokens_found"),
            "x_ticks_not_clipped": axis_layout.get("clipped") is False,
            "x_axis_label_not_clipped": axis_layout.get("x_axis_label_clipped") is False
            and axis_layout.get("x_axis_label_visible") is True,
        }
        region_results.append({"region_key": region.get("region_key"), "checks": checks, "passed": all(checks.values())})

    # `ax.set_aspect("auto")` 應使四區主圖與色條占用完全相同的像素矩形；這裡
    # 用容許 1 px 的實際 bbox 差異驗證，而不是只相信 renderer 的常數設定。
    axis_boxes = []
    colorbar_boxes = []
    for region in manifest.get("regions", []):
        visual = region.get("visual_spec", {})
        axis_box = visual.get("axis_ticks", {}).get("x", {}).get("label_bbox_qa", {}).get("main_axes_bbox_px")
        cbar_box = visual.get("colorbar_alignment", {}).get("colorbar_bbox_px")
        if isinstance(axis_box, list) and len(axis_box) == 4:
            axis_boxes.append([float(value) for value in axis_box])
        if isinstance(cbar_box, list) and len(cbar_box) == 4:
            colorbar_boxes.append([float(value) for value in cbar_box])

    def _box_spread(boxes: list[list[float]]) -> float | None:
        """計算跨區 bbox 每一座標的最大差異；空集合回傳 None。"""

        if not boxes:
            return None
        array = np.asarray(boxes, dtype=np.float64)
        # 不能把 bbox 的 x0、y0、x1、y1 四種不同座標彼此相減；應逐一比較
        # 四個座標在 A–D 間的最大差異。否則即使四區 bbox 完全相同，也會因
        # x0 與 x1 的絕對值不同而得到錯誤的數百像素 spread。
        return float(np.ptp(array, axis=0).max())

    axis_spread = _box_spread(axis_boxes)
    colorbar_spread = _box_spread(colorbar_boxes)
    shared_frame_passed = bool(
        len(axis_boxes) == len(region_results) and axis_spread is not None and axis_spread <= 1.0
    )
    shared_colorbar_passed = bool(
        len(colorbar_boxes) == len(region_results) and colorbar_spread is not None and colorbar_spread <= 1.0
    )
    for item in region_results:
        item["checks"]["shared_main_axes_bbox"] = shared_frame_passed
        item["checks"]["shared_colorbar_bbox"] = shared_colorbar_passed
        item["passed"] = all(item["checks"].values())
    return {
        "validated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "regions": region_results,
        "layout_consistency": {
            "mode": manifest.get("render_policy", {}).get("panel_layout", {}).get("mode"),
            "main_axes_bbox_px": axis_boxes,
            "main_axes_bbox_spread_px": axis_spread,
            "main_axes_bbox_tolerance_px": 1.0,
            "shared_main_axes_bbox_passed": shared_frame_passed,
            "colorbar_bbox_px": colorbar_boxes,
            "colorbar_bbox_spread_px": colorbar_spread,
            "colorbar_bbox_tolerance_px": 1.0,
            "shared_colorbar_bbox_passed": shared_colorbar_passed,
        },
        "all_passed": bool(region_results) and all(item["passed"] for item in region_results),
        "manual_review_required": "請觀看各區 poster/contact sheet，並在簡報 2×2 縮放後確認箭頭、色條與陸地邊界；此自動 QA 不取代人工視覺檢查。",
    }


def parse_args() -> argparse.Namespace:
    """解析 server/local 共用的純原始動畫參數。"""

    parser = argparse.ArgumentParser(description="Render four raw surface-current-only OCM animations.")
    parser.add_argument("--svd-base", type=Path, required=True, help="formal water-column SVD parent directory")
    parser.add_argument("--surface-cache-base", type=Path, required=True, help="same-source preprocessed/ocm_surface parent directory")
    parser.add_argument("--full-product-dir", type=Path, required=True, help="full Taiwan 1 km 6-hour product for time/QC audit")
    parser.add_argument("--coastline-geojson", type=Path, required=True, help="exact coastline GeoJSON used at render time")
    parser.add_argument("--output-dir", type=Path, required=True, help="new versioned output directory")
    parser.add_argument("--regions", default="A,B,C,D", help="comma-separated A/B/C/D")
    parser.add_argument("--fps", type=int, default=RAW_FPS, help="video fps")
    parser.add_argument("--width", type=int, default=RAW_WIDTH, help="video width")
    parser.add_argument("--height", type=int, default=RAW_HEIGHT, help="video height")
    parser.add_argument("--dpi", type=int, default=DEFAULT_RENDER_DPI, help="Matplotlib raster dpi")
    parser.add_argument("--target-arrows", type=int, default=DEFAULT_TARGET_ARROWS, help="approximate arrows per map")
    parser.add_argument("--quiver-scale-multiplier", type=float, default=DEFAULT_QUIVER_SCALE_MULTIPLIER, help="larger means shorter arrows")
    parser.add_argument(
        "--fixed-speed-vmax",
        type=float,
        default=RAW_DEFAULT_SPEED_VMAX_MPS,
        help="fixed cross-region colorbar upper bound in m/s; display normalization only",
    )
    parser.add_argument(
        "--speed-tick-step",
        type=float,
        default=RAW_DEFAULT_SPEED_TICK_STEP_MPS,
        help="fixed colorbar major tick spacing in m/s",
    )
    parser.add_argument("--font-path", type=Path, default=None, help="CJK font path")
    parser.add_argument(
        "--show-title",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否在影片內嵌 figure-level 主標題；--no-show-title 供簡報後製疊加可編輯標題",
    )
    parser.add_argument("--temporal-interpolation", action="store_true", help="use display-only 12-hour-anchor interpolation")
    parser.add_argument("--overwrite", action="store_true", help="overwrite only files in explicitly supplied output directory")
    return parser.parse_args()


def main() -> None:
    """載入正式資料、輸出四支 raw-only 動畫並寫入 manifest/README/QA。"""

    args = parse_args()
    if args.fps <= 0 or args.width <= 0 or args.height <= 0 or args.dpi <= 0 or args.target_arrows <= 0 or args.quiver_scale_multiplier <= 0:
        raise ValueError("fps/width/height/dpi/target-arrows/quiver-scale-multiplier 必須為正值")
    if not np.isfinite(args.fixed_speed_vmax) or args.fixed_speed_vmax <= 0.0:
        raise ValueError("fixed-speed-vmax 必須為正有限值")
    if not np.isfinite(args.speed_tick_step) or args.speed_tick_step <= 0.0 or args.speed_tick_step > args.fixed_speed_vmax:
        raise ValueError("speed-tick-step 必須為正值且不可大於 fixed-speed-vmax")
    if args.width % 2 or args.height % 2:
        raise ValueError("H.264 yuv420p 輸出要求 width/height 為偶數")
    if args.width != RAW_WIDTH or args.height != RAW_HEIGHT:
        print(f"注意：目前版面確認基準為 {RAW_WIDTH}×{RAW_HEIGHT}，本次使用 {args.width}×{args.height}", flush=True)
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
        select_phase_windows(dataset)
        materialize_payloads(dataset, full_audit)
        if args.temporal_interpolation:
            apply_temporal_interpolation(dataset)
        else:
            set_temporal_interpolation_disabled(dataset)
        _choose_raw_speed_scale(dataset)
        _choose_raw_arrow_scale(dataset)
        print(
            f"{spec.key} source_mode={dataset.source_mode} common_6h={dataset.common_time_ns.size} "
            f"raw_p995={dataset.speed_scale_p995:.3f} raw_arrow_p95={dataset.quiver_reference_mps:.3f}",
            flush=True,
        )
        datasets.append(dataset)
    # choose_global_vmax 仍執行一次，只為保留跨區原始 p99.5 的診斷資訊；正式
    # renderer 必須以使用者指定的固定色階繪圖，不能讓資料導出的上限覆蓋簡報
    # 靜態圖規格。因此在此處明確覆寫每區 speed_scale_vmax 與 tick step。
    auto_shared_vmax = choose_global_vmax(datasets)
    shared_vmax = float(args.fixed_speed_vmax)
    for dataset in datasets:
        dataset.speed_scale_vmax = shared_vmax
        dataset.speed_tick_step_mps = float(args.speed_tick_step)
    print(
        f"raw p99.5 diagnostic common vmax={auto_shared_vmax:.1f} m/s; "
        f"fixed display vmax={shared_vmax:.1f} m/s; ticks every {args.speed_tick_step:.1f} m/s",
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
                temporal_interpolated=args.temporal_interpolation,
                show_title=args.show_title,
                overwrite=args.overwrite,
            )
        )
    manifest = _build_manifest(args, datasets, summaries, full_audit, shared_vmax)
    manifest["qa"] = _run_manifest_qa(manifest, args.output_dir)
    manifest_path = args.output_dir / "animation_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_readme(args.output_dir / "README.md", manifest)
    (args.output_dir / "RENDER_COMPLETE").write_text(
        "A–D 純原始表層流場動畫已完成；正式 SVD 未改寫，coastline 修正僅限 visualization-only。\n",
        encoding="utf-8",
    )
    print(f"manifest={manifest_path}", flush=True)
    print(json.dumps(manifest["qa"], ensure_ascii=False, indent=2), flush=True)
    if not manifest["qa"]["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
