"""彙整 OCM/SCHISM 月資料輸出，建立年度完成檢查摘要。

此工具讀取 `preprocess_ocm_month.py` 產生的月份資料夾，不重新計算流場，也不把
整年 `u/v/speed/zcor` 載入記憶體。它只檢查必要檔案是否存在、讀取 `.npy`
metadata（shape 與 dtype）、比對 `monthly_summary.json` 的時間與格點資訊，並輸出
年度 JSON/CSV 摘要。

輸入資料結構：
- 每個月份資料夾需包含 `monthly_summary.json`。
- 必要陣列包含 `lon.npy`, `lat.npy`, `time_iso.npy`, `u.npy`, `v.npy`,
  `speed.npy`, `zcor_mean.npy`, `bathymetry.npy`, `mask.npy`。
- 若使用 `--require-elev`，會額外要求 `elev.npy`，形狀應為 `(time, lat, lon)`。
- 若使用 `--require-zcor-time`，會額外要求 `zcor.npy`，形狀應為
  `(time, layer, lat, lon)`。

輸出語意：
- JSON 保留每月完整檢查結果、缺檔清單、shape/dtype 與年度層級警告。
- CSV 是方便人工掃描的扁平表格，列出月份、完整性、時間數、格點大小與速度摘要。

限制與假設：
- 本工具不驗證物理合理性，例如水位、流速或乾濕遮罩是否符合海洋學預期。
- shape 檢查依目前專案慣例：主要流場陣列為 `time, layer, lat, lon`。
- 若不同月份使用不同 bbox、解析度或 layer 數，工具會列為年度警告，但仍保留
  每個月份的原始摘要，方便後續人工判斷是否為刻意實驗設定。
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


# 基本月資料產品所需檔案。這些檔案是後續動畫、年度特徵與分群前處理的共同基礎；
# 若缺少任一項，該月份不應被視為完整。
BASE_REQUIRED_FILES = (
    "monthly_summary.json",
    "lon.npy",
    "lat.npy",
    "time_iso.npy",
    "sigma.npy",
    "u.npy",
    "v.npy",
    "speed.npy",
    "zcor_mean.npy",
    "bathymetry.npy",
    "mask.npy",
)

# 主要陣列的預期維度名稱。這些名稱不會參與計算，只用於 JSON 摘要，讓維護者能直接
# 看出每個 axis 代表的資料意義與後續合併限制。
EXPECTED_DIMENSIONS = {
    "lon.npy": ("lon",),
    "lat.npy": ("lat",),
    "time_iso.npy": ("time",),
    "sigma.npy": ("layer",),
    "u.npy": ("time", "layer", "lat", "lon"),
    "v.npy": ("time", "layer", "lat", "lon"),
    "speed.npy": ("time", "layer", "lat", "lon"),
    "zcor_mean.npy": ("layer", "lat", "lon"),
    "zcor.npy": ("time", "layer", "lat", "lon"),
    "elev.npy": ("time", "lat", "lon"),
    "bathymetry.npy": ("lat", "lon"),
    "mask.npy": ("lat", "lon"),
}


@dataclass(frozen=True)
class ArrayInspection:
    """單一 `.npy` 檔案的輕量檢查結果。

    `shape` 與 `dtype` 來自 NumPy header 或 memmap 物件，不需要把大型陣列完整讀進
    RAM。`dimensions` 是本專案對該檔案各軸的語意標籤，用來輔助年度資料合併前的
    人工檢查。
    """

    exists: bool
    shape: tuple[int, ...] | None
    dtype: str | None
    dimensions: tuple[str, ...] | None
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        """轉成可寫入 JSON 的基本型別。"""

        return {
            "exists": self.exists,
            "shape": list(self.shape) if self.shape is not None else None,
            "dtype": self.dtype,
            "dimensions": list(self.dimensions) if self.dimensions is not None else None,
            "error": self.error,
        }


@dataclass(frozen=True)
class MonthInspection:
    """單一月份資料夾的完成檢查結果。

    `complete` 只代表必要檔案與基本 shape 檢查通過，不代表海洋物理品質已驗證。
    `warnings` 會記錄非致命問題，例如 metadata 與陣列 shape 不一致；`missing_files`
    則是會讓該月份無法進入年度合併的硬性缺漏。
    """

    path: Path
    month: int | None
    complete: bool
    missing_files: list[str]
    warnings: list[str]
    metadata: dict[str, Any]
    arrays: dict[str, ArrayInspection]

    def to_json(self) -> dict[str, Any]:
        """轉成年度 JSON 摘要中的月份項目。"""

        return {
            "path": str(self.path),
            "month": self.month,
            "complete": self.complete,
            "missing_files": self.missing_files,
            "warnings": self.warnings,
            "metadata": self.metadata,
            "arrays": {name: inspection.to_json() for name, inspection in self.arrays.items()},
        }


def parse_args() -> argparse.Namespace:
    """解析年度摘要命令列參數。"""

    parser = argparse.ArgumentParser(description="Summarize preprocessed OCM monthly outputs for a full year.")
    parser.add_argument(
        "month_dirs",
        nargs="*",
        type=Path,
        help="Monthly output directories. If omitted, --input-root and --pattern are used for discovery.",
    )
    parser.add_argument("--year", required=True, type=int, help="Data year represented by the monthly outputs.")
    parser.add_argument(
        "--input-root",
        type=Path,
        default=Path("outputs"),
        help="Root directory used when month_dirs are omitted.",
    )
    parser.add_argument(
        "--pattern",
        help=(
            "Glob pattern under --input-root for discovery. "
            "Default: ocm_<year>_??_*"
        ),
    )
    parser.add_argument("--output", required=True, type=Path, help="Path for JSON yearly summary.")
    parser.add_argument("--csv-output", type=Path, help="Optional CSV path for a compact month table.")
    parser.add_argument("--require-elev", action="store_true", help="Treat elev.npy as a required monthly output.")
    parser.add_argument(
        "--require-zcor-time",
        action="store_true",
        help="Treat time-varying zcor.npy as a required monthly output.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 1 when any month is incomplete or yearly consistency warnings exist.",
    )
    parser.add_argument(
        "--allow-partial-year",
        action="store_true",
        help=(
            "Do not warn when the provided month directories do not cover all 12 calendar months. "
            "Use this for one-month-at-a-time processing."
        ),
    )
    return parser.parse_args()


def discover_month_dirs(input_root: Path, pattern: str) -> list[Path]:
    """依 glob pattern 尋找月份輸出資料夾。

    此路徑只在使用者沒有明確傳入 month_dirs 時使用；正式 server 批次腳本會直接傳入
    本次處理的月份資料夾，避免同一個 outputs/ 底下其它 smoke test 或 QC 實驗被誤納入。
    """

    return sorted(path for path in input_root.glob(pattern) if path.is_dir())


def read_json(path: Path) -> tuple[dict[str, Any], str | None]:
    """讀取 JSON 檔並回傳 `(內容, 錯誤訊息)`。

    若檔案缺失或格式錯誤，回傳空 dict 與錯誤文字，而不是直接丟例外；這樣年度摘要
    可以同時列出所有壞月份，不會只停在第一個問題。
    """

    if not path.exists():
        return {}, f"missing {path.name}"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as exc:
        return {}, f"invalid JSON in {path.name}: {exc}"


def inspect_array(path: Path) -> ArrayInspection:
    """檢查 `.npy` 陣列的 shape、dtype 與維度語意。

    大型流場陣列使用 `mmap_mode='r'` 讀取，讓 NumPy 只建立 memory-map 物件並讀取
    header；這避免年度摘要工具因 12 個月份的 `u/v/speed/zcor` 太大而耗盡記憶體。
    """

    dimensions = EXPECTED_DIMENSIONS.get(path.name)
    if not path.exists():
        return ArrayInspection(False, None, None, dimensions)
    try:
        array = np.load(path, mmap_mode="r")
        return ArrayInspection(True, tuple(int(value) for value in array.shape), str(array.dtype), dimensions)
    except Exception as exc:  # noqa: BLE001 - 摘要工具需保留每月錯誤並繼續檢查其它月份。
        return ArrayInspection(True, None, None, dimensions, error=f"{type(exc).__name__}: {exc}")


def infer_month(path: Path, metadata: dict[str, Any]) -> int | None:
    """從 metadata 或資料夾名稱推斷月份。

    優先使用 `monthly_summary.json` 的 `month` 欄位，因為它來自前處理命令列參數；
    若 metadata 不可用，才從 `ocm_2025_02_...` 這類資料夾名稱中取第三段。
    """

    month = metadata.get("month")
    if isinstance(month, int):
        return month
    if isinstance(month, str) and month.isdigit():
        return int(month)

    parts = path.name.split("_")
    for index, part in enumerate(parts):
        if part == str(metadata.get("year", "")) and index + 1 < len(parts):
            candidate = parts[index + 1]
            if candidate.isdigit():
                return int(candidate)
    for part in parts:
        if len(part) == 2 and part.isdigit() and 1 <= int(part) <= 12:
            return int(part)
    return None


def required_files(require_elev: bool, require_zcor_time: bool) -> list[str]:
    """依命令列需求建立必要檔案清單。"""

    files = list(BASE_REQUIRED_FILES)
    if require_elev:
        files.append("elev.npy")
    if require_zcor_time:
        files.append("zcor.npy")
    return files


def validate_shapes(
    arrays: dict[str, ArrayInspection],
    metadata: dict[str, Any],
    require_elev: bool,
    require_zcor_time: bool,
) -> list[str]:
    """比對月份 metadata 與陣列 shape 的基本一致性。

    檢查重點是年度合併最容易出錯的軸：time、layer、lat、lon。這不是完整數值驗證，
    但可以及早發現不同月份用了不同 bbox、解析度或時間抽樣設定。
    """

    warnings: list[str] = []
    time_count = metadata.get("time_count")
    layer_count = metadata.get("layer_count")
    grid = metadata.get("grid", {})
    lat_count = grid.get("lat_count") if isinstance(grid, dict) else None
    lon_count = grid.get("lon_count") if isinstance(grid, dict) else None

    def shape_of(name: str) -> tuple[int, ...] | None:
        inspection = arrays.get(name)
        if inspection is None or inspection.error is not None:
            return None
        return inspection.shape

    time_shape = shape_of("time_iso.npy")
    if isinstance(time_count, int) and time_shape and time_shape[0] != time_count:
        warnings.append(f"time_iso length {time_shape[0]} != metadata time_count {time_count}")

    lon_shape = shape_of("lon.npy")
    lat_shape = shape_of("lat.npy")
    if isinstance(lon_count, int) and lon_shape and lon_shape[0] != lon_count:
        warnings.append(f"lon length {lon_shape[0]} != metadata lon_count {lon_count}")
    if isinstance(lat_count, int) and lat_shape and lat_shape[0] != lat_count:
        warnings.append(f"lat length {lat_shape[0]} != metadata lat_count {lat_count}")

    expected_flow_shape = None
    if all(isinstance(value, int) for value in (time_count, layer_count, lat_count, lon_count)):
        expected_flow_shape = (time_count, layer_count, lat_count, lon_count)
        for name in ("u.npy", "v.npy", "speed.npy"):
            actual = shape_of(name)
            if actual and actual != expected_flow_shape:
                warnings.append(f"{name} shape {actual} != expected {expected_flow_shape}")

    if all(isinstance(value, int) for value in (layer_count, lat_count, lon_count)):
        expected_zcor_mean_shape = (layer_count, lat_count, lon_count)
        actual = shape_of("zcor_mean.npy")
        if actual and actual != expected_zcor_mean_shape:
            warnings.append(f"zcor_mean.npy shape {actual} != expected {expected_zcor_mean_shape}")

    if all(isinstance(value, int) for value in (lat_count, lon_count)):
        expected_horizontal_shape = (lat_count, lon_count)
        for name in ("bathymetry.npy", "mask.npy"):
            actual = shape_of(name)
            if actual and actual != expected_horizontal_shape:
                warnings.append(f"{name} shape {actual} != expected {expected_horizontal_shape}")

    if require_elev and all(isinstance(value, int) for value in (time_count, lat_count, lon_count)):
        expected_elev_shape = (time_count, lat_count, lon_count)
        actual = shape_of("elev.npy")
        if actual and actual != expected_elev_shape:
            warnings.append(f"elev.npy shape {actual} != expected {expected_elev_shape}")

    if require_zcor_time and expected_flow_shape is not None:
        actual = shape_of("zcor.npy")
        if actual and actual != expected_flow_shape:
            warnings.append(f"zcor.npy shape {actual} != expected {expected_flow_shape}")

    for name, inspection in arrays.items():
        if inspection.error is not None:
            warnings.append(f"{name} cannot be inspected: {inspection.error}")

    return warnings


def validate_month_calendar(year: int, month: int | None, metadata: dict[str, Any]) -> list[str]:
    """檢查月份輸入日檔是否涵蓋該月所有日期。

    `monthly_summary.json` 的 `input_files` 來自前處理實際讀到的 NetCDF 日檔，因此這裡
    只做「檔名日期完整性」檢查，不嘗試判斷每個檔案內的時間步是否完整。若來源資料
    缺日，後續月平均、年度時間序列與季節統計都會偏向可用日期；因此即使前處理成功，
    年度摘要仍需要把缺日列為 warning。
    """

    if month is None:
        return ["cannot validate calendar coverage because month is unknown"]

    input_files = metadata.get("input_files")
    if not isinstance(input_files, list):
        return ["metadata input_files is missing or not a list"]

    observed_days: set[int] = set()
    for value in input_files:
        path = Path(str(value))
        stem = path.name
        if len(stem) < 8 or not stem[:8].isdigit():
            continue
        file_year = int(stem[:4])
        file_month = int(stem[4:6])
        file_day = int(stem[6:8])
        if file_year == year and file_month == month:
            observed_days.add(file_day)

    expected_day_count = calendar.monthrange(year, month)[1]
    expected_days = set(range(1, expected_day_count + 1))
    missing_days = sorted(expected_days - observed_days)
    if missing_days:
        return [f"missing input daily files for days: {missing_days}"]
    return []


def inspect_month(path: Path, require_elev: bool, require_zcor_time: bool) -> MonthInspection:
    """檢查單一月份輸出資料夾。"""

    metadata, metadata_error = read_json(path / "monthly_summary.json")
    required = required_files(require_elev, require_zcor_time)
    missing = [name for name in required if not (path / name).exists()]

    arrays = {
        name: inspect_array(path / name)
        for name in sorted((set(required) | set(EXPECTED_DIMENSIONS)))
        if name.endswith(".npy") and ((path / name).exists() or name in required)
    }
    warnings = []
    if metadata_error is not None:
        warnings.append(metadata_error)
    warnings.extend(validate_shapes(arrays, metadata, require_elev, require_zcor_time))

    month = infer_month(path, metadata)
    year_value = metadata.get("year")
    if isinstance(year_value, int):
        warnings.extend(validate_month_calendar(year_value, month, metadata))

    complete = not missing and not any(inspection.error for inspection in arrays.values())
    return MonthInspection(path, month, complete, missing, warnings, metadata, arrays)


def shape_signature(month: MonthInspection) -> tuple[Any, ...]:
    """建立跨月份一致性比對用的 shape 簽章。

    年度合併通常要求 layer、lat、lon 一致；time_count 可因月份天數不同而不同，因此
    這裡不把時間長度納入簽章。若簽章不同，摘要會提示可能混用了不同 bbox 或解析度。
    """

    speed = month.arrays.get("speed.npy")
    if speed is None or speed.shape is None or len(speed.shape) != 4:
        return (None,)
    _, layer_count, lat_count, lon_count = speed.shape
    domain = month.metadata.get("domain", {})
    if not isinstance(domain, dict):
        domain = {}
    return (
        layer_count,
        lat_count,
        lon_count,
        domain.get("lon_min"),
        domain.get("lon_max"),
        domain.get("lat_min"),
        domain.get("lat_max"),
        domain.get("target_resolution_km"),
    )


def build_year_summary(year: int, months: list[MonthInspection], allow_partial_year: bool) -> dict[str, Any]:
    """建立年度或選定月份 JSON 摘要資料結構。

    `allow_partial_year=True` 用於「一次只處理一個月」的工作模式。此時工具仍會檢查
    已提供月份的檔案、shape 與缺日，但不會因為沒有傳入其它月份就產生年度層級
    warning。若要驗收真正完整全年，則保持 False，讓缺少月份被明確列出。
    """

    complete_months = [month for month in months if month.complete]
    incomplete_months = [month for month in months if not month.complete]
    signatures = {shape_signature(month) for month in complete_months}

    warnings: list[str] = []
    month_numbers = [month.month for month in months if month.month is not None]
    if len(set(month_numbers)) != len(month_numbers):
        warnings.append("duplicate month numbers detected")
    expected_months = set(range(1, 13))
    present_months = set(month_numbers)
    missing_calendar_months = sorted(expected_months - present_months)
    if missing_calendar_months and not allow_partial_year:
        warnings.append(f"missing calendar months: {missing_calendar_months}")
    if len(signatures) > 1:
        warnings.append("complete months do not share the same layer/grid/domain signature")

    total_time_count = 0
    for month in complete_months:
        time_count = month.metadata.get("time_count")
        if isinstance(time_count, int):
            total_time_count += time_count

    return {
        "year": year,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "month_count": len(months),
        "complete_month_count": len(complete_months),
        "incomplete_month_count": len(incomplete_months),
        "total_time_count_from_metadata": total_time_count,
        "warnings": warnings,
        "months": [month.to_json() for month in sorted(months, key=lambda item: item.month or 99)],
    }


def write_csv(path: Path, months: list[MonthInspection]) -> None:
    """輸出方便人工掃描的年度 CSV 表格。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "month",
                "complete",
                "path",
                "time_count",
                "layer_count",
                "lat_count",
                "lon_count",
                "time_start",
                "time_end",
                "speed_mean_m_per_s",
                "speed_p95_m_per_s",
                "speed_max_m_per_s",
                "missing_files",
                "warning_count",
            ],
        )
        writer.writeheader()
        for month in sorted(months, key=lambda item: item.month or 99):
            metadata = month.metadata
            grid = metadata.get("grid", {})
            if not isinstance(grid, dict):
                grid = {}
            speed_stats = metadata.get("speed_m_per_s", {})
            if not isinstance(speed_stats, dict):
                speed_stats = {}
            writer.writerow(
                {
                    "month": month.month,
                    "complete": month.complete,
                    "path": str(month.path),
                    "time_count": metadata.get("time_count"),
                    "layer_count": metadata.get("layer_count"),
                    "lat_count": grid.get("lat_count"),
                    "lon_count": grid.get("lon_count"),
                    "time_start": metadata.get("time_start"),
                    "time_end": metadata.get("time_end"),
                    "speed_mean_m_per_s": speed_stats.get("mean"),
                    "speed_p95_m_per_s": speed_stats.get("p95"),
                    "speed_max_m_per_s": speed_stats.get("max"),
                    "missing_files": ";".join(month.missing_files),
                    "warning_count": len(month.warnings),
                }
            )


def main() -> int:
    """執行年度摘要建立流程並回傳 shell exit code。"""

    args = parse_args()
    pattern = args.pattern or f"ocm_{args.year}_??_*"
    month_dirs = args.month_dirs or discover_month_dirs(args.input_root, pattern)
    if not month_dirs:
        print("沒有找到任何月份輸出資料夾。", file=sys.stderr)
        return 1

    months = [inspect_month(path, args.require_elev, args.require_zcor_time) for path in month_dirs]
    summary = build_year_summary(args.year, months, args.allow_partial_year)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.csv_output is not None:
        write_csv(args.csv_output, months)

    has_incomplete = any(not month.complete for month in months)
    has_year_warnings = bool(summary["warnings"])
    has_month_warnings = any(month.warnings for month in months)
    print(
        f"年度摘要完成：{args.output}；完整月份 {summary['complete_month_count']}/"
        f"{summary['month_count']}；年度警告 {len(summary['warnings'])}"
    )
    if args.strict and (has_incomplete or has_year_warnings or has_month_warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
