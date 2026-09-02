#!/usr/bin/env python3
"""渲染四海域三日「實際每小時觀測」原始表層流場成果。

本模組與既有 6 小時 raw-only renderer 分開，專門處理使用者指定的嚴格資料語意：

* 原始來源是 `/CWA-OCM/YYYY/YYYYMMDD_schout.nc` 的 hourly `hvel`，固定讀取
  SCHISM 表層 `layer 047`；每個動畫影格都對應一個實際 NetCDF `time` index。
* 72 個連續小時觀測涵蓋三個原始日檔，影片以 2 fps 播放，精確為 36 秒。
  renderer 不做時間內插、淡化、三點平滑或重複產生中間流場，避免把非觀測值
  誤稱為原始觀測。
* 3 日起始時窗由 `select_ocm_raw_hourly_window.py` 先以既有 6 小時產品的原始
  表層流場變化選取；真正畫圖時只讀取該選窗的 hourly NetCDF 前處理結果。
* 四區共用固定色階、刻度與 quiver scale，並使用已核對的 exact coastline vector
  overlay。色階上限與刻度間距可由命令列明確指定：小範圍色階適合與靜態圖對照，
  較高上限則保留強流區內部流場細節。畫面上方只在中央更新真實 UTC 日期時間；
  海域／原始流場標籤留給簡報端後製，不加入 SVD、PC、模態或其他分析文字。

`--preview-only` 只輸出四區 2×2 靜態審核圖與 C 區中間時刻放大圖，不建立 MP4。
待版面確認後，以同一支 renderer 加上 `--render-video` 才會產生 A–D 四支動畫。
所有中間檔與成果都應放在新的版本化目錄，避免覆寫既有 v1/v2/v3/v4 產品。

本模組另支援 `--reference-cache-base`：此模式採用外觀正常的 full 2024–2025
版本所使用之正式 flow-cache 1 km lon/lat/mask 網格，將 hourly 原始 u/v 以有限
角點權重重新正規化的雙線性方式做空間重網格。這不是時間內插；每一支 2 fps 影片仍只有 72 個
實際 hourly 時間位置。參考 cache 的速度陣列不會被讀入或混用，僅提供顯示網格與
模型有效域語意；此差異會完整寫入 manifest，避免把空間重網格誤讀成新增觀測。
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import numpy as np

try:
    import imageio.v2 as imageio
except ImportError:  # pragma: no cover - SERVER 舊版 imageio 的相容分支
    import imageio  # type: ignore[no-redef]

from coastline_utils import build_coastline_land_mask, load_outer_rings
from render_ocm_raw_surface_only import (
    RAW_AXES_RECT,
    RAW_ARROW_KEY_ZORDER,
    RAW_COLORBAR_RECT,
    RAW_HEIGHT,
    RAW_DYNAMIC_TITLE_FONT_SIZE_PT,
    RAW_DYNAMIC_TITLE_Y_FRACTION,
    RAW_TITLE_Y_FRACTION,
    RAW_WIDTH,
    RAW_VECTOR_LAND_TOP_ZORDER,
    _create_raw_scene,
    _ffprobe,
    _raw_ticks,
    _update_raw_scene,
)
from visualize_ocm_svd_modal_context import (
    DISPLAY_AXIS_SPECS,
    _format_time_utc,
    build_region_specs,
    find_cjk_font,
    sha256_file,
)


SCRIPT_VERSION = "1.2.3"
"""本次實際 hourly raw-only renderer 的 manifest schema 版本。

1.2.3 修正 SERVER Matplotlib 3.5 的 QuiverKey 圖層相容問題：比例尺容器會在首次
draw 後明確置於 exact coastline 向量陸地之上，避免 B 區右下海岸／陸地覆蓋白色
比例尺。此為純展示圖層修正，U=1.0、共用 quiver scale、原始 u/v 與時間軸不變。
1.2.2 新增 ``--speed-vmax-mps`` 與 ``--speed-tick-step-mps``；使用者可維持四區
共同線性色階、但依實際強流比例放寬上限，避免大面積飽和為同一個黃色而失去
流場細節。色階調整只改 pcolormesh 的 Normalize 與 colorbar，不改原始 u/v、
時間軸、箭頭或任何 SVD 結果。
1.2.1 修正 formal reference grid 近岸 conservative land cell 與高解析 vector
岸線之間的白色縫帶：僅在 render mesh 使用同影格最近非陸地有限速度作為接縫填色，
不改動 raw payload、時間軸或箭頭資料；exact vector polygon 仍是唯一可見陸地。
1.2.0 新增與正常參考版一致的 formal flow-cache 顯示網格模式，明確區分時間軸
仍為實際 hourly 觀測與僅限展示的空間雙線性重網格。1.1.1 將 exact coastline
vector land underlay/top overlay 的繪圖政策寫入 manifest；它只修補透明 raster
與向量岸線之間可能露出的 figure 白底，不改變 hourly 原始 u/v、時間軸、模型
遮罩或任何科學資料。
"""

HOURLY_FRAME_COUNT = 72
"""三日資料的實際 hourly frame count；不含片頭／片尾額外影格。"""

HOURLY_INTERVAL_HOURS = 1
"""輸出影格的真實時間間隔；每一格對應一個 NetCDF hourly time index。"""

SOURCE_LAYER_INDEX = 47
"""固定使用 OCM/SCHISM 的表層模型層 047。"""

DEFAULT_FPS = 2
"""72 個實際小時影格以 2 fps 播放為 36 秒；不以內插增加影格數。"""

DEFAULT_WIDTH = RAW_WIDTH
"""沿用目前四區 2×2 簡報版面的單區寬度。"""

DEFAULT_HEIGHT = RAW_HEIGHT
"""沿用目前四區 2×2 簡報版面的緊湊高度。"""

DEFAULT_DPI = 150
"""Matplotlib 內部 raster DPI；輸出畫布仍由 width/height 決定。"""

DEFAULT_TARGET_ARROWS = 420
"""每區約 420 支箭頭；只控制視覺抽樣，不降低原始格點解析度。"""

DEFAULT_QUIVER_SCALE_MULTIPLIER = 28.0
"""四區共用的箭頭 scale 倍率；不依各區流速分布重新估算。"""

FIXED_SPEED_VMAX_MPS = 0.8
"""既有簡報靜態圖相容的預設色階上限；正式 render 可由 CLI 明確覆寫。"""

FIXED_SPEED_TICK_STEP_MPS = 0.2
"""既有簡報靜態圖相容的預設色階刻度間距；正式 render 可由 CLI 明確覆寫。"""

COASTLINE_SHA256_EXPECTED = "9e2e0ac9bc527aca87d89332cd428fdcb776eefbf94a85dd70f887f729b95fdd"
"""使用者指定 exact coastline GeoJSON 的 SHA-256，防止錯用低解析度岸線。"""


@dataclass
class HourlyPayload:
    """單一實際 hourly 觀測影格的繪圖資料。

    `raw_u/raw_v/raw_speed` 都是 `[lat, lon]` 的規則 1 km 格點、單位 m/s；
    `record.time_ns` 直接來自前處理所保存的原始日檔日期與 hourly index，並非
    renderer 內插產生。重建欄位不存在，這個類別刻意只保存 raw surface 物理量。
    """

    record: Any
    raw_u: np.ndarray
    raw_v: np.ndarray
    raw_speed: np.ndarray
    display_time_ns: int


@dataclass
class HourlyDataset:
    """一個海域的 hourly raw-only 格點、遮罩、岸線與 72 個觀測 payload。"""

    spec: Any
    lon: np.ndarray
    lat: np.ndarray
    display_axis_spec: dict[str, Any]
    static_ocean_mask: np.ndarray
    analysis_geometry_mask: np.ndarray
    velocity_feature_mask_surface: np.ndarray
    coastline_land_mask: np.ndarray
    land_rings: list[np.ndarray]
    coastline_summary: dict[str, Any]
    plot_mask: np.ndarray
    render_mask: np.ndarray
    payloads: list[HourlyPayload]
    source_metadata: dict[str, Any]
    source_product_dir: Path
    grid_source: str = "hourly_product_crop"
    spatial_interpolation_method: str = "none"
    reference_grid_dir: str | None = None
    speed_scale_vmax: float = FIXED_SPEED_VMAX_MPS
    speed_tick_step_mps: float = FIXED_SPEED_TICK_STEP_MPS
    quiver_reference_mps: float = 1.0


def _read_json(path: Path) -> dict[str, Any]:
    """讀取 UTF-8 JSON，並確認根節點為 object。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根節點必須是 object：{path}")
    return value


