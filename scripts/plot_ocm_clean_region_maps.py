"""產生投影片後製用的 OCM 乾淨區域框與放大圖。

此腳本專門處理報告/投影片製圖需求，刻意和 `visualize_ocm_month.py` 的一般
動畫與 QC 圖分開。輸入仍是 `preprocess_ocm_month.py` 產生的月資料中間檔：
`lon.npy`、`lat.npy`、`u.npy`、`v.npy`、`speed.npy`、`mask.npy` 與
`monthly_summary.json`。輸出圖只保留經緯度刻度數字與 `Longitude`、`Latitude`，
不放標題、圖例、比例尺文字、區域名稱、anchor 名稱或任何註解，方便後續在簡報
軟體中另行加字。

新需求的固定設計：
- 主圖使用四個等物理尺寸 flow-domain bbox：連江共用域、東北台灣共用域、新竹單區域、
  屏東/海生館單區域。龜山島與貢寮共用東北台灣 flow-domain，南北竿共用連江 flow-domain。
- 龜山島、貢寮、南北竿另外各自輸出獨立放大圖，放大圖不再嵌在主圖內。
- 流速箭頭使用同一時間、同一 layer 的 98 百分位流速作為共同縮放基準，但箭頭比
  `visualize_ocm_month.py` 的一般動畫更短、更細，避免干擾 bbox 與岸線。

限制與假設：
- bbox 與 zoom extent 使用 WGS84 `(lon_min, lon_max, lat_min, lat_max)`。
- GeoJSON 陸地只作視覺疊圖，不改變 `mask.npy` 或任何流速統計。
- 目前只取 GeoJSON polygon 外環；若未來改用含重要內洞的正式岸線資料，應補上
  interior ring 處理。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np

from visualize_ocm_month import (
    LAND_COLOR,
    LAND_EDGE_COLOR,
    MISSING_DATA_COLOR,
    OCEAN_COLOR,
    QUIVER_COLOR,
    apply_ocean_mask,
    choose_quiver_step,
    draw_land_overlay,
    layer_role_name,
    load_month,
    normalize_ocean_mask,
    resolve_layer_index,
)


@dataclass(frozen=True)
class RegionBox:
    """報告主圖上的 flow-domain bbox 定義。

    `bbox_lonlat` 使用專案 CLI 順序 `(lon_min, lon_max, lat_min, lat_max)`，代表後續
    EOF/統計可從 raw OCM 前處理出的共同快取來源範圍。這些框不是作業邊界，也不是
    等深線或行政區 GIS polygon；主圖只用它們標示四個相同物理大小的資料快取區。
    `name` 只寫入 JSON metadata，不會被畫到 PNG 上。
    """

    id: str
    name: str
    bbox_lonlat: tuple[float, float, float, float]


@dataclass(frozen=True)
class ZoomWindow:
    """獨立放大圖的可視範圍定義。

    `extent_lonlat` 只控制輸出圖的裁切範圍，目的是讓龜山島、貢寮岬角與南北竿岸線
    在投影片上可直接使用。它不代表正式 `focus_bbox`；後續若要計算 EOF、統計或 AOI
    平均，仍應另行定義 `focus_bbox` 或 polygon mask。
    """

    id: str
    filename_suffix: str
    name: str
    extent_lonlat: tuple[float, float, float, float]


# 四個等物理尺寸 flow-domain bbox 取自
# docs/flow_domain_cache/REGION_BBOX_RECORD.md 的 v3 規格。此版本約為
# 150 km x 100 km；因不同緯度的一度經度長度不同，所以經度跨度會有小幅差異，
# 但物理寬高一致，適合後續資料快取與 EOF 比較。
FLOW_DOMAIN_BBOXES = (
    RegionBox(
        id="lienchiang_common",
        name="連江共用域",
        bbox_lonlat=(119.199120, 120.700880, 25.750844, 26.649156),
    ),
    RegionBox(
        id="hsinchu",
        name="新竹單區域",
        bbox_lonlat=(119.708120, 121.191880, 24.300844, 25.199156),
    ),
    RegionBox(
        id="northeast_taiwan_common",
        name="東北台灣共用域",
        bbox_lonlat=(121.306315, 122.793685, 24.600844, 25.499156),
    ),
    RegionBox(
        id="houwan_nmmba",
        name="屏東/海生館單區域",
        bbox_lonlat=(120.166710, 121.620000, 21.550844, 22.449156),
    ),
)

# 三張投影片用獨立放大圖。這些範圍比舊圖的 inset 更單純：不在圖上放名稱、
# anchor 或連接線，只輸出乾淨經緯度座標圖。bbox 邊界刻意採一位小數，讓報告圖
# 的座標外框乾淨一致；範圍選擇以不向外放大為原則，避免主體在圖面中相對縮小。
ZOOM_WINDOWS = (
    ZoomWindow(
        id="guishan",
        filename_suffix="guishan_zoom_clean",
        name="龜山島放大圖",
        extent_lonlat=(121.80, 122.20, 24.60, 25.00),
    ),
    ZoomWindow(
        id="gongliao",
        filename_suffix="gongliao_zoom_clean",
        name="貢寮放大圖",
        extent_lonlat=(121.70, 122.20, 24.80, 25.30),
    ),
    ZoomWindow(
        id="lienchiang_nangan_beigan",
        filename_suffix="lienchiang_nangan_beigan_zoom_clean",
        name="南北竿放大圖",
        extent_lonlat=(119.80, 120.20, 26.00, 26.40),
    ),
)

# 投影片版 bbox 外觀。使用單一顏色與半透明填色，目的是維持「四個同尺寸框」的視覺
# 一致性，不讓讀者把不同色彩誤解為不同資料類別、優先順序或統計權重。
BBOX_EDGE_COLOR = "#2c2f83"
BBOX_FACE_COLOR = "#7476b6"
BBOX_FACE_ALPHA = 0.18
BBOX_LINEWIDTH = 1.65

# 座標軸最多顯示的主要刻度數。這裡刻意包含圖面上下左右邊界，讓 PNG 可直接被報告
# 引用時看得出裁切範圍；數量不能太高，否則小範圍 zoom 圖的經緯度數字會互相重疊。
BOUNDARY_TICK_MAX_COUNT = 9
BOUNDARY_TICK_MIN_GAP_FACTOR = 0.65
# 固定間距 zoom 圖要盡量保留 0.1 度尺度刻度，因此只移除非常貼近邊界的內部刻度。
FIXED_INTERVAL_TICK_MIN_GAP_FACTOR = 0.35
ZOOM_COORDINATE_TICK_INTERVAL_DEG = 0.1
"""三張獨立放大圖預設使用的經緯度主要刻度間距。

