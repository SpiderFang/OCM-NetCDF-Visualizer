"""檢查 OCM/SCHISM NetCDF 檔案結構。

這個腳本用於前處理前的資料盤點。OCM 輸出通常是 SCHISM 非結構網格，
不同批次資料可能在變數屬性、維度名稱或時間單位上有差異；先輸出 JSON
摘要可以降低後續插值與動畫流程讀錯欄位的風險。

說明與步驟:
- 讀取指定的 NetCDF 檔案並提取全域屬性、維度與每個變數的 metadata（名稱、shape、dtype、屬性）。
- 對常用的座標與流場變數（如節點經緯度、depth、hvel、zcor 等）抽樣計算有限值範圍（min/max）
    以便快速判讀資料是否有缺值或不合理範圍。
- 輸出一個 JSON 摘要（可選寫入檔案或輸出到標準輸出），供 `preprocess_ocm_month.py` 與其他腳本
    作為資料假設與變數名稱映射的參考。

實作重點與註記:
- `sample_variable_values` 會依變數大小與維度智慧抽樣，避免在檢查時載入整個巨型陣列。
- 對於可能的 sentinel 或非常大的值（如 9.969e36），在統計時會先視為 NaN，避免污染範圍統計。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
from netCDF4 import Dataset, num2date


def scalar_to_json(value: Any) -> Any:
    """將 NetCDF/numpy 屬性轉成 JSON 可序列化資料。

    NetCDF 屬性常包含 numpy scalar、bytes 或 ndarray。JSON 摘要只需要保留
    可讀資訊，因此這裡會把陣列轉成 list、bytes 轉成 UTF-8 字串，避免輸出
    階段因型別不支援而中斷。
    """

    # NetCDF 屬性可能是 numpy 的 scalar 或 ndarray，也可能是 bytes（例如 units）
    # 這裡把它們轉成純 Python 型別以利 JSON 序列化
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def finite_range(values: np.ndarray) -> dict[str, float | int | None]:
    """回傳陣列有限值數量與範圍。

    OCM 原始資料可能含有 `_FillValue` 或陸地遮罩。範圍統計只使用有限值，
    讓檢查結果能代表實際海域資料，而不是被缺值或遮罩值扭曲。
    """

    # 將非有限值或極大值視為缺值，僅對有限值計算統計
    array = np.asarray(values, dtype=np.float64)
    array[(np.abs(array) > 1.0e20) | ~np.isfinite(array)] = np.nan
    finite = np.isfinite(array)
    if not finite.any():
        return {"finite_count": 0, "min": None, "max": None}
    selected = array[finite]
    return {
        "finite_count": int(finite.sum()),
        "min": float(np.nanmin(selected)),
        "max": float(np.nanmax(selected)),
    }


def summarize_variable(ds: Dataset, name: str) -> dict[str, Any]:
    """摘要單一 NetCDF 變數的維度、形狀與屬性。

    此函式不讀取大型變數完整內容，只保留變數 metadata。大型流速或 zcor
    陣列會在主要變數摘要中另外抽樣，避免單純檢查結構時耗用過多記憶體。
    """

    # 僅取得變數的結構性 metadata（不讀取大量資料值）以保持檢查快速且低記憶體
    variable = ds.variables[name]
    return {
        "dimensions": list(variable.dimensions),
        "shape": list(variable.shape),
        "dtype": str(variable.dtype),
        "attributes": {attr: scalar_to_json(getattr(variable, attr)) for attr in variable.ncattrs()},
    }


def summarize_time(ds: Dataset) -> dict[str, Any] | None:
    """解析時間軸起訖，輸出 ISO 字串與原始單位。

    SCHISM 日檔通常以秒為單位儲存相對時間。若變數提供 CF-style `units`，
    會用 netCDF4 轉換成真實日期；若缺少單位，則保留原始數值範圍作為警示。
    """

    # 如果沒有 time 變數，回傳 None
    if "time" not in ds.variables:
        return None
    variable = ds.variables["time"]
    values = np.asarray(variable[:])
    summary: dict[str, Any] = {
        "shape": list(variable.shape),
        "attributes": {attr: scalar_to_json(getattr(variable, attr)) for attr in variable.ncattrs()},
        "raw_range": finite_range(values),
    }
    units = getattr(variable, "units", None)
    calendar = getattr(variable, "calendar", "standard")
    # 若有 CF-style units，嘗試將起訖時間轉為 ISO 字串，否則保留 raw_range
    if units and values.size:
        dates = num2date(values[[0, -1]], units=units, calendar=calendar, only_use_cftime_datetimes=False)
        summary["start"] = dates[0].isoformat()
        summary["end"] = dates[1].isoformat()
    return summary


def sample_variable_values(variable: Any, max_node_samples: int = 1200) -> np.ndarray:
    """從變數中抽樣足以代表範圍的數值。

    大型 SCHISM 變數通常是 `(time, node, layer[, component])`。若只取前三個
    節點，常會剛好落在陸地或底層缺值，導致檢查摘要誤判全為缺值。因此這裡
    對 node 軸做均勻抽樣，時間軸取第一步，其餘小維度完整保留。
    """

    # 若變數不大，直接回傳完整陣列；否則針對 node/time 軸做均勻抽樣
    if variable.size <= 2_000_000:
        return np.asarray(variable[:], dtype=np.float64)

    slices: list[slice | int] = []
    for axis, (dim_name, size) in enumerate(zip(variable.dimensions, variable.shape)):
        lowered = dim_name.lower()
        if lowered == "time":
            # 只取第一個時間步作為代表
            slices.append(0)
        elif "node" in lowered and size > max_node_samples:
            # 在 node 長度過大時以 step 均勻抽樣，避免落入局部陸域或缺值區域偏誤
            step = max(1, size // max_node_samples)
            slices.append(slice(0, size, step))
        elif size <= 64:
            # 若維度很小就保留全部
            slices.append(slice(None))
        else:
            # 其他大維度保留最多 max_node_samples 個元素
            slices.append(slice(0, min(size, max_node_samples)))
    return np.asarray(variable[tuple(slices)], dtype=np.float64)


def build_summary(path: Path) -> dict[str, Any]:
    """建立 OCM NetCDF 檔案的完整檢查摘要。

    輸出包含維度、全域屬性、變數 metadata，以及常用座標與流場變數的數值範圍。
    這些資訊會作為月處理腳本參數與資料假設的依據。
    """

    # 產生檔案摘要：包含維度長度、全域屬性、每個變數的 metadata，以及針對常見變數的數值範圍
    with Dataset(path) as ds:
        summary: dict[str, Any] = {
            "path": str(path),
            "dimensions": {name: len(dim) for name, dim in ds.dimensions.items()},
            "global_attributes": {attr: scalar_to_json(getattr(ds, attr)) for attr in ds.ncattrs()},
            "variables": {name: summarize_variable(ds, name) for name in ds.variables},
            "time": summarize_time(ds),
            "ranges": {},
        }

        # 對常見且重要的變數做數值範圍檢查，這有助於發現資料單位或遮罩問題
        for name in (
            "SCHISM_hgrid_node_x",
            "SCHISM_hgrid_node_y",
            "depth",
            "sigma",
            "elev",
            "zcor",
            "hvel",
            "verticalVelocity",
            "node_bottom_index",
        ):
            if name not in ds.variables:
                continue
            variable = ds.variables[name]
            summary["ranges"][name] = finite_range(sample_variable_values(variable))
        return summary


def parse_args() -> argparse.Namespace:
    """解析命令列參數。"""

    parser = argparse.ArgumentParser(description="Inspect an OCM/SCHISM NetCDF file.")
    parser.add_argument("--input", required=True, type=Path, help="NetCDF file to inspect.")
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    return parser.parse_args()


def main() -> None:
    """執行 NetCDF 結構檢查並輸出 JSON。"""

    # 解析命令列參數並產生摘要文字
    args = parse_args()
    summary = build_summary(args.input)
    text = json.dumps(summary, ensure_ascii=False, indent=2)

    # 若指定輸出檔案，寫入 JSON；否則輸出到標準輸出以便管線處理或直接閱讀
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
