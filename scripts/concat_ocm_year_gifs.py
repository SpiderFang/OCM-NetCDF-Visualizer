"""將已完成的 OCM 月份 GIF 串接成年度 GIF。

此工具只處理「已經畫好的每月 GIF」，不重新讀取 NetCDF、不重跑前處理，也不重畫
每個時間步的流場。典型用途是在 12 個月份的 2D 動畫都已完成後，把同一種圖層
依月份順序接成全年動畫，例如：

- `surface_speed_elev_anomaly_quiver.gif`：主要研究圖，表層流場加月平均水位異常。
- `surface_speed_elev_quiver.gif`：原始自由水面高度檢查圖。
- `surface_layer_047_horizontal_current_speed_quiver.gif`：表層中性底圖流場。

輸入資料結構：
- 預設從 `outputs/ocm_<year>_<MM>_<suffix>/figures/<figure-name>` 讀取每月 GIF。
- `year` 用於資料年份與月份資料夾命名，例如 `2025`。
- `suffix` 用於辨識同一批 bbox、解析度與時間抽樣設定，例如 `taiwan_10km_3h`。
- `months` 控制要串接的月份順序，預設為 1 月到 12 月。

輸出語意：
- 年度 GIF 預設寫到 `outputs/ocm_<year>_year_<suffix>/figures/<figure-name>`。
- 同資料夾會額外寫出 `.manifest.json`，記錄來源月份、輸入路徑、每月幀數、
  輸出 fps 與 frame shape，方便回查年度動畫是如何組成的。

限制與假設：
- 本工具是「串接每月 GIF」，因此每個月份原本的色階、標題與月平均水位異常基準
  都會保留各自月份的設定。它不會產生全年統一色階，也不會把 `elev_anomaly`
  改成全年平均基準。
- 所有輸入 GIF 的影格尺寸必須一致。若某個月份使用不同 bbox、解析度、DPI 或圖面
  版型，工具會停止並回報是哪個月份不一致，避免產生跳動或破版的全年動畫。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import imageio.v2 as imageio
import numpy as np


# 預設串接主要研究用 2D 圖。這張圖的底圖是每個格點扣除該月平均水位後的 η'，
# 箭頭方向與長度代表表層水平流向與流速大小，是目前最常用的成果動畫。
DEFAULT_FIGURE_NAME = "surface_speed_elev_anomaly_quiver.gif"

# 預設月份順序為完整日曆年。若只想測試或串接部分月份，可用 --months 覆蓋。
DEFAULT_MONTHS = tuple(range(1, 13))


@dataclass(frozen=True)
class MonthGif:
    """單一月份 GIF 的來源描述。

    `month` 是 1 到 12 的月份數字；`path` 是此月份同一種圖層 GIF 的實際檔案路徑。
    將兩者包成資料類別，是為了在錯誤訊息、manifest 與串接流程中保留清楚的月份
    語意，避免只看到一長串檔名時不易判斷是哪個月份缺檔或尺寸不一致。
    """

    month: int
    path: Path


@dataclass(frozen=True)
class MonthWriteSummary:
    """單一月份被寫入年度 GIF 後的摘要。

    `frame_count` 是實際寫入的影格數；若每月繪圖時使用不同 `--frame-stride` 或缺少
    部分原始日檔，這個數字會反映在年度 manifest 中。`frame_shape` 用於確認各月圖面
    尺寸一致，格式通常是 `[height, width, channels]`。
    """

    month: int
    path: Path
    frame_count: int
    frame_shape: tuple[int, ...]

    def to_json(self) -> dict[str, Any]:
        """轉成可寫入 JSON manifest 的基本型別。"""

        return {
            "month": self.month,
            "path": str(self.path),
            "frame_count": self.frame_count,
            "frame_shape": list(self.frame_shape),
        }


def parse_months(raw_months: str) -> list[int]:
    """解析 `--months` 參數並驗證月份範圍。

    使用者可能輸入 `01,02,03` 或 `"01 02 03"`。此函式同時接受逗號與空白分隔，
    並保留輸入順序，因為年度動畫的時間順序完全由這個月份列表決定。
    """

    tokens = raw_months.replace(",", " ").split()
    if not tokens:
        raise argparse.ArgumentTypeError("--months 至少需要一個月份，例如 01 或 01,02,03")

    months: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        try:
            month = int(token, 10)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"月份必須是數字，收到：{token}") from exc

        if month < 1 or month > 12:
            raise argparse.ArgumentTypeError(f"月份必須介於 1 到 12，收到：{token}")
        if month in seen:
            raise argparse.ArgumentTypeError(f"月份不可重複，重複值：{month:02d}")

        seen.add(month)
        months.append(month)

    return months


def positive_fps(raw_fps: str) -> float:
    """解析並驗證 GIF 播放 fps。

    fps 只控制輸出 GIF 的播放速度，不代表原始 OCM 資料時間解析度。若每月 GIF 原本
    是 3 小時一幀，年度 GIF 仍只是把幀接起來；調高 fps 只會讓播放更快。
    """

    try:
        fps = float(raw_fps)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--fps 必須是正數，收到：{raw_fps}") from exc
    if fps <= 0:
        raise argparse.ArgumentTypeError(f"--fps 必須大於 0，收到：{raw_fps}")
    return fps


def parse_args() -> argparse.Namespace:
    """解析命令列參數。

    CLI 預設符合本專案 server 批次輸出命名：`outputs/ocm_2025_01_taiwan_10km_3h`
    到 `outputs/ocm_2025_12_taiwan_10km_3h`。若使用 GeoJSON QC 或其它實驗後綴，
    只需要修改 `--suffix`。
    """

    parser = argparse.ArgumentParser(
        description="Concatenate existing monthly OCM GIFs into one yearly GIF.",
    )
    parser.add_argument("--year", required=True, type=int, help="資料年份，例如 2025。")
    parser.add_argument(
        "--input-root",
        type=Path,
        default=Path("outputs"),
        help="月份輸出資料夾所在根目錄，預設為 outputs。",
    )
    parser.add_argument(
        "--suffix",
        default="taiwan_10km_3h",
        help="月份資料夾後綴，例如 taiwan_10km_3h 或 taiwan_10km_geojson_qc。",
    )
    parser.add_argument(
        "--months",
        type=parse_months,
        default=list(DEFAULT_MONTHS),
        help="要串接的月份，接受逗號或空白分隔；預設為 01 到 12。",
    )
    parser.add_argument(
        "--figure-name",
        default=DEFAULT_FIGURE_NAME,
        help=f"每月 figures/ 內要串接的 GIF 檔名，預設為 {DEFAULT_FIGURE_NAME}。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="年度 GIF 輸出資料夾；未指定時使用 outputs/ocm_<year>_year_<suffix>/figures。",
    )
    parser.add_argument(
        "--output-name",
        help="年度 GIF 檔名；未指定時沿用 --figure-name。",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="manifest JSON 輸出路徑；未指定時使用年度 GIF 同名 .manifest.json。",
    )
    parser.add_argument(
        "--fps",
        type=positive_fps,
        default=2.0,
        help="年度 GIF 播放 fps；只影響播放速度，不改變原始資料時間間隔。",
    )
    return parser.parse_args()


def build_month_gif_paths(
    input_root: Path,
    year: int,
    suffix: str,
    months: Iterable[int],
    figure_name: str,
) -> list[MonthGif]:
    """依專案月份輸出命名規則組出每月 GIF 路徑。

    路徑格式固定為 `ocm_<year>_<MM>_<suffix>/figures/<figure-name>`。這讓同一支工具
    可用在標準輸出、GeoJSON QC 輸出或其它解析度實驗，只要 `suffix` 對應即可。
    """

    return [
        MonthGif(
            month=month,
            path=input_root / f"ocm_{year}_{month:02d}_{suffix}" / "figures" / figure_name,
        )
        for month in months
    ]


def require_input_gifs(month_gifs: list[MonthGif]) -> None:
    """確認所有指定月份 GIF 都存在。

    年度 GIF 若少了一個月份仍可技術上串接，但那會讓輸出被誤認為完整全年動畫。
    因此預設採取嚴格檢查：任何月份缺檔都直接停止，並列出缺少的完整路徑。
    """

    missing = [month_gif for month_gif in month_gifs if not month_gif.path.exists()]
    if not missing:
        return

    lines = ["缺少以下月份 GIF，年度串接已停止："]
    lines.extend(f"- {item.month:02d}: {item.path}" for item in missing)
    raise FileNotFoundError("\n".join(lines))


def iter_gif_frames(path: Path) -> Iterable[np.ndarray]:
    """逐幀讀取 GIF。

    使用 generator 是為了避免一次把整個月份 GIF 讀進記憶體。年度動畫可能包含數千幀，
    因此每次只取出一張影格、檢查尺寸、寫入年度 GIF，是比較穩定的 server 工作模式。
    """

    reader = imageio.get_reader(path)
    try:
        for frame in reader:
            yield np.asarray(frame)
    finally:
        reader.close()


def append_month_to_writer(
    writer: imageio.core.format.Writer,
    month_gif: MonthGif,
    expected_shape: tuple[int, ...] | None,
) -> tuple[MonthWriteSummary, tuple[int, ...]]:
    """把單一月份 GIF 的所有影格寫入年度 GIF writer。

    `expected_shape` 由第一個有效影格決定。後續月份若影格尺寸不同，通常代表
    月份圖是用不同 bbox、DPI、figure size 或腳本版本產生；此時強行串接會造成
    年度動畫跳動或 writer 錯誤，因此直接拋出 ValueError。
    """

    frame_count = 0
    month_shape: tuple[int, ...] | None = None
    resolved_expected_shape = expected_shape

    for frame in iter_gif_frames(month_gif.path):
        frame_shape = tuple(int(value) for value in frame.shape)
        if month_shape is None:
            month_shape = frame_shape

        if resolved_expected_shape is None:
            resolved_expected_shape = frame_shape
        elif frame_shape != resolved_expected_shape:
            raise ValueError(
                "GIF 影格尺寸不一致，無法安全串接。\n"
                f"月份：{month_gif.month:02d}\n"
                f"檔案：{month_gif.path}\n"
                f"預期尺寸：{resolved_expected_shape}\n"
                f"實際尺寸：{frame_shape}"
            )

        writer.append_data(frame)
        frame_count += 1

    if frame_count == 0 or month_shape is None:
        raise ValueError(f"月份 {month_gif.month:02d} 的 GIF 沒有任何影格：{month_gif.path}")

    return (
        MonthWriteSummary(
            month=month_gif.month,
            path=month_gif.path,
            frame_count=frame_count,
            frame_shape=month_shape,
        ),
        resolved_expected_shape,
    )


def concatenate_month_gifs(
    month_gifs: list[MonthGif],
    output_gif: Path,
    fps: float,
) -> list[MonthWriteSummary]:
    """依月份順序串接 GIF 並回傳寫入摘要。

    函式不自行尋找月份，也不決定輸出命名；它只負責核心 I/O：開啟年度 writer、
    逐月讀取影格、檢查尺寸一致性、寫入年度 GIF。這樣能讓 CLI 與未來測試更容易
    分開維護。
    """

    output_gif.parent.mkdir(parents=True, exist_ok=True)
    summaries: list[MonthWriteSummary] = []
    expected_shape: tuple[int, ...] | None = None

    with imageio.get_writer(output_gif, mode="I", fps=fps) as writer:
        for month_gif in month_gifs:
            summary, expected_shape = append_month_to_writer(writer, month_gif, expected_shape)
            summaries.append(summary)

    return summaries


def write_manifest(
    manifest_path: Path,
    *,
    year: int,
    suffix: str,
    figure_name: str,
    output_gif: Path,
    fps: float,
    summaries: list[MonthWriteSummary],
) -> None:
    """寫出年度 GIF 的組成紀錄。

    manifest 是給人工與後續批次流程回查用的輕量檔案，不參與 GIF 播放。它明確寫出
    「此年度 GIF 是由每月 GIF 串接而來」，避免日後誤以為此檔已重新計算全年統一
    色階或全年平均水位異常。
    """

    total_frames = sum(summary.frame_count for summary in summaries)
    frame_shape = list(summaries[0].frame_shape) if summaries else None
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "year": year,
        "suffix": suffix,
        "figure_name": figure_name,
        "output_gif": str(output_gif),
        "fps": fps,
        "total_frames": total_frames,
        "frame_shape": frame_shape,
        "months": [summary.to_json() for summary in summaries],
        "method": "concatenate_existing_monthly_gifs",
        "limitations": (
            "This file concatenates already-rendered monthly GIFs. "
            "Monthly color scales, titles, and elev_anomaly baselines remain month-specific."
        ),
    }

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    """命令列入口。"""

    args = parse_args()
    output_dir = args.output_dir or (args.input_root / f"ocm_{args.year}_year_{args.suffix}" / "figures")
    output_name = args.output_name or args.figure_name
    output_gif = output_dir / output_name
    manifest_path = args.manifest or output_gif.with_suffix(".manifest.json")

    month_gifs = build_month_gif_paths(
        input_root=args.input_root,
        year=args.year,
        suffix=args.suffix,
        months=args.months,
        figure_name=args.figure_name,
    )
    require_input_gifs(month_gifs)

    summaries = concatenate_month_gifs(month_gifs, output_gif, args.fps)
    write_manifest(
        manifest_path,
        year=args.year,
        suffix=args.suffix,
        figure_name=args.figure_name,
        output_gif=output_gif,
        fps=args.fps,
        summaries=summaries,
    )

    total_frames = sum(summary.frame_count for summary in summaries)
    print(f"年度 GIF 已輸出：{output_gif}")
    print(f"manifest 已輸出：{manifest_path}")
    print(f"串接月份數：{len(summaries)}，總影格數：{total_frames}，fps：{args.fps:g}")


if __name__ == "__main__":
    main()
