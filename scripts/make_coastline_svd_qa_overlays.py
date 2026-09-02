#!/usr/bin/env python3
"""產生 A–D exact coastline、1 km 網格與代表影格疊圖 QA。

這些 PNG 是地理 QA 證據，不是簡報主圖，也不會修改 PPTX。每一區輸出一張 2×3
疊圖：左上顯示 conservative exact-land mask 與 1 km cell-center 網格；其餘面板
顯示模態 1 時間係數正/負相位案例的同源原始流場與前 n 個模態重建流場。所有流速/箭頭
面板同時保留兩套語意：conservative
``analysis_geometry & static_ocean & surface_feature & ~coastline_land`` 只供
資料／land audit 計數；可見疊圖則使用不扣除 conservative land 的
``analysis_geometry & static_ocean & surface_feature``，再由高解析度 GeoJSON
vector polygon 在最上層覆蓋真實陸地。這樣 QA 可以同時證明 audit mask 上
raw/reconstruction finite render 與箭頭有效數為零，以及「保留邊界 cell 供繪圖」沒有
在最終 canvas 的 exact polygon 內留下流場。GeoJSON 外環以最高 z-order 蓋住底層，
專門檢查臺灣南端、龜山島、馬祖群島及中國沿岸是否出現假海岸線或 raster 白階梯。

本程式讀取 2024–2025 同源 surface monthly cache 的單一代表時間，並由既有正式 SVD
的 raw-PC 公式重建表層；不把 standardized PC 乘入 loading，也不計算 RMSE。代表性
相位選取沿用完整 28 影格、6 小時連續、模態 1 時間係數絕對值優先規則。coastline
修正只限展示階段，不改寫既有正式 SVD。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
from matplotlib.path import Path as MatplotlibPath
import numpy as np

from coastline_utils import build_coastline_land_mask, draw_vector_land_overlay, load_outer_rings
from compare_ocm_svd_coastline_versions import REGIONS, _common_time, _select_windows, _utc
from visualize_ocm_svd_modal_context import (
    AXIS_FONT_SIZE_PT,
    CAPTION_FONT_SIZE_PT,
    DISPLAY_REGION_TITLES,
    LAND_ANTIALIASED,
    LAND_COLOR,
    LAND_EDGE_COLOR,
    LAND_EDGE_WIDTH,
    TEXT_COLOR,
    _display_axis_spec_for_region,
    _apply_geographic_axis_style,
    choose_quiver_step,
)


def _cache_refs(cache_root: Path) -> dict[int, tuple[Path, int]]:
    """建立 cache UTC ns 到 monthly row 的索引；只保存路徑/row，不載入兩年 u/v。"""

    refs: dict[int, tuple[Path, int]] = {}
    for month_dir in sorted(cache_root.glob("months/20*")):
        if not month_dir.is_dir() or not month_dir.name.isdigit():
            continue
        path = month_dir / "time_utc_ns.npy"
        if not path.is_file():
            continue
        times = np.load(path, allow_pickle=False).astype(np.int64, copy=False)
        for row, value in enumerate(times.tolist()):
            refs[int(value)] = (month_dir, row)
    if not refs:
        raise FileNotFoundError(f"找不到可用 surface cache 時間軸：{cache_root}")
    return refs


def _read_raw(cache_ref: tuple[Path, int], expected_shape: tuple[int, int], valid_mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """讀取一個 monthly row 並套用呼叫端指定的有效遮罩。

    monthly cache 的 ``u_surface_mps``、``v_surface_mps`` 仍是原始同源表層資料；
    ``valid_mask`` 在本程式可能是兩種語意：conservative audit mask 會排除
    exact-land cell，display mask 則保留與 vector coastline 相交的邊界 cell，讓
    高解析度 polygon 決定可見海岸線。兩者都會再與逐時 valid/u/v finite 交集；這裡
    只把不應進入該 QA 面板的 cell 設成 NaN，不回寫 cache，也不改變正式 SVD 的
    輸入或係數。
    """

    month_dir, row = cache_ref
    u = np.asarray(np.load(month_dir / "u_surface_mps.npy", mmap_mode="r", allow_pickle=False)[row], dtype=np.float32).copy()
    v = np.asarray(np.load(month_dir / "v_surface_mps.npy", mmap_mode="r", allow_pickle=False)[row], dtype=np.float32).copy()
    source_valid = np.asarray(np.load(month_dir / "valid_mask_surface.npy", mmap_mode="r", allow_pickle=False)[row], dtype=bool)
    if u.shape != expected_shape or v.shape != expected_shape or source_valid.shape != expected_shape:
        raise ValueError(f"surface row shape 不符：{u.shape}, {v.shape}, {source_valid.shape} != {expected_shape}")
    valid = valid_mask & source_valid & np.isfinite(u) & np.isfinite(v)
    u[~valid] = np.nan
    v[~valid] = np.nan
    return u, v


def _reconstruct(svd_dir: Path, svd_index: int, k90: int, valid_mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """以既有正式 SVD 的 mean、raw PC 與 per-raw-PC mode 重建表層 u/v。

    正式結果的 level=0 表層 mean、``pc.npy`` 與
    ``mode_*_mps_per_raw_pc.npy`` 均以 ``lat, lon`` 網格保存；此函式只在選定代表
    時刻取前 ``k90`` 個模態，並依呼叫端選擇套用 conservative audit mask 或
    display mask。這不是 coastline-corrected SVD 重算，且不使用 standardized PC
    進行重建。
    """

    mean_u = np.asarray(np.load(svd_dir / "mean_u_mps.npy", mmap_mode="r", allow_pickle=False)[0], dtype=np.float32).copy()
    mean_v = np.asarray(np.load(svd_dir / "mean_v_mps.npy", mmap_mode="r", allow_pickle=False)[0], dtype=np.float32).copy()
    pc = np.asarray(np.load(svd_dir / "pc.npy", mmap_mode="r", allow_pickle=False)[:k90, svd_index], dtype=np.float32)
    mode_u = np.asarray(np.load(svd_dir / "mode_u_mps_per_raw_pc.npy", mmap_mode="r", allow_pickle=False)[:k90, 0], dtype=np.float32)
    mode_v = np.asarray(np.load(svd_dir / "mode_v_mps_per_raw_pc.npy", mmap_mode="r", allow_pickle=False)[:k90, 0], dtype=np.float32)
    u = mean_u + np.einsum("k,kyx->yx", pc, mode_u)
    v = mean_v + np.einsum("k,kyx->yx", pc, mode_v)
    valid = valid_mask & np.isfinite(u) & np.isfinite(v)
    u[~valid] = np.nan
    v[~valid] = np.nan
    return u, v


def _speed(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """計算 m/s 流速並維持 NaN 語意。"""

    value = np.hypot(u, v).astype(np.float32)
    value[~np.isfinite(u) | ~np.isfinite(v)] = np.nan
    return value


def _format_axes(
    ax: Any,
    lon: np.ndarray,
    lat: np.ndarray,
    font: Any | None,
    display_axis_spec: dict[str, Any] | None = None,
) -> None:
    """套用與動畫相同的 display-only 經緯度、範圍與中文字型設定。"""

    # QA 疊圖直接重用正式 renderer 的 locator/formatter，避免 QA 圖仍顯示舊的
    # 每 0.2° 三位數刻度，導致獨立檢查者無法確認實際 bbox endpoint 是否可見。
    _apply_geographic_axis_style(
        ax,
        lon,
        lat,
        font=font,
        show_x_labels=True,
        display_axis_spec=display_axis_spec,
    )


def _sample_land_pixels_on_final_canvas(
    ax: Any,
    rgba: np.ndarray,
    candidate_points: np.ndarray,
    land_rings: list[np.ndarray],
) -> dict[str, Any]:
    """在完成整張 QA 圖版面配置後，抽查 exact-land 代表點的實際像素。

    ``constrained_layout`` 可能在後續面板、標題與軸名加入後重新調整 axes 位置；
    因此第一次繪製單一面板時取得的 ``transData`` 不一定等於最後輸出 PNG 的座標。
    本函式刻意在所有面板建立完成、canvas 最終 draw 後再次取樣，避免把「舊 layout
    的座標」誤當作陸地遮蔽失敗。``candidate_points`` 已由前面的 exact vector
    polygon containment 與安全內縮規則篩選；此處再用最後 axes 的像素座標反算
    一個約 2 px 的屏幕內縮區，排除極細小島或岸線 anti-alias 邊界無法代表「實心
    陸地」的取樣點。這只改變 QA 的代表點選擇，不重新建立科學 raster mask，也
    不放寬陸地遮蔽判定。
    """

    pixel_samples = 0
    pixel_mismatch = 0
    pixel_mismatch_examples: list[dict[str, Any]] = []
    if candidate_points.size:
        # 先為每個候選點找出實際包含它的外環，後續只對該外環測試屏幕內縮點。
        # 直接讓每個偏移點重新掃描 1,912 個 polygon 會使四區 QA 成本急遽增加；
        # 以 ring index 分組仍保留 exact vector 語意，同時把計算限制在候選外環。
        candidate_ring_index = np.full(candidate_points.shape[0], -1, dtype=np.int32)
        for ring_index, ring in enumerate(land_rings):
            unknown = candidate_ring_index < 0
            if not np.any(unknown):
                break
            bbox = (
                (candidate_points[:, 0] >= float(np.min(ring[:, 0])))
                & (candidate_points[:, 0] <= float(np.max(ring[:, 0])))
                & (candidate_points[:, 1] >= float(np.min(ring[:, 1])))
                & (candidate_points[:, 1] <= float(np.max(ring[:, 1])))
            )
            test_indices = np.flatnonzero(unknown & bbox)
            if test_indices.size:
                hits = MatplotlibPath(ring).contains_points(candidate_points[test_indices])
                candidate_ring_index[test_indices[hits]] = ring_index
        contained = candidate_ring_index >= 0
        candidate_points = candidate_points[contained]
        candidate_ring_index = candidate_ring_index[contained]
        if candidate_points.size:
            # 以最後 canvas 的像素尺寸定義內縮，而不是把經緯度直接當成距離；
            # 這可適應 A–D 不同 display extent 與各自 axes 寬高。九宮格的邊界
            # 測試要求偏移後仍在同一 exact outer ring 內，避免取到狹窄岸線邊緣。
            center_xy = ax.transData.transform(candidate_points)
            interior_radius_px = 2.0
            pixel_offsets = np.asarray(
                [
                    (-interior_radius_px, -interior_radius_px),
                    (-interior_radius_px, 0.0),
                    (-interior_radius_px, interior_radius_px),
                    (0.0, -interior_radius_px),
                    (0.0, interior_radius_px),
                    (interior_radius_px, -interior_radius_px),
                    (interior_radius_px, 0.0),
                    (interior_radius_px, interior_radius_px),
                ],
                dtype=np.float64,
            )
            screen_interior = np.ones(candidate_points.shape[0], dtype=bool)
            inverse_transform = ax.transData.inverted()
            for pixel_offset in pixel_offsets:
                shifted_data = inverse_transform.transform(center_xy + pixel_offset)
                for ring_index in np.unique(candidate_ring_index).tolist():
                    group = candidate_ring_index == ring_index
                    screen_interior[group] &= MatplotlibPath(land_rings[ring_index]).contains_points(shifted_data[group])
            candidate_points = candidate_points[screen_interior]
        display_xy = ax.transData.transform(candidate_points)
        height_px, width_px = rgba.shape[:2]
        pixel_x = np.rint(display_xy[:, 0]).astype(int)
        # Matplotlib transform 的 y 原點在圖框底部，而 Agg buffer 的 row index
        # 以圖像上方為 0；轉換後才是與最後 PNG 相同的像素位置。
        pixel_y = height_px - 1 - np.rint(display_xy[:, 1]).astype(int)
        inside_canvas = (
            (pixel_x >= 0)
            & (pixel_x < width_px)
            & (pixel_y >= 0)
            & (pixel_y < height_px)
        )
        sampled_rgb = rgba[pixel_y[inside_canvas], pixel_x[inside_canvas], :3].astype(np.int16)
        expected_rgb = np.rint(np.asarray(mcolors.to_rgb(LAND_COLOR)) * 255.0).astype(np.int16)
        pixel_samples = int(sampled_rgb.shape[0])
        mismatch_mask = np.max(np.abs(sampled_rgb - expected_rgb), axis=1) > 35
        pixel_mismatch = int(np.count_nonzero(mismatch_mask))
        mismatch_indices = np.flatnonzero(mismatch_mask)[:12]
        for mismatch_index in mismatch_indices.tolist():
            valid_points = candidate_points[inside_canvas]
            valid_display_xy = display_xy[inside_canvas]
            pixel_mismatch_examples.append(
                {
                    "data_lon": float(valid_points[mismatch_index, 0]),
                    "data_lat": float(valid_points[mismatch_index, 1]),
                    "canvas_x_px": int(pixel_x[inside_canvas][mismatch_index]),
                    "canvas_y_px": int(pixel_y[inside_canvas][mismatch_index]),
                    "display_x_px": float(valid_display_xy[mismatch_index, 0]),
                    "display_y_px_bottom_origin": float(valid_display_xy[mismatch_index, 1]),
                    "rgb": [int(value) for value in sampled_rgb[mismatch_index].tolist()],
                }
            )
    return {
        "visible_land_pixel_sample_count": pixel_samples,
        "visible_land_pixel_mismatch_count": pixel_mismatch,
        "visible_land_pixel_mismatch_examples": pixel_mismatch_examples,
        "visible_land_pixel_fill_passed": pixel_samples > 0 and pixel_mismatch == 0,
    }


def _plot_field(
    ax: Any,
    lon: np.ndarray,
    lat: np.ndarray,
    u: np.ndarray,
    v: np.ndarray,
    *,
    audit_u: np.ndarray,
    audit_v: np.ndarray,
    land_rings: list[np.ndarray],
    land_mask: np.ndarray,
    title: str,
    field_kind: str,
    font: Any | None,
    display_axis_spec: dict[str, Any],
) -> dict[str, Any]:
    """繪製單一代表流場，並回傳可區分 raw／reconstruction 的地理 QA 計數。

    ``u/v`` 是要實際 rasterize 的 display field；它刻意可保留與 exact polygon 相交
    的邊界 cell，避免 conservative raster land mask 形成白色鋸齒。``audit_u/audit_v``
    則由 conservative ``plot_mask`` 產生，只用來計算 exact-land finite/arrow 計數，
    因而不會把「為了讓 vector coastline 無白邊而保留的底層 cell」誤報為資料稽核
    污染。最終是否真的可見，另由 vector patch z-order 與 canvas pixel QA 驗證。

    ``field_kind`` 只允許 ``raw`` 或 ``reconstruction``，因為獨立檢查需要知道
    exact-land 上的有限值與箭頭是否分別出現在原始面板或模態重建面板；若只累加成
    單一總數，無法排除某一面板通過、另一面板污染的情形。此函式仍只檢查代表性
    正／負相位影格，兩年全時序的 source-valid／缺值統計則由 land audit 負責。
    同時把目前 axes rasterize 成 canvas，抽查 exact polygon 內的顯示像素，驗證
    「最終可見畫面」確實由 vector land fill 覆蓋 flow artists，而不是只依賴資料
    陣列 finite 計數。
    """

    if field_kind not in {"raw", "reconstruction"}:
        raise ValueError(f"不支援的 QA 流場類型：{field_kind}")
    if u.shape != audit_u.shape or v.shape != audit_v.shape:
        raise ValueError("display field 與 audit field shape 不一致")

    speed = _speed(u, v)
    audit_speed = _speed(audit_u, audit_v)
    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad((0.0, 0.0, 0.0, 0.0))
    ax.set_facecolor("#ffffff")
    # QA 圖統一沿用 v2 跨區 0–2.2 m/s 色階；超過上限飽和而不截斷原始值。
    speed_mesh = ax.pcolormesh(
        lon,
        lat,
        np.ma.masked_invalid(np.clip(speed, 0.0, 2.2)),
        shading="auto",
        cmap=cmap,
        vmin=0.0,
        vmax=2.2,
        zorder=2,
    )
    step_y, step_x = choose_quiver_step(lon.size, lat.size, 420)
    # 這裡故意不再以 conservative land_mask 砍掉 display field。相交 cell 的海側
    # 部分必須先保留，才能讓高解析度 vector polygon 的真實邊界覆蓋到底層，不產生
    # 一公里階梯白邊；polygon 內的箭頭會由 zorder=30 的陸地 patch 完整遮蔽。
    valid = np.isfinite(u) & np.isfinite(v)
    quiver = ax.quiver(
        lon[::step_x],
        lat[::step_y],
        np.ma.masked_where(~valid[::step_y, ::step_x], u[::step_y, ::step_x]),
        np.ma.masked_where(~valid[::step_y, ::step_x], v[::step_y, ::step_x]),
        color="#ffffff",
        scale=20.0 * max(float(np.nanpercentile(speed[np.isfinite(speed)], 95.0)) if np.isfinite(speed).any() else 0.05, 0.05),
        width=0.0025,
        headwidth=3.1,
        headlength=4.2,
        zorder=10,
    )
    quiver.set_path_effects([])
    # QA 流場面板也必須與正式 renderer 相同：保守 raster mask 只負責遮罩／計數，
    # 可見陸地由 exact GeoJSON 高解析度 vector fill 提供，且不加深色 artificial edge。
    patch_start = len(ax.patches)
    draw_vector_land_overlay(
        ax,
        land_rings,
        tuple(float(value) for value in display_axis_spec["display_extent"]),
        facecolor=LAND_COLOR,
        edgecolor=LAND_EDGE_COLOR,
        linewidth=LAND_EDGE_WIDTH,
        antialiased=LAND_ANTIALIASED,
        zorder=30,
    )
    land_patches = list(ax.patches[patch_start:])
    _format_axes(ax, lon, lat, font, display_axis_spec)
    ax.set_title(title, fontsize=CAPTION_FONT_SIZE_PT, fontproperties=font, color=TEXT_COLOR, pad=3)
    sampled_land = land_mask[::step_y, ::step_x]
    # land audit 僅使用 conservative audit field；display field 可能在 raster land
    # cell 上仍有有限值，這是為了避免白階梯的繪圖策略，不是「陸地資料通過稽核」。
    arrow_count_on_land = int(
        np.count_nonzero(
            np.isfinite(audit_u[::step_y, ::step_x])
            & np.isfinite(audit_v[::step_y, ::step_x])
            & sampled_land
        )
    )
    render_finite_on_raster_land = int(np.count_nonzero(np.isfinite(speed) & land_mask))
    render_arrow_on_raster_land = int(
        np.count_nonzero(
            np.isfinite(u[::step_y, ::step_x])
            & np.isfinite(v[::step_y, ::step_x])
            & sampled_land
        )
    )
    # 這裡不是只檢查 u/v 陣列，而是將目前 axes 真正 rasterize 到 canvas 後，
    # 在 exact polygon 內抽樣像素。若 vector land patch 沒有位於 speed mesh/quiver
    # 之上，或 polygon 填色未出現在最終 canvas，這項 visual occlusion QA 會失敗。
    ax.figure.canvas.draw()
    rgba = np.asarray(ax.figure.canvas.buffer_rgba(), dtype=np.uint8)
    grid_lon, grid_lat = np.meshgrid(lon, lat)
    grid_points = np.column_stack((grid_lon.ravel(), grid_lat.ravel()))
    inside_vector = np.zeros(grid_points.shape[0], dtype=bool)
    for ring in land_rings:
        inside_vector |= MatplotlibPath(ring).contains_points(grid_points)
    # 直接抽樣所有 exact-land cell center 會把岸線附近的 anti-aliased 邊界像素
    # 誤當成填色失敗：那些像素本來就會混合指定陸地色與下方背景。為了回答
    # 「最終可見畫面是否在 polygon 內遮住流場」而非「邊界每一個混合像素是否為單色」，
    # 先用四周 0.25 個網格間距的內縮安全點抽樣。這只影響 QA 的像素取樣，不影響
    # conservative raster land mask、renderer 遮罩或 vector polygon 的實際繪製。
    # 內縮一個格點是因為目前 QA 圖的 axes 寬度有限；四捨五入到鄰近 canvas pixel
    # 後，靠近 ring 的中心點仍可能抽到 vector fill 的 anti-aliased 邊緣。主島內部
    # 仍有大量安全樣本，若只有小島則會依下方 fallback 保留中心點證據；這只調整
    # QA 抽樣位置，不改變 polygon、raster land mask 或正式動畫的繪圖結果。
    lon_step = float(np.median(np.diff(lon))) if lon.size > 1 else 0.0
    lat_step = float(np.median(np.diff(lat))) if lat.size > 1 else 0.0
    # 網格最外圈的 center 會正好落在 axes spine 上；即使它位於 polygon 內，
    # canvas 取樣也可能讀到邊框 RGB 而非 land fill。排除半格邊界只影響像素 QA，
    # 不改變 land cell count，也不代表最外圈在正式 renderer 中未被 vector polygon
    # 覆蓋。完整地理遮罩仍由上游 exact-land 陣列計數驗證。
    # QA 像素抽樣必須只取 display-only viewport 內的 exact-land cell；display
    # crop 外的完整 SVD 網格雖然仍保留在資料／audit，但其資料座標轉換會落在
    # axes 外的 figure 白底，若拿來和 land fill 比較會製造錯誤的遮蔽失敗。
    display_lon_min, display_lon_max, display_lat_min, display_lat_max = (
        float(value) for value in display_axis_spec["display_extent"]
    )
    view_interior = np.ones(grid_points.shape[0], dtype=bool)
    if lon_step > 0.0 and lat_step > 0.0:
        view_interior = (
            (grid_points[:, 0] > display_lon_min + 0.5 * lon_step)
            & (grid_points[:, 0] < display_lon_max - 0.5 * lon_step)
            & (grid_points[:, 1] > display_lat_min + 0.5 * lat_step)
            & (grid_points[:, 1] < display_lat_max - 0.5 * lat_step)
        )
    land_candidates = inside_vector & land_mask.ravel() & view_interior

    def _inside_any_ring(points: np.ndarray) -> np.ndarray:
        """回傳點是否落在任一 exact coastline 外環內。"""

        inside = np.zeros(points.shape[0], dtype=bool)
        for ring in land_rings:
            inside |= MatplotlibPath(ring).contains_points(points)
        return inside

    safe_core = land_candidates.copy()
    if lon_step > 0.0 and lat_step > 0.0 and np.any(land_candidates):
        candidate_xy = grid_points[land_candidates]
        # 這裡只是在實際 canvas 上抽取「可見陸地填色」的代表點，不是重新定義
        # 科學 land mask。內縮兩個網格步長可避開 1 km raster audit mask 與
        # 高解析度 vector coastline 之間的子像素邊界；否則複雜小島的單一
        # 邊界像素可能混入底層 pcolormesh 顏色，造成 QA 將 anti-alias 邊界
        # 誤判為流速仍穿透陸地。正式繪圖仍完整使用 exact vector polygon，
        # 並不因這個取樣策略而放寬陸地遮蔽規則。
        for offset_lon, offset_lat in (
            (2.00 * lon_step, 0.0),
            (-2.00 * lon_step, 0.0),
            (0.0, 2.00 * lat_step),
            (0.0, -2.00 * lat_step),
        ):
            shifted = candidate_xy + np.array([offset_lon, offset_lat], dtype=np.float64)
            safe_core[land_candidates] &= _inside_any_ring(shifted)
    if np.any(safe_core):
        candidate_points = grid_points[safe_core]
    else:
        # 小島若沒有足夠內縮 cell，仍保留 center sample；這時結果只作補充證據，
        # 而非因 sample 數為零就錯誤宣稱 polygon 內沒有可見陸地。
        candidate_points = grid_points[land_candidates]
    pixel_samples = 0
    pixel_mismatch = 0
    pixel_mismatch_examples: list[dict[str, Any]] = []
    if candidate_points.size:
        display_xy = ax.transData.transform(candidate_points)
        height_px, width_px = rgba.shape[:2]
        pixel_x = np.rint(display_xy[:, 0]).astype(int)
        pixel_y = height_px - 1 - np.rint(display_xy[:, 1]).astype(int)
        inside_canvas = (
            (pixel_x >= 0)
            & (pixel_x < width_px)
            & (pixel_y >= 0)
            & (pixel_y < height_px)
        )
        sampled_rgb = rgba[pixel_y[inside_canvas], pixel_x[inside_canvas], :3].astype(np.int16)
        expected_rgb = np.rint(np.asarray(mcolors.to_rgb(LAND_COLOR)) * 255.0).astype(np.int16)
        pixel_samples = int(sampled_rgb.shape[0])
        mismatch_mask = np.max(np.abs(sampled_rgb - expected_rgb), axis=1) > 35
        pixel_mismatch = int(np.count_nonzero(mismatch_mask))
        # 保留少量座標與實際 RGB 作為失敗診斷，避免只看到一個總數卻無法判斷
        # 是 anti-alias 邊界、canvas 取樣偏移，還是向量 polygon 未覆蓋到底層流場。
        # 這些是 QA metadata，不會寫入觀眾可見畫面。
        mismatch_indices = np.flatnonzero(mismatch_mask)[:12]
        for mismatch_index in mismatch_indices.tolist():
            pixel_mismatch_examples.append(
                {
                    "data_lon": float(candidate_points[inside_canvas][mismatch_index, 0]),
                    "data_lat": float(candidate_points[inside_canvas][mismatch_index, 1]),
                    # 同時記錄 canvas 座標，方便區分「polygon 未覆蓋」與
                    # QA 取樣座標轉換／影像上下方向錯置；這些欄位只屬稽核資訊。
                    "canvas_x_px": int(pixel_x[inside_canvas][mismatch_index]),
                    "canvas_y_px": int(pixel_y[inside_canvas][mismatch_index]),
                    "display_x_px": float(display_xy[inside_canvas][mismatch_index, 0]),
                    "display_y_px_bottom_origin": float(display_xy[inside_canvas][mismatch_index, 1]),
                    "rgb": [int(value) for value in sampled_rgb[mismatch_index].tolist()],
                }
            )
    overlay_zorder_ok = bool(land_patches) and all(
        patch.get_zorder() > speed_mesh.get_zorder() and patch.get_zorder() > quiver.get_zorder()
        for patch in land_patches
    )
    overlay_edge_removed = bool(land_patches) and all(
        patch.get_linewidth() == 0.0 and np.asarray(patch.get_edgecolor())[-1] == 0.0
        for patch in land_patches
    )
    overlay_antialiased = bool(land_patches) and all(
        bool(patch.get_antialiased()) for patch in land_patches
    )
    return {
        "field_kind": field_kind,
        "land_finite_count": int(np.count_nonzero(np.isfinite(audit_speed) & land_mask)),
        "land_arrow_count": arrow_count_on_land,
        "finite_count": int(np.count_nonzero(np.isfinite(speed))),
        "render_field_finite_count_on_raster_land_before_vector_occlusion": render_finite_on_raster_land,
        "render_field_arrow_count_on_raster_land_before_vector_occlusion": render_arrow_on_raster_land,
        "visible_land_pixel_sample_count": pixel_samples,
        "visible_land_pixel_mismatch_count": pixel_mismatch,
        "visible_land_pixel_mismatch_examples": pixel_mismatch_examples,
        "visible_land_pixel_fill_passed": pixel_samples > 0 and pixel_mismatch == 0,
        "vector_overlay_zorder_passed": overlay_zorder_ok,
        "vector_overlay_edge_removed": overlay_edge_removed,
        "vector_overlay_antialiased": overlay_antialiased,
        # 只供 make_region_overlay 在所有 axes 完成 layout 後重取樣；此內部
        # NumPy 陣列不會寫入 JSON，避免把大筆網格座標混入交付文件。
        "_visible_land_pixel_sample_points": candidate_points,
    }


def make_region_overlay(key: str, *, svd_dir: Path, cache_root: Path, full_product_dir: Path, coastline_path: Path, output_dir: Path, suffix: str, font: Any | None) -> dict[str, Any]:
    """產出單區 exact mask + 網格 + 正/負代表影格疊圖與 QA 計數。"""

    lon = np.load(svd_dir / "lon.npy", allow_pickle=False).astype(np.float64)
    lat = np.load(svd_dir / "lat.npy", allow_pickle=False).astype(np.float64)
    # QA overlay 與正式動畫共用同一個 display-only 座標規格；A–D 分別依簡報第
    # 6–9 頁靜態流場圖裁切，科學稽核仍保留完整原始 SVD bbox 與全網格 land count。
    display_axis_spec = _display_axis_spec_for_region(key, lon, lat)
    static = np.load(svd_dir / "static_ocean_mask.npy", allow_pickle=False).astype(bool)
    geometry = np.load(svd_dir / "analysis_geometry_mask.npy", allow_pickle=False).astype(bool)
    feature = np.load(svd_dir / "velocity_feature_mask.npy", allow_pickle=False).astype(bool)[0]
    land, coastline_summary = build_coastline_land_mask(lon, lat, coastline_path)
    # conservative plot_mask 只供 exact-land 資料稽核；display_mask 保留相交 cell，
    # 讓 vector polygon 而不是一公里 raster 邊界決定最後可見岸線。兩套 mask 必須
    # 同時記錄，否則 QA 會把「陸地上資料為零」與「畫面邊界無白階梯」混成一件事。
    plot_mask = geometry & static & feature & ~land
    display_mask = geometry & static & feature
    # 這項計數把「真實地理 polygon」與「分析幾何域」分開驗證。若兩者重疊於
    # geometry=False 的 cell，renderer 應以分析域外中性色處理，而不是把它寫成
    # 模型有效域中的陸地；目前四區矩形分析幾何完整覆蓋 bbox，預期為零。
    land_outside_geometry_count = int(np.count_nonzero(land & ~geometry))
    cumulative = np.load(svd_dir / "cumulative_explained_variance.npy", allow_pickle=False).astype(np.float64)
    k90 = int(np.flatnonzero(cumulative >= 0.9)[0] + 1)
    common_time, common_indices = _common_time(svd_dir, full_product_dir, cache_root)
    pc_std = np.asarray(np.load(svd_dir / "pc_standardized.npy", mmap_mode="r", allow_pickle=False)[0, common_indices], dtype=np.float64)
    selection = _select_windows(common_time, pc_std)
    refs = _cache_refs(cache_root)
    display_fields: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    audit_fields: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    frame_meta = []
    for phase in ("positive", "negative"):
        center_order = selection["windows"][phase]["center"]
        time_ns = int(common_time[center_order])
        display_raw = _read_raw(refs[time_ns], (lat.size, lon.size), display_mask)
        audit_raw = _read_raw(refs[time_ns], (lat.size, lon.size), plot_mask)
        display_rec = _reconstruct(svd_dir, int(common_indices[center_order]), k90, display_mask)
        audit_rec = _reconstruct(svd_dir, int(common_indices[center_order]), k90, plot_mask)
        display_fields[f"raw_{phase}"] = display_raw
        display_fields[f"reconstruction_{phase}"] = display_rec
        audit_fields[f"raw_{phase}"] = audit_raw
        audit_fields[f"reconstruction_{phase}"] = audit_rec
        frame_meta.append({"phase": phase, "utc": _utc(time_ns), "pc1_standardized": float(pc_std[center_order])})

    fig, axes = plt.subplots(2, 3, figsize=(15, 9), dpi=140, constrained_layout=True)
    mask_cmap = mcolors.ListedColormap(["#ffffff", "#a29d93"])
    axes[0, 0].pcolormesh(lon, lat, land.astype(np.int8), shading="auto", cmap=mask_cmap, vmin=0, vmax=1)
    for grid_lon in lon[::max(1, lon.size // 15)]:
        axes[0, 0].axvline(float(grid_lon), color="#5c6d73", alpha=0.18, linewidth=0.35)
    for grid_lat in lat[::max(1, lat.size // 12)]:
        axes[0, 0].axhline(float(grid_lat), color="#5c6d73", alpha=0.18, linewidth=0.35)
    draw_vector_land_overlay(
        axes[0, 0],
        load_outer_rings(coastline_path),
        tuple(float(value) for value in display_axis_spec["display_extent"]),
        facecolor=LAND_COLOR,
        edgecolor=LAND_EDGE_COLOR,
        linewidth=LAND_EDGE_WIDTH,
        antialiased=LAND_ANTIALIASED,
        zorder=30,
    )
    _format_axes(axes[0, 0], lon, lat, font, display_axis_spec)
    axes[0, 0].set_title("科學稽核陸地遮罩＋1 km 網格（不作為可見岸線）", fontsize=CAPTION_FONT_SIZE_PT, fontproperties=font, color=TEXT_COLOR, pad=3)
    axes[0, 0].text(0.02, 0.03, f"land={int(np.count_nonzero(land))}/{land.size} ({np.mean(land)*100:.2f}%)", transform=axes[0, 0].transAxes, fontsize=AXIS_FONT_SIZE_PT, color=TEXT_COLOR, fontproperties=font, bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "none"})
    qa_records: list[dict[str, Any]] = []
    qa_axes: list[Any] = []
    # QA 疊圖本身也可能被獨立檢查或人工截圖觀看，因此可見標籤沿用簡報術語；
    # k90 與 PC1 僅保留在 JSON/manifest 的內部欄位，不在圖中文字中展開。
    land_rings = load_outer_rings(coastline_path)
    qa_records.append(_plot_field(axes[0, 1], lon, lat, *display_fields["raw_positive"], audit_u=audit_fields["raw_positive"][0], audit_v=audit_fields["raw_positive"][1], land_rings=land_rings, land_mask=land, title="原始流場｜模態 1 時間係數：正相位案例", field_kind="raw", font=font, display_axis_spec=display_axis_spec))
    qa_axes.append(axes[0, 1])
    qa_records.append(_plot_field(axes[0, 2], lon, lat, *display_fields["reconstruction_positive"], audit_u=audit_fields["reconstruction_positive"][0], audit_v=audit_fields["reconstruction_positive"][1], land_rings=land_rings, land_mask=land, title=f"前 {k90} 個模態重建流場｜正相位案例", field_kind="reconstruction", font=font, display_axis_spec=display_axis_spec))
    qa_axes.append(axes[0, 2])
    qa_records.append(_plot_field(axes[1, 0], lon, lat, *display_fields["raw_negative"], audit_u=audit_fields["raw_negative"][0], audit_v=audit_fields["raw_negative"][1], land_rings=land_rings, land_mask=land, title="原始流場｜模態 1 時間係數：負相位案例", field_kind="raw", font=font, display_axis_spec=display_axis_spec))
    qa_axes.append(axes[1, 0])
    qa_records.append(_plot_field(axes[1, 1], lon, lat, *display_fields["reconstruction_negative"], audit_u=audit_fields["reconstruction_negative"][0], audit_v=audit_fields["reconstruction_negative"][1], land_rings=land_rings, land_mask=land, title=f"前 {k90} 個模態重建流場｜負相位案例", field_kind="reconstruction", font=font, display_axis_spec=display_axis_spec))
    qa_axes.append(axes[1, 1])
    axes[1, 2].axis("off")
    axes[1, 2].text(0.02, 0.96, f"{DISPLAY_REGION_TITLES[key]}\n\n原始／重建流場：陸地上無有限流速／無箭頭\n展示遮罩：geometry & static & feature & ~land", va="top", fontsize=CAPTION_FONT_SIZE_PT, color=TEXT_COLOR, fontproperties=font, linespacing=1.5)
    # 所有面板、標題與軸名都建立後才固定 constrained layout；此時重新 draw
    # 並用同一批 exact-land candidate points 取樣，確保 QA 對應的是最後輸出
    # poster 的 canvas，而不是某個中間面板尚未完成時的舊 axes transform。
    fig.canvas.draw()
    final_rgba = np.asarray(fig.canvas.buffer_rgba(), dtype=np.uint8)
    for record, axis in zip(qa_records, qa_axes):
        record.update(
            _sample_land_pixels_on_final_canvas(
                axis,
                final_rgba,
                record.pop("_visible_land_pixel_sample_points"),
                land_rings,
            )
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"region_{key}_coastline_mask_grid_representative.png"
    fig.savefig(output_path, facecolor="white")
    plt.close(fig)
    raw_records = [item for item in qa_records if item["field_kind"] == "raw"]
    reconstruction_records = [item for item in qa_records if item["field_kind"] == "reconstruction"]
    # 將 raw 與 reconstruction 分開彙總，讓 manifest／獨立檢查能直接核對兩個面板
    # 均沒有 exact-land finite render 或箭頭；總數仍保留以相容既有摘要欄位。
    raw_land_finite_count = int(sum(item["land_finite_count"] for item in raw_records))
    reconstruction_land_finite_count = int(sum(item["land_finite_count"] for item in reconstruction_records))
    raw_land_arrow_count = int(sum(item["land_arrow_count"] for item in raw_records))
    reconstruction_land_arrow_count = int(sum(item["land_arrow_count"] for item in reconstruction_records))
    return {
        "region_key": key,
        "svd_dir": str(svd_dir),
        "output": str(output_path),
        "coastline": coastline_summary,
        "grid_shape_lat_lon": [int(lat.size), int(lon.size)],
        "raw_grid_bbox": [float(lon[0]), float(lon[-1]), float(lat[0]), float(lat[-1])],
        "display_extent_source": display_axis_spec["display_extent_source"],
        "display_extent": [float(value) for value in display_axis_spec["display_extent"]],
        "display_x_major_values": [float(value) for value in display_axis_spec["x_major_values"]],
        "display_y_major_values": [float(value) for value in display_axis_spec["y_major_values"]],
        "k90": k90,
        "cumulative_explained_variance_first4_percent": float(cumulative[3] * 100.0),
        "plot_mask_cell_count": int(np.count_nonzero(plot_mask)),
        "display_mask_cell_count": int(np.count_nonzero(display_mask)),
        "land_mask_cell_count": int(np.count_nonzero(land)),
        "frames": frame_meta,
        "geographic_qa": {
            "land_finite_render_count": int(sum(item["land_finite_count"] for item in qa_records)),
            "land_arrow_count": int(sum(item["land_arrow_count"] for item in qa_records)),
            "raw_land_finite_render_count": raw_land_finite_count,
            "reconstruction_land_finite_render_count": reconstruction_land_finite_count,
            "raw_land_arrow_count": raw_land_arrow_count,
            "reconstruction_land_arrow_count": reconstruction_land_arrow_count,
            "analysis_geometry_outside_marked_as_land_count": land_outside_geometry_count,
            "analysis_geometry_outside_not_marked_as_land": land_outside_geometry_count == 0,
            "raw_land_finite_render_zero": raw_land_finite_count == 0,
            "reconstruction_land_finite_render_zero": reconstruction_land_finite_count == 0,
            "raw_land_arrow_zero": raw_land_arrow_count == 0,
            "reconstruction_land_arrow_zero": reconstruction_land_arrow_count == 0,
            "render_field_finite_count_on_raster_land_before_vector_occlusion": int(
                sum(item["render_field_finite_count_on_raster_land_before_vector_occlusion"] for item in qa_records)
            ),
            "render_field_arrow_count_on_raster_land_before_vector_occlusion": int(
                sum(item["render_field_arrow_count_on_raster_land_before_vector_occlusion"] for item in qa_records)
            ),
            "raw_visible_land_pixel_mismatch_count": int(
                sum(item["visible_land_pixel_mismatch_count"] for item in raw_records)
            ),
            "reconstruction_visible_land_pixel_mismatch_count": int(
                sum(item["visible_land_pixel_mismatch_count"] for item in reconstruction_records)
            ),
            "raw_visible_land_pixel_sample_count": int(
                sum(item["visible_land_pixel_sample_count"] for item in raw_records)
            ),
            "reconstruction_visible_land_pixel_sample_count": int(
                sum(item["visible_land_pixel_sample_count"] for item in reconstruction_records)
            ),
            "visible_land_pixel_mismatch_examples": [
                example
                for item in qa_records
                for example in item["visible_land_pixel_mismatch_examples"]
            ][:24],
            "visible_land_pixel_occlusion_passed": all(
                item["visible_land_pixel_fill_passed"]
                and item["vector_overlay_zorder_passed"]
                and item["vector_overlay_edge_removed"]
                and item["vector_overlay_antialiased"]
                for item in qa_records
            ),
            "all_land_finite_render_zero": all(item["land_finite_count"] == 0 for item in qa_records),
            "all_land_arrow_zero": all(item["land_arrow_count"] == 0 for item in qa_records),
        },
    }


def main() -> None:
    """執行所選區域 QA 疊圖並寫出摘要 JSON。"""

    parser = argparse.ArgumentParser(description="Create exact coastline + grid + representative-frame QA overlays.")
    parser.add_argument("--svd-base", type=Path, required=True)
    parser.add_argument("--svd-suffix", default="_coastline_corrected_v2")
    parser.add_argument("--surface-cache-base", type=Path, required=True)
    parser.add_argument("--full-product-dir", type=Path, required=True)
    parser.add_argument("--coastline-geojson", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--regions", default="A,B,C,D")
    parser.add_argument("--font-path", type=Path, default=None)
    args = parser.parse_args()
    from visualize_ocm_svd_modal_context import find_cjk_font

    font = find_cjk_font(args.font_path)
    selected = [item.strip().upper() for item in args.regions.split(",") if item.strip()]
    unknown = [item for item in selected if item not in REGIONS]
    if unknown:
        raise ValueError(f"未知區域：{unknown}")
    cache_ids = {"A": "northeast_taiwan_common_cache_v3", "B": "hsinchu_cache_v3", "C": "houwan_nmmba_cache_v3", "D": "lienchiang_common_cache_v3"}
    records = []
    for key in selected:
        records.append(
            make_region_overlay(
                key,
                svd_dir=args.svd_base / f"{REGIONS[key]}{args.svd_suffix}",
                cache_root=args.surface_cache_base / cache_ids[key],
                full_product_dir=args.full_product_dir,
                coastline_path=args.coastline_geojson,
                output_dir=args.output_dir,
                suffix=args.svd_suffix,
                font=font,
            )
        )
    summary = {
        "schema_name": "ocm_coastline_svd_qa_overlay_summary",
        "schema_version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "regions": records,
        "all_geographic_qa_passed": all(
            record["geographic_qa"]["all_land_finite_render_zero"]
            and record["geographic_qa"]["all_land_arrow_zero"]
            and record["geographic_qa"]["visible_land_pixel_occlusion_passed"]
            for record in records
        ),
    }
    (args.output_dir / "coastline_svd_qa_overlay_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_dir": str(args.output_dir), "regions": selected, "all_geographic_qa_passed": summary["all_geographic_qa_passed"]}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
