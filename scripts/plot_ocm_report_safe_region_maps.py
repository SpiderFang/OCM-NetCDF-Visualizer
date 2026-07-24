"""產生報告用的 OCM 乾淨區域圖，並消除陸地上可見箭頭。

此腳本是 `plot_ocm_clean_region_maps.py` 的報告安全版，刻意另建新檔案以保留
既有舊程式與舊成果。輸入仍讀取既有月資料中間檔，不會修改 `mask.npy`、`u.npy`、
`v.npy` 或 `speed.npy`；但在繪圖階段會用指定的高解析岸線 GeoJSON 重新建立一層
「報告視覺遮罩」，用來排除落在陸地 cell 內的箭頭 anchor，並在箭頭上方再覆蓋
向量陸地 polygon。

設計目的：
- 保留舊成果做比對，不覆蓋 `figures/` 或舊版 clean map 腳本。
- 報告圖不能出現南北竿、龜山島、貢寮岬角等陸地上仍可見箭頭的視覺疑慮。
- 繪圖遮罩只改變圖面呈現，不回寫或改變任何月資料中間檔；正式數值分析若要採用
  新岸線，仍應另行以前處理重產新的 `mask.npy`。

限制與假設：
- GeoJSON 必須是 WGS84 lon/lat 的 Polygon 或 MultiPolygon。
- 報告視覺遮罩使用 `preprocess_ocm_month.py` 的 cell-overlap rasterize：格點中心、
  四角或 GeoJSON ring 頂點只要接觸 polygon，即視為該格點 cell 為陸地。
- 箭頭 anchor 被排除後，離岸箭頭仍可能因箭頭長度跨過岸線；因此本腳本會在箭頭
  上方再畫一次向量陸地，遮住伸進陸地的箭頭段。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from plot_ocm_clean_region_maps import (
    FLOW_DOMAIN_BBOXES,
    ZOOM_WINDOWS,
    draw_region_boxes,
    draw_vector_land_overlay,
    frame_stem,
    load_geojson_land_rings,
    slice_axis_to_extent,
    validate_time_index,
)
from preprocess_ocm_month import build_geojson_land_mask
from visualize_ocm_month import (
    MISSING_DATA_COLOR,
    OCEAN_COLOR,
    QUIVER_COLOR,
    apply_ocean_mask,
    choose_quiver_step,
    load_month,
    normalize_ocean_mask,
    resolve_layer_index,
)


REPORT_LAND_EDGE_WIDTH = 0.34
"""報告用向量岸線線寬。

