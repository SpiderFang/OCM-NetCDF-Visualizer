"""將 OCM/SCHISM 月資料插值成規則格點中間檔。

本腳本以每日 NetCDF 檔為輸入，讀取節點經緯度、垂向流速與實際 z 座標，
再將非結構水平網格插值到指定 bbox 的規則經緯度格點。輸出採用 numpy
陣列與 JSON metadata，目的在於讓後續動畫、三維示意與全年批次處理共用
同一份月資料產品。

說明與處理流程概覽：
- 讀取輸入資料夾內的日 NetCDF (`*_schout.nc`) 檔案，按時間順序處理。
- 從第一個檔案讀取節點經緯度、原始水平元素連結與垂向層資訊；若原檔提供
    `SCHISM_hgrid_face_nodes`，優先用原始 UGRID/SCHISM 元素建立插值權重，讓
    海岸線與陸地洞能反映到輸出遮罩。
- 針對目標的規則 (lon, lat) 格點，先計算一次元素內線性插值權重（vertices
    與 barycentric weights），這些權重可重複套用在每個時間與每個欄位上，節省計算。
- 每個時間步：
    1) 讀取 hvel（速度）、選擇性 zcor（實際層位）與選擇性 elev（自由水面高度），
       並將特殊缺值轉成 NaN；
    2) 將 hvel 從非結構節點插值到規則格點（對每層與每一個分量分別插值），
       若來源檔提供 `wetdry_elem`，會依逐時元素乾濕狀態排除乾出格點；
    3) 若有 zcor，累加用於月平均計算；需要逐時水位/層位動畫時，可用
       `--include-zcor-time` 額外輸出完整 `zcor.npy`。
- 最後將 `time, layer, lat, lon` 排列的 `u, v, speed`、`time, lat, lon`
    排列的 `elev` 與其它輔助陣列儲存為 .npy，
    並寫出 metadata JSON（包含速度統計、時間範圍、格點資訊等）。

實作注意事項：
- 對缺值採用 NaN 處理以避免污染統計；對大型陣列採用合理資料型別以降低磁碟/記憶體占用。
- 水平插值優先沿用原始 mesh 元素；缺少 face connectivity 時才退回 Delaunay
    凸包插值。Delaunay fallback 不能保留陸地洞，因此只適合作為相容性路徑。
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from netCDF4 import Dataset, num2date
from scipy.spatial import Delaunay


EARTH_KM_PER_DEG_LAT = 111.32


@dataclass(frozen=True)
class Domain:
    """研究區域與水平重採樣設定。

    lon/lat bbox 使用 WGS84 經緯度。target_resolution_km 代表目標規則格點
    約略間距；經度方向會依區域中央緯度換算，避免在台灣緯度直接把 1 度
    經度誤當 111 公里。source_margin_deg 是插值來源節點外擴範圍，用來降低
    bbox 邊界因缺少鄰近三角形而產生缺值的機率。
    """

    domain_id: str
    lon_min: float
    lon_max: float
    lat_min: float
    lat_max: float
    target_resolution_km: float
    source_margin_deg: float


@dataclass(frozen=True)
class NodeWindow:
    """來源節點選取結果。

    indices 是原始 NetCDF 節點索引；points 是這些節點的 `(lon, lat)` 座標。
    Delaunay 與插值器只針對此視窗建立，可避免整個台灣周遭網格反覆參與
    每個時間步的插值，降低記憶體與計算成本。
    """

    indices: np.ndarray
    points: np.ndarray


@dataclass(frozen=True)
class InterpolationWeights:
    """規則格點對來源三角網的重心插值權重。

    vertices 儲存每個目標格點所屬三角形的三個來源節點局部索引，weights 是
    對應重心權重。valid 標示目標點是否落在來源節點凸包內。預先計算這些值
    可避免逐時間、逐層重建 scipy 插值器，讓單月到整年的批次處理可擴充。
    """

    vertices: np.ndarray
    weights: np.ndarray
    valid: np.ndarray


@dataclass(frozen=True)
class MeshWeights:
    """依原始 SCHISM/UGRID 水平元素建立的插值權重。

    vertices 儲存每個目標規則格點所落入的原始水平元素節點，最多支援四邊形；
    weights 是對應節點的線性權重，element_indices 記錄目標點所屬的原始元素；
    valid 則表示該目標點確實位於原始水體元素內。這和 Delaunay 凸包插值不同：
    原始 mesh 不含陸地洞時，陸地洞不會被人工補滿，因此可避免在台灣本島或
    海岸陸域畫出假流速。element_indices 也讓逐時 `wetdry_elem` 能回投影到
    規則格點，排除潮間帶或淺灘乾出時的速度。
    """

    vertices: np.ndarray
    weights: np.ndarray
    element_indices: np.ndarray
    valid: np.ndarray


def _ring_to_lonlat_array(raw_ring: Any) -> np.ndarray | None:
    """將 GeoJSON ring 座標轉成 `(n, 2)` 經緯度陣列。

    GeoJSON 的 polygon ring 以 `[lon, lat]` 或 `[lon, lat, z]` 座標序列表示；
    本專案只需要水平遮罩，因此只保留前兩欄。若 ring 點數不足、座標維度不符
    或含有非有限值，回傳 None 代表該 ring 不適合作為陸域遮罩。這種保守處理
    可避免外部圖資局部壞點讓整個月前處理中斷。
    """

    # 外部 GeoJSON 可能含有第三維高程或其它屬性；只取 lon/lat 兩欄做水平判斷。
    ring = np.asarray(raw_ring, dtype=np.float64)
    if ring.ndim != 2 or ring.shape[0] < 4 or ring.shape[1] < 2:
        return None
    lonlat = ring[:, :2]
    if not np.isfinite(lonlat).all():
        return None
    return lonlat


def iter_geojson_polygon_rings(geometry: dict[str, Any] | None) -> Iterable[list[np.ndarray]]:
    """逐一產生 GeoJSON polygon 的 ring 陣列。

    輸入可為 Geometry、Feature、FeatureCollection 或 GeometryCollection。輸出
    每個 polygon 的 rings，第一個 ring 是外環，後續 ring 是洞環；呼叫端會用
    外環加入陸域、洞環扣回非陸域。只處理 Polygon/MultiPolygon，LineString、
    Point 等幾何無法定義面積，因此會被略過。
    """

    if not geometry:
        return

    geometry_type = geometry.get("type")
    if geometry_type == "FeatureCollection":
        # FeatureCollection 是 g0v/twgeojson 與多數行政區 GeoJSON 的常見格式。
        # 逐一遞迴 feature，可支援不同縣市分別作為獨立 feature 的資料結構。
        for feature in geometry.get("features", []):
            yield from iter_geojson_polygon_rings(feature)
        return
    if geometry_type == "Feature":
        # Feature 的 properties 僅用於屬性描述；遮罩只需 geometry。
        yield from iter_geojson_polygon_rings(geometry.get("geometry"))
        return
    if geometry_type == "GeometryCollection":
        # 少數資料會把多種 geometry 放在 collection；只抽取其中面狀幾何。
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
    """判斷多個 `(lon, lat)` 點是否位於單一 polygon ring 內。

    使用向量化 ray-casting 演算法：從每個目標點向右發射水平射線，射線穿過 ring
    邊界的次數為奇數即視為在內。回傳值包含落在邊界上的點；對陸地遮罩而言，
    邊界點保守視為陸地，可避免海岸線剛好穿過格點時仍留下假流速。座標假設為
    WGS84 經緯度，適用於台灣區域這種小範圍遮罩；不處理跨日期變更線的 polygon。
    """

    x = points[:, 0]
    y = points[:, 1]
    ring_x = ring[:, 0]
    ring_y = ring[:, 1]
    inside = np.zeros(points.shape[0], dtype=bool)
    on_boundary = np.zeros(points.shape[0], dtype=bool)

    # 逐段檢查 polygon 邊。迴圈跑在邊數上，點集合運算仍由 NumPy 向量化處理；
    # 對本專案規則格點數量來說，比引入額外 GIS 原生相依更容易部署與維護。
    previous = ring.shape[0] - 1
    for current in range(ring.shape[0]):
        x0 = ring_x[previous]
        y0 = ring_y[previous]
        x1 = ring_x[current]
        y1 = ring_y[current]

        # 邊界判斷：若點到線段的外積接近 0，且投影落在線段 bbox 內，就視為在邊界。
        # tolerance 使用經緯度的極小值，只處理浮點誤差，不主動擴張岸線。
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

        # Ray casting 主體：水平邊不會穿越射線，會被第一個條件排除；np.errstate
        # 避免水平邊造成除以 0 警告，但不改變邏輯結果。
        crosses_latitude = (y0 > y) != (y1 > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            crossing_lon = (segment_dx * (y - y0) / (y1 - y0)) + x0
        inside ^= crosses_latitude & (x < crossing_lon)
        previous = current

    return inside | on_boundary


def build_geojson_land_mask(
    geojson_path: Path,
    target_points: np.ndarray,
    grid_shape: tuple[int, int],
) -> tuple[np.ndarray, dict[str, Any]]:
    """從 GeoJSON 面狀圖資建立規則格點陸地遮罩。

    geojson_path 指向本機 GeoJSON 檔，資料來源可為 g0v/twgeojson、OXXO 文章
    指向的 Sheethub 下載檔，或其它正式岸線/行政區 polygon。回傳的 land_mask
    形狀為 `(lat, lon)`，True 代表目標格點落在 GeoJSON 陸域 polygon 內，應從
    OCM 有效海域遮罩扣除。summary 用於寫入 metadata，保留圖資路徑、命中格點
    與略過 polygon 數量，方便日後追溯遮罩來源與遮蔽程度。
    """

    # 明確以 UTF-8 讀取 GeoJSON，避免中文縣市名稱或屬性在 metadata 來源檢查時出錯。
    geojson = json.loads(geojson_path.read_text(encoding="utf-8"))
    flat_land_mask = np.zeros(target_points.shape[0], dtype=bool)
    polygon_count = 0
    skipped_polygon_count = 0
    candidate_point_tests = 0

    target_lon = target_points[:, 0]
    target_lat = target_points[:, 1]
    for rings in iter_geojson_polygon_rings(geojson):
        if not rings:
            skipped_polygon_count += 1
            continue
        polygon_count += 1
        exterior = rings[0]

        # 先用 exterior bbox 篩出可能命中的格點，避免每個縣市 polygon 都掃描整個網格。
        lon_min = float(np.nanmin(exterior[:, 0]))
        lon_max = float(np.nanmax(exterior[:, 0]))
        lat_min = float(np.nanmin(exterior[:, 1]))
        lat_max = float(np.nanmax(exterior[:, 1]))
        candidate_indices = np.flatnonzero(
            (target_lon >= lon_min)
            & (target_lon <= lon_max)
            & (target_lat >= lat_min)
            & (target_lat <= lat_max)
        )
        if candidate_indices.size == 0:
            continue

        candidate_point_tests += int(candidate_indices.size)
        candidate_points = target_points[candidate_indices]
        polygon_land = points_in_ring(candidate_points, exterior)
        if not polygon_land.any():
            continue

        # GeoJSON Polygon 後續 ring 是洞環。洞環內的點不應視為陸地；這對湖泊、
        # 特殊行政區界線或未來更細的岸線資料很重要。
        for hole in rings[1:]:
            if not polygon_land.any():
                break
            hole_candidates = np.flatnonzero(polygon_land)
            polygon_land[hole_candidates] &= ~points_in_ring(candidate_points[hole_candidates], hole)

        flat_land_mask[candidate_indices] |= polygon_land

    summary = {
        "path": str(geojson_path),
        "format": "GeoJSON Polygon/MultiPolygon",
        "polygon_count": int(polygon_count),
        "skipped_polygon_count": int(skipped_polygon_count),
        "candidate_point_tests": int(candidate_point_tests),
        "land_grid_cell_count": int(flat_land_mask.sum()),
    }
    return flat_land_mask.reshape(grid_shape), summary


def list_month_files(input_dir: Path, max_files: int | None) -> list[Path]:
    """列出單月 NetCDF 日檔。

    檔名以排序後順序作為時間處理順序。max_files 只用於快速測試或除錯，
    正式月產品應保留為 None 以免時間序列不完整。
    """

    # 按字典順序列出所有符合名稱模式的檔案，排序結果決定處理時間順序
    files = sorted(input_dir.glob("*_schout.nc"))
    if not files:
        # 若找不到任何檔案，主動失敗以避免產生空的月產品
        raise FileNotFoundError(f"No *_schout.nc files found in {input_dir}")
    # 若提供 max_files 用於測試，回傳子集；正式處理時應傳 None
    return files[:max_files] if max_files else files


def build_target_grid(domain: Domain) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """依 bbox 與公里解析度建立規則經緯度格點。

    回傳 lon、lat 一維座標與 `(lon, lat)` 目標點陣列。lat/lon 的格點數至少為
    2，避免非常小的測試 bbox 導致後續繪圖或插值沒有空間維度。
    """

    # 以目標解析度（km）換算成經緯度度數：緯度近似恆定，經度依中心緯度收縮
    center_lat = 0.5 * (domain.lat_min + domain.lat_max)
    deg_lat = domain.target_resolution_km / EARTH_KM_PER_DEG_LAT
    # 避免 cos(90°) 除以零，故以 min cos 值 0.1 當作下限
    deg_lon = domain.target_resolution_km / (EARTH_KM_PER_DEG_LAT * max(math.cos(math.radians(center_lat)), 0.1))

    # 計算格點數；至少保留 2 個格點以避免過小格網產生 shape 問題
    lon_count = max(2, int(math.floor((domain.lon_max - domain.lon_min) / deg_lon)) + 1)
    lat_count = max(2, int(math.floor((domain.lat_max - domain.lat_min) / deg_lat)) + 1)

    # 建立一維經緯座標陣列，並產生二維網格以便回傳平面座標列表
    lon = np.linspace(domain.lon_min, domain.lon_max, lon_count, dtype=np.float64)
    lat = np.linspace(domain.lat_min, domain.lat_max, lat_count, dtype=np.float64)
    mesh_lon, mesh_lat = np.meshgrid(lon, lat)
    return lon, lat, np.column_stack([mesh_lon.ravel(), mesh_lat.ravel()])


def read_node_coordinates(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """讀取 SCHISM 水平節點經緯度。

    目前 OCM 檔案預期使用 `SCHISM_hgrid_node_x/y`。若未來資料改名，應在此處
    加入別名判斷，而不是在各處硬編碼不同變數名稱。
    """

    # 從 NetCDF 中讀出 SCHISM 的節點經緯度變數，並轉為 float64
    with Dataset(path) as ds:
        lon = np.asarray(ds.variables["SCHISM_hgrid_node_x"][:], dtype=np.float64)
        lat = np.asarray(ds.variables["SCHISM_hgrid_node_y"][:], dtype=np.float64)
    return lon, lat


def select_source_nodes(path: Path, domain: Domain) -> NodeWindow:
    """依研究區域外擴範圍選取來源節點。

    選取範圍比輸出 bbox 稍大，讓 Delaunay 三角網在邊界外仍有鄰近節點。若
    bbox 太小或資料不覆蓋此區域，會明確拋出錯誤，避免輸出全缺值產品。
    """

    # 讀取全部節點的經緯度，並以 bbox 加 margin 選取來源節點
    lon, lat = read_node_coordinates(path)
    mask = (
        (lon >= domain.lon_min - domain.source_margin_deg)
        & (lon <= domain.lon_max + domain.source_margin_deg)
        & (lat >= domain.lat_min - domain.source_margin_deg)
        & (lat <= domain.lat_max + domain.source_margin_deg)
    )
    indices = np.flatnonzero(mask)
    # 如果選到的節點少於 3 個，無法建立三角網，主動報錯並建議擴大範圍
    if indices.size < 3:
        raise ValueError("Selected source window has fewer than 3 nodes; enlarge bbox or source margin.")
    points = np.column_stack([lon[indices], lat[indices]])
    return NodeWindow(indices=indices.astype(np.int64), points=points)


def read_sigma_or_layer_axis(path: Path, layer_count: int) -> np.ndarray:
    """讀取垂向層代表座標。

    若原檔提供 `sigma`，它代表非維度化垂向座標，通常由海底負值到表層 0。
    若缺少 sigma，則回傳 0 到 layer_count-1 的層索引，並由 metadata 註明這是
    模型層序號而非真實深度。
    """

    # 嘗試讀取 sigma（非維度垂向座標），若存在且與層數一致且有實際變化，回傳 sigma
    # 否則回傳單純的層索引陣列以作為替代說明（metadata 會指出這是層編號）
    with Dataset(path) as ds:
        if "sigma" in ds.variables:
            sigma = np.asarray(ds.variables["sigma"][:], dtype=np.float32)
            if sigma.size == layer_count and np.nanmax(sigma) > np.nanmin(sigma):
                return sigma
    return np.arange(layer_count, dtype=np.float32)


def clean_missing_values(values: np.ndarray, missing_value: float | None = None) -> np.ndarray:
    """將 SCHISM 缺值旗標轉成 NaN。

    OCM/SCHISM 常用約 `9.969e36` 作為 missing value。這些數值對 numpy 來說
    仍是有限值，若不先轉成 NaN，流速統計與色階會被缺值污染。missing_value
    來自變數屬性；即使屬性缺失，也會把非常大的哨兵值視為缺值。
    """

    # 將可能的 sentinel 或非數值轉為 NaN，方便後續的 numpy 統計與遮罩運算
    array = np.asarray(values, dtype=np.float64)
    cleaned = array.copy()
    # 常見 sentinel 或極大數視為缺值
    invalid = ~np.isfinite(cleaned) | (np.abs(cleaned) > 1.0e20)
    # 若變數有 missing_value 屬性，也將其視為缺值。OCM/SCHISM 常見的
    # 9.969e36 這類巨大 sentinel 已經會被上面的 abs > 1e20 捕捉；此時避免再對
    # hvel/zcor 等大型陣列執行昂貴的 np.isclose。只有 missing_value 位於正常數值
    # 尺度時才做額外比較，避免誤把一般海洋物理量或流速值保留下來。
    if missing_value is not None and np.isfinite(missing_value) and abs(float(missing_value)) <= 1.0e20:
        invalid |= np.isclose(cleaned, missing_value)
    cleaned[invalid] = np.nan
    return cleaned


def parse_times(ds: Dataset, selected: np.ndarray) -> list[str]:
    """將 NetCDF 時間值轉成 ISO 字串。

    若 time 變數缺少 CF `units`，改用原始數值字串，確保時間序列仍能保留
    順序資訊；這種情況會在 metadata 中由原始屬性進一步確認。
    """

    # 將 NetCDF 的 time 變數轉成 ISO 字串；若缺少 CF 的 units，則保留原始數值字串
    time_var = ds.variables["time"]
    values = np.asarray(time_var[selected])
    units = getattr(time_var, "units", None)
    calendar = getattr(time_var, "calendar", "standard")
    if units:
        # 使用 num2date 將數值轉為 datetime，再用 isoformat 序列化
        dates = num2date(values, units=units, calendar=calendar, only_use_cftime_datetimes=False)
        return [date.isoformat() for date in dates]
    # 若沒有 units，就回傳原始值的字串，保留順序資訊
    return [str(value) for value in values]


def build_interpolation_weights(triangulation: Delaunay, target_points: np.ndarray) -> InterpolationWeights:
    """建立目標格點的 Delaunay 重心插值權重。

    scipy 的 Delaunay transform 可直接把目標點轉成所屬 simplex 的重心座標。
    對於固定 bbox 與固定來源節點視窗，這些權重整個月都不變，因此先算一次
    就能重複套用在 u、v、zcor 與水深欄位。
    """

    # 使用 Delaunay 提供的 transform 將目標點映射至其所屬 simplex，取得重心座標
    simplex = triangulation.find_simplex(target_points)
    valid = simplex >= 0

    # 初始化為不合法的頂點與 NaN 權重
    vertices = np.full((target_points.shape[0], 3), -1, dtype=np.int64)
    weights = np.full((target_points.shape[0], 3), np.nan, dtype=np.float64)

    if valid.any():
        # transform 的形式可用來計算 barycentric coordinates
        transform = triangulation.transform[simplex[valid]]
        delta = target_points[valid] - transform[:, 2]
        bary = np.einsum("ijk,ik->ij", transform[:, :2], delta)
        # bary 只有前兩個分量，第三個由 1-sum(bary[:2]) 得到
        weights[valid, :2] = bary
        weights[valid, 2] = 1.0 - bary.sum(axis=1)
        # 取得對應 simplex 的節點索引
        vertices[valid] = triangulation.simplices[simplex[valid]]

    # valid 表示目標點是否落在來源節點凸包內
    return InterpolationWeights(vertices=vertices, weights=weights, valid=valid)


def _normalize_face_nodes(raw_faces: np.ndarray, node_count: int) -> np.ndarray:
    """將 UGRID face-node connectivity 正規化為 0-based 節點索引。

    SCHISM/UGRID 的 `SCHISM_hgrid_face_nodes` 通常以 1-based 節點編號儲存，並
    以 `_FillValue` 或負值填補三角形的第 4 個欄位。此函式會把有效節點轉為
    Python/NumPy 使用的 0-based 索引，無效欄位保留為 -1。node_count 用來判斷
    原始資料是否已是 0-based，避免對不同來源檔案硬套同一種編號規則。
    """

    faces = np.asarray(raw_faces)
    if np.ma.isMaskedArray(faces):
        # NetCDF 變數若帶有 _FillValue，netCDF4 可能回傳 masked array；先將被遮蔽
        # 的填補欄位改成 -1，後續統一視為「此元素沒有這個節點」。
        faces = np.ma.filled(faces, -1)
    faces = np.asarray(faces, dtype=np.int64)
    positive = faces[faces > 0]
    if positive.size and int(positive.max()) == node_count:
        # 全域 face connectivity 若出現 node_count 這個最大節點號，代表它是 UGRID
        # 常見的 1-based 編號。部分檔案會用 0 填補三角形第 4 欄，不能把這些 0
        # 誤當成 0-based 的真實節點；0-based 檔案的最大合法節點號則會是 node_count-1。
        faces = np.where(faces > 0, faces - 1, -1)
    faces[(faces < 0) | (faces >= node_count)] = -1
    return faces


def _triangle_barycentric(points: np.ndarray, triangle: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """計算多個目標點相對單一三角形的重心座標。

    points 為 `(n, 2)` 的 `(lon, lat)` 目標點，triangle 為三個原始節點座標。
    回傳 inside 布林陣列與 `(n, 3)` 權重。退化三角形沒有穩定面積，會全部
    視為無效，避免除以接近 0 的面積造成極端權重。
    """

    a, b, c = triangle
    denom = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
    if abs(float(denom)) < 1.0e-14:
        return np.zeros(points.shape[0], dtype=bool), np.full((points.shape[0], 3), np.nan, dtype=np.float64)
    w0 = ((b[1] - c[1]) * (points[:, 0] - c[0]) + (c[0] - b[0]) * (points[:, 1] - c[1])) / denom
    w1 = ((c[1] - a[1]) * (points[:, 0] - c[0]) + (a[0] - c[0]) * (points[:, 1] - c[1])) / denom
    w2 = 1.0 - w0 - w1
    weights = np.column_stack([w0, w1, w2])
    tolerance = 1.0e-10
    inside = (weights >= -tolerance).all(axis=1) & (weights <= 1.0 + tolerance).all(axis=1)
    return inside, weights


def _assign_triangle_weights(
    target_points: np.ndarray,
    triangle_points: np.ndarray,
    triangle_vertices: np.ndarray,
    element_index: int,
    candidate_indices: np.ndarray,
    vertices: np.ndarray,
    weights: np.ndarray,
    element_indices: np.ndarray,
    valid: np.ndarray,
) -> None:
    """把落在單一三角形內的目標點寫入全域權重陣列。

    candidate_indices 是已由元素 bbox 初篩的目標格點索引。函式只填入尚未被其它
    元素命中的格點；若目標點剛好落在元素邊界，保留第一個命中的元素即可，因為
    相鄰元素在同一邊界上的線性插值理論上會給出相同結果。element_index 會同步
    寫入，供後續將 `wetdry_elem(time, elem)` 轉為規則格點逐時乾濕遮罩。
    """

    unresolved = candidate_indices[~valid[candidate_indices]]
    if unresolved.size == 0:
        return
    inside, local_weights = _triangle_barycentric(target_points[unresolved], triangle_points)
    if not inside.any():
        return
    matched = unresolved[inside]
    vertices[matched, :3] = triangle_vertices
    weights[matched, :3] = local_weights[inside]
    element_indices[matched] = element_index
    valid[matched] = True


def grid_bbox_candidate_indices(
    lon_axis: np.ndarray,
    lat_axis: np.ndarray,
    lon_min: float,
    lon_max: float,
    lat_min: float,
    lat_max: float,
    valid: np.ndarray,
) -> np.ndarray:
    """用規則格點座標軸快速找出落在 bbox 內的目標格點索引。

    原先做法會對每一個 SCHISM face 掃描整個目標格點陣列；在 10 km 網格尚可，
    但 1 km 台灣 bbox 會放大到約 32 萬格點，導致 face 數量乘上格點數的成本
    過高。這裡利用 lon/lat 規則遞增的一維軸，以 `searchsorted` 直接切出 face
    bbox 涵蓋的 x/y 範圍，再轉回 target_points 的 row-major 扁平索引。輸出只
    包含尚未被其它元素命中的格點，維持原本「第一個命中元素」的邊界處理語意。
    """

    lon_start = int(np.searchsorted(lon_axis, lon_min, side="left"))
    lon_stop = int(np.searchsorted(lon_axis, lon_max, side="right"))
    lat_start = int(np.searchsorted(lat_axis, lat_min, side="left"))
    lat_stop = int(np.searchsorted(lat_axis, lat_max, side="right"))

    # 若 face bbox 完全不與目標規則格點交會，直接回傳空陣列，避免後續建立 meshgrid。
    if lon_start >= lon_stop or lat_start >= lat_stop:
        return np.empty(0, dtype=np.int64)

    lon_count = lon_axis.size
    x_indices = np.arange(lon_start, lon_stop, dtype=np.int64)
    y_indices = np.arange(lat_start, lat_stop, dtype=np.int64)
    candidate_indices = (y_indices[:, None] * lon_count + x_indices[None, :]).ravel()
    return candidate_indices[~valid[candidate_indices]]


def build_mesh_interpolation_weights(path: Path, target_points: np.ndarray, grid_shape: tuple[int, int]) -> MeshWeights:
    """用原始 SCHISM 水平元素建立規則格點插值權重。

    資料來源是 NetCDF 的 `SCHISM_hgrid_face_nodes` 與節點經緯度。此方式只接受
    落在原始三角形或四邊形元素內的目標點，因此原始網格中的陸地洞、海岸線與
    開放邊界會直接反映到輸出的 mask。回傳的 element_indices 會保存每個格點
    所屬元素，讓逐時 `wetdry_elem` 可在不重新做幾何搜尋的情況下套用。若檔案
    缺少 face connectivity，呼叫端可退回 Delaunay，但那種 fallback 只適合快速
    檢查，不能嚴格代表海陸邊界。
    """

    with Dataset(path) as ds:
        if "SCHISM_hgrid_face_nodes" not in ds.variables:
            raise KeyError("SCHISM_hgrid_face_nodes not found")
        lon = np.asarray(ds.variables["SCHISM_hgrid_node_x"][:], dtype=np.float64)
        lat = np.asarray(ds.variables["SCHISM_hgrid_node_y"][:], dtype=np.float64)
        faces = _normalize_face_nodes(ds.variables["SCHISM_hgrid_face_nodes"][:], lon.size)

    lat_count, lon_count = grid_shape
    # target_points 由 build_target_grid 的 np.meshgrid(lon, lat) 依 row-major 攤平成
    # `(lon, lat)` 點列；因此前 lon_count 個點就是經度軸，每隔 lon_count 個點就是
    # 緯度軸。後續 face bbox 搜尋使用這兩條軸，避免每個 face 全域掃描所有格點。
    lon_axis = target_points[:lon_count, 0]
    lat_axis = target_points[::lon_count, 1]
    vertices = np.full((target_points.shape[0], 4), -1, dtype=np.int64)
    weights = np.full((target_points.shape[0], 4), np.nan, dtype=np.float64)
    element_indices = np.full(target_points.shape[0], -1, dtype=np.int64)
    valid = np.zeros(target_points.shape[0], dtype=bool)

    for face_index, face in enumerate(faces):
        face_nodes = face[face >= 0]
        if face_nodes.size < 3:
            continue

        polygon = np.column_stack([lon[face_nodes], lat[face_nodes]])
        lon_min = float(np.nanmin(polygon[:, 0]))
        lon_max = float(np.nanmax(polygon[:, 0]))
        lat_min = float(np.nanmin(polygon[:, 1]))
        lat_max = float(np.nanmax(polygon[:, 1]))
        candidate_indices = grid_bbox_candidate_indices(
            lon_axis,
            lat_axis,
            lon_min,
            lon_max,
            lat_min,
            lat_max,
            valid,
        )
        if candidate_indices.size == 0:
            continue

        if face_nodes.size == 3:
            _assign_triangle_weights(
                target_points,
                polygon,
                face_nodes,
                face_index,
                candidate_indices,
                vertices,
                weights,
                element_indices,
                valid,
            )
        else:
            # UGRID 可含四邊形元素；拆成兩個三角形後仍保留原始四個節點欄位。
            # 這裡採用 (0,1,2) 與 (0,2,3) 的固定對角線，對 SCHISM 常見凸四邊形
            # 足以提供穩定插值；非凸元素若出現，未命中的區域會保守地維持陸地/無效。
            for tri_local in ((0, 1, 2), (0, 2, 3)):
                tri_vertices = face_nodes[list(tri_local)]
                tri_points = np.column_stack([lon[tri_vertices], lat[tri_vertices]])
                _assign_triangle_weights(
                    target_points,
                    tri_points,
                    tri_vertices,
                    face_index,
                    candidate_indices,
                    vertices,
                    weights,
                    element_indices,
                    valid,
                )

    return MeshWeights(vertices=vertices, weights=weights, element_indices=element_indices, valid=valid)


def apply_interpolation(source_values: np.ndarray, weights: InterpolationWeights, shape: tuple[int, int]) -> np.ndarray:
    """套用預先計算的水平插值權重。

    source_values 必須已經裁切成來源節點視窗的順序。若任一三角形頂點為 NaN，
    對應目標格點也會輸出 NaN，這可保留底層缺值與乾濕遮罩資訊。
    """

    # 對於所有目標點，使用預先計算的 vertices 與 weights 對來源節點值做線性組合
    result = np.full(weights.valid.shape[0], np.nan, dtype=np.float32)
    # 只處理那些落在凸包內的目標點
    valid_vertices = weights.vertices[weights.valid]
    values = source_values[valid_vertices]  # (n_valid, 3)

    # 若三個頂點中任一個為 NaN，該目標點視為不可用
    valid_values = np.isfinite(values).all(axis=1)
    # 對可用位置計算加權和
    interpolated = np.sum(values[valid_values] * weights.weights[weights.valid][valid_values], axis=1)
    valid_positions = np.flatnonzero(weights.valid)[valid_values]
    result[valid_positions] = interpolated.astype(np.float32)
    # 將結果重塑回 (lat, lon) 形狀
    return result.reshape(shape)


def apply_mesh_interpolation(source_values: np.ndarray, weights: MeshWeights, shape: tuple[int, int]) -> np.ndarray:
    """套用原始 mesh 元素權重進行水平插值。

    source_values 必須使用 NetCDF 全域節點索引排序，因為 MeshWeights 的 vertices
    直接保存原始節點編號。輸出形狀為 `(lat, lon)`；未落在原始水體元素內、元素
    任一頂點缺值，或權重不可用時皆輸出 NaN，供 mask 與後續繪圖明確區分陸地。
    """

    result = np.full(weights.valid.shape[0], np.nan, dtype=np.float32)
    valid_positions = np.flatnonzero(weights.valid)
    if valid_positions.size == 0:
        return result.reshape(shape)

    selected_vertices = weights.vertices[valid_positions]
    selected_weights = weights.weights[valid_positions]
    used = selected_vertices >= 0

    # 將 -1 佔位索引暫時改成 0，避免 NumPy 進階索引誤取最後一個節點；這些欄位
    # 會用 used mask 將權重與值清為 0，因此不會參與加權和。這個向量化做法比
    # 逐格 Python 迴圈快得多，適合月資料的 time/layer 批次插值。
    safe_vertices = np.where(used, selected_vertices, 0)
    values = np.asarray(source_values, dtype=np.float64)[safe_vertices]
    finite_values = np.isfinite(values) | ~used
    finite_weights = np.isfinite(selected_weights) | ~used
    valid_values = finite_values.all(axis=1) & finite_weights.all(axis=1)
    weighted_values = np.where(used, values * np.where(used, selected_weights, 0.0), 0.0)
    result[valid_positions[valid_values]] = weighted_values[valid_values].sum(axis=1).astype(np.float32)
    return result.reshape(shape)


def build_wetdry_ocean_mask(
    wetdry_values: np.ndarray,
    mesh_weights: MeshWeights,
    static_ocean_mask: np.ndarray,
    grid_shape: tuple[int, int],
    missing_value: float | None,
) -> np.ndarray:
    """將 `wetdry_elem` 的逐時元素旗標轉成規則格點海域遮罩。

    SCHISM 的 `wetdry_elem` 位於元素中心，形狀通常為 `(time, nSCHISM_hgrid_face)`；
    在常見輸出中 `0` 代表 wet、`1` 代表 dry。因為 MeshWeights 已記錄每個目標
    格點所屬元素，本函式只需用 element_indices 查表即可取得逐時乾濕狀態。
    回傳值仍是 `(lat, lon)` 布林陣列，True 代表該時間步可繪製或納入統計的
    海水格點；False 代表靜態陸地、mesh 外、缺值或該時間步乾出。
    """

    wetdry = np.asarray(wetdry_values, dtype=np.float64).copy()
    invalid = ~np.isfinite(wetdry)
    if missing_value is not None and np.isfinite(missing_value):
        invalid |= np.isclose(wetdry, missing_value)
    wetdry[invalid] = np.nan

    flat_mask = static_ocean_mask.ravel().copy()
    element_indices = mesh_weights.element_indices
    valid_positions = flat_mask & (element_indices >= 0)
    if valid_positions.any():
        # SCHISM wetdry convention: 0 為 wet，非 0 通常代表 dry。這裡使用 <=0.5
        # 做容忍判斷，是為了處理 float32 儲存或少量數值誤差；NaN 一律視為不可用。
        element_wet = np.isfinite(wetdry[element_indices[valid_positions]]) & (
            wetdry[element_indices[valid_positions]] <= 0.5
        )
        flat_mask[valid_positions] &= element_wet
    return flat_mask.reshape(grid_shape)


def infer_hvel_layout(ds: Dataset) -> tuple[int, int]:
    """推斷 hvel 的節點與垂向層維度位置。

    SCHISM 常見 hvel 維度為 `(time, node, layer, component)`。為了保留對相近
    輸出格式的容忍度，此處根據維度名稱尋找 node 與 vertical layer 位置，
    回傳的是移除 time 後的資料軸位置。
    """

    # 根據 hvel 的維度名稱推斷 node 與 layer 在非 time 軸集合中的位置。
    # 常見情況為 hvel.dimensions 包含 time, node, layer, component 等。
    dims = ds.variables["hvel"].dimensions
    non_time = [dim for dim in dims if dim != "time"]
    node_axis = next((i for i, dim in enumerate(non_time) if "node" in dim.lower()), None)
    layer_axis = next((i for i, dim in enumerate(non_time) if "vgrid" in dim.lower() or "layer" in dim.lower()), None)
    if node_axis is None or layer_axis is None:
        raise ValueError(f"Cannot infer hvel node/layer axes from dimensions: {dims}")
    return node_axis, layer_axis


def normalize_hvel_slice(raw: np.ndarray, node_axis: int, layer_axis: int) -> np.ndarray:
    """將單一時間的 hvel 正規化為 `(node, layer, component)`。

    後續插值假設最後一軸是東西/南北分量。若原始維度順序不同，這裡會集中
    處理轉軸，避免視覺化腳本再理解 NetCDF 的多種排列方式。
    """

    # 找到代表東西/南北分量的軸（大小為 2），並將節點、層與分量軸移到最後三個位置
    component_axis = next((i for i, size in enumerate(raw.shape) if size == 2 and i not in (node_axis, layer_axis)), None)
    if component_axis is None:
        raise ValueError(f"Cannot find 2-component velocity axis in hvel slice shape {raw.shape}")
    # 返回形狀為 (node, layer, component) 的陣列，方便後續以 node 與 layer 為索引插值
    return np.moveaxis(raw, (node_axis, layer_axis, component_axis), (0, 1, 2))


def normalize_zcor_slice(raw: np.ndarray, node_axis: int, layer_axis: int) -> np.ndarray:
    """將單一時間的 zcor 正規化為 `(node, layer)`。

    zcor 代表每個節點與垂向層的實際高度，單位通常為公尺。月產品只輸出平均
    zcor，用於三維示意圖標示層位，避免把完整時間變化 zcor 寫成過大的檔案。
    """

    # 將 zcor 的軸順整理為 (node, layer)
    return np.moveaxis(raw, (node_axis, layer_axis), (0, 1))


def iter_selected_times(file_time_count: int, stride: int) -> np.ndarray:
    """回傳單一檔案內要處理的時間索引。

    time_stride 以檔案內時間步為單位。日檔若為 hourly，stride=3 即代表約每
    3 小時取樣一次；若資料時間解析度不同，應先用檢查腳本確認。這個設定會
    直接影響輸出時間解析度、動畫幀數與中間檔大小。
    """

    # 回傳在單一文件中要處理的時間索引，stride 控制抽樣率（以原始檔案的 time 步為單位）
    return np.arange(0, file_time_count, max(stride, 1), dtype=np.int64)


def process_month(
    files: list[Path],
    output_dir: Path,
    domain: Domain,
    year: int,
    month: int,
    time_stride: int,
    include_zcor_time: bool,
    include_elev: bool,
    land_geojson: Path | None,
) -> None:
    """處理完整月資料並寫出中間檔。

    輸出陣列形狀固定為 `time, layer, lat, lon`。這種排列讓動畫可快速沿 time
    讀取，也讓後續研究區域分割能直接在 layer/lat/lon 軸上計算統計特徵。
    include_zcor_time 控制是否額外保存完整逐時 `zcor.npy`；此檔可呈現水位
    與 sigma/z 層位逐時變動，但大小約與單一流速分量相同，因此預設不輸出。
    include_elev 控制是否讀取原始 `elev(time, node)` 並輸出成
    `elev.npy(time, lat, lon)`；這個欄位是自由水面高度 η，適合用作 2D
    表層流場底圖色階，但不應和流速 `speed` 共用單位或 colorbar。
    land_geojson 是選用的外部陸域面狀圖資；若提供，會在原始 mesh/wetdry 遮罩
    之外再扣除落在 GeoJSON polygon 內的格點，修正原始 mesh 在陸地內殘留的
    假有效格點。
    """

    # 建立輸出資料夾與目標格點
    output_dir.mkdir(parents=True, exist_ok=True)
    lon, lat, target_points = build_target_grid(domain)
    grid_shape = (lat.size, lon.size)

    # 優先使用原始 SCHISM/UGRID 水平元素建立插值權重，因為原始 mesh 能保留
    # 陸地洞與海岸邊界；若檔案缺少 face connectivity，才退回 Delaunay 快速插值。
    node_window = select_source_nodes(files[0], domain)
    try:
        mesh_weights = build_mesh_interpolation_weights(files[0], target_points, grid_shape)
        interpolation_weights: InterpolationWeights | None = None
        interpolation_method = "ugrid_face_nodes"
    except KeyError:
        triangulation = Delaunay(node_window.points)
        interpolation_weights = build_interpolation_weights(triangulation, target_points)
        mesh_weights = None
        interpolation_method = "delaunay_source_window"

    # 從第一個檔案取得變數格式與層數資訊（用來配置陣列大小與推斷 layer_count）
    with Dataset(files[0]) as first:
        hvel = first.variables["hvel"]
        node_axis, layer_axis = infer_hvel_layout(first)
        # 讀取第一個時間步並清理缺值後，將維度整理以取得 layer_count
        sample_time = clean_missing_values(hvel[0], getattr(hvel, "missing_value", None))
        normalized = normalize_hvel_slice(sample_time, node_axis, layer_axis)
        layer_count = normalized.shape[1]
        # 讀取 depth 變數，作為插值成 bathymetry 的來源
        depth_var = first.variables["depth"]
        depth_values = clean_missing_values(depth_var[:], getattr(depth_var, "missing_value", None))
        if include_elev and "elev" not in first.variables:
            # `elev` 是 η（自由水面高度）的原始節點欄位；若使用者要求輸出
            # 水位底圖卻缺少此變數，必須在前處理階段明確中止，避免後續視覺化
            # 用 zcor 表層或其它欄位假冒 η 而造成物理意義錯置。
            raise KeyError("Requested --include-elev but source NetCDF does not contain elev(time, node).")

    # 讀取 sigma（若存在）或回傳層索引
    sigma = read_sigma_or_layer_axis(files[0], layer_count)
    # 將 depth（節點上）插值到目標規則格點以得到 bathymetry。使用原始 mesh 權重時，
    # mask 表示目標點落在 SCHISM 水平元素內；退回 Delaunay 時才以水深有限值近似海域。
    if mesh_weights is not None:
        bathymetry = apply_mesh_interpolation(depth_values, mesh_weights, grid_shape)
        mask = mesh_weights.valid.reshape(grid_shape) & np.isfinite(bathymetry)
    else:
        bathymetry = apply_interpolation(depth_values[node_window.indices], interpolation_weights, grid_shape)
        mask = np.isfinite(bathymetry)

    # 選用外部 GeoJSON 陸域遮罩。這一步在時間迴圈前完成，因為行政區/岸線 polygon
    # 是靜態地理遮罩，不隨時間改變；先更新 mask 可讓後續 hvel/zcor/wetdry 全部沿用
    # 同一套海域定義，避免同一格點在不同輸出檔有不一致的海陸語意。
    land_geojson_summary: dict[str, Any] | None = None
    if land_geojson is not None:
        land_mask, land_geojson_summary = build_geojson_land_mask(land_geojson, target_points, grid_shape)
        ocean_cells_before_land_mask = int(mask.sum())
        mask = mask & ~land_mask
        # bathymetry 在陸地遮罩內改為 NaN，讓下游若直接讀 bathymetry 也不會把
        # GeoJSON 已判定為陸地的位置誤解為可用水深。
        bathymetry = np.asarray(bathymetry, dtype=np.float32).copy()
        bathymetry[land_mask] = np.nan
        land_geojson_summary.update(
            {
                "ocean_grid_cell_count_before": ocean_cells_before_land_mask,
                "ocean_grid_cell_count_after": int(mask.sum()),
                "masked_ocean_grid_cell_count": ocean_cells_before_land_mask - int(mask.sum()),
            }
        )
    # 準備暫存容器：時間列表、每個時間的 u/v frames、選擇性的逐時 zcor，以及 zcor 的累加器
    times: list[str] = []
    u_frames: list[np.ndarray] = []
    v_frames: list[np.ndarray] = []
    zcor_frames: list[np.ndarray] = []
    elev_frames: list[np.ndarray] = []
    zcor_sum = np.zeros((layer_count, lat.size, lon.size), dtype=np.float64)
    zcor_count = np.zeros((layer_count, lat.size, lon.size), dtype=np.int32)
    wetdry_elem_applied = False
    wetdry_dry_grid_cell_count = 0

    # 逐檔案處理：每個日檔可能包含多個時間步，使用 iter_selected_times 控制抽樣
    for file_index, path in enumerate(files, start=1):
        with Dataset(path) as ds:
            selected_times = iter_selected_times(len(ds.dimensions["time"]), time_stride)
            # 解析時間成 ISO 字串並加入總時間序列
            times.extend(parse_times(ds, selected_times))
            hvel_var = ds.variables["hvel"]
            zcor_var = ds.variables["zcor"] if "zcor" in ds.variables else None
            elev_var = ds.variables["elev"] if include_elev and "elev" in ds.variables else None
            if include_elev and elev_var is None:
                # 每個日檔都必須提供 elev，否則輸出的 elev.npy 時間軸會和
                # u/v/speed 不一致。這裡不允許跳過缺檔，讓資料問題在產製時就暴露。
                raise KeyError(f"Requested --include-elev but {path.name} does not contain elev.")
            wetdry_var = ds.variables["wetdry_elem"] if mesh_weights is not None and "wetdry_elem" in ds.variables else None
            for time_index in selected_times:
                # wetdry_elem 是逐時、逐水平元素的乾濕旗標。靜態 mask 只表示格點落在
                # 原始水體 mesh 內；time_mask 進一步排除該時間步乾出的元素，避免潮間帶
                # 或淺灘暫時乾出時仍被畫成有流速。若來源檔沒有 wetdry_elem，則沿用靜態 mask。
                time_mask = mask
                if wetdry_var is not None:
                    time_mask = build_wetdry_ocean_mask(
                        wetdry_var[time_index],
                        mesh_weights,
                        mask,
                        grid_shape,
                        getattr(wetdry_var, "missing_value", None),
                    )
                    wetdry_elem_applied = True
                    wetdry_dry_grid_cell_count += int(mask.sum() - time_mask.sum())

                # 讀取該時間步的 hvel，將 sentinel 轉 NaN
                raw_hvel = clean_missing_values(hvel_var[time_index], getattr(hvel_var, "missing_value", None))
                # 將維度整理為 (node, layer, component)
                velocity = normalize_hvel_slice(raw_hvel, node_axis, layer_axis)

                # 為每層建立容器，稍後會填入插值後的格點值
                u_layers = np.empty((layer_count, lat.size, lon.size), dtype=np.float32)
                v_layers = np.empty_like(u_layers)

                # 對每一層進行水平插值（來源值在 node_window.indices 的順序）
                for layer in range(layer_count):
                    if mesh_weights is not None:
                        u_layers[layer] = apply_mesh_interpolation(velocity[:, layer, 0], mesh_weights, grid_shape)
                        v_layers[layer] = apply_mesh_interpolation(velocity[:, layer, 1], mesh_weights, grid_shape)
                    else:
                        u_layers[layer] = apply_interpolation(
                            velocity[node_window.indices, layer, 0], interpolation_weights, grid_shape
                        )
                        v_layers[layer] = apply_interpolation(
                            velocity[node_window.indices, layer, 1], interpolation_weights, grid_shape
                        )
                    # 使用逐時 time_mask 將陸地、mesh 外、無效海域或 dry element 設為 NaN。
                    # 這裡會直接影響輸出的 u/v/speed，因此後續繪圖不需要再讀 wetdry_elem。
                    u_layers[layer, ~time_mask] = np.nan
                    v_layers[layer, ~time_mask] = np.nan

                # 將這個時間步的層資料加入 frames 列表（稍後會沿 time 軸堆疊）
                u_frames.append(u_layers)
                v_frames.append(v_layers)

                if elev_var is not None:
                    # elev 是 SCHISM/OCM 原始輸出的自由水面高度 η，來源維度為
                    # (time, node)。它位於水平節點而非垂向層，因此插值後輸出
                    # 只需要 (lat, lon)。此欄位的單位在原檔未明寫，但數值範圍
                    # 為公尺等級，且物理上代表相對基準面的海表面高度變化。
                    raw_elev = clean_missing_values(elev_var[time_index], getattr(elev_var, "missing_value", None))
                    if mesh_weights is not None:
                        elev_grid = apply_mesh_interpolation(raw_elev, mesh_weights, grid_shape)
                    else:
                        elev_grid = apply_interpolation(raw_elev[node_window.indices], interpolation_weights, grid_shape)
                    # 和速度場使用相同的逐時乾濕遮罩：乾出元素或陸域不應被水位底圖上色，
                    # 否則 GIF 會在無效海域顯示 η，造成「有水位資料」的錯覺。
                    elev_grid[~time_mask] = np.nan
                    elev_frames.append(elev_grid.astype(np.float32, copy=False))

                # 若有 zcor，插值並累加以計算月平均 zcor
                if zcor_var is not None:
                    raw_zcor = clean_missing_values(zcor_var[time_index], getattr(zcor_var, "missing_value", None))
                    zcor = normalize_zcor_slice(raw_zcor, node_axis, layer_axis)
                    # zcor_layers 代表此時間步所有 layer 在目標規則格點上的實際 z 座標。
                    # 若 include_zcor_time=True，稍後會保存成 `zcor.npy`，供 3D 時間動畫使用。
                    zcor_layers = np.empty((layer_count, lat.size, lon.size), dtype=np.float32)
                    for layer in range(layer_count):
                        if mesh_weights is not None:
                            z_grid = apply_mesh_interpolation(zcor[:, layer], mesh_weights, grid_shape)
                        else:
                            z_grid = apply_interpolation(zcor[node_window.indices, layer], interpolation_weights, grid_shape)
                        valid = np.isfinite(z_grid) & time_mask
                        zcor_sum[layer, valid] += z_grid[valid]
                        zcor_count[layer, valid] += 1
                        zcor_layers[layer] = z_grid
                        zcor_layers[layer, ~time_mask] = np.nan
                    if include_zcor_time:
                        zcor_frames.append(zcor_layers)
        # 列印簡單進度資訊以利監控長時間運算
        print(f"processed {file_index}/{len(files)}: {path.name}")

    # 將 frame 列表堆疊為 (time, layer, lat, lon) 的陣列
    u = np.stack(u_frames, axis=0)
    v = np.stack(v_frames, axis=0)
    # 計算水平流速 magnitude
    speed = np.sqrt(u * u + v * v, dtype=np.float32)
    elev = np.stack(elev_frames, axis=0) if include_elev else None
    if include_elev and (elev is None or elev.shape[0] != len(times)):
        # elev 必須與 time_iso、u/v/speed 完全同一個時間軸。若數量不一致，
        # 代表某些檔案或時間步缺少水位資料，後續動畫會錯幀，因此主動失敗。
        raise ValueError("Requested --include-elev but collected elev frames do not match selected times.")

    # 計算 zcor 的月平均（僅對有累計的點計算）
    zcor_mean = np.full_like(zcor_sum, np.nan, dtype=np.float32)
    valid_z = zcor_count > 0
    zcor_mean[valid_z] = (zcor_sum[valid_z] / zcor_count[valid_z]).astype(np.float32)

    # 寫出各種中間檔供後續視覺化使用，保留必要的資料型別以節省空間
    np.save(output_dir / "lon.npy", lon.astype(np.float32))
    np.save(output_dir / "lat.npy", lat.astype(np.float32))
    np.save(output_dir / "sigma.npy", sigma.astype(np.float32))
    np.save(output_dir / "time_iso.npy", np.asarray(times, dtype="U32"))
    np.save(output_dir / "u.npy", u)
    np.save(output_dir / "v.npy", v)
    np.save(output_dir / "speed.npy", speed)
    np.save(output_dir / "bathymetry.npy", bathymetry)
    np.save(output_dir / "mask.npy", mask)
    np.save(output_dir / "zcor_mean.npy", zcor_mean)
    if elev is not None:
        # elev.npy 是 η（自由水面高度）的規則格點版本，形狀為 (time, lat, lon)。
        # 它和 surface speed 的 shape 不同，因為 η 沒有垂向 layer 維度；視覺化時
        # 只能當作背景標量場，不應拿來替代流速 magnitude。
        np.save(output_dir / "elev.npy", elev.astype(np.float32, copy=False))
    if include_zcor_time:
        if len(zcor_frames) != len(times):
            raise ValueError("Requested --include-zcor-time but source files do not provide complete zcor time frames.")
        np.save(output_dir / "zcor.npy", np.stack(zcor_frames, axis=0))

    # 撰寫摘要 JSON，包含時間、格點、層數與速度統計等 metadata
    summary = {
        "year": year,
        "month": month,
        "domain": asdict(domain),
        "input_files": [str(path) for path in files],
        "time_stride": time_stride,
        "time_count": len(times),
        "layer_count": int(layer_count),
        "grid": {"lat_count": int(lat.size), "lon_count": int(lon.size)},
        "time_start": times[0] if times else None,
        "time_end": times[-1] if times else None,
        "speed_m_per_s": {
            "mean": float(np.nanmean(speed)),
            "p95": float(np.nanpercentile(speed, 95)),
            "max": float(np.nanmax(speed)),
        },
        "elev_m": {
            "included": bool(elev is not None),
            "source_variable": "elev",
            "meaning": "η / sea-surface elevation on SCHISM nodes, interpolated to time, lat, lon.",
            "shape": list(elev.shape) if elev is not None else None,
            "mean": float(np.nanmean(elev)) if elev is not None else None,
            "p05": float(np.nanpercentile(elev, 5)) if elev is not None else None,
            "p95": float(np.nanpercentile(elev, 95)) if elev is not None else None,
            "min": float(np.nanmin(elev)) if elev is not None else None,
            "max": float(np.nanmax(elev)) if elev is not None else None,
        },
        "valid_ocean_fraction": float(np.isfinite(speed).mean()),
        "wetdry_elem": {
            "applied": bool(wetdry_elem_applied),
            "convention": "0=wet, nonzero/dry or missing=masked",
            "dry_grid_cell_count_over_selected_times": int(wetdry_dry_grid_cell_count),
        },
        "notes": [
            "u/v/speed arrays use shape time, layer, lat, lon.",
            "zcor_mean is monthly mean vertical coordinate for visual reference, not a fixed-depth remap.",
            f"Horizontal interpolation method: {interpolation_method}.",
            "mask.npy marks target cells inside the original water mesh when UGRID face connectivity is available.",
            "wetdry_elem is applied per selected time step when available; dry elements are written as NaN in u/v/speed/zcor.",
        ],
        "outputs": {
            "zcor_time": bool(include_zcor_time),
            "zcor_time_file": "zcor.npy" if include_zcor_time else None,
            "zcor_time_shape": [len(times), int(layer_count), int(lat.size), int(lon.size)]
            if include_zcor_time
            else None,
            "elev": bool(elev is not None),
            "elev_file": "elev.npy" if elev is not None else None,
            "elev_shape": [len(times), int(lat.size), int(lon.size)] if elev is not None else None,
        },
    }
    if land_geojson_summary is not None:
        # 只有使用者明確傳入 --land-geojson 時才把外部遮罩資訊寫入 metadata。
        # 未啟用時維持舊版 monthly_summary 結構，避免原有批次流程或下游檢查腳本
        # 因多出的欄位與 note 產生不必要差異。
        summary["land_geojson"] = {
            "applied": True,
            "source": land_geojson_summary,
            "semantics": "GeoJSON Polygon/MultiPolygon cells are removed from mask.npy and written as NaN in bathymetry/u/v/speed/elev/zcor when present.",
        }
        summary["notes"].append(
            "Optional --land-geojson subtracts static land polygons from mask.npy before all time-varying arrays are written."
        )
    (output_dir / "monthly_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_bbox(values: Iterable[float]) -> tuple[float, float, float, float]:
    """解析 bbox 並檢查經緯度順序。"""

    lon_min, lon_max, lat_min, lat_max = values
    if lon_min >= lon_max or lat_min >= lat_max:
        raise ValueError("bbox must be lon_min lon_max lat_min lat_max")
    return lon_min, lon_max, lat_min, lat_max


def parse_args() -> argparse.Namespace:
    """解析月處理命令列參數。"""

    parser = argparse.ArgumentParser(description="Preprocess monthly OCM/SCHISM NetCDF files.")
    parser.add_argument("--input-dir", required=True, type=Path, help="Directory containing *_schout.nc files.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for monthly preprocessed arrays.")
    parser.add_argument("--year", required=True, type=int, help="Data year for metadata.")
    parser.add_argument("--month", required=True, type=int, help="Data month for metadata.")
    parser.add_argument("--domain-id", default="taiwan-surrounding", help="Domain identifier for metadata.")
    parser.add_argument("--bbox", nargs=4, type=float, default=(119.0, 123.0, 20.0, 27.0), metavar=("LON_MIN", "LON_MAX", "LAT_MIN", "LAT_MAX"))
    parser.add_argument("--target-resolution-km", type=float, default=10.0, help="Regular output grid spacing in km.")
    parser.add_argument("--source-margin-deg", type=float, default=0.25, help="Extra source-node margin around bbox in degrees.")
    parser.add_argument("--time-stride", type=int, default=3, help="Use every Nth time step from each daily file.")
    parser.add_argument(
        "--include-zcor-time",
        action="store_true",
        help="Save full time-varying zcor.npy for 3D animations that need water-level/layer motion.",
    )
    parser.add_argument(
        "--include-elev",
        action="store_true",
        help="Save elev.npy (η / sea-surface elevation) on the regular grid for water-level background maps.",
    )
    parser.add_argument(
        "--land-geojson",
        type=Path,
        help=(
            "Optional local GeoJSON Polygon/MultiPolygon file used as a static land mask. "
            "Cells inside land polygons are removed from mask.npy and written as NaN in outputs."
        ),
    )
    parser.add_argument("--max-files", type=int, help="Optional cap for quick tests.")
    return parser.parse_args()


def main() -> None:
    """執行月資料前處理。"""

    args = parse_args()
    lon_min, lon_max, lat_min, lat_max = parse_bbox(args.bbox)
    domain = Domain(
        domain_id=args.domain_id,
        lon_min=lon_min,
        lon_max=lon_max,
        lat_min=lat_min,
        lat_max=lat_max,
        target_resolution_km=args.target_resolution_km,
        source_margin_deg=args.source_margin_deg,
    )
    files = list_month_files(args.input_dir, args.max_files)
    # 外部陸域遮罩必須是本機可讀檔案；前處理不在執行中下載遠端 URL，因為批次
    # 月資料處理需要可重現的圖資版本與穩定 I/O，避免網路狀態改變輸出結果。
    land_geojson = args.land_geojson
    if land_geojson is not None and not land_geojson.exists():
        raise FileNotFoundError(f"--land-geojson file not found: {land_geojson}")
    process_month(
        files,
        args.output_dir,
        domain,
        args.year,
        args.month,
        args.time_stride,
        args.include_zcor_time,
        args.include_elev,
        land_geojson,
    )


if __name__ == "__main__":
    main()