單位是 WGS84 經緯度的度。三張 zoom 圖的 bbox 大小不同，若完全交給 Matplotlib
自動刻度，龜山島可能選到 0.05 度、貢寮可能選到 0.1 度，放在同一份報告時尺度
語意不一致。固定 zoom 圖內部刻度間距可讓讀者用相同座標尺度比較不同區域；圖面
四個上下限仍會額外標示，因此最靠近邊界的一段距離可能小於指定間距。
"""


def validate_time_index(time_index: int, time_count: int) -> int:
    """解析時間索引並支援 Python 負索引。

    `time_index` 對應 `time_iso.npy` 與 `u/v/speed` 的第一維。乾淨報告圖預設使用
    第一個時間步，但保留此參數方便未來重畫其它時間。若索引超出範圍，立即拋出
    `IndexError`，避免悄悄畫錯時間。
    """

    resolved = time_index if time_index >= 0 else time_count + time_index
    if resolved < 0 or resolved >= time_count:
        raise IndexError(f"time_index {time_index} outside time count {time_count}")
    return resolved


def slice_axis_to_extent(axis: np.ndarray, lower: float, upper: float, axis_name: str) -> slice:
    """依經緯度範圍從一維座標軸切出資料 slice。

    `axis` 是 `lon.npy` 或 `lat.npy` 的單調座標；輸出 slice 會保留範圍內格點，
    並在兩側多取一格，讓 `pcolormesh` 的格點面不會在圖邊界留下半格空白。若
    指定範圍完全不和資料相交，通常代表輸入資料 bbox 或 zoom extent 寫錯，因此
    直接報錯比產生空白圖更安全。
    """

    if lower > upper:
        raise ValueError(f"{axis_name} extent lower bound {lower} is greater than upper bound {upper}")
    indices = np.flatnonzero((axis >= lower) & (axis <= upper))
    if indices.size == 0:
        raise ValueError(f"{axis_name} extent {lower} to {upper} does not overlap available coordinates.")
    start = max(int(indices[0]) - 1, 0)
    stop = min(int(indices[-1]) + 2, axis.size)
    return slice(start, stop)


def boundary_inclusive_ticks(
    lower: float,
    upper: float,
    *,
    max_count: int = BOUNDARY_TICK_MAX_COUNT,
    tick_interval: float | None = None,
) -> np.ndarray:
    """建立一定包含上下界的座標軸 major ticks。

    Matplotlib 自動刻度會優先選擇好看的整數間距，但不保證 `set_xlim()`/`set_ylim()`
    指定的圖面邊界本身會被標示。報告用 zoom 圖需要明確看出四個經緯度裁切邊界，
    並且三張放大圖需要固定同一尺度；因此 `tick_interval` 有值時，內部刻度會使用
    該間距的整齊倍數，例如 0.1 度刻度會落在 `121.8`、`121.9` 這類位置，再額外
    加入 `lower` 與 `upper`。若 `tick_interval=None`，則保留主圖使用的
    `MaxNLocator` 自動刻度，再強制加入上下界。

    當固定間距刻度非常靠近上下界時，會移除該內部刻度，避免邊界標籤和鄰近標籤
    重疊；固定間距模式的避讓門檻比自動模式低，原因是 zoom 圖需要盡量保留 0.1 度
    內部刻度，讓尺度差異可被讀者辨識。這表示最靠近邊界的一段距離可小於或略大於
    `tick_interval`，但圖面核心的內部座標尺度仍維持一致，且四個 bbox 上下限一定
    可讀。

    輸入與輸出皆為 WGS84 經緯度數值，函式只影響圖面文字標示，不改變資料 slice、
    pcolormesh、GeoJSON 疊圖或 quiver 箭頭位置。
    """

    if lower > upper:
        raise ValueError(f"tick lower bound {lower} is greater than upper bound {upper}")
    if np.isclose(lower, upper):
        return np.asarray([float(lower)], dtype=np.float64)

    if tick_interval is not None:
        if tick_interval <= 0:
            raise ValueError(f"tick_interval must be positive, got {tick_interval}")
        first_tick = np.ceil((lower / tick_interval) - 1e-10) * tick_interval
        tick_count = int(np.floor((upper - first_tick) / tick_interval)) + 1
        interior_ticks = first_tick + tick_interval * np.arange(max(tick_count, 0), dtype=np.float64)
        interior_ticks = interior_ticks[(interior_ticks > lower) & (interior_ticks < upper)]
    else:
        locator = mticker.MaxNLocator(nbins=max(max_count - 2, 1), steps=[1, 2, 2.5, 5, 10])
        candidate_ticks = np.asarray(locator.tick_values(lower, upper), dtype=np.float64)
        interior_ticks = candidate_ticks[(candidate_ticks > lower) & (candidate_ticks < upper)]
    if interior_ticks.size:
        # 以目前自動刻度的最小間距當作標籤安全距離基準。距邊界太近的中間刻度不具備
        # 額外判讀價值，且容易和強制加入的邊界標籤重疊，所以在這裡排除。
        diffs = np.diff(np.sort(interior_ticks))
        typical_step = float(tick_interval) if tick_interval is not None else (
            float(np.nanmin(diffs)) if diffs.size else float(upper - lower)
        )
        gap_factor = BOUNDARY_TICK_MIN_GAP_FACTOR if tick_interval is None else FIXED_INTERVAL_TICK_MIN_GAP_FACTOR
        min_gap = typical_step * gap_factor
        if tick_interval is None:
            interior_ticks = interior_ticks[(interior_ticks - lower >= min_gap) & (upper - interior_ticks >= min_gap)]
        else:
            # 固定間距 zoom 圖仍要排除太貼近上下界的內部刻度。上下界會另外標示，
            # 所以保留重疊風險較高的鄰近刻度沒有額外資訊價值。
            interior_ticks = interior_ticks[(interior_ticks - lower >= min_gap) & (upper - interior_ticks >= min_gap)]

    ticks = np.concatenate(([float(lower)], interior_ticks, [float(upper)]))
    ticks = np.unique(np.round(ticks, decimals=10))
    if ticks.size > max_count:
        # 極小 bbox 或 locator 選到太密間距時，優先保留邊界，再等距抽樣中間刻度；這讓
        # 報告圖仍可讀，同時維持「上下左右邊界一定標示」這個核心需求。
        interior = ticks[1:-1]
        keep_count = max(max_count - 2, 0)
        if keep_count > 0 and interior.size:
            keep_indices = np.linspace(0, interior.size - 1, keep_count, dtype=int)
            interior = interior[keep_indices]
        else:
            interior = np.asarray([], dtype=np.float64)
        ticks = np.concatenate(([float(lower)], interior, [float(upper)]))
    return ticks.astype(np.float64)


def format_coordinate_tick_labels(ticks: np.ndarray, *, min_decimals: int = 0) -> list[str]:
    """依刻度間距產生一致的小數位標籤。

    大範圍主圖通常使用 0.5 或 1 度間距，因此一位或零位小數即可；固定間距 zoom
    圖則需要一致顯示小數位，例如 `121.80`、`121.90`、`122.00`。`min_decimals`
    讓 zoom 圖至少保留兩位小數，避免不同圖因浮點間距推論而出現一位與兩位小數
    混用。此函式只格式化座標軸文字，不更動實際 tick 位置。
    """

    sorted_ticks = np.sort(np.asarray(ticks, dtype=np.float64))
    diffs = np.diff(sorted_ticks)
    positive_diffs = diffs[diffs > 0]
    min_step = float(np.nanmin(positive_diffs)) if positive_diffs.size else 1.0
    if min_step >= 1.0:
        decimals = 0
    elif min_step >= 0.1:
        decimals = 1
    elif min_step >= 0.01:
        decimals = 2
    else:
        decimals = 3
    decimals = max(decimals, min_decimals)
    return [f"{0.0 if np.isclose(tick, 0.0) else tick:.{decimals}f}" for tick in sorted_ticks]


def apply_boundary_coordinate_ticks(
    ax: plt.Axes,
    extent: tuple[float, float, float, float],
    *,
    tick_interval: float | None = None,
) -> dict[str, list[float] | float | None]:
    """把圖面四個經緯度邊界固定標到座標軸上。

    `extent` 使用專案標準順序 `(lon_min, lon_max, lat_min, lat_max)`。函式會設定
    x/y major locator 與 formatter，讓輸出 PNG 的下方可讀到左右經度邊界，左側可
    讀到下上緯度邊界。`tick_interval` 只應用在需要跨圖比較尺度的 zoom 圖；主圖
    傳入 `None` 時仍使用自動好讀刻度。回傳值寫入 sidecar JSON，便於日後追溯某張
    圖實際使用哪些顯示刻度與固定間距設定。
    """

    lon_min, lon_max, lat_min, lat_max = extent
    x_ticks = boundary_inclusive_ticks(lon_min, lon_max, tick_interval=tick_interval)
    y_ticks = boundary_inclusive_ticks(lat_min, lat_max, tick_interval=tick_interval)
    min_decimals = 2 if tick_interval is not None else 0
    ax.xaxis.set_major_locator(mticker.FixedLocator(x_ticks))
    ax.xaxis.set_major_formatter(mticker.FixedFormatter(format_coordinate_tick_labels(x_ticks, min_decimals=min_decimals)))
    ax.yaxis.set_major_locator(mticker.FixedLocator(y_ticks))
    ax.yaxis.set_major_formatter(mticker.FixedFormatter(format_coordinate_tick_labels(y_ticks, min_decimals=min_decimals)))
    return {
        "longitude": [float(tick) for tick in x_ticks],
        "latitude": [float(tick) for tick in y_ticks],
        "fixed_interval_deg": None if tick_interval is None else float(tick_interval),
    }


def ring_overlaps_extent(ring: np.ndarray, extent: tuple[float, float, float, float]) -> bool:
    """用 GeoJSON ring 外接矩形判斷是否需要繪製。

    `ring` 為 `(point, 2)` 的 WGS84 經緯度座標，第一欄經度、第二欄緯度。此函式
    只做快速 bbox 初篩，不做精確 polygon intersection；原因是繪圖只需要避免全台
    所有 polygon 都被畫進小範圍 zoom，並不需要用這個判斷做定量遮罩。
    """

    lon_min, lon_max, lat_min, lat_max = extent
    ring_lon_min = float(np.nanmin(ring[:, 0]))
    ring_lon_max = float(np.nanmax(ring[:, 0]))
    ring_lat_min = float(np.nanmin(ring[:, 1]))
    ring_lat_max = float(np.nanmax(ring[:, 1]))
    return not (
        ring_lon_max < lon_min
        or ring_lon_min > lon_max
        or ring_lat_max < lat_min
        or ring_lat_min > lat_max
    )


def load_geojson_land_rings(geojson_path: Path) -> list[np.ndarray]:
    """讀取 GeoJSON 陸地 polygon 外環。

    輸入可為 `FeatureCollection`、`Feature` 或 geometry 物件，geometry 支援
    `Polygon`、`MultiPolygon` 與 `GeometryCollection`。回傳只包含外環座標，不讀
    屬性名稱，也不會在圖上產生文字。這符合本次需求：地圖上除座標軸以外不可出現
    其它字，陸地資料只提供岸線與島嶼輪廓。
    """

    if not geojson_path.exists():
        raise FileNotFoundError(f"Land overlay GeoJSON not found: {geojson_path}")
    geojson = json.loads(geojson_path.read_text(encoding="utf-8"))
    rings: list[np.ndarray] = []

    def append_polygon_outer_ring(coordinates: list) -> None:
        """從 GeoJSON Polygon 取外環並轉成 `(lon, lat)` numpy 陣列。"""

        if not coordinates:
            return
        ring = np.asarray(coordinates[0], dtype=np.float64)
        if ring.ndim != 2 or ring.shape[1] < 2 or ring.shape[0] < 3:
            return
        rings.append(ring[:, :2])

    def visit_geometry(geometry: dict | None) -> None:
        """遞迴走訪 GeoJSON geometry，統一收集 polygon 外環。"""

        if not geometry:
            return
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates")
        if geometry_type == "Polygon":
            append_polygon_outer_ring(coordinates or [])
        elif geometry_type == "MultiPolygon":
            for polygon_coordinates in coordinates or []:
                append_polygon_outer_ring(polygon_coordinates)
        elif geometry_type == "GeometryCollection":
            for child_geometry in geometry.get("geometries", []):
                visit_geometry(child_geometry)

    geojson_type = geojson.get("type")
    if geojson_type == "FeatureCollection":
        for feature in geojson.get("features", []):
            visit_geometry(feature.get("geometry"))
    elif geojson_type == "Feature":
        visit_geometry(geojson.get("geometry"))
    else:
        visit_geometry(geojson)
    if not rings:
        raise ValueError(f"No Polygon or MultiPolygon exterior rings found in {geojson_path}")
    return rings


def draw_vector_land_overlay(
    ax: plt.Axes,
    land_rings: list[np.ndarray],
    extent: tuple[float, float, float, float],
    *,
    linewidth: float,
    zorder: int,
) -> None:
    """疊加 GeoJSON 向量陸地，改善小島與海岸線辨識度。

    向量陸地和 OCM 流場資料的解析度不同：它只畫在圖上，不改變 `mask.npy`、
    `speed.npy`、`u.npy` 或 `v.npy`。在放大圖中，這能讓龜山島、貢寮岬角與南北竿
    輪廓比 1 km 規則格點 mask 更清楚。
    """

    for ring in land_rings:
        if not ring_overlaps_extent(ring, extent):
            continue
        ax.add_patch(
            mpatches.Polygon(
                ring,
                closed=True,
                facecolor=LAND_COLOR,
                edgecolor=LAND_EDGE_COLOR,
                linewidth=linewidth,
                zorder=zorder,
            )
        )


def compute_quiver_vmax(speed_frame: np.ndarray, ocean_mask: np.ndarray) -> float:
    """計算報告圖共用的箭頭縮放流速。

    `speed_frame` 為單一時間、單一 layer 的 `(lat, lon)` 水平流速大小，單位為 m/s。
    函式先套用海域遮罩，再取第 98 百分位作為代表性高流速，避免單一極端值把所有
    箭頭壓得過短。主圖與三張放大圖共用同一個 `vmax`，所以相同 m/s 在不同圖上的
    箭頭長度語意一致。
    """

    speed = apply_ocean_mask(speed_frame, ocean_mask)
    finite_speed = speed[np.isfinite(speed)]
    if finite_speed.size == 0:
        raise ValueError("Selected frame has no finite ocean speed values.")
    vmax = float(np.nanpercentile(finite_speed, 98))
    return vmax if np.isfinite(vmax) and vmax > 0 else 1.0


def draw_region_boxes(ax: plt.Axes, region_bboxes: tuple[RegionBox, ...], *, fill: bool, zorder: int) -> None:
    """畫主圖的四個 flow-domain bbox，且不加入任何文字標籤。

    函式會被呼叫兩次：第一次畫半透明填色，讓投影片上能看出區域範圍；第二次畫
    外框，避免外框被流速箭頭遮住。bbox 顏色固定一致，避免不同框被誤解成不同類別。
    """

    for region in region_bboxes:
        lon_min, lon_max, lat_min, lat_max = region.bbox_lonlat
        facecolor = matplotlib.colors.to_rgba(BBOX_FACE_COLOR, BBOX_FACE_ALPHA) if fill else "none"
        ax.add_patch(
            mpatches.Rectangle(
                (lon_min, lat_min),
                lon_max - lon_min,
                lat_max - lat_min,
                facecolor=facecolor,
                edgecolor=BBOX_EDGE_COLOR,
                linewidth=BBOX_LINEWIDTH,
                zorder=zorder,
            )
        )


def frame_stem(layer: int, layer_count: int, time_index: int) -> str:
    """建立可追溯 layer 與時間的輸出檔名前綴。

    `layer` 對應 `speed/u/v` 的第二維；`layer_role_name()` 會把最上層寫成 surface、
    最底層寫成 bottom。`time_index=0` 採用 `first_frame`，延續既有 QC 圖命名。
    """

    time_part = "first_frame" if time_index == 0 else f"time_{time_index:04d}"
    return f"{layer_role_name(layer, layer_count)}_layer_{layer:03d}_{time_part}"


def draw_clean_current_map(
    data: dict[str, np.ndarray | dict],
    output_path: Path,
    *,
    layer: int,
    time_index: int,
    extent: tuple[float, float, float, float],
    land_rings: list[np.ndarray],
    region_bboxes: tuple[RegionBox, ...],
    target_arrows: int,
    quiver_scale_multiplier: float,
    figsize: tuple[float, float],
    dpi: int,
    vmax: float,
    draw_mask_land: bool,
    coordinate_tick_interval: float | None,
) -> dict:
    """繪製單張乾淨流場圖。

    圖面規則非常嚴格：不呼叫 `set_title()`、不建立 legend、不畫 quiverkey、不畫
    label annotation，也不畫 anchor 文字。唯一可見文字來自座標軸刻度與
    `Longitude`/`Latitude`。`coordinate_tick_interval` 只用於三張放大圖，用來把
    經緯度主要刻度固定成相同度數間距；主圖傳入 `None` 時保留自動好讀刻度。這樣
    做是為了讓投影片或論文排版時能用外部文字系統統一標題、說明與編號，同時避免
    不同 zoom 圖被自動刻度畫成不同尺度。
    """

    lon_all = np.asarray(data["lon"], dtype=np.float64)
    lat_all = np.asarray(data["lat"], dtype=np.float64)
    ocean_mask_all = normalize_ocean_mask(data["mask"], (len(lat_all), len(lon_all)))
    lon_min, lon_max, lat_min, lat_max = extent
    x_slice = slice_axis_to_extent(lon_all, lon_min, lon_max, "longitude")
    y_slice = slice_axis_to_extent(lat_all, lat_min, lat_max, "latitude")

    lon = lon_all[x_slice]
    lat = lat_all[y_slice]
    ocean_mask = ocean_mask_all[y_slice, x_slice]
    speed = apply_ocean_mask(np.asarray(data["speed"][time_index, layer, y_slice, x_slice], dtype=np.float32), ocean_mask)
    u = apply_ocean_mask(np.asarray(data["u"][time_index, layer, y_slice, x_slice], dtype=np.float32), ocean_mask)
    v = apply_ocean_mask(np.asarray(data["v"][time_index, layer, y_slice, x_slice], dtype=np.float32), ocean_mask)
    valid_speed = np.isfinite(speed)
    valid_vector = valid_speed & np.isfinite(u) & np.isfinite(v)

    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    if draw_mask_land or not land_rings:
        # 主圖保留 `mask.npy` 的缺值/陸地格點語意：海域只畫在有效 surface/layer
        # 流速格點上，缺值區保留淡灰，便於大範圍資料 QC。
        ax.set_facecolor(MISSING_DATA_COLOR)
        ocean_layer = np.ma.masked_where(~valid_speed, np.ones_like(speed, dtype=np.float32))
        ocean_cmap = matplotlib.colors.ListedColormap([OCEAN_COLOR])
        ax.pcolormesh(lon, lat, ocean_layer, shading="auto", cmap=ocean_cmap, vmin=0, vmax=1, zorder=1)
        draw_land_overlay(ax, lon, lat, ocean_mask)
        land_visual_mode = "mask_and_vector_land"
    else:
        # 獨立放大圖以 GeoJSON 向量陸地作為唯一可見陸地輪廓；流場與箭頭仍使用
        # `mask.npy` 排除陸地格點，但不再把 1 km mask 方格畫出來。這可避免小島
        # 周圍出現階梯狀灰塊，看起來像底圖超出向量描邊。
        ax.set_facecolor(OCEAN_COLOR)
        land_visual_mode = "vector_land_only"
    draw_vector_land_overlay(ax, land_rings, extent, linewidth=0.30, zorder=4)

    # bbox 半透明填色放在箭頭下方；外框稍後再畫一次以保持清楚。
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
            # scale 越大箭頭越短。本腳本預設比一般動畫的 scale 更大，符合投影片版
            # 「箭頭小支一點、短一點」的需求。
            scale=max(vmax * quiver_scale_multiplier, 0.1),
            width=0.00155,
            headwidth=2.8,
            headlength=3.5,
            alpha=0.78,
            zorder=6,
        )

    draw_region_boxes(ax, region_bboxes, fill=False, zorder=7)
    ax.set_xlim(lon_min, lon_max)
    ax.set_ylim(lat_min, lat_max)
    coordinate_ticks = apply_boundary_coordinate_ticks(ax, extent, tick_interval=coordinate_tick_interval)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal", adjustable="box")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(pad=0.45)
    # 使用 tight bbox 裁掉不同 zoom 長寬比造成的外圍留白；這只影響 PNG 畫布邊界，
    # 不會新增任何文字或改變座標軸、bbox、流場箭頭的位置關係。
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.04)
    plt.close(fig)
    return {
        "path": str(output_path),
        "extent_lonlat": [float(lon_min), float(lon_max), float(lat_min), float(lat_max)],
        "coordinate_ticks": coordinate_ticks,
        "target_arrows": int(target_arrows),
        "quiver_step_yx": [int(sy), int(sx)],
        "valid_vector_cells": int(np.count_nonzero(valid_vector)),
        "total_cells_in_view": int(valid_vector.size),
        "land_visual_mode": land_visual_mode,
    }


def make_clean_region_maps(args: argparse.Namespace) -> list[Path]:
    """依 CLI 參數產生主圖、三張放大圖與 JSON metadata。

    函式集中處理資料讀取、layer/time 解析、共用 quiver 縮放、輸出檔名與 metadata。
    這讓本需求可以獨立於一般動畫流程重跑，也讓未來若要調整投影片圖面，不需要修改
    `visualize_ocm_month.py` 的既有設計。
    """

    data = load_month(args.input_dir)
    output_dir = args.output_dir or (args.input_dir / "figures")
    lon = np.asarray(data["lon"], dtype=np.float64)
    lat = np.asarray(data["lat"], dtype=np.float64)
    speed = data["speed"]
    layer_count = speed.shape[1]
    layer = resolve_layer_index(args.layer_index, layer_count)
    selected_time_index = validate_time_index(args.time_index, speed.shape[0])
    ocean_mask = normalize_ocean_mask(data["mask"], (len(lat), len(lon)))
    vmax = compute_quiver_vmax(np.asarray(speed[selected_time_index, layer], dtype=np.float32), ocean_mask)

    land_rings = [] if args.no_vector_land else load_geojson_land_rings(args.land_geojson)
    stem = frame_stem(layer, layer_count, selected_time_index)
    full_extent = (float(np.nanmin(lon)), float(np.nanmax(lon)), float(np.nanmin(lat)), float(np.nanmax(lat)))

    output_paths: list[Path] = []
    main_path = output_dir / f"{stem}_four_region_equal_bbox_clean.png"
    main_metadata = draw_clean_current_map(
        data,
        main_path,
        layer=layer,
        time_index=selected_time_index,
        extent=full_extent,
        land_rings=land_rings,
        region_bboxes=FLOW_DOMAIN_BBOXES,
        target_arrows=args.full_target_arrows,
        quiver_scale_multiplier=args.quiver_scale_multiplier,
        figsize=(8.5, 11.0),
        dpi=args.dpi,
        vmax=vmax,
        draw_mask_land=True,
        coordinate_tick_interval=None,
    )
    output_paths.append(main_path)

    zoom_metadata: list[dict] = []
    for zoom in ZOOM_WINDOWS:
        zoom_path = output_dir / f"{stem}_{zoom.filename_suffix}.png"
        metadata = draw_clean_current_map(
            data,
            zoom_path,
            layer=layer,
            time_index=selected_time_index,
            extent=zoom.extent_lonlat,
            land_rings=land_rings,
            region_bboxes=(),
            target_arrows=args.zoom_target_arrows,
            quiver_scale_multiplier=args.quiver_scale_multiplier,
            figsize=(6.2, 5.6),
            dpi=args.dpi,
            vmax=vmax,
            draw_mask_land=False,
            coordinate_tick_interval=args.zoom_coordinate_tick_interval,
        )
        metadata.update({"id": zoom.id, "name": zoom.name})
        zoom_metadata.append(metadata)
        output_paths.append(zoom_path)

    metadata_path = output_dir / f"{stem}_four_region_equal_bbox_clean.json"
    metadata = {
        "source_dir": str(args.input_dir),
        "land_geojson": None if args.no_vector_land else str(args.land_geojson),
        "time": str(data["time_iso"][selected_time_index]),
        "time_index": int(selected_time_index),
        "layer_index": int(layer),
        "domain": data.get("summary", {}).get("domain", {}),
        "text_policy": "PNG only draws coordinate tick labels plus Longitude and Latitude.",
        "quiver_policy": {
            "vmax_98pct_m_per_s": float(vmax),
            "scale_multiplier": float(args.quiver_scale_multiplier),
            "note": "Higher scale multiplier means shorter arrows. No quiver scale key is drawn.",
        },
        "flow_domain_bboxes": [asdict(region) for region in FLOW_DOMAIN_BBOXES],
        "main_figure": main_metadata,
        "zoom_figures": zoom_metadata,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_paths.append(metadata_path)
    return output_paths


def parse_args() -> argparse.Namespace:
    """解析乾淨區域圖 CLI 參數。

    預設值對齊目前 1 km GeoJSON QC 資料：表層 layer、第一個時間步、輸出到
    `<input-dir>/figures`。若要調整箭頭長度，可增加 `--quiver-scale-multiplier`；
    Matplotlib quiver 的規則是 scale 越大，箭頭越短。
    """

    parser = argparse.ArgumentParser(description="Create clean OCM report maps with equal-size region bboxes.")
    parser.add_argument("--input-dir", required=True, type=Path, help="Directory produced by preprocess_ocm_month.py.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for PNG/JSON outputs. Defaults to <input-dir>/figures.",
    )
    parser.add_argument("--layer-index", type=int, default=-1, help="Layer index to plot; -1 usually means surface.")
    parser.add_argument("--time-index", type=int, default=0, help="Time index to plot; default is first frame.")
    parser.add_argument(
        "--land-geojson",
        type=Path,
        default=Path("data/geojson/twCounty2010.geo.json"),
        help="WGS84 Polygon/MultiPolygon GeoJSON used only for visual land overlay.",
    )
    parser.add_argument(
        "--no-vector-land",
        action="store_true",
        help="Disable GeoJSON land overlay and use only mask.npy land cells.",
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
        help=(
            "Approximate arrow count for each independent zoom map. The default keeps local flow patterns visible "
            "after the one-decimal display extents are harmonized to 0.1-degree grid intervals."
        ),
    )
    parser.add_argument(
        "--quiver-scale-multiplier",
        type=float,
        default=20.0,
        help="Multiplier applied to vmax for Matplotlib quiver scale; larger means shorter arrows.",
    )
    parser.add_argument(
        "--zoom-coordinate-tick-interval",
        type=float,
        default=ZOOM_COORDINATE_TICK_INTERVAL_DEG,
        help=(
            "Major coordinate tick interval in degrees for the three independent zoom maps. "
            "Boundary ticks are still added even when the extent is not an exact multiple."
        ),
    )
    parser.add_argument("--dpi", type=int, default=180, help="Output PNG resolution.")
    return parser.parse_args()


def main() -> None:
    """程式入口：產生投影片用乾淨主圖與三張放大圖。"""

    paths = make_clean_region_maps(parse_args())
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