def _parse_time_ns(value: Any) -> int:
    """將 hourly 前處理的 ISO UTC 字串轉為 epoch ns，不做時區修正。"""

    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return int(np.datetime64(text, "ns").astype("int64"))


def _format_time_for_filename(time_ns: int) -> str:
    """將 UTC epoch ns 轉為不含空白的檔名時間。"""

    value = datetime.fromtimestamp(int(time_ns) / 1_000_000_000.0, tz=timezone.utc)
    return value.strftime("%Y%m%dT%H%MZ")


def _validate_selection(selection: dict[str, Any]) -> tuple[int, list[str]]:
    """驗證選窗 JSON 的 72 小時與三個原始日檔契約。"""

    window = selection.get("target_animation_window", {})
    if window.get("frame_count") != HOURLY_FRAME_COUNT:
        raise ValueError(f"選窗不是 {HOURLY_FRAME_COUNT} 個 hourly frames：{window}")
    if window.get("display_interval_hours") != HOURLY_INTERVAL_HOURS:
        raise ValueError(f"選窗不是每小時一幀：{window}")
    candidate = selection.get("selected_candidate", {})
    daily_files = [str(value) for value in candidate.get("daily_files", [])]
    if len(daily_files) != 3 or not all(Path(value).is_file() for value in daily_files):
        raise FileNotFoundError(f"選窗對應的三個原始日檔不完整：{daily_files}")
    expected_times = [int(value) for value in window.get("expected_time_ns", [])]
    if len(expected_times) != HOURLY_FRAME_COUNT or any(
        right - left != 3_600_000_000_000 for left, right in zip(expected_times[:-1], expected_times[1:])
    ):
        raise ValueError("選窗 expected_time_ns 不是連續每小時時間軸")
    return expected_times[0], daily_files


def _load_hourly_product(product_dir: Path, selection: dict[str, Any]) -> dict[str, Any]:
    """讀取並嚴格驗證三日 hourly 前處理產品。

    此函式只接受 `time_step_hours=1`、72 個 source-observed frame 且沒有 imputed
    frame 的產品。`u_surface/v_surface` 以 memory-map 開啟，後續只在各區 crop 時
    materialize；這可保留原始資料的逐格 NaN 與遮罩語意，同時避免不必要的記憶體峰值。
    """

    metadata = _read_json(product_dir / "metadata.json")
    time_axis = metadata.get("time_axis", {})
    if time_axis.get("time_step_hours") != HOURLY_INTERVAL_HOURS:
        raise ValueError(f"hourly 產品 time_step_hours 不符：{time_axis}")
    time_values = np.asarray([_parse_time_ns(value) for value in np.load(product_dir / "time_iso.npy", allow_pickle=False)], dtype=np.int64)
    source_valid = np.load(product_dir / "source_valid.npy", allow_pickle=False).astype(bool)
    imputed = np.load(product_dir / "imputed.npy", allow_pickle=False).astype(bool)
    time_status = np.asarray(np.load(product_dir / "time_status.npy", allow_pickle=False)).astype(str)
    if time_values.size != HOURLY_FRAME_COUNT:
        raise ValueError(f"hourly 產品不是 72 frames：{time_values.size}")
    if not np.all(source_valid) or np.any(imputed) or np.any(time_status != "source_observed"):
        raise ValueError(
            "hourly 產品含 invalid/imputed frame，不能作為『每幀實際觀測』成果："
            f"source_valid={np.count_nonzero(source_valid)}/{source_valid.size}, "
            f"imputed={np.count_nonzero(imputed)}, status={sorted(set(time_status.tolist()))}"
        )
    if not np.all(np.diff(time_values) == 3_600_000_000_000):
        raise ValueError("hourly 產品時間軸不是連續 1 小時")
    expected_start_ns, _ = _validate_selection(selection)
    expected_times = np.asarray(
        [expected_start_ns + index * 3_600_000_000_000 for index in range(HOURLY_FRAME_COUNT)],
        dtype=np.int64,
    )
    if not np.array_equal(time_values, expected_times):
        raise ValueError(
            "hourly 前處理時間軸與選窗不一致："
            f"product={_format_time_utc(int(time_values[0]))}..{_format_time_utc(int(time_values[-1]))}, "
            f"selection={_format_time_utc(int(expected_times[0]))}..{_format_time_utc(int(expected_times[-1]))}"
        )
    lon = np.load(product_dir / "lon.npy", allow_pickle=False).astype(np.float64)
    lat = np.load(product_dir / "lat.npy", allow_pickle=False).astype(np.float64)
    static_mask = np.load(product_dir / "mask.npy", allow_pickle=False).astype(bool)
    u_source = np.load(product_dir / "u_surface.npy", mmap_mode="r")
    v_source = np.load(product_dir / "v_surface.npy", mmap_mode="r")
    expected_shape = (HOURLY_FRAME_COUNT, lat.size, lon.size)
    if tuple(u_source.shape) != expected_shape or tuple(v_source.shape) != expected_shape:
        raise ValueError(f"hourly u/v shape 不符：u={u_source.shape}, v={v_source.shape}, expected={expected_shape}")
    if static_mask.shape != (lat.size, lon.size):
        raise ValueError(f"hourly mask shape 不符：{static_mask.shape} != {(lat.size, lon.size)}")
    return {
        "metadata": metadata,
        "time_ns": time_values,
        "lon": lon,
        "lat": lat,
        "static_mask": static_mask,
        "u_source": u_source,
        "v_source": v_source,
        "selection": selection,
    }


