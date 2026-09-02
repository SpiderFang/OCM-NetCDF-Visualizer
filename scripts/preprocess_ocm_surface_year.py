"""將多年度 OCM/SCHISM 原始 NetCDF 轉成完整台灣周邊 1 km 表層中間檔。

本模組是參考 GIF「原始 NetCDF → NumPy 中間檔 → GIF」架構的年度表層版本，
專門處理 `/CWA-OCM/<year>/YYYYMMDD_schout.nc` 這種每日原始檔。它只萃取
固定模式層 `layer 047` 的水平流速，避免把 48 層完整資料寫成數百 GB 的年度
中間檔；輸出範圍、規則格點與報告圖一致，使用經度 119–123°E、緯度 20–27°N、
約 1 km 間距的 780 × 409 網格。

主要處理流程如下：

1. 依檔名日期建立指定時間步的標準 UTC 時間軸；`time_step_hours=1` 時保留每日
   24 個原始小時觀測，`time_step_hours=6` 時才抽取 6 小時產品。
2. 以第一個可用檔案的 SCHISM/UGRID face connectivity 建立一次水平插值權重。
3. 對每個原始日檔只讀取固定表層 `hvel`，並套用 `wetdry_elem` 與岸線遮罩。
4. 將有效資料寫入 `.npy` memory-map 中間檔；缺少的日檔先保留為 NaN。
5. 對缺少的日內／日檔時間幀使用前後最近有效時間幀做逐格線性補值，並以
   `imputed.npy` 明確標記；本次三日實際小時觀測流程會先驗證選定日期沒有缺檔，
   因而不應產生任何 imputed frame。

時間修復的核心假設與限制：

- 檔名中的 `YYYYMMDD` 是每日資料的可信日期來源；NetCDF `time` 的 units 基準日
  只做稽核，不直接用來命名年度時間軸，因為部分檔案已確認存在月份偏移。
- 原始每日資料的 24 個時間步仍依序代表 `01:00, 02:00, ..., 24:00` UTC；輸出
  索引依 `time_step_hours` 選取。例如 1 小時流程取索引 0–23，6 小時流程取
  索引 0、6、12、18，分別對應 `01:00–24:00` 與 `01:00、07:00、13:00、19:00`。
- 缺日的線性補值只服務於全域趨勢展示與動畫連續性；原始/補值狀態、缺日清單與
  補值方法都會寫入 metadata，後續研究統計應優先使用 `source_valid.npy` 排除補值。

本檔依賴同資料夾的 `preprocess_ocm_month.py`，重用其中已驗證的 UGRID 插值、
缺值清理與 GeoJSON rasterize 函式；不會修改既有月份前處理行為。
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from netCDF4 import Dataset, num2date

# SERVER 直接以檔案路徑執行時，Python 會把 `scripts/` 放在 import path；本機
# 以 `import scripts.preprocess_ocm_surface_year` 做測試時，則需要從 namespace
# package 路徑載入同一個模組。兩條 import 路徑都指向既有月前處理實作，避免
# 為年度表層流程複製一份容易分歧的 UGRID/GeoJSON 演算法。
try:
    from preprocess_ocm_month import (
        Domain,
        InterpolationWeights,
        MeshWeights,
        NodeWindow,
        apply_interpolation,
        apply_mesh_interpolation,
        build_geojson_land_mask,
        build_interpolation_weights,
        build_mesh_interpolation_weights,
        build_target_grid,
        clean_missing_values,
        select_source_nodes,
    )
except ModuleNotFoundError:
    from scripts.preprocess_ocm_month import (
        Domain,
        InterpolationWeights,
        MeshWeights,
        NodeWindow,
        apply_interpolation,
        apply_mesh_interpolation,
        build_geojson_land_mask,
        build_interpolation_weights,
        build_mesh_interpolation_weights,
        build_target_grid,
        clean_missing_values,
        select_source_nodes,
    )


FILENAME_PATTERN = re.compile(r"^(?P<date>\d{8})_schout\.nc$")
"""原始日檔命名規則；日期由檔名擷取，不使用可能偏移的 NetCDF units 基準日。"""

DEFAULT_YEARS = (2024, 2025)
"""本次完整年度產品的目標年份；可由 CLI 覆蓋以便做小量驗證。"""

DEFAULT_TIME_STEP_HOURS = 6
"""預設輸出時間軸間距；每日 24 個小時步會取四個時間幀，可由 CLI 改成 1 小時。"""

DEFAULT_DAILY_FIRST_HOUR = 1
"""OCM 每日第一個有效時間的 UTC 小時，對應原始 time 數值 3600 秒。"""

EXPECTED_GRID_SHAPE = (780, 409)
"""119–123°E、20–27°N 的 1 km 報告圖規則格點形狀 `(lat, lon)`。"""

FIXED_SPEED_COLORBAR_VMIN_M_PER_S = 0.0
"""年度固定流速備查圖的規格下限；不是由原始資料 min/max 推導。"""

FIXED_SPEED_COLORBAR_VMAX_M_PER_S = 2.0
"""年度固定流速備查圖的規格上限；超過值以色階最深端呈現。"""

FIXED_SPEED_COLORBAR_TICKS_M_PER_S = (0.0, 0.5, 1.0, 1.5, 2.0)
"""年度固定流速備查圖的固定刻度，所有影格共用且顯示一位小數。"""

FIXED_SPEED_COLORBAR_LABEL = "流速(公尺/秒)"
"""固定流速備查圖的色條標籤；`eta` 不屬於本產品的色彩物理量。"""


@dataclass(frozen=True)
class FileInfo:
    """單一原始日檔的日期、時間稽核與可用時間步資訊。

    `filename_date` 是重建年度時間軸使用的日期；`decoded_first/last` 只描述
    原始 NetCDF `time` 依其 units 解碼後的結果，用於揭露偏移而不參與修復。
    `time_count` 與 `selected_indices` 讓缺少部分日內時間步的檔案也能被標記，
    不會默默把相鄰時間資料錯配到年度時間軸。
    """

    path: Path
    filename_date: date
    time_count: int
    time_units: str | None
    time_calendar: str | None
    raw_first_value: float | None
    raw_last_value: float | None
    decoded_first: str | None
    decoded_last: str | None
    decoded_date_offset_days: int | None
    selected_indices: tuple[int, ...]


@dataclass(frozen=True)
class FrameRecord:
    """指定輸出時間軸上的一個輸出幀。

    `source_index` 是該日 NetCDF 的 time 索引；當原始檔或該索引不存在時為 None，
    後續會在 `.npy` 中保留空幀並執行可追溯的時間補值。對本次小時版，該索引
    每一幀都直接對應原始日檔的實際 `time` index，不由影片 renderer 產生內插場。
    """

    frame_index: int
    timestamp: str
    calendar_date: str
    source_file: str | None
    source_index: int | None
    status: str


@dataclass(frozen=True)
class HvelLayout:
    """固定表層 `hvel` 讀取所需的 NetCDF 軸位置。

    原始 OCM 檔目前為 `(time, node, layer, two)`，但以維度名稱推斷軸位置可
    避免未來資料提供者調整維度排列時，把 layer 或東西/南北分量讀錯。
    """

    dimensions: tuple[str, ...]
    time_axis: int
    node_axis: int
    layer_axis: int
    component_axis: int
    layer_count: int
    node_count: int


def parse_filename_date(path: Path) -> date | None:
    """由 `YYYYMMDD_schout.nc` 檔名擷取日期。

    不符合命名規則的檔案會被忽略，避免把暫存檔、不同年度資料或人工複製檔
    混入年度時間軸。日期本身不代表 NetCDF 內部 `time` 的 units 基準日。
    """

    match = FILENAME_PATTERN.match(path.name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group("date"), "%Y%m%d").date()
    except ValueError:
        return None


def list_year_files(source_root: Path, year: int) -> dict[date, Path]:
    """列出指定年份可用的原始日檔，並檢查日期是否重複。

    輸入根目錄預期含有 `/CWA-OCM/2024`、`/CWA-OCM/2025` 等年份子目錄。
    回傳以檔名日期為 key 的 mapping；缺少日期不在這裡補檔，而由後續標準時間軸
    與補值流程處理。若同一天有兩個檔案，直接失敗以避免資料來源不明。
    """

    year_dir = source_root / str(year)
    if not year_dir.is_dir():
        raise FileNotFoundError(f"source year directory not found: {year_dir}")

    files_by_date: dict[date, Path] = {}
    for path in sorted(year_dir.glob("*_schout.nc")):
        file_date = parse_filename_date(path)
        if file_date is None or file_date.year != year:
            continue
        if file_date in files_by_date:
            raise ValueError(f"duplicate source date {file_date}: {files_by_date[file_date]} and {path}")
        files_by_date[file_date] = path
    if not files_by_date:
        raise FileNotFoundError(f"no YYYYMMDD_schout.nc files found for year {year_dir}")
    return files_by_date


def _safe_float(value: Any) -> float | None:
    """把 NetCDF scalar 轉成可寫入 JSON 的有限 float。

    時間變數可能包含 masked、NaN 或非數值 sentinel；稽核資訊保留為 None 比
    強制轉成 0 更安全，因為 0 會看起來像真實時間。
    """

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _iso_value(value: Any) -> str | None:
    """將 `datetime`/`cftime` 時間物件轉成不含時區名稱的 ISO 字串。"""

    return value.isoformat() if hasattr(value, "isoformat") else None


def inspect_source_file(path: Path, time_step_hours: int) -> FileInfo:
    """讀取單一檔案的時間 metadata，不讀取大型流速陣列。

    `time` 只用於稽核 units 基準日、日內步數與數值範圍；年度標準時間仍以檔名
    日期與固定的 OCM 日內 1–24 小時慣例重建。這是處理 2024-04/05 偏移的關鍵。
    """

    file_date = parse_filename_date(path)
    if file_date is None:
        raise ValueError(f"cannot parse date from source filename: {path.name}")
    with Dataset(path, "r") as ds:
        if "time" not in ds.variables or "time" not in ds.dimensions:
            raise KeyError(f"source file has no time variable/dimension: {path}")
        time_var = ds.variables["time"]
        time_count = len(ds.dimensions["time"])
        raw_values = np.asarray(time_var[:], dtype=np.float64)
        units_attr = getattr(time_var, "units", None)
        calendar_attr = getattr(time_var, "calendar", "standard")
        units = str(units_attr) if units_attr else None
        calendar = str(calendar_attr) if calendar_attr else "standard"

        decoded_first = None
        decoded_last = None
        decoded_offset = None
        if units and raw_values.size:
            try:
                decoded = num2date(
                    raw_values[[0, -1]],
                    units=units,
                    calendar=calendar,
                    only_use_cftime_datetimes=False,
                )
                decoded_first = _iso_value(decoded[0])
                decoded_last = _iso_value(decoded[-1])
                decoded_date = decoded[0].date()
                decoded_offset = int((decoded_date - file_date).days)
            except (TypeError, ValueError, OverflowError, RuntimeError):
                # 稽核不能阻止流速資料處理；若時間 metadata 壞掉，後續仍會以檔名日期
                # 和固定日內索引建立標準軸，並在 JSON 中保留 decoded 欄位為 null。
                decoded_first = None
                decoded_last = None
                decoded_offset = None

    # 原始檔是每小時 24 筆；若時間步數不足，保留存在的索引，缺少的 slot 由補值流程
    # 處理。這裡不把任意額外時間步靜默壓到 6 小時位置。
    selected_indices = tuple(
        index for index in range(0, 24, time_step_hours) if index < time_count
    )
    return FileInfo(
        path=path,
        filename_date=file_date,
        time_count=time_count,
        time_units=units,
        time_calendar=calendar,
        raw_first_value=_safe_float(raw_values[0]) if raw_values.size else None,
        raw_last_value=_safe_float(raw_values[-1]) if raw_values.size else None,
        decoded_first=decoded_first,
        decoded_last=decoded_last,
        decoded_date_offset_days=decoded_offset,
        selected_indices=selected_indices,
    )


def iter_dates(start_date: date, end_date: date) -> Iterable[date]:
    """逐日產生包含閏年的指定日曆範圍。"""

    if start_date > end_date:
        raise ValueError("start_date must not be later than end_date")
    current = start_date
    end = end_date
    while current <= end:
        yield current
        current += timedelta(days=1)


def build_frame_plan(
    file_infos: dict[date, FileInfo],
    start_date: date,
    end_date: date,
    time_step_hours: int,
    first_hour: int,
) -> list[FrameRecord]:
    """以檔名日期建立連續的標準 UTC 時間幀計畫。

    每日時間幀的數量由 `24 / time_step_hours` 決定；6 小時產品為 4 幀，1 小時
    產品為 24 幀。若日檔缺少，或檔案內沒有對應的 time index，該幀標為
    `missing_source`，不會把下一日的資料往前填入造成時間錯位。
    """

    if time_step_hours <= 0 or 24 % time_step_hours != 0:
        raise ValueError("time_step_hours must be a positive divisor of 24")
    if not 0 <= first_hour < 24:
        raise ValueError("first_hour must be in [0, 24)")

    frames: list[FrameRecord] = []
    frame_index = 0
    for calendar_date in iter_dates(start_date, end_date):
        info = file_infos.get(calendar_date)
        for slot, source_index in enumerate(range(0, 24, time_step_hours)):
            hour = first_hour + slot * time_step_hours
            timestamp = datetime.combine(calendar_date, datetime.min.time()) + timedelta(hours=hour)
            source_available = info is not None and source_index < info.time_count
            frames.append(
                FrameRecord(
                    frame_index=frame_index,
                    timestamp=timestamp.isoformat(timespec="seconds"),
                    calendar_date=calendar_date.isoformat(),
                    source_file=str(info.path) if source_available and info is not None else None,
                    source_index=source_index if source_available else None,
                    status="source_available" if source_available else "missing_source",
                )
            )
            frame_index += 1
    return frames


def infer_hvel_layout(path: Path) -> HvelLayout:
    """由 hvel 維度名稱推斷 time/node/layer/component 軸。

    目前資料是 `(time, nSCHISM_hgrid_node, nSCHISM_vgrid_layers, two)`；使用維度
    名稱而不是固定軸號，可在不改變物理解讀的前提下容忍軸順序調整。
    """

    with Dataset(path, "r") as ds:
        if "hvel" not in ds.variables:
            raise KeyError(f"source file has no hvel variable: {path}")
        variable = ds.variables["hvel"]
        dimensions = tuple(variable.dimensions)
        sizes = tuple(int(size) for size in variable.shape)

    def find_axis(predicate: Any, description: str) -> int:
        matches = [index for index, name in enumerate(dimensions) if predicate(name, sizes[index])]
        if len(matches) != 1:
            raise ValueError(f"cannot infer unique {description} axis from {dimensions} / {sizes}")
        return matches[0]

    time_axis = find_axis(lambda name, _size: name.lower() == "time", "time")
    node_axis = find_axis(lambda name, _size: "node" in name.lower(), "node")
    layer_axis = find_axis(
        lambda name, _size: "vgrid" in name.lower() or "layer" in name.lower(),
        "vertical layer",
    )
    component_axis = find_axis(
        lambda name, size: name.lower() == "two" or size == 2,
        "horizontal component",
    )
    if len({time_axis, node_axis, layer_axis, component_axis}) != 4:
        raise ValueError(f"hvel axes overlap: {dimensions}")
    return HvelLayout(
        dimensions=dimensions,
        time_axis=time_axis,
        node_axis=node_axis,
        layer_axis=layer_axis,
        component_axis=component_axis,
        layer_count=sizes[layer_axis],
        node_count=sizes[node_axis],
    )


def read_surface_hvel(
    variable: Any,
    layout: HvelLayout,
    time_index: int,
    layer_index: int,
) -> np.ndarray:
    """只讀取一個時間與固定 layer 的 `(node, component)` hvel。

    原始 hvel 以 time-chunk 儲存，底層 HDF5 可能仍需解壓完整 time chunk；但此函式
    不會把其它 47 個垂向層留在 Python 記憶體或輸出陣列，將年度中間檔維持在可管理
    的表層大小。回傳最後一軸 0/1 分別代表東向、北向速度，單位沿用原始 m/s。
    """

    if layer_index < 0 or layer_index >= layout.layer_count:
        raise IndexError(f"layer_index {layer_index} outside {layout.layer_count} layers")
    selectors: list[Any] = [slice(None)] * len(layout.dimensions)
    selectors[layout.time_axis] = int(time_index)
    selectors[layout.layer_axis] = int(layer_index)
    raw = np.asarray(variable[tuple(selectors)])
    remaining_dimensions = [
        name
        for axis, name in enumerate(layout.dimensions)
        if axis not in (layout.time_axis, layout.layer_axis)
    ]
    remaining_sizes = list(raw.shape)
    node_axis = remaining_dimensions.index(layout.dimensions[layout.node_axis])
    component_axis = remaining_dimensions.index(layout.dimensions[layout.component_axis])
    normalized = np.moveaxis(raw, (node_axis, component_axis), (0, 1))
    if normalized.ndim != 2 or normalized.shape[1] != 2:
        raise ValueError(f"surface hvel did not normalize to (node, 2): {normalized.shape}")
    return normalized


def create_memmap(path: Path, shape: tuple[int, ...]) -> np.memmap:
    """建立未初始化填值的 `.npy` memory-map。

    年度 1 km 表層陣列約為 `(2924, 780, 409)`；若先把整個陣列寫成 NaN，SERVER
    的 NFS 會額外執行數十 GB 的初始化 I/O，且增加無意義的 RAM/dirty-page 壓力。
    本流程會逐幀覆寫所有 source-valid 幀，缺日幀則由 temporal imputation 寫入；
    最終若仍有未填幀才明確寫成 NaN。因此不初始化不會改變完成產品的缺值語意，
    但能讓長時間前處理更快開始讀取 NetCDF。
    """

    return np.lib.format.open_memmap(path, mode="w+", dtype=np.float32, shape=shape)


def interpolate_source_field(
    source_values: np.ndarray,
    mesh_weights: MeshWeights | None,
    interpolation_weights: InterpolationWeights | None,
    node_window: NodeWindow,
    grid_shape: tuple[int, int],
) -> np.ndarray:
    """將一個節點欄位插值到規則 `(lat, lon)` 網格。

    UGRID face weights 優先保留原始海岸洞；只有輸入檔缺少 face connectivity 時，
    才使用 Delaunay source window。兩條路徑都回傳 float32，降低年度暫存記憶體量。
    """

    if mesh_weights is not None:
        return apply_mesh_interpolation(source_values, mesh_weights, grid_shape)
    if interpolation_weights is None:
        raise RuntimeError("interpolation weights are unavailable")
    return apply_interpolation(source_values[node_window.indices], interpolation_weights, grid_shape)


def _finite_linear_fill(left: np.ndarray, right: np.ndarray, alpha: float) -> np.ndarray:
    """逐格在前後兩個流場間線性補值，並保守處理局部 NaN。

    若兩端都有有限值，使用 `left + alpha * (right-left)`；只有一端有限時沿用
    該端；兩端都無效時維持 NaN。這可避免 wet/dry 或近岸缺值在補值過程被填成
    虛假的零流速。
    """

    left_valid = np.isfinite(left)
    right_valid = np.isfinite(right)
    output = np.full(left.shape, np.nan, dtype=np.float32)
    both = left_valid & right_valid
    output[both] = (left[both] + np.float32(alpha) * (right[both] - left[both])).astype(np.float32)
    output[left_valid & ~right_valid] = left[left_valid & ~right_valid]
    output[right_valid & ~left_valid] = right[right_valid & ~left_valid]
    return output


def impute_missing_frames(
    u: np.memmap,
    v: np.memmap,
    speed: np.memmap,
    source_valid: np.ndarray,
    imputed: np.ndarray,
) -> None:
    """以最近前後有效幀補齊缺日時段，並直接更新 memory-map。

    補值使用標準時間軸的 frame index 計算 alpha，因此跨越單日或連續缺日都能
    保持時間比例。此函式只處理整個輸出幀缺少原始檔的情況；原始有效幀內部的
    wet/dry NaN 不會被填補，避免改寫模式的局部乾濕語意。
    """

    valid_indices = np.flatnonzero(source_valid)
    missing_indices = np.flatnonzero(~source_valid)
    if valid_indices.size == 0:
        raise RuntimeError("no valid source frames are available for temporal imputation")

    for frame_index in missing_indices:
        right_position = int(np.searchsorted(valid_indices, frame_index, side="right"))
        left_position = right_position - 1
        left_index = int(valid_indices[left_position]) if left_position >= 0 else None
        right_index = int(valid_indices[right_position]) if right_position < valid_indices.size else None

        if left_index is None:
            u[frame_index] = u[right_index]
            v[frame_index] = v[right_index]
        elif right_index is None:
            u[frame_index] = u[left_index]
            v[frame_index] = v[left_index]
        else:
            alpha = (frame_index - left_index) / float(right_index - left_index)
            u[frame_index] = _finite_linear_fill(u[left_index], u[right_index], alpha)
            v[frame_index] = _finite_linear_fill(v[left_index], v[right_index], alpha)
        speed[frame_index] = np.sqrt(u[frame_index] * u[frame_index] + v[frame_index] * v[frame_index], dtype=np.float32)
        imputed[frame_index] = True

    u.flush()
    v.flush()
    speed.flush()


def sampled_speed_statistics(
    speed: np.memmap,
    source_valid: np.ndarray,
    time_sample_limit: int = 256,
    spatial_stride: int = 4,
) -> dict[str, float | int]:
    """以可控記憶體成本估算原始幀流速統計與箭頭品質查核值。

    逐幀掃描全部 1 km 格點會產生數 GB 的暫時陣列；本函式固定抽取最多 256 個
    原始幀及每四格一點，提供 P98/P99 與抽樣最大值供箭頭縮放和資料品質查核。
    固定流速 colorbar 的 0.0–2.0 m/s 上下限由產品規格指定，不使用本函式的
    percentile 或最大值；metadata 會記錄抽樣方式，避免把抽樣統計宣稱為逐格精確
    全域統計。
    """

    valid_indices = np.flatnonzero(source_valid)
    if valid_indices.size == 0:
        raise RuntimeError("cannot compute speed statistics without source-valid frames")
    step = max(1, int(math.ceil(valid_indices.size / time_sample_limit)))
    sampled_values = []
    for frame_index in valid_indices[::step]:
        sampled_values.append(np.asarray(speed[int(frame_index), ::spatial_stride, ::spatial_stride]).ravel())
    values = np.concatenate(sampled_values)
    values = values[np.isfinite(values)]
    if values.size == 0:
        raise RuntimeError("source-valid speed frames contain no finite values")
    p98 = float(np.percentile(values, 98.0))
    p99 = float(np.percentile(values, 99.0))
    maximum = float(np.max(values))
    diagnostic_vmax = max(0.5, math.ceil(maximum if maximum < 2.0 else p98 * 1.15))
    # 這個值只作為資料品質與箭頭尺度的診斷參考；固定 colorbar 另由產品規格常數
    # 指定為 0.0–2.0 m/s，不應把此抽樣值誤讀成正式色階上限。
    diagnostic_vmax = max(0.5, math.ceil(diagnostic_vmax * 2.0) / 2.0)
    return {
        "sampled_value_count": int(values.size),
        "sampled_time_stride": int(step),
        "sampled_spatial_stride": int(spatial_stride),
        "p98_m_per_s": p98,
        "p99_m_per_s": p99,
        "sampled_max_m_per_s": maximum,
        "diagnostic_rounded_vmax_m_per_s": diagnostic_vmax,
    }


def process_years(
    source_root: Path,
    output_dir: Path,
    years: tuple[int, ...],
    domain: Domain,
    layer_index: int,
    time_step_hours: int,
    first_hour: int,
    land_geojson: Path | None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> None:
    """執行多年度固定表層前處理並寫出完整 metadata。

    所有輸出都限定在 `output_dir`；函式不會清理、覆寫或搬移 output_dir 之外的
    既有資料。若目錄已有檔案，直接停止，避免誤把新的處理結果混入既有 cache。
    """

    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"output directory is not empty; use a new directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    if start_date is None:
        start_date = date(min(years), 1, 1)
    if end_date is None:
        end_date = date(max(years), 12, 31)
    if start_date > end_date:
        raise ValueError("start_date must not be later than end_date")
    file_infos: dict[date, FileInfo] = {}
    for year in years:
        for file_date, path in list_year_files(source_root, year).items():
            if not (start_date <= file_date <= end_date):
                continue
            file_infos[file_date] = inspect_source_file(path, time_step_hours)
    if not file_infos:
        raise FileNotFoundError(f"no source files fall inside requested date range {start_date}..{end_date}")

    frames = build_frame_plan(file_infos, start_date, end_date, time_step_hours, first_hour)
    if not frames:
        raise RuntimeError("empty standard frame plan")
    frame_by_file: dict[Path, list[FrameRecord]] = {}
    for frame in frames:
        if frame.source_file is not None:
            frame_by_file.setdefault(Path(frame.source_file), []).append(frame)

    first_path = next(iter(file_infos.values())).path
    lon, lat, target_points = build_target_grid(domain)
    grid_shape = (lat.size, lon.size)
    if grid_shape != EXPECTED_GRID_SHAPE:
        raise ValueError(f"1 km report grid changed unexpectedly: {grid_shape} != {EXPECTED_GRID_SHAPE}")

    node_window = select_source_nodes(first_path, domain)
    try:
        mesh_weights: MeshWeights | None = build_mesh_interpolation_weights(first_path, target_points, grid_shape)
        interpolation_weights: InterpolationWeights | None = None
        interpolation_method = "ugrid_face_nodes"
    except KeyError:
        from scipy.spatial import Delaunay

        triangulation = Delaunay(node_window.points)
        interpolation_weights = build_interpolation_weights(triangulation, target_points)
        mesh_weights = None
        interpolation_method = "delaunay_source_window"

    layout = infer_hvel_layout(first_path)
    if layer_index < 0:
        layer_index = layout.layer_count + layer_index
    if layer_index < 0 or layer_index >= layout.layer_count:
        raise IndexError(f"layer_index {layer_index} outside {layout.layer_count} layers")
    if layout.node_count <= 0:
        raise ValueError("source hvel node count is not positive")

    with Dataset(first_path, "r") as ds:
        depth = clean_missing_values(ds.variables["depth"][:], getattr(ds.variables["depth"], "missing_value", None))
    bathymetry = interpolate_source_field(depth, mesh_weights, interpolation_weights, node_window, grid_shape)
    static_mask = np.isfinite(bathymetry)
    land_geojson_summary: dict[str, Any] | None = None
    if land_geojson is not None:
        land_mask, land_geojson_summary = build_geojson_land_mask(land_geojson, target_points, grid_shape)
        before = int(static_mask.sum())
        static_mask &= ~land_mask
        bathymetry = bathymetry.astype(np.float32, copy=False)
        bathymetry[land_mask] = np.nan
        land_geojson_summary.update(
            {
                "ocean_grid_cell_count_before": before,
                "ocean_grid_cell_count_after": int(static_mask.sum()),
                "masked_ocean_grid_cell_count": before - int(static_mask.sum()),
            }
        )

    frame_shape = (len(frames), lat.size, lon.size)
    u = create_memmap(output_dir / "u_surface.npy", frame_shape)
    v = create_memmap(output_dir / "v_surface.npy", frame_shape)
    speed = create_memmap(output_dir / "speed_surface.npy", frame_shape)
    source_valid = np.zeros(len(frames), dtype=bool)
    imputed = np.zeros(len(frames), dtype=bool)
    source_frame_index = np.full(len(frames), -1, dtype=np.int32)
    source_file_names = np.full(len(frames), "", dtype="U32")

    processed_files = 0
    wetdry_applied = False
    dry_cell_count = 0
    for file_index, (path, file_frames) in enumerate(sorted(frame_by_file.items()), start=1):
        with Dataset(path, "r") as ds:
            hvel_var = ds.variables["hvel"]
            wetdry_var = ds.variables.get("wetdry_elem") if mesh_weights is not None else None
            for frame in file_frames:
                if frame.source_index is None:
                    continue
                time_mask = static_mask
                if wetdry_var is not None:
                    wetdry_values = clean_missing_values(
                        wetdry_var[frame.source_index],
                        getattr(wetdry_var, "missing_value", None),
                    )
                    flat_mask = static_mask.ravel().copy()
                    element_indices = mesh_weights.element_indices
                    valid_positions = flat_mask & (element_indices >= 0)
                    if valid_positions.any():
                        element_wet = np.isfinite(wetdry_values[element_indices[valid_positions]]) & (
                            wetdry_values[element_indices[valid_positions]] <= 0.5
                        )
                        flat_mask[valid_positions] &= element_wet
                    time_mask = flat_mask.reshape(grid_shape)
                    wetdry_applied = True
                    dry_cell_count += int(static_mask.sum() - time_mask.sum())

                velocity = clean_missing_values(
                    read_surface_hvel(hvel_var, layout, frame.source_index, layer_index),
                    getattr(hvel_var, "missing_value", None),
                )
                u_grid = interpolate_source_field(
                    velocity[:, 0], mesh_weights, interpolation_weights, node_window, grid_shape
                )
                v_grid = interpolate_source_field(
                    velocity[:, 1], mesh_weights, interpolation_weights, node_window, grid_shape
                )
                u_grid = u_grid.astype(np.float32, copy=False)
                v_grid = v_grid.astype(np.float32, copy=False)
                u_grid[~time_mask] = np.nan
                v_grid[~time_mask] = np.nan
                u[frame.frame_index] = u_grid
                v[frame.frame_index] = v_grid
                speed[frame.frame_index] = np.sqrt(u_grid * u_grid + v_grid * v_grid, dtype=np.float32)
                source_valid[frame.frame_index] = True
                source_frame_index[frame.frame_index] = frame.source_index
                source_file_names[frame.frame_index] = path.name
        processed_files += 1
        print(f"processed {processed_files}/{len(frame_by_file)}: {path.name}", flush=True)

    u.flush()
    v.flush()
    speed.flush()
    impute_missing_frames(u, v, speed, source_valid, imputed)

    time_status = np.full(len(frames), "missing_unfilled", dtype="U32")
    time_status[source_valid] = "source_observed"
    time_status[imputed] = "imputed_temporal_linear"
    np.save(output_dir / "lon.npy", lon.astype(np.float32))
    np.save(output_dir / "lat.npy", lat.astype(np.float32))
    np.save(output_dir / "bathymetry.npy", bathymetry.astype(np.float32, copy=False))
    np.save(output_dir / "mask.npy", static_mask)
    np.save(output_dir / "time_iso.npy", np.asarray([frame.timestamp for frame in frames], dtype="U32"))
    np.save(output_dir / "source_valid.npy", source_valid)
    np.save(output_dir / "imputed.npy", imputed)
    np.save(output_dir / "source_frame_index.npy", source_frame_index)
    np.save(output_dir / "source_file_name.npy", source_file_names)
    np.save(output_dir / "time_status.npy", time_status)

    speed_statistics = sampled_speed_statistics(speed, source_valid)
    missing_dates = sorted({frame.calendar_date for frame in frames if frame.status != "source_available"})
    offset_values = [
        info.decoded_date_offset_days
        for info in file_infos.values()
        if info.decoded_date_offset_days not in (None, 0)
    ]
    observed_count = int(np.count_nonzero(source_valid))
    imputed_count = int(np.count_nonzero(imputed))
    summary = {
        "product": f"ocm_taiwan_surrounding_1km_surface_{time_step_hours}h",
        "source_root": str(source_root),
        "years": [int(year) for year in years],
        "calendar_range": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        "domain": asdict(domain),
        "expected_report_grid_shape_lat_lon": list(EXPECTED_GRID_SHAPE),
        "grid": {"lat_count": int(lat.size), "lon_count": int(lon.size)},
        "layer_index": int(layer_index),
        "layer_role": "surface_layer_047",
        "layer_count_in_source": int(layout.layer_count),
        "time_axis": {
            "time_step_hours": int(time_step_hours),
            "first_hour_utc": int(first_hour),
            "timestamp_convention": (
                "filename date + original daily slot starting at first_hour_utc; "
                "NetCDF units base date is audit-only"
            ),
            "time_count": len(frames),
            "time_start": frames[0].timestamp,
            "time_end": frames[-1].timestamp,
            "observed_frame_count": observed_count,
            "imputed_frame_count": imputed_count,
            "unfilled_frame_count": int(len(frames) - observed_count - imputed_count),
            "missing_calendar_dates": missing_dates,
        },
        "time_repair": {
            "filename_date_is_authoritative": True,
            "raw_netcdf_time_units_are_not_used_for_output_labels": True,
            "nonzero_decoded_date_offset_file_count": len(offset_values),
            "nonzero_decoded_date_offsets_days": sorted(set(offset_values)),
            "missing_source_policy": "linear interpolation between nearest source-valid frames; status retained in imputed.npy",
            "source_valid_file": "source_valid.npy",
            "imputed_file": "imputed.npy",
            "time_status_file": "time_status.npy",
        },
        "interpolation": {
            "method": interpolation_method,
            "source_margin_deg": float(domain.source_margin_deg),
            "source_node_count_in_window": int(node_window.indices.size),
            "target_points": int(target_points.shape[0]),
        },
        "wetdry_elem": {
            "applied": bool(wetdry_applied),
            "convention": "0=wet, nonzero/dry or missing=masked",
            "cumulative_dry_grid_cell_count": int(dry_cell_count),
        },
        "land_geojson": land_geojson_summary,
        "speed_statistics_observed_sample": speed_statistics,
        "fixed_speed_colorbar": {
            "vmin_m_per_s": FIXED_SPEED_COLORBAR_VMIN_M_PER_S,
            "vmax_m_per_s": FIXED_SPEED_COLORBAR_VMAX_M_PER_S,
            "ticks_m_per_s": list(FIXED_SPEED_COLORBAR_TICKS_M_PER_S),
            "label": FIXED_SPEED_COLORBAR_LABEL,
            "data_derived_limits": False,
        },
        "arrays": {
            "u_surface_file": "u_surface.npy",
            "v_surface_file": "v_surface.npy",
            "speed_surface_file": "speed_surface.npy",
            "shape": list(frame_shape),
            "units": "m s-1",
            "meaning": "fixed SCHISM model layer 047 horizontal eastward/northward surface-current components",
        },
        "source_files": [
            {
                "date": file_date.isoformat(),
                "path": str(info.path),
                "time_count": info.time_count,
                "selected_indices": list(info.selected_indices),
            }
            for file_date, info in sorted(file_infos.items())
        ],
    }
    (output_dir / "metadata.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit = [
        {
            **asdict(info),
            "path": str(info.path),
            "filename_date": info.filename_date.isoformat(),
            "selected_indices": list(info.selected_indices),
        }
        for _, info in sorted(file_infos.items())
    ]
    (output_dir / "source_time_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (output_dir / "PREPROCESS_COMPLETE").write_text(
        "This marker means all source frames were processed and missing frames were imputed.\n",
        encoding="utf-8",
    )
    print(json.dumps({"output_dir": str(output_dir), "metadata": summary}, ensure_ascii=False), flush=True)


def parse_args() -> argparse.Namespace:
    """解析年度表層前處理 CLI 參數。"""

    parser = argparse.ArgumentParser(description="Preprocess OCM/SCHISM raw NetCDF into annual 1 km surface arrays.")
    parser.add_argument("--source-root", type=Path, required=True, help="Root containing year folders such as /CWA-OCM/2024.")
    parser.add_argument("--output-dir", type=Path, required=True, help="New, isolated directory for all generated arrays and metadata.")
    parser.add_argument("--years", nargs="+", type=int, default=list(DEFAULT_YEARS), help="Years to include in the canonical timeline.")
    parser.add_argument("--bbox", nargs=4, type=float, default=(119.0, 123.0, 20.0, 27.0), metavar=("LON_MIN", "LON_MAX", "LAT_MIN", "LAT_MAX"))
    parser.add_argument("--target-resolution-km", type=float, default=1.0, help="Regular target grid spacing in km.")
    parser.add_argument("--source-margin-deg", type=float, default=0.25, help="Source node margin around target bbox in degrees.")
    parser.add_argument("--layer-index", type=int, default=47, help="Fixed source model layer; -1 means the last source layer.")
    parser.add_argument("--time-step-hours", type=int, default=DEFAULT_TIME_STEP_HOURS, help="Canonical output interval; 6 gives four frames per source day.")
    parser.add_argument("--first-hour-utc", type=int, default=DEFAULT_DAILY_FIRST_HOUR, help="First daily timestamp used by the repaired axis; source OCM convention is 1 UTC.")
    parser.add_argument("--land-geojson", type=Path, help="Optional Polygon/MultiPolygon coastline mask used to reproduce the report map coastline.")
    parser.add_argument("--start-date", type=lambda value: date.fromisoformat(value), help="Optional inclusive ISO date for a short validation run.")
    parser.add_argument("--end-date", type=lambda value: date.fromisoformat(value), help="Optional inclusive ISO date for a short validation run.")
    return parser.parse_args()


def main() -> None:
    """建立完整台灣周邊年度表層中間檔。"""

    args = parse_args()
    years = tuple(sorted(set(args.years)))
    if not years:
        raise ValueError("at least one year is required")
    lon_min, lon_max, lat_min, lat_max = args.bbox
    if lon_min >= lon_max or lat_min >= lat_max:
        raise ValueError("bbox must be lon_min lon_max lat_min lat_max")
    if args.target_resolution_km <= 0:
        raise ValueError("target-resolution-km must be positive")
    domain = Domain(
        domain_id="taiwan-surrounding-report-1km",
        lon_min=lon_min,
        lon_max=lon_max,
        lat_min=lat_min,
        lat_max=lat_max,
        target_resolution_km=args.target_resolution_km,
        source_margin_deg=args.source_margin_deg,
    )
    if args.land_geojson is not None and not args.land_geojson.is_file():
        raise FileNotFoundError(f"land GeoJSON not found: {args.land_geojson}")
    process_years(
        source_root=args.source_root,
        output_dir=args.output_dir,
        years=years,
        domain=domain,
        layer_index=args.layer_index,
        time_step_hours=args.time_step_hours,
        first_hour=args.first_hour_utc,
        land_geojson=args.land_geojson,
        start_date=args.start_date,
        end_date=args.end_date,
    )


if __name__ == "__main__":
    main()