線寬略大於舊乾淨圖預設值，原因是此圖會把陸地最後蓋在箭頭上方；較清楚的岸線
能讓被裁掉的箭頭段看起來是被陸域遮罩遮住，而不是輸出圖破損。
"""


def build_target_points(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """把一維經緯度座標轉成 GeoJSON rasterize 需要的 `(point, 2)` 陣列。

    `preprocess_ocm_month.build_geojson_land_mask()` 需要和前處理階段相同的
    攤平順序：先建立 `(lat, lon)` 規則網格，再以 C-order 攤平成多個
    `[longitude, latitude]` 目標點。這樣回傳的 land mask 才能 reshape 回
    `(lat, lon)` 並與 `u/v/speed/mask` 的空間維度完全對齊。
    """

    lon_grid, lat_grid = np.meshgrid(lon, lat)
    return np.column_stack([lon_grid.ravel(), lat_grid.ravel()])


def compute_report_land_mask(
    land_geojson: Path,
    lon: np.ndarray,
    lat: np.ndarray,
) -> tuple[np.ndarray, dict]:
    """依指定岸線 GeoJSON 建立報告階段的陸地遮罩。

    回傳的 `report_land_mask=True` 代表該規則格點 cell 與陸地 polygon 接觸。此遮罩
    只用於報告圖的視覺排除：箭頭不會畫在這些格點上，海域底色也不會把它們標為
    有效流場。它不會改寫輸入資料夾中的 `mask.npy`，避免混淆舊版成果與新報告圖。
    """

    target_points = build_target_points(lon, lat)
    return build_geojson_land_mask(land_geojson, target_points, (lat.size, lon.size))


def compute_report_quiver_vmax(speed_frame: np.ndarray, effective_ocean_mask: np.ndarray) -> float:
    """計算報告圖共用的箭頭縮放基準。

    `effective_ocean_mask` 已經同時扣除原始 `mask.npy=False` 與報告岸線遮罩，因此
    第 98 百分位不會被陸地或新岸線判定為陸地的格點影響。主圖與各 zoom 圖共用
    這個基準，確保相同流速在不同圖中的箭頭長度仍可比較。
    """

    speed = apply_ocean_mask(speed_frame, effective_ocean_mask)
    finite_speed = speed[np.isfinite(speed)]
    if finite_speed.size == 0:
        raise ValueError("Selected frame has no finite speed values after report land masking.")
    vmax = float(np.nanpercentile(finite_speed, 98))
    return vmax if np.isfinite(vmax) and vmax > 0 else 1.0


def draw_report_safe_current_map(
    data: dict[str, np.ndarray | dict],
    output_path: Path,
    *,
    layer: int,
    time_index: int,
    extent: tuple[float, float, float, float],
    land_rings: list[np.ndarray],
    report_land_mask_all: np.ndarray,
    effective_ocean_mask_all: np.ndarray,
    region_bboxes: tuple,
    target_arrows: int,
    quiver_scale_multiplier: float,
    figsize: tuple[float, float],
    dpi: int,
    vmax: float,
    draw_mask_land: bool,
) -> dict:
    """繪製單張報告安全乾淨圖。

    與舊版 clean map 的差異有兩個：
    1. `valid_vector` 會再扣掉 `report_land_mask_all`，所以南北竿等小島 cell 不會
       成為箭頭 anchor。
    2. 向量陸地只在箭頭後畫一次，遮住離岸箭頭因長度而伸進 polygon 的線段，
       同時避免同一條岸線被前後重複描邊。

    圖面仍維持乾淨圖規則：不加標題、legend、quiverkey、區域名稱或說明文字，
    只保留座標軸刻度與 `Longitude`、`Latitude`。`region_bboxes` 若傳入空 tuple，
    代表這張圖只要完整台灣流場與岸線，不需要四個 flow-domain 視覺框；這只影響
    PNG 上的標示，不改變遮罩、箭頭抽樣、流速縮放或任何輸入 `.npy` 資料。
    """

    lon_all = np.asarray(data["lon"], dtype=np.float64)
    lat_all = np.asarray(data["lat"], dtype=np.float64)
    lon_min, lon_max, lat_min, lat_max = extent
    x_slice = slice_axis_to_extent(lon_all, lon_min, lon_max, "longitude")
    y_slice = slice_axis_to_extent(lat_all, lat_min, lat_max, "latitude")

    lon = lon_all[x_slice]
    lat = lat_all[y_slice]
    report_land_mask = report_land_mask_all[y_slice, x_slice]
    effective_ocean_mask = effective_ocean_mask_all[y_slice, x_slice]
    speed = apply_ocean_mask(
        np.asarray(data["speed"][time_index, layer, y_slice, x_slice], dtype=np.float32),
        effective_ocean_mask,
    )
    u = apply_ocean_mask(
        np.asarray(data["u"][time_index, layer, y_slice, x_slice], dtype=np.float32),
        effective_ocean_mask,
    )
    v = apply_ocean_mask(
        np.asarray(data["v"][time_index, layer, y_slice, x_slice], dtype=np.float32),
        effective_ocean_mask,
    )
    valid_speed = np.isfinite(speed)
    valid_vector = valid_speed & np.isfinite(u) & np.isfinite(v) & ~report_land_mask

    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    if draw_mask_land:
        # 主圖保留 mask/QC 語意，但海域底色使用「原始 mask 扣除報告岸線」後的結果。
        # 這可避免新岸線已判為陸地的格點仍被淡藍色海域底色標成有效流場。
        ax.set_facecolor(MISSING_DATA_COLOR)
        ocean_layer = np.ma.masked_where(~valid_speed, np.ones_like(speed, dtype=np.float32))
        ocean_cmap = matplotlib.colors.ListedColormap([OCEAN_COLOR])
        ax.pcolormesh(lon, lat, ocean_layer, shading="auto", cmap=ocean_cmap, vmin=0, vmax=1, zorder=1)
        # 報告安全版主圖不再畫 `mask.npy` 方格陸地邊界，因為同一位置稍後會用
        # 高解析 GeoJSON 陸地 polygon 覆蓋。若兩者同時畫出邊線，台灣本島與離島
        # 會呈現像雙重描邊的視覺效果，反而降低報告圖的乾淨程度。
        land_visual_mode = "report_mask_and_vector_land_over_quiver"
    else:
        # zoom 圖不畫 1 km mask 方格，讓高解析向量岸線成為唯一可見陸地輪廓。
        # 箭頭有效性仍由 effective_ocean_mask 控制，因此不會在新岸線陸地 cell 上顯示。
        ax.set_facecolor(OCEAN_COLOR)
        land_visual_mode = "report_vector_land_only_over_quiver"

    draw_region_boxes(ax, region_bboxes, fill=True, zorder=5)

    sy, sx = choose_quiver_step(len(lon), len(lat), target_arrows)
    sampled_valid_vector = valid_vector[::sy, ::sx]
    if np.any(sampled_valid_vector):
        sampled_u = np.ma.masked_where(~sampled_valid_vector, u[::sy, ::sx])
        sampled_v = np.ma.masked_where(~sampled_valid_vector, v[::sy, ::sx])
        ax.quiver(
            lon[::sx],
            lat[::sy],
            sampled_u,
            sampled_v,
            color=QUIVER_COLOR,
            scale=max(vmax * quiver_scale_multiplier, 0.1),
            width=0.00155,
            headwidth=2.8,
            headlength=3.5,
            alpha=0.78,
            zorder=6,
        )

    # 這次陸地疊圖是報告用視覺保險：即使海上 anchor 的箭頭線段跨過岸線，
    # 陸地 polygon 仍會蓋住進入陸域的部分。若有 bbox，外框最後畫，避免被陸地遮住；
    # 若 `region_bboxes` 為空，這個呼叫會自然略過，輸出即為無四框的完整主圖。
    draw_vector_land_overlay(ax, land_rings, extent, linewidth=REPORT_LAND_EDGE_WIDTH, zorder=7)
    draw_region_boxes(ax, region_bboxes, fill=False, zorder=8)
    ax.set_xlim(lon_min, lon_max)
    ax.set_ylim(lat_min, lat_max)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal", adjustable="box")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(pad=0.45)
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.04)
    plt.close(fig)
    return {
        "path": str(output_path),
        "extent_lonlat": [float(lon_min), float(lon_max), float(lat_min), float(lat_max)],
        "target_arrows": int(target_arrows),
        "quiver_step_yx": [int(sy), int(sx)],
        "valid_vector_cells_after_report_mask": int(np.count_nonzero(valid_vector)),
        "report_land_cells_in_view": int(np.count_nonzero(report_land_mask)),
        "total_cells_in_view": int(valid_vector.size),
        "land_visual_mode": land_visual_mode,
    }


def make_report_safe_region_maps(args: argparse.Namespace) -> list[Path]:
    """依 CLI 參數產生報告安全版主圖、zoom 圖與 metadata。

    metadata 會記錄原始資料夾、報告岸線 GeoJSON、報告遮罩命中格點數與每張圖的
    有效箭頭數。這些資訊讓報告圖可追溯：讀者若問「為什麼沒有陸地箭頭」，
    可以說明圖面同時使用既有 `mask.npy` 與指定岸線 GeoJSON 的 cell-overlap 遮罩。
    """

    data = load_month(args.input_dir)
    output_dir = args.output_dir or (args.input_dir / "figures_report_safe_exact_coastline")
    lon = np.asarray(data["lon"], dtype=np.float64)
    lat = np.asarray(data["lat"], dtype=np.float64)
    speed = data["speed"]
    layer_count = speed.shape[1]
    layer = resolve_layer_index(args.layer_index, layer_count)
    selected_time_index = validate_time_index(args.time_index, speed.shape[0])
    original_ocean_mask = normalize_ocean_mask(data["mask"], (lat.size, lon.size))
    report_land_mask, report_land_summary = compute_report_land_mask(args.land_geojson, lon, lat)
    effective_ocean_mask = original_ocean_mask & ~report_land_mask
    vmax = compute_report_quiver_vmax(
        np.asarray(speed[selected_time_index, layer], dtype=np.float32),
        effective_ocean_mask,
    )

    land_rings = load_geojson_land_rings(args.land_geojson)
    stem = frame_stem(layer, layer_count, selected_time_index)
    full_extent = (float(np.nanmin(lon)), float(np.nanmax(lon)), float(np.nanmin(lat)), float(np.nanmax(lat)))

    output_paths: list[Path] = []
    # `--hide-main-region-boxes` 用於產生和原本四框主圖相同範圍、同一層、同一時間的
    # 無框版本。檔名改用 `no_region_bbox`，避免覆蓋需要保留區域框的報告版本。
    main_region_bboxes = () if args.hide_main_region_boxes else FLOW_DOMAIN_BBOXES
    main_suffix = "no_region_bbox_report_safe" if args.hide_main_region_boxes else "four_region_equal_bbox_report_safe"
    main_path = output_dir / f"{stem}_{main_suffix}.png"
    main_metadata = draw_report_safe_current_map(
        data,
        main_path,
        layer=layer,
        time_index=selected_time_index,
        extent=full_extent,
        land_rings=land_rings,
        report_land_mask_all=report_land_mask,
        effective_ocean_mask_all=effective_ocean_mask,
        region_bboxes=main_region_bboxes,
        target_arrows=args.full_target_arrows,
        quiver_scale_multiplier=args.quiver_scale_multiplier,
        figsize=(8.5, 11.0),
        dpi=args.dpi,
        vmax=vmax,
        draw_mask_land=True,
    )
    output_paths.append(main_path)

    zoom_metadata: list[dict] = []
    for zoom in ZOOM_WINDOWS:
        zoom_path = output_dir / f"{stem}_{zoom.filename_suffix.replace('_clean', '_report_safe')}.png"
        metadata = draw_report_safe_current_map(
            data,
            zoom_path,
            layer=layer,
            time_index=selected_time_index,
            extent=zoom.extent_lonlat,
            land_rings=land_rings,
            report_land_mask_all=report_land_mask,
            effective_ocean_mask_all=effective_ocean_mask,
            region_bboxes=(),
            target_arrows=args.zoom_target_arrows,
            quiver_scale_multiplier=args.quiver_scale_multiplier,
            figsize=(6.2, 5.6),
            dpi=args.dpi,
            vmax=vmax,
            draw_mask_land=False,
        )
        metadata.update({"id": zoom.id, "name": zoom.name})
        zoom_metadata.append(metadata)
        output_paths.append(zoom_path)

    metadata_path = output_dir / f"{stem}_{main_suffix}.json"
    metadata = {
        "source_dir": str(args.input_dir),
        "land_geojson": str(args.land_geojson),
        "time": str(data["time_iso"][selected_time_index]),
        "time_index": int(selected_time_index),
        "layer_index": int(layer),
        "domain": data.get("summary", {}).get("domain", {}),
        "text_policy": "PNG only draws coordinate tick labels plus Longitude and Latitude.",
        "report_land_mask_policy": {
            "source": report_land_summary,
            "original_ocean_grid_cell_count": int(np.count_nonzero(original_ocean_mask)),
            "effective_ocean_grid_cell_count": int(np.count_nonzero(effective_ocean_mask)),
            "additional_cells_removed_for_report": int(np.count_nonzero(original_ocean_mask & report_land_mask)),
            "semantics": (
                "Existing mask.npy is kept unchanged; report maps additionally remove quiver anchors "
                "touching the selected coastline GeoJSON and redraw vector land above quiver arrows."
            ),
        },
        "quiver_policy": {
            "vmax_98pct_m_per_s_after_report_mask": float(vmax),
            "scale_multiplier": float(args.quiver_scale_multiplier),
            "note": "Higher scale multiplier means shorter arrows. No quiver scale key is drawn.",
        },
        "flow_domain_bboxes": [asdict(region) for region in FLOW_DOMAIN_BBOXES],
        "main_region_boxes_drawn": not args.hide_main_region_boxes,
        "main_figure": main_metadata,
        "zoom_figures": zoom_metadata,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_paths.append(metadata_path)
    return output_paths


def parse_args() -> argparse.Namespace:
    """解析報告安全版乾淨區域圖 CLI 參數。

    預設輸出到 `<input-dir>/figures_report_safe_exact_coastline`，避免覆蓋舊版
    `figures/` 與 `figures_taiwan_exact_coastline/`。若需要比較不同岸線來源，
    可改 `--output-dir` 另存成新的資料夾。
    """

    parser = argparse.ArgumentParser(description="Create report-safe OCM clean maps with coastline-masked quiver arrows.")
    parser.add_argument("--input-dir", required=True, type=Path, help="Directory produced by preprocess_ocm_month.py.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for PNG/JSON outputs. Defaults to <input-dir>/figures_report_safe_exact_coastline.",
    )
    parser.add_argument("--layer-index", type=int, default=-1, help="Layer index to plot; -1 usually means surface.")
    parser.add_argument("--time-index", type=int, default=0, help="Time index to plot; default is first frame.")
    parser.add_argument(
        "--land-geojson",
        type=Path,
        default=Path("data/geojson/taiwan_exact_coastline.geojson"),
        help="WGS84 Polygon/MultiPolygon GeoJSON used for report land masking and visual overlay.",
    )
    parser.add_argument(
        "--full-target-arrows",
        type=int,
        default=850,
        help="Approximate arrow count for the full-domain main map.",
    )
    parser.add_argument(
        "--zoom-target-arrows",
        type=int,
        default=300,
        help="Approximate arrow count for each independent zoom map.",
    )
    parser.add_argument(
        "--quiver-scale-multiplier",
        type=float,
        default=20.0,
        help="Multiplier applied to vmax for Matplotlib quiver scale; larger means shorter arrows.",
    )
    parser.add_argument(
        "--hide-main-region-boxes",
        action="store_true",
        help=(
            "Do not draw the four flow-domain rectangles on the full-domain main map. "
            "This keeps the same data extent and coastline masking but writes a no_region_bbox output."
        ),
    )
    parser.add_argument("--dpi", type=int, default=180, help="Output PNG resolution.")
    return parser.parse_args()


def main() -> None:
    """程式入口：產生報告安全版乾淨主圖與三張放大圖。"""

    paths = make_report_safe_region_maps(parse_args())
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