def _crop_grid(lon: np.ndarray, lat: np.ndarray, extent: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    """取得覆蓋 display extent 的格點索引，保留一格邊界供 pcolormesh 顯示。"""

    lon_step = float(np.median(np.diff(lon)))
    lat_step = float(np.median(np.diff(lat)))
    lon_indices = np.flatnonzero((lon >= float(extent[0]) - lon_step) & (lon <= float(extent[1]) + lon_step))
    lat_indices = np.flatnonzero((lat >= float(extent[2]) - lat_step) & (lat <= float(extent[3]) + lat_step))
    if lon_indices.size < 2 or lat_indices.size < 2:
        raise ValueError(f"display extent 沒有足夠 hourly 格點：extent={extent}")
    return lon_indices, lat_indices


def _bilinear_regrid_frame(
    field: np.ndarray,
    source_lon: np.ndarray,
    source_lat: np.ndarray,
    target_lon: np.ndarray,
    target_lat: np.ndarray,
) -> np.ndarray:
    """將一個 hourly 規則格網場雙線性重網格至參考 1 km 格網。

    ``field`` 是單一實際 hourly 觀測影格，維度為 ``[source_lat, source_lon]``；
    ``target_lon/target_lat`` 則是既有正式 SVD／surface cache 的規則格網中心。
    這裡只在空間方向做內插，時間索引仍由呼叫端逐格保留，因此不會把每小時
    觀測誤寫成時間內插影格。一般情況使用四個支撐角點的雙線性權重；若岸線附近
    的原始規則 hourly grid 有部分 NaN 角點，則只以仍有限的角點重新正規化權重，
    避免把「已在參考 flow-cache 有效域內」的近岸格點畫成一圈白色缺值。四角點
    全部無效時才保留 NaN，因此模型域外仍不會被任意補成流速。

    參考版 full 2024--2025 動畫使用的是正式 flow cache 的 102×151（C 為
    102×152）網格，而三日 hourly 產品的 full-grid crop 具有不同的原點與格點數；
    本函式讓 hourly 原始場在不改變時間語意的前提下，採用相同的顯示網格，降低
    pcolormesh cell edge 與 exact coastline vector polygon 之間的像素接縫差異。
    """

    source = np.asarray(field, dtype=np.float32)
    source_lon = np.asarray(source_lon, dtype=np.float64)
    source_lat = np.asarray(source_lat, dtype=np.float64)
    target_lon = np.asarray(target_lon, dtype=np.float64)
    target_lat = np.asarray(target_lat, dtype=np.float64)
    if source.ndim != 2 or source.shape != (source_lat.size, source_lon.size):
        raise ValueError(
            "雙線性重網格輸入維度不符："
            f"field={source.shape}, expected={(source_lat.size, source_lon.size)}"
        )
    if source_lon.size < 2 or source_lat.size < 2:
        raise ValueError("雙線性重網格至少需要兩個經度與兩個緯度支撐點")
    if not np.all(np.diff(source_lon) > 0.0) or not np.all(np.diff(source_lat) > 0.0):
        raise ValueError("雙線性重網格只接受遞增的規則經緯度軸")

    target_lon_grid, target_lat_grid = np.meshgrid(target_lon, target_lat)
    right_x = np.searchsorted(source_lon, target_lon_grid, side="left")
    right_y = np.searchsorted(source_lat, target_lat_grid, side="left")
    inside = (
        (target_lon_grid >= source_lon[0])
        & (target_lon_grid <= source_lon[-1])
        & (target_lat_grid >= source_lat[0])
        & (target_lat_grid <= source_lat[-1])
    )
    right_x = np.clip(right_x, 1, source_lon.size - 1)
    right_y = np.clip(right_y, 1, source_lat.size - 1)
    left_x = right_x - 1
    left_y = right_y - 1
    x0 = source_lon[left_x]
    x1 = source_lon[right_x]
    y0 = source_lat[left_y]
    y1 = source_lat[right_y]
    with np.errstate(divide="ignore", invalid="ignore"):
        alpha_x = (target_lon_grid - x0) / (x1 - x0)
        alpha_y = (target_lat_grid - y0) / (y1 - y0)

    lower_left = source[left_y, left_x]
    lower_right = source[left_y, right_x]
    upper_left = source[right_y, left_x]
    upper_right = source[right_y, right_x]
    corner_values = np.stack((lower_left, lower_right, upper_left, upper_right), axis=0)
    corner_weights = np.stack(
        (
            (1.0 - alpha_y) * (1.0 - alpha_x),
            (1.0 - alpha_y) * alpha_x,
            alpha_y * (1.0 - alpha_x),
            alpha_y * alpha_x,
        ),
        axis=0,
    )
    finite_corners = np.isfinite(corner_values)
    finite_weights = np.where(finite_corners, corner_weights, 0.0)
    weight_sum = np.sum(finite_weights, axis=0)
    weighted_sum = np.sum(np.where(finite_corners, corner_values * corner_weights, 0.0), axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        result = (weighted_sum / weight_sum).astype(np.float32, copy=False)
    result[~(inside & (weight_sum > 0.0))] = np.nan
    return result


def _build_region_dataset(
    spec: Any,
    product: dict[str, Any],
    coastline_geojson: Path,
    reference_cache_base: Path | None = None,
) -> HourlyDataset:
    """從 hourly full grid 建立單區 raw payload 與向量岸線。

    預設模式直接從 hourly full grid 擷取對應簡報 display extent；若指定
    ``reference_cache_base``，則改用既有正式 surface cache 的 lon/lat/mask 作為
    顯示網格，再將每一個 hourly ``u/v`` 影格以有限四角點雙線性空間內插到該網格。
    後者是為了和已確認外觀正常的 full 2024--2025 renderer 使用同一套 1 km
    reference grid；它不讀取 cache 中的速度值，也不改寫 SVD、時間軸或 hourly 原始
    產品。真實可見岸線仍由高解析度 GeoJSON vector polygon 疊加，保守 exact-land
    raster mask 只保存供 manifest/audit。
    """

    axis_spec = dict(DISPLAY_AXIS_SPECS[spec.key])
    extent = axis_spec["display_extent"]
    global_lon = product["lon"]
    global_lat = product["lat"]
    reference_grid_dir: Path | None = None
    if reference_cache_base is not None:
        # 這些 lon/lat/mask 只定義畫面使用的正式 1 km 網格與模型可用域；不取代
        # hourly source u/v，也不把 reference cache 的 6 小時速度值混進本版本。
        reference_grid_dir = reference_cache_base / spec.flow_domain_id / "grid"
        required_grid_files = ("lon.npy", "lat.npy", "mask_static.npy", "metadata.json")
        missing = [name for name in required_grid_files if not (reference_grid_dir / name).is_file()]
        if missing:
            raise FileNotFoundError(f"{spec.key} reference grid 缺少檔案：{missing}")
        lon = np.load(reference_grid_dir / "lon.npy", allow_pickle=False).astype(np.float64)
        lat = np.load(reference_grid_dir / "lat.npy", allow_pickle=False).astype(np.float64)
        static_mask = np.load(reference_grid_dir / "mask_static.npy", allow_pickle=False).astype(bool)
        if static_mask.shape != (lat.size, lon.size):
            raise ValueError(f"{spec.key} reference static mask shape 不符：{static_mask.shape}")
        if not (
            float(lon[0]) >= float(global_lon[0])
            and float(lon[-1]) <= float(global_lon[-1])
            and float(lat[0]) >= float(global_lat[0])
            and float(lat[-1]) <= float(global_lat[-1])
        ):
            raise ValueError(f"{spec.key} reference grid 超出 hourly full grid 支撐範圍")
        grid_source = "reference_formal_svd_surface_cache_grid"
        spatial_interpolation_method = "bilinear_partial_finite_corner_renormalized_regrid_hourly_product_to_reference_grid"
    else:
        lon_indices, lat_indices = _crop_grid(global_lon, global_lat, extent)
        lon = global_lon[lon_indices]
        lat = global_lat[lat_indices]
        crop_selection = np.ix_(lat_indices, lon_indices)
        static_mask = np.asarray(product["static_mask"][crop_selection], dtype=bool)
        grid_source = "hourly_product_crop"
        spatial_interpolation_method = "none"
    coastline_land_mask, coastline_summary = build_coastline_land_mask(lon, lat, coastline_geojson)
    land_rings = load_outer_rings(coastline_geojson)
    # raw product 的模型 mask 是「模型可提供值」的語意，不是精確海岸線；可見
    # 真實陸地在 renderer 中由 vector polygon 完全覆蓋。將 exact land 不從這裡
    # 整格扣除，避免 conservative cell-overlap rasterization 形成白色階梯邊界。
    render_mask = static_mask.copy()
    payloads: list[HourlyPayload] = []
    for frame_index, time_ns in enumerate(product["time_ns"].tolist()):
        u_global = np.asarray(product["u_source"][frame_index], dtype=np.float32)
        v_global = np.asarray(product["v_source"][frame_index], dtype=np.float32)
        if reference_cache_base is not None:
            u = _bilinear_regrid_frame(u_global, global_lon, global_lat, lon, lat)
            v = _bilinear_regrid_frame(v_global, global_lon, global_lat, lon, lat)
        else:
            u = np.asarray(u_global[crop_selection], dtype=np.float32).copy()
            v = np.asarray(v_global[crop_selection], dtype=np.float32).copy()
        u[~render_mask] = np.nan
        v[~render_mask] = np.nan
        record = SimpleNamespace(time_ns=int(time_ns), source_frame_index=frame_index)
        payloads.append(
            HourlyPayload(
                record=record,
                raw_u=u,
                raw_v=v,
                raw_speed=np.hypot(u, v).astype(np.float32),
                display_time_ns=int(time_ns),
            )
        )
    analysis_mask = np.ones_like(static_mask, dtype=bool)
    dataset = HourlyDataset(
        spec=spec,
        lon=lon,
        lat=lat,
        display_axis_spec={
            **axis_spec,
            "raw_grid_bbox": [float(lon[0]), float(lon[-1]), float(lat[0]), float(lat[-1])],
        },
        static_ocean_mask=static_mask,
        analysis_geometry_mask=analysis_mask,
        velocity_feature_mask_surface=static_mask.copy(),
        coastline_land_mask=coastline_land_mask,
        land_rings=land_rings,
        coastline_summary=coastline_summary,
        plot_mask=static_mask.copy(),
        render_mask=render_mask,
        payloads=payloads,
        source_metadata=product["metadata"],
        source_product_dir=Path(product["selection"]["raw_netcdf_source"]["root"]),
        grid_source=grid_source,
        spatial_interpolation_method=spatial_interpolation_method,
        reference_grid_dir=str(reference_grid_dir) if reference_grid_dir is not None else None,
    )
    return dataset


def _render_frame(dataset: HourlyDataset, payload_index: int, *, width: int, height: int, dpi: int, target_arrows: int, quiver_scale_multiplier: float, font: Any | None) -> np.ndarray:
    """建立單一場景並輸出一張 RGB 影格；用於靜態預覽，確保與影片相同 renderer。"""

    scene = _create_raw_scene(
        dataset,
        width=width,
        height=height,
        dpi=dpi,
        target_arrows=target_arrows,
        quiver_scale_multiplier=quiver_scale_multiplier,
        font=font,
        show_title=True,
        dynamic_time_title=True,
    )
    try:
        return _update_raw_scene(scene, dataset, dataset.payloads[payload_index])
    finally:
        import matplotlib.pyplot as plt

        plt.close(scene.fig)


def _render_region_video(
    dataset: HourlyDataset,
    output_dir: Path,
    *,
    width: int,
    height: int,
    dpi: int,
    fps: int,
    target_arrows: int,
    quiver_scale_multiplier: float,
    font: Any | None,
    overwrite: bool,
) -> dict[str, Any]:
    """以 72 個實際 hourly payload 串流寫出一支 MP4、poster 與 contact sheet。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"region_{dataset.spec.key}_{dataset.spec.short_name}_raw_surface_only_3day_hourly_actual_2fps"
    mp4_path = output_dir / f"{stem}.mp4"
    poster_path = output_dir / f"{stem}_poster.png"
    contact_path = output_dir / f"{stem}_first_middle_last_contact.png"
    targets = (mp4_path, poster_path, contact_path)
    if not overwrite:
        existing = [str(path) for path in targets if path.exists()]
        if existing:
            raise FileExistsError("輸出已存在，請改用 --overwrite：" + ", ".join(existing))
    scene = _create_raw_scene(
        dataset,
        width=width,
        height=height,
        dpi=dpi,
        target_arrows=target_arrows,
        quiver_scale_multiplier=quiver_scale_multiplier,
        font=font,
        show_title=True,
        dynamic_time_title=True,
    )
    # scene 建立時已完成首次 draw、向量陸地與 QuiverKey 的最終圖層設定；先擷取
    # 像素 bbox 與 z-order 稽核，避免 close figure 後無法追溯 B 區比例尺是否真的
    # 高於陸地。這些數值只描述版面，不含任何 raw u/v 的改寫結果。
    render_layout = {
        "arrow_key": dict(scene.arrow_key_layout),
        "colorbar_alignment": dict(scene.colorbar_alignment),
        "axis_tick_layout": dict(scene.axis_tick_layout),
    }
    ffmpeg_executable = shutil.which("ffmpeg")
    if ffmpeg_executable is None:
        try:
            from render_ocm_raw_surface_only import _configure_imageio_ffmpeg

            ffmpeg_executable = _configure_imageio_ffmpeg()
        except Exception as exc:  # pragma: no cover - 僅於極簡環境發生
            raise RuntimeError("找不到可用的 ffmpeg/imageio-ffmpeg") from exc
    writer = imageio.get_writer(
        str(mp4_path),
        mode="I",
        fps=fps,
        codec="libx264",
        quality=10,
        macro_block_size=1,
        ffmpeg_log_level="error",
        output_params=["-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", "-crf", "16", "-preset", "slow"],
    )
    saved_frames: dict[int, np.ndarray] = {}
    try:
        for frame_index, payload in enumerate(dataset.payloads):
            rgb = _update_raw_scene(scene, dataset, payload)
            writer.append_data(rgb)
            if frame_index in (0, HOURLY_FRAME_COUNT // 2, HOURLY_FRAME_COUNT - 1):
                saved_frames[frame_index] = rgb.copy()
            if (frame_index + 1) % 12 == 0 or frame_index + 1 == HOURLY_FRAME_COUNT:
                print(f"{dataset.spec.key} rendered {frame_index + 1}/{HOURLY_FRAME_COUNT} frames", flush=True)
    finally:
        writer.close()
        import matplotlib.pyplot as plt

        plt.close(scene.fig)
    imageio.imwrite(poster_path, saved_frames[0])
    imageio.imwrite(contact_path, np.concatenate([saved_frames[index] for index in (0, HOURLY_FRAME_COUNT // 2, HOURLY_FRAME_COUNT - 1)], axis=1))
    return {
        "render_layout": render_layout,
        "mp4": {
            "path": str(mp4_path.resolve()),
            "filename": mp4_path.name,
            "sha256": sha256_file(mp4_path),
            "frame_count_expected": HOURLY_FRAME_COUNT,
            "fps_requested": fps,
            "duration_expected_seconds": HOURLY_FRAME_COUNT / float(fps),
            "ffprobe": _ffprobe(mp4_path),
        },
        "poster": {"path": str(poster_path.resolve()), "filename": poster_path.name, "sha256": sha256_file(poster_path)},
        "contact_sheet": {"path": str(contact_path.resolve()), "filename": contact_path.name, "sha256": sha256_file(contact_path)},
    }


def _build_manifest(
    selection: dict[str, Any],
    datasets: Sequence[HourlyDataset],
    *,
    output_dir: Path,
    args: argparse.Namespace,
    outputs: dict[str, Any],
    preview_outputs: dict[str, Any],
) -> dict[str, Any]:
    """建立預覽／正式成果共用的資料、時間、畫面與 QA manifest。"""

    coastline = datasets[0].coastline_summary if datasets else {}
    time_window = selection["target_animation_window"]
    region_manifest = []
    for dataset in datasets:
        region_manifest.append(
            {
                "region_key": dataset.spec.key,
                "title": "UTC timestamp only",
                "visible_title_format": "YYYY-MM-DD HH:MM UTC",
                "region_name_visible_in_title": False,
                "raw_surface_label_visible_in_title": False,
                "display_title": dataset.display_axis_spec,
                "grid": {
                    "shape_lat_lon": [int(dataset.lat.size), int(dataset.lon.size)],
                    "bbox_lon_min_lon_max_lat_min_lat_max": [float(dataset.lon[0]), float(dataset.lon[-1]), float(dataset.lat[0]), float(dataset.lat[-1])],
                    "grid_source": dataset.grid_source,
                    "spatial_interpolation_method": dataset.spatial_interpolation_method,
                    "reference_grid_dir": dataset.reference_grid_dir,
                },
                "source": {
                    "product_dir": str(dataset.source_product_dir),
                    "layer_index": SOURCE_LAYER_INDEX,
                    "time_count": len(dataset.payloads),
                    "time_step_hours": HOURLY_INTERVAL_HOURS,
                    "all_frames_actual_source_observations": True,
                    "temporal_interpolation_used": False,
                    "spatial_interpolation_used": dataset.spatial_interpolation_method != "none",
                    "spatial_interpolation_method": dataset.spatial_interpolation_method,
                    "smoothing_used": False,
                },
                "coastline": dataset.coastline_summary,
                "mask": {
                    "model_static_ocean_cell_count": int(np.count_nonzero(dataset.static_ocean_mask)),
                    "exact_land_cell_count_conservative_audit": int(np.count_nonzero(dataset.coastline_land_mask)),
                    "exact_land_fraction_conservative_audit": float(np.mean(dataset.coastline_land_mask)),
                    "render_mask_definition": "hourly source model mask; exact coastline vector polygon overlays visible land",
                    "raster_land_background_visible": False,
                },
                "coastline_rendering": {
                    "visible_land_source": "high-resolution exact coastline GeoJSON vector polygon",
                    "conservative_raster_mask_role": "audit_only; not assigned to visible semantic background",
                    "render_only_coastal_speed_fill": getattr(dataset, "coastline_display_fill_summary", {
                        "enabled": False,
                        "method": "not_recorded",
                    }),
                    "vector_land_underlay": True,
                    "vector_land_top_overlay": True,
                    "dark_outline": False,
                    "antialiased_vector_fill": True,
                    "white_fringe_correction": (
                        "coastal conservative land cells use display-only nearest finite non-land "
                        "speed, then opaque vector land covers the true polygon"
                    ),
                },
                "outputs": outputs.get(dataset.spec.key, {}),
            }
        )
    reference_grid_mode = bool(datasets and datasets[0].grid_source == "reference_formal_svd_surface_cache_grid")
    # 逐區 renderer 在建立 scene 時量測 QuiverKey 容器、箭頭與文字的最終 z-order。
    # 這是針對 SERVER Matplotlib 3.5 的必要畫面 QA：B 區比例尺錨點位於右下陸地，
    # 若容器仍低於向量陸地 z=30，雖然資料與影片編碼都合法，觀眾仍會看不到比例尺。
    arrow_key_layer_values = [
        outputs.get(dataset.spec.key, {})
        .get("render_layout", {})
        .get("arrow_key", {})
        .get("above_vector_land_top_overlay")
        for dataset in datasets
    ]
    arrow_key_above_land_qa = bool(
        not outputs
        or (
            len(arrow_key_layer_values) == len(datasets)
            and all(value is True for value in arrow_key_layer_values)
        )
    )
    manifest = {
        "schema_name": "ocm_raw_surface_actual_hourly_three_day_animation_manifest",
        "schema_version": SCRIPT_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "purpose": "四海域三日實際每小時原始表層流場；供 PI 觀察短期潮汐週期變化",
        "raw_surface_only": True,
        "reconstruction_rendered": False,
        "svd_used_for_rendering": False,
        "spatial_grid_policy": {
            "mode": "reference_formal_svd_surface_cache_grid" if reference_grid_mode else "hourly_product_crop",
            "reference_grid_is_geometry_only": reference_grid_mode,
            "spatial_interpolation_method": (
                datasets[0].spatial_interpolation_method if reference_grid_mode and datasets else "none"
            ),
            "temporal_interpolation_used": False,
            "reason": (
                "採用與外觀正常的 full 2024–2025 參考版相同的 formal SVD/surface-cache 1 km 網格；"
                "hourly 原始 u/v 只在空間方向重網格，時間仍逐小時保留。"
                if reference_grid_mode
                else "沿用 hourly full product 的區域 crop 網格。"
            ),
        },
        "source_semantics": (
            "每個 payload 的時間索引直接對應 hourly 前處理產品中的原始 NetCDF hvel time index；"
            "若使用 reference formal SVD grid，只做空間雙線性重網格，不做時間內插、展示平滑、淡化或重建。"
        ),
        "selection": selection,
        "time_window": {
            "start_utc": time_window["start_utc"],
            "end_utc": time_window["end_utc"],
            "frame_count": HOURLY_FRAME_COUNT,
            "actual_observation_interval_hours": HOURLY_INTERVAL_HOURS,
            "calendar_source_day_count": 3,
            "endpoint_span_hours": HOURLY_FRAME_COUNT - 1,
        },
        "render_policy": {
            "width_px": args.width,
            "height_px": args.height,
            "fps": args.fps,
            "expected_frame_count": HOURLY_FRAME_COUNT,
            "expected_duration_seconds": HOURLY_FRAME_COUNT / float(args.fps),
            "no_audio": True,
            "codec": "libx264",
            "pixel_format": "yuv420p",
            "h264_crf": 16,
            "h264_preset": "slow",
            "dynamic_time_title": True,
            "title_alignment": "center",
            "title_time_format": "YYYY-MM-DD HH:MM UTC",
            "title_content": "UTC timestamp only",
            "title_fontsize_points": RAW_DYNAMIC_TITLE_FONT_SIZE_PT,
            "title_y_fraction": RAW_DYNAMIC_TITLE_Y_FRACTION,
            "region_name_visible_in_title": False,
            "raw_surface_label_visible_in_title": False,
            # 色階必須在四區與所有影格固定，否則動畫會有逐幀色彩跳動、四區 2×2
            # 也無法比較。值由本次命令明確傳入，而不是從任一影格的分布自動推導。
            "fixed_speed_vmin_mps": 0.0,
            "fixed_speed_vmax_mps": float(args.speed_vmax_mps),
            "fixed_speed_ticks_mps": [float(value) for value in _raw_ticks(
                float(args.speed_vmax_mps), float(args.speed_tick_step_mps)
            )],
            "target_arrows": args.target_arrows,
            "quiver_reference_mps": 1.0,
            "quiver_scale_multiplier": args.quiver_scale_multiplier,
            "quiver_scale_shared_across_regions": True,
            "colorbar_label": "流速（公尺／秒）",
            "colorbar_label_rotation_degrees": 90,
            "arrow_label": "1 公尺／秒",
            "arrow_key_outline": False,
            "arrow_key_zorder": RAW_ARROW_KEY_ZORDER,
            "vector_land_top_zorder": RAW_VECTOR_LAND_TOP_ZORDER,
            "arrow_key_above_vector_land_required": True,
            "matplotlib_3_5_quiverkey_layer_fix": True,
        },
        "visible_text": {
            "dynamic_only": "centered UTC timestamp only",
            "sample": "2024-11-01 13:00 UTC",
            "region_and_raw_surface_title_removed_for_ppt_overlay": True,
            "forbidden_analysis_text": ["PC", "K", "K90", "模態", "相位", "SVD 重建"],
        },
        "coastline": coastline,
        "coastline_sha256_expected": COASTLINE_SHA256_EXPECTED,
        "coastline_rendering_policy": {
            "scope": "visualization_only",
            "visible_land_source": "high-resolution exact coastline GeoJSON vector polygon",
            "conservative_raster_mask_role": "scientific/audit mask only; not a visible coastline",
            "render_only_coastal_speed_fill": {
                "enabled": True,
                "method": "nearest_finite_non_land_speed_bfs_for_display_only",
                "purpose": "remove white raster/vector seam in conservative boundary cells",
                "raw_payload_unchanged": True,
                "quiver_not_filled": True,
            },
            "vector_land_underlay": True,
            "vector_land_top_overlay": True,
            "white_fringe_corrected": True,
        },
        "preview_outputs": preview_outputs,
        "outputs": outputs,
        "regions": region_manifest,
        "qa": {
            "selection_hourly_count_ok": bool(selection["target_animation_window"]["frame_count"] == HOURLY_FRAME_COUNT),
            "all_frames_are_actual_hourly_source_observations": True,
            "interpolation_frame_count": 0,
            "smoothing_frame_count": 0,
            "coastline_sha256_ok": bool(coastline.get("sha256") == COASTLINE_SHA256_EXPECTED),
            "all_region_rendered": bool(len(datasets) == 4),
            "arrow_key_above_vector_land_all_regions": arrow_key_above_land_qa,
            "all_passed": bool(
                coastline.get("sha256") == COASTLINE_SHA256_EXPECTED
                and len(datasets) == 4
                and arrow_key_above_land_qa
            ),
        },
    }
    return manifest


def _write_readme(path: Path, manifest: dict[str, Any]) -> None:
    """寫出說明資料來源、實際 hourly 語意與 2 fps 播放取捨的 README。"""

    window = manifest["time_window"]
    policy = manifest["render_policy"]
    grid_policy = manifest.get("spatial_grid_policy", {})
    lines = [
        "# 四海域三日實際每小時原始表層流場動畫",
        "",
        "本成果只展示 A、B、C、D 四個海域的 OCM 原始表層流場，不包含 SVD 模態重建。",
        "三日資料取自原始 SCHISM NetCDF 的 hourly `hvel` time index；每一個影片影格",
        "都對應一個實際觀測時間，不使用時間內插、三點平滑、淡化或預測值；參考網格模式的空間重網格另行記錄。",
        "",
        "## 時間與播放",
        "",
        f"- 時窗：`{window['start_utc']}` 至 `{window['end_utc']}`。",
        f"- 實際 hourly 觀測影格：{window['frame_count']} 幀；時間間隔：{window['actual_observation_interval_hours']} 小時。",
        f"- 播放：{policy['fps']} fps、{window['frame_count'] / float(policy['fps']):.1f} 秒；沒有額外片頭／片尾影格。",
        "- 2 fps 是為了同時保留完整 72 個真實 hourly 觀測並落在 30–60 秒範圍；",
        "  它保證播放穩定，但不會以人工時間內插製造不存在的中間觀測。",
        "",
        "## 選窗與資料限制",
        "",
        "- 三日選窗由四區共同有效的既有 6 小時原始表層變化指標自動挑選，",
        "  只作展示代表性排序，不是潮汐調和分析或潮汐訊號分離。",
        "- 正式繪圖資料改讀三個原始日 NetCDF 經 `time_step_hours=1` 前處理所得的",
        "  1 km 表層陣列；前處理使用檔名日期修復已知 NetCDF time units 偏移。",
        f"- 固定模型層：`layer {SOURCE_LAYER_INDEX:03d}`；速度單位：m/s。",
        "- 這裡的潮汐變化是原始表層流場中的週期性變化，未將風場、背景流與潮汐成分分離。",
        "",
        "## 網格與岸線顯示",
        "",
        f"- 顯示網格模式：`{grid_policy.get('mode', 'hourly_product_crop')}`。",
        f"- 空間處理：`{grid_policy.get('spatial_interpolation_method', 'none')}`；"
        "若啟用參考網格，只有 lon/lat 空間位置被重網格，時間仍是每小時實際觀測。",
        "- 參考版使用的 formal SVD／surface-cache 網格只提供既有 1 km 顯示座標與靜態模型遮罩，"
        "  不讀取其速度值，也不改寫正式 SVD。",
        "- exact coastline 高解析度向量多邊形先作不透明底墊，再於最高圖層覆蓋流速色塊與箭頭；"
        "  保守 raster land mask 僅供 audit。對 conservative 邊界 cell，僅在 render mesh "
        "以同影格最近非陸地有限速度填補接縫，避免向量 polygon 外側露出白色鋸齒縫帶；"
        "此填補不回寫 raw payload，也不產生箭頭。",
        "",
        "## 視覺規格",
        "",
        f"- 四區使用相同 864×500 畫布、主圖／色條位置、0.0–{policy['fixed_speed_vmax_mps']:.1f} m/s "
        f"固定色階與 {policy['fixed_speed_ticks_mps'][1] - policy['fixed_speed_ticks_mps'][0]:.1f} m/s 刻度。",
        "- 高解析度 exact coastline vector polygon 先作不透明底墊、再於最高 z-order",
        "  覆蓋流速色塊與箭頭；保守 raster land mask 僅作資料／地理稽核語意，",
        "  不繪製階梯狀可見海岸線。conservative 邊界 cell 的 render-only 速度填補",
        "  只取同影格最近非陸地有限值，讓 polygon 外側與海水色階連續，且不改變原始資料。",
        f"- 畫面上方只在中央顯示隨影格更新的 UTC 日期時間 `YYYY-MM-DD HH:MM UTC`；",
        f"  使用 {RAW_DYNAMIC_TITLE_FONT_SIZE_PT:.1f} pt 字級並與主圖上緣保留間距，",
        "  海域名稱與「原始流場」標籤不嵌入影片，留給簡報端另行製作。",
        "- 圖內 `1 公尺／秒` 比例尺仍由同一個 quiver artist 的 U=1.0 建立；為兼容",
        "  SERVER Matplotlib 3.5，首次 draw 後會明確提升比例尺容器、箭頭與文字至向量",
        "  陸地圖層之上。因此即使 B 區右下錨點落在陸地，比例尺仍可見且不使用外框。",
        "- 不顯示 PC、K、K90、模態、相位或其他 SVD 分析資訊。",
        "",
        "## QA",
        "",
        "- `animation_manifest.json` 保存逐區 hourly source validity、選窗、exact coastline 雜湊、",
        "  固定色階、共用箭頭尺度、影片編碼與輸出雜湊。",
        "- `qa` 只有在四區均建立、exact coastline SHA 正確且 72 個影格均為 source-observed、",
        "  無時間內插／平滑、比例尺容器高於向量陸地且空間重網格方法已明確記錄時才可通過；",
        "  影片完成後仍應人工檢查簡報 2×2 縮放。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _render_preview(
    datasets: Sequence[HourlyDataset],
    output_dir: Path,
    *,
    width: int,
    height: int,
    dpi: int,
    target_arrows: int,
    quiver_scale_multiplier: float,
    font: Any | None,
) -> dict[str, Any]:
    """輸出一張四區中間時刻 2×2 審核圖，以及 C 區同一影格放大圖。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    middle_index = HOURLY_FRAME_COUNT // 2
    region_images: dict[str, np.ndarray] = {}
    for dataset in datasets:
        region_images[dataset.spec.key] = _render_frame(
            dataset,
            middle_index,
            width=width,
            height=height,
            dpi=dpi,
            target_arrows=target_arrows,
            quiver_scale_multiplier=quiver_scale_multiplier,
            font=font,
        )
    contact = np.concatenate(
        [
            np.concatenate([region_images["A"], region_images["B"]], axis=1),
            np.concatenate([region_images["C"], region_images["D"]], axis=1),
        ],
        axis=0,
    )
    contact_path = output_dir / "preview_four_region_2x2_middle_actual_hourly.png"
    c_path = output_dir / "preview_region_C_middle_actual_hourly.png"
    imageio.imwrite(contact_path, contact)
    imageio.imwrite(c_path, region_images["C"])
    return {
        "four_region_2x2_middle": {"path": str(contact_path.resolve()), "sha256": sha256_file(contact_path), "frame_index": middle_index},
        "region_C_middle": {"path": str(c_path.resolve()), "sha256": sha256_file(c_path), "frame_index": middle_index},
    }


def parse_args() -> argparse.Namespace:
    """解析 hourly raw-only preview／video renderer 參數。"""

    parser = argparse.ArgumentParser(description="Render four-region actual-hourly raw surface OCM animations.")
    parser.add_argument("--hourly-product-dir", type=Path, required=True, help="new isolated 1-hour preprocessed product")
    parser.add_argument("--selection-json", type=Path, required=True, help="selection JSON from select_ocm_raw_hourly_window.py")
    parser.add_argument("--coastline-geojson", type=Path, required=True, help="exact coastline GeoJSON")
    parser.add_argument(
        "--reference-cache-base",
        type=Path,
        default=None,
        help="optional preprocessed/ocm_surface base; regrid hourly u/v to each formal cache grid",
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="new preview or formal output directory")
    parser.add_argument("--regions", default="A,B,C,D", help="comma-separated regions")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS, help="video fps; 2 gives 36 seconds for 72 frames")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="output width in pixels")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help="output height in pixels")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI, help="Matplotlib raster DPI")
    parser.add_argument("--target-arrows", type=int, default=DEFAULT_TARGET_ARROWS, help="approximate arrows per region")
    parser.add_argument("--quiver-scale-multiplier", type=float, default=DEFAULT_QUIVER_SCALE_MULTIPLIER, help="shared quiver scale multiplier")
    parser.add_argument(
        "--speed-vmax-mps",
        type=float,
        default=FIXED_SPEED_VMAX_MPS,
        help="四區共用固定色階上限（m/s）；不依影格或海域自動變動",
    )
    parser.add_argument(
        "--speed-tick-step-mps",
        type=float,
        default=FIXED_SPEED_TICK_STEP_MPS,
        help="固定色條刻度間距（m/s）；須不大於色階上限",
    )
    parser.add_argument("--font-path", type=Path, default=None, help="optional CJK font path")
    parser.add_argument("--preview-only", action="store_true", help="only write static preview images and manifest")
    parser.add_argument("--render-video", action="store_true", help="write four MP4 files in addition to preview assets")
    parser.add_argument("--overwrite", action="store_true", help="overwrite files in this explicitly supplied new output directory")
    return parser.parse_args()


def main() -> None:
    """載入實際 hourly payload、輸出靜態審核圖，必要時再輸出四支 MP4。"""

    args = parse_args()
    if not args.preview_only and not args.render_video:
        raise ValueError("請指定 --preview-only 或 --render-video")
    if args.fps <= 0 or args.width <= 0 or args.height <= 0 or args.dpi <= 0 or args.target_arrows <= 0 or args.quiver_scale_multiplier <= 0:
        raise ValueError("fps/width/height/dpi/target-arrows/quiver-scale-multiplier 必須為正值")
    if not np.isfinite(args.speed_vmax_mps) or args.speed_vmax_mps <= 0.0:
        raise ValueError("speed-vmax-mps 必須是正的有限流速（m/s）")
    if not np.isfinite(args.speed_tick_step_mps) or args.speed_tick_step_mps <= 0.0:
        raise ValueError("speed-tick-step-mps 必須是正的有限流速（m/s）")
    if args.speed_tick_step_mps > args.speed_vmax_mps:
        raise ValueError("speed-tick-step-mps 不可大於 speed-vmax-mps")
    if args.width % 2 or args.height % 2:
        raise ValueError("H.264 yuv420p 輸出要求 width/height 為偶數")
    if not args.coastline_geojson.is_file():
        raise FileNotFoundError(f"找不到 exact coastline：{args.coastline_geojson}")
    if args.reference_cache_base is not None and not args.reference_cache_base.is_dir():
        raise FileNotFoundError(f"找不到 reference cache base：{args.reference_cache_base}")
    selection = _read_json(args.selection_json)
    _validate_selection(selection)
    product = _load_hourly_product(args.hourly_product_dir, selection)
    specs = build_region_specs(args.regions.split(","))
    font = find_cjk_font(args.font_path)
    datasets = [
        _build_region_dataset(
            spec,
            product,
            args.coastline_geojson,
            reference_cache_base=args.reference_cache_base,
        )
        for spec in specs
    ]
    # Dataset 將此固定尺度交給共用 raw renderer。每區都寫入相同數值，保留跨區、
    # 跨影格的色彩可比較性；這是展示 Normalize，並不截斷或回寫原始流速資料。
    for dataset in datasets:
        dataset.speed_scale_vmax = float(args.speed_vmax_mps)
        dataset.speed_tick_step_mps = float(args.speed_tick_step_mps)
    if len(datasets) != 4:
        raise ValueError("本交付規格必須同時包含 A、B、C、D 四區")
    coastline_hashes = {dataset.coastline_summary.get("sha256") for dataset in datasets}
    if coastline_hashes != {COASTLINE_SHA256_EXPECTED}:
        raise ValueError(f"exact coastline SHA 不符：{coastline_hashes}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    preview_outputs = _render_preview(
        datasets,
        args.output_dir,
        width=args.width,
        height=args.height,
        dpi=args.dpi,
        target_arrows=args.target_arrows,
        quiver_scale_multiplier=args.quiver_scale_multiplier,
        font=font,
    )
    outputs: dict[str, Any] = {}
    if args.render_video:
        for dataset in datasets:
            outputs[dataset.spec.key] = _render_region_video(
                dataset,
                args.output_dir,
                width=args.width,
                height=args.height,
                dpi=args.dpi,
                fps=args.fps,
                target_arrows=args.target_arrows,
                quiver_scale_multiplier=args.quiver_scale_multiplier,
                font=font,
                overwrite=args.overwrite,
            )
            dataset.payloads = []
            gc.collect()
    manifest = _build_manifest(
        selection,
        datasets,
        output_dir=args.output_dir,
        args=args,
        outputs=outputs,
        preview_outputs=preview_outputs,
    )
    manifest_path = args.output_dir / "animation_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_readme(args.output_dir / "README.md", manifest)
    (args.output_dir / "PREVIEW_COMPLETE" if args.preview_only else args.output_dir / "RENDER_COMPLETE").write_text(
        "C/A–D actual-hourly raw-only rendering completed; no temporal interpolation or SVD reconstruction; spatial regrid is documented.\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output_dir": str(args.output_dir.resolve()),
        "preview_outputs": preview_outputs,
        "video_regions": sorted(outputs),
        "selection_start_utc": selection["target_animation_window"]["start_utc"],
        "selection_end_utc": selection["target_animation_window"]["end_utc"],
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
