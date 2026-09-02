#!/usr/bin/env python3
"""驗證四海域 SVD modal-context MP4 的編碼、尺寸、影格與 poster QA。

這個檢查器在 renderer 完成後執行，不重算 OCM/SVD，也不修改任何來源資料。它讀取
`animation_manifest.json`，逐支影片呼叫本機 `ffprobe`，確認 PowerPoint 常用的
H.264/yuv420p、無音訊、864×1080、4 fps 與約 16 秒規格；再以 imageio 抽查首幀、
中間幀、末幀，檢查每個讀出的 RGB frame 尺寸，並建立三幀 contact sheet 供人工檢視
文字、色條、箭頭、遮罩和面板是否裁切。原始 manifest 的 server 路徑若已同步到
本機，程式會以相同檔名在 manifest 所在目錄尋找 local copy，不改寫來源路徑，只在
`local_path` 欄位補上本機成果位置。

限制：影像尺寸/編碼與 frame extraction 是可自動驗證事項；「文字是否過小、教授在
投影片 35% 區域縮放後是否仍舒適」仍需要觀看 contact sheet 或把影片插入 PPTX 後
人工確認。本檢查器不會開啟或改寫既有 PowerPoint。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw

try:
    from coastline_utils import load_coastline_geojson
except ImportError:  # pragma: no cover - v1 manifest 不需要地理 QA
    load_coastline_geojson = None  # type: ignore[assignment]


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """以串流方式計算成果檔案雜湊，避免影片整檔載入記憶體。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(path: Path) -> dict[str, Any]:
    """讀取 renderer 產生的 JSON，並確認四區清單存在。"""

    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("regions"), list):
        raise ValueError("manifest 必須包含 regions array")
    return manifest


def resolve_output_path(entry: dict[str, Any], manifest_dir: Path, field: str) -> Path:
    """以 manifest 原始路徑或同步目錄中的同名檔案解析輸出位置。"""

    raw = entry.get(field)
    if not isinstance(raw, dict):
        raise ValueError(f"manifest 缺少 outputs.{field}")
    original = Path(str(raw.get("path", "")))
    if original.is_file():
        return original
    local_candidate = manifest_dir / original.name
    if local_candidate.is_file():
        return local_candidate
    raise FileNotFoundError(f"找不到 {field}：{original} 或 {local_candidate}")


def run_ffprobe(path: Path, executable: str) -> dict[str, Any]:
    """取得影片容器/影像串流資訊，只保留 manifest 需要的可讀欄位。

    `ffprobe` 使用 JSON 輸出而不是解析人類可讀文字，避免不同 ffmpeg 版本的欄位
    排版差異造成誤判；`-show_streams/-show_format` 同時讓檢查器能確認沒有音訊流。
    """

    result = subprocess.run(
        [executable, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 失敗：{path}\n{result.stderr}")
    raw = json.loads(result.stdout)
    streams = raw.get("streams", [])
    formats = raw.get("format", {})
    compact_streams = []
    for stream in streams:
        compact_streams.append(
            {
                key: stream[key]
                for key in (
                    "index",
                    "codec_type",
                    "codec_name",
                    "profile",
                    "width",
                    "height",
                    "pix_fmt",
                    "r_frame_rate",
                    "avg_frame_rate",
                    "nb_frames",
                    "duration",
                    "sample_aspect_ratio",
                )
                if key in stream
            }
        )
    return {
        "executable": executable,
        "streams": compact_streams,
        "format": {
            key: formats[key]
            for key in ("format_name", "duration", "size", "bit_rate")
            if key in formats
        },
    }


def _parse_rate(rate: str | None) -> float:
    """把 ffprobe 的 `numerator/denominator` frame-rate 字串轉成 float。"""

    if not rate or "/" not in rate:
        return float("nan")
    numerator, denominator = rate.split("/", 1)
    try:
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return float("nan")


def extract_contact_sheet(
    video_path: Path,
    output_path: Path,
    *,
    expected_size: tuple[int, int],
) -> dict[str, Any]:
    """抽取首/中/末幀並合成 contact sheet，回報每幀尺寸與讀取成功狀態。

    imageio reader 只在三個指定索引 materialize 影像；不會把整部 MP4 讀入 RAM。
    contact sheet 先縮小到每幀寬 360 px，保留足夠人工檢查 panel 排版的資訊，原始
    poster/影片本身不被修改。
    """

    reader = imageio.get_reader(str(video_path))
    try:
        metadata = reader.get_meta_data()
        try:
            frame_count = int(reader.count_frames())
        except Exception:
            frame_count = int(metadata.get("nframes", 0))
        if frame_count <= 0:
            raise ValueError(f"無法取得影片影格數：{video_path}")
        indices = [0, frame_count // 2, frame_count - 1]
        frames: list[Image.Image] = []
        frame_records: list[dict[str, Any]] = []
        for index in indices:
            array = np.asarray(reader.get_data(index))
            size = [int(array.shape[1]), int(array.shape[0])] if array.ndim >= 2 else []
            passed = array.ndim == 3 and tuple(size) == expected_size and array.shape[2] in (3, 4)
            frame_records.append({"index": index, "size": size, "passed": passed})
            if not passed:
                continue
            rgb = array[:, :, :3]
            image = Image.fromarray(rgb, mode="RGB")
            image.thumbnail((360, 450), Image.Resampling.LANCZOS)
            frames.append(image)
        if len(frames) != 3:
            return {"frame_count": frame_count, "frames": frame_records, "contact_sheet": None, "passed": False}
        sheet = Image.new("RGB", (360 * 3, 480), "white")
        draw = ImageDraw.Draw(sheet)
        labels = ("first", "middle", "last")
        for index, (image, label) in enumerate(zip(frames, labels)):
            x = index * 360
            sheet.paste(image, (x, 24))
            draw.text((x + 8, 5), label, fill="#1f3038")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(output_path, format="PNG")
        return {
            "frame_count": frame_count,
            "frames": frame_records,
            "contact_sheet": str(output_path),
            "contact_sheet_sha256": sha256_file(output_path),
            "passed": all(record["passed"] for record in frame_records),
        }
    finally:
        reader.close()


def measure_poster_bottom_white_margin(
    poster_path: Path,
    *,
    near_white_threshold: int = 245,
    target_range_px: tuple[int, int] = (30, 90),
) -> dict[str, Any]:
    """量測 poster 最後一個可見像素到畫布底端的純白留白。

    這是針對版面退修要求的 raster QA：poster 是 renderer 實際輸出的 864×1080
    PNG，不能只依賴 figure fraction 推測文字是否真的落在預期位置。以 RGB 任一
    通道低於 ``near_white_threshold`` 視為可見內容，量測最後一列可見像素與底端
    的距離；箭頭、caption、經度軸名均會被納入。結果只檢查版面，不解讀流場數值，
    也不會修改 poster。目標範圍依使用者指定的「約 30–50 px、最多 70–90 px」
    設為 30–90 px，避免文字被裁切或底部再次留下 15% 空白。
    """

    with Image.open(poster_path) as image:
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    visible = np.any(rgb < int(near_white_threshold), axis=2)
    rows = np.flatnonzero(np.any(visible, axis=1))
    if rows.size == 0:
        return {
            "poster_path": str(poster_path),
            "height_px": int(rgb.shape[0]),
            "near_white_threshold": int(near_white_threshold),
            "last_visible_row_px": None,
            "bottom_white_margin_px": None,
            "target_range_px": [int(target_range_px[0]), int(target_range_px[1])],
            "passed": False,
        }
    last_visible_row = int(rows[-1])
    bottom_margin = int(rgb.shape[0] - 1 - last_visible_row)
    return {
        "poster_path": str(poster_path),
        "height_px": int(rgb.shape[0]),
        "near_white_threshold": int(near_white_threshold),
        "last_visible_row_px": last_visible_row,
        "bottom_white_margin_px": bottom_margin,
        "target_range_px": [int(target_range_px[0]), int(target_range_px[1])],
        "passed": int(target_range_px[0]) <= bottom_margin <= int(target_range_px[1]),
    }


def validate_video(
    video_path: Path,
    *,
    expected_size: tuple[int, int],
    expected_fps: float,
    expected_duration: float,
    contact_sheet_path: Path,
    ffprobe_executable: str,
) -> dict[str, Any]:
    """綜合 ffprobe 與首/中/末幀檢查，回傳可序列化 QA 結果。"""

    probe = run_ffprobe(video_path, ffprobe_executable)
    streams = probe["streams"]
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    video = video_streams[0] if video_streams else {}
    width_ok = video.get("width") == expected_size[0]
    height_ok = video.get("height") == expected_size[1]
    codec_ok = video.get("codec_name") == "h264"
    pixel_ok = video.get("pix_fmt") == "yuv420p"
    rate = _parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    fps_ok = bool(np.isfinite(rate) and abs(rate - expected_fps) < 0.02)
    duration = float(probe["format"].get("duration", video.get("duration", "nan")))
    duration_ok = bool(np.isfinite(duration) and abs(duration - expected_duration) <= 0.60)
    frames = extract_contact_sheet(video_path, contact_sheet_path, expected_size=expected_size)
    return {
        "ffprobe": probe,
        "checks": {
            "exactly_one_video_stream": len(video_streams) == 1,
            "no_audio_stream": len(audio_streams) == 0,
            "width": width_ok,
            "height": height_ok,
            "codec_h264": codec_ok,
            "pixel_format_yuv420p": pixel_ok,
            "fps": fps_ok,
            "duration": duration_ok,
            "first_middle_last_frames": bool(frames["passed"]),
        },
        "frame_qa": frames,
        "passed": all(
            (
                len(video_streams) == 1,
                len(audio_streams) == 0,
                width_ok,
                height_ok,
                codec_ok,
                pixel_ok,
                fps_ok,
                duration_ok,
                bool(frames["passed"]),
            )
        ),
    }


def validate_geography(
    manifest: dict[str, Any],
    *,
    manifest_dir: Path,
    coastline_geojson: Path | None,
) -> dict[str, Any]:
    """驗證 v2 exact coastline 雜湊、land count 與代表影格地理 QA 證據。

    v2 renderer/overlay script 會把每區 rasterize 的 land cell count、exact-land 上
    raw/reconstruction finite render、quiver count 與實際 canvas 像素遮蔽檢查寫入
    overlay summary。這裡不重新讀取兩年 u/v，而是重新 hash 同一份 GeoJSON，再核對
    版本化 QA 疊圖的自動計數，避免把只有視覺上的灰色覆蓋誤當成科學遮罩已生效；
    `visible_land_pixel_occlusion_passed` 另外確認 vector patch 已在 flow artists
    之上且 exact polygon 內抽樣像素呈現陸地填色。v1 manifest 沒有 coastline_source
    時回傳 ``not_applicable``，不影響既有 v1 的純編碼驗證。
    """

    source = manifest.get("coastline_source")
    if not isinstance(source, dict):
        return {"status": "not_applicable", "passed": True, "reason": "manifest is v1 or has no coastline_source"}
    if load_coastline_geojson is None:
        return {"status": "failed", "passed": False, "reason": "coastline_utils import unavailable"}
    candidate = coastline_geojson
    if candidate is None:
        raw_path = Path(str(source.get("path", "")))
        candidate = raw_path if raw_path.is_file() else None
    if candidate is None or not candidate.is_file():
        return {"status": "failed", "passed": False, "reason": "coastline GeoJSON unavailable for hash verification"}
    _document, actual = load_coastline_geojson(candidate)
    hash_ok = actual.get("sha256") == source.get("sha256")
    polygon_ok = actual.get("polygon_count") == source.get("polygon_count")
    summary_path = manifest_dir / "coastline_svd_qa_overlay_summary.json"
    summary_ok = summary_path.is_file()
    overlay_regions: dict[str, Any] = {}
    if summary_ok:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        for region in summary.get("regions", []):
            if isinstance(region, dict):
                overlay_regions[str(region.get("region_key"))] = region
    region_checks: list[dict[str, Any]] = []
    for region in manifest.get("regions", []):
        key = str(region.get("region_key", "unknown"))
        expected_mask = region.get("mask", {})
        overlay = overlay_regions.get(key, {})
        geographic = overlay.get("geographic_qa", {}) if isinstance(overlay, dict) else {}
        expected_land_count = expected_mask.get("exact_coastline_land_cell_count")
        actual_land_count = overlay.get("land_mask_cell_count") if isinstance(overlay, dict) else None
        count_ok = expected_land_count is not None and actual_land_count == expected_land_count
        region_checks.append(
            {
                "region_key": key,
                "land_mask_cell_count_matches": bool(count_ok),
                "land_finite_render_count": geographic.get("land_finite_render_count"),
                "land_arrow_count": geographic.get("land_arrow_count"),
                "raw_land_finite_render_count": geographic.get("raw_land_finite_render_count"),
                "reconstruction_land_finite_render_count": geographic.get(
                    "reconstruction_land_finite_render_count"
                ),
                "raw_land_arrow_count": geographic.get("raw_land_arrow_count"),
                "reconstruction_land_arrow_count": geographic.get("reconstruction_land_arrow_count"),
                "raw_visible_land_pixel_mismatch_count": geographic.get(
                    "raw_visible_land_pixel_mismatch_count"
                ),
                "reconstruction_visible_land_pixel_mismatch_count": geographic.get(
                    "reconstruction_visible_land_pixel_mismatch_count"
                ),
                "visible_land_pixel_occlusion_passed": geographic.get(
                    "visible_land_pixel_occlusion_passed"
                ) is True,
                "analysis_geometry_outside_marked_as_land_count": geographic.get(
                    "analysis_geometry_outside_marked_as_land_count"
                ),
                "analysis_geometry_outside_not_marked_as_land": geographic.get(
                    "analysis_geometry_outside_not_marked_as_land"
                ) is True,
                "land_finite_render_zero": geographic.get("all_land_finite_render_zero") is True,
                "land_arrow_zero": geographic.get("all_land_arrow_zero") is True,
                "raw_land_finite_render_zero": geographic.get("raw_land_finite_render_zero") is True,
                "reconstruction_land_finite_render_zero": geographic.get(
                    "reconstruction_land_finite_render_zero"
                ) is True,
                "raw_land_arrow_zero": geographic.get("raw_land_arrow_zero") is True,
                "reconstruction_land_arrow_zero": geographic.get("reconstruction_land_arrow_zero") is True,
                "overlay_present": bool(overlay),
                "passed": bool(
                    count_ok
                    and geographic.get("analysis_geometry_outside_not_marked_as_land") is True
                    and geographic.get("all_land_finite_render_zero") is True
                    and geographic.get("all_land_arrow_zero") is True
                    and geographic.get("raw_land_finite_render_zero") is True
                    and geographic.get("reconstruction_land_finite_render_zero") is True
                    and geographic.get("raw_land_arrow_zero") is True
                    and geographic.get("reconstruction_land_arrow_zero") is True
                    and geographic.get("visible_land_pixel_occlusion_passed") is True
                ),
            }
        )
    all_regions = bool(region_checks) and all(item["passed"] for item in region_checks)
    return {
        "status": "checked",
        "coastline_path": str(candidate.resolve()),
        "coastline_sha256_expected": source.get("sha256"),
        "coastline_sha256_actual": actual.get("sha256"),
        "coastline_sha256_matches": bool(hash_ok),
        "polygon_count_expected": source.get("polygon_count"),
        "polygon_count_actual": actual.get("polygon_count"),
        "polygon_count_matches": bool(polygon_ok),
        "overlay_summary_path": str(summary_path),
        "overlay_summary_present": summary_ok,
        "regions": region_checks,
        "passed": bool(hash_ok and polygon_ok and summary_ok and all_regions),
    }


DISPLAY_REGION_TITLES = {
    "A": "海域 A（東北角）",
    "B": "海域 B（新竹外海）",
    "C": "海域 C（後灣海域）",
    "D": "海域 D（連江海域）",
}
"""簡報第 6–9 頁所使用的四個觀眾可見海域標題；驗證時要求逐字符一致。"""

SLIDE_DISPLAY_SPECS: dict[str, dict[str, Any]] = {
    "A": {
        "display_extent": [121.3, 122.8, 24.6, 25.5],
        "x_major_values": [121.3, 121.7, 122.0, 122.4, 122.8],
        "y_major_values": [24.6, 24.8, 25.0, 25.3, 25.5],
        "display_extent_source": "slide_6_static_flow_figures",
        "reference_page": 6,
        "reference_image_sha256": "d5e4bb0bb8abf2284e20ed6f71006e62e12c7353fabcc55fe8f98fda8290cd51",
    },
    "B": {
        "display_extent": [119.7, 121.2, 24.3, 25.2],
        "x_major_values": [119.7, 120.1, 120.4, 120.8, 121.2],
        "y_major_values": [24.3, 24.5, 24.8, 25.0, 25.2],
        "display_extent_source": "slide_7_static_flow_figures",
        "reference_page": 7,
        "reference_image_sha256": "f4aba297d8bd3027506f0b2302dccea0b398818e20500be3f519575bdd770b2c",
    },
    "C": {
        "display_extent": [120.2, 121.6, 21.6, 22.4],
        "x_major_values": [120.2, 120.5, 120.9, 121.3, 121.6],
        "y_major_values": [21.6, 21.8, 22.0, 22.2, 22.4],
        "display_extent_source": "slide_8_static_flow_figures",
        "reference_page": 8,
        "reference_image_sha256": "7042267618ff547ec2d432dbaed964422472e63bc3fcea6e222ae8281467398e",
    },
    "D": {
        "display_extent": [119.2, 120.7, 25.8, 26.6],
        "x_major_values": [119.2, 119.6, 119.9, 120.3, 120.7],
        "y_major_values": [25.8, 26.0, 26.2, 26.4, 26.6],
        "display_extent_source": "slide_9_static_flow_figures",
        "reference_page": 9,
        "reference_image_sha256": "f45448277268d30179d50337900e5f66dec5cca24f92b3800a8e0a98fb3d9fa5",
    },
}
"""簡報第 6–9 頁各自核對的 display-only 範圍、major tick 與參考圖雜湊。

驗證器必須逐區比對這份表，而不是只驗證 C 或把某一頁座標複製到其他海域；raw
SVD grid bbox 仍另由 manifest 的 `grid` 與 `display_axis_spec.raw_grid_bbox` 保留。
"""

DISPLAY_FORBIDDEN_TOKENS = (
    "PC",
    "K",
    "K90",
    "K=",
    "解釋變異",
    "前四模態累積流場變異百分比",
    "臺灣東北海域",
    "新竹海域",
    "箭頭基準",
    "六層聯合 SVD 模態之表層分量",
)
"""觀眾可見文字禁用字串；manifest 的內部資料欄位不在本檢查範圍。"""


def validate_display_text(manifest: dict[str, Any]) -> dict[str, Any]:
    """驗證 renderer 寫入的觀眾可見詞彙、圖例及圖外 caption 版面規格。

    此函式檢查的是 renderer 對實際送入 Matplotlib 的字串規格，不是 OCR；因此仍
    需要人工觀看 poster/contact sheet。但它能自動阻擋舊標題、PC/K90 數值列、
    「箭頭基準」、前四模態資訊列與色條單位重疊版型重新被當成正式成果。除了
    `caption_layout` 必須宣告兩個 caption 在 axes bbox 外且 `clip_on=False`，也要
    檢查新版本的字級／純黑字色、quiver key 底部帶位置、A–D 各自固定的簡報頁面
    X/Y 軸規格與 vector land 無深色描邊。
    """

    region_checks: list[dict[str, Any]] = []
    for region in manifest.get("regions", []):
        key = str(region.get("region_key", "unknown"))
        visual = region.get("visual_spec", {}) if isinstance(region, dict) else {}
        display = visual.get("display_text", {}) if isinstance(visual, dict) else {}
        strings = display.get("strings", {}) if isinstance(display, dict) else {}
        if not isinstance(strings, dict):
            strings = {}
        k90_value = region.get("svd", {}).get("k90") if isinstance(region.get("svd"), dict) else None
        expected_bottom = (
            f"前 {int(k90_value)} 個模態重建流場"
            if isinstance(k90_value, (int, float))
            else None
        )
        expected_top = "原始流場" if region.get("source_mode") == "same_source_surface_cache" else "外部 1 km 流場對照"
        expected_title = DISPLAY_REGION_TITLES.get(key)
        caption_layout = display.get("caption_layout", {}) if isinstance(display, dict) else {}
        font_and_color = display.get("font_and_color", {}) if isinstance(display, dict) else {}
        text_style = visual.get("text_style", {}) if isinstance(visual, dict) else {}
        panel_layout = visual.get("panel_layout", {}) if isinstance(visual, dict) else {}
        axis_labels = visual.get("axis_labels", {}) if isinstance(visual, dict) else {}
        axis_ticks = visual.get("axis_ticks", {}) if isinstance(visual, dict) else {}
        x_ticks = axis_ticks.get("x", {}) if isinstance(axis_ticks, dict) else {}
        y_ticks = axis_ticks.get("y", {}) if isinstance(axis_ticks, dict) else {}
        quiver = visual.get("quiver", {}) if isinstance(visual, dict) else {}
        colorbar = visual.get("fixed_speed_colorbar", {}) if isinstance(visual, dict) else {}
        coastline = visual.get("coastline_overlay", {}) if isinstance(visual, dict) else {}
        info_line = display.get("info_line", {}) if isinstance(display, dict) else {}
        forbidden_found = {
            token: [name for name, value in strings.items() if token in str(value)]
            for token in DISPLAY_FORBIDDEN_TOKENS
        }
        forbidden_found = {token: names for token, names in forbidden_found.items() if names}
        major_values = np.asarray(x_ticks.get("major_values", []), dtype=np.float64)
        y_major_values = np.asarray(y_ticks.get("major_values", []), dtype=np.float64)
        raw_bbox = region.get("grid", {}).get("bbox_lon_min_lon_max_lat_min_lat_max", [])
        display_extent = np.asarray(region.get("display_extent", []), dtype=np.float64)
        display_axis_spec = region.get("display_axis_spec", {}) if isinstance(region, dict) else {}
        raw_grid_bbox_recorded = bool(
            len(raw_bbox) == 4
            and np.asarray(display_axis_spec.get("raw_grid_bbox", []), dtype=np.float64).size == 4
            and np.allclose(
                np.asarray(display_axis_spec.get("raw_grid_bbox", []), dtype=np.float64),
                np.asarray(raw_bbox, dtype=np.float64),
                rtol=0.0,
                atol=1.0e-8,
            )
        )
        expected_axis = SLIDE_DISPLAY_SPECS.get(key, {})
        expected_extent = np.asarray(expected_axis.get("display_extent", []), dtype=np.float64)
        expected_x_ticks = np.asarray(expected_axis.get("x_major_values", []), dtype=np.float64)
        expected_y_ticks = np.asarray(expected_axis.get("y_major_values", []), dtype=np.float64)
        display_extent_ok = bool(
            expected_extent.size == 4
            and display_extent.size == expected_extent.size
            and np.allclose(display_extent, expected_extent, rtol=0.0, atol=1.0e-8)
        )
        display_x_ticks_ok = bool(
            expected_x_ticks.size == 5
            and major_values.size == expected_x_ticks.size
            and np.allclose(major_values, expected_x_ticks, rtol=0.0, atol=1.0e-8)
        )
        display_y_ticks_ok = bool(
            expected_y_ticks.size == 5
            and y_major_values.size == expected_y_ticks.size
            and np.allclose(y_major_values, expected_y_ticks, rtol=0.0, atol=1.0e-8)
        )
        tick_label_layout = x_ticks.get("label_bbox_qa", {}) if isinstance(x_ticks, dict) else {}
        x_tick_label_spacing_ok = bool(
            key != "C"
            or (
                tick_label_layout.get("passed") is True
                and float(tick_label_layout.get("minimum_gap_px", -1.0)) > 8.0
                and tick_label_layout.get("no_overlap") is True
                and tick_label_layout.get("clipped") is False
            )
        )
        legend_alignment = quiver.get("legend_alignment", {}) if isinstance(quiver, dict) else {}
        quiver_key_right_alignment_ok = bool(
            legend_alignment.get("right_aligned") is True
            and abs(float(legend_alignment.get("right_edge_diff_px", float("inf")))) <= 4.0
        )
        checks = {
            "title_exact": strings.get("main_title") == expected_title,
            "positive_phase_exact": strings.get("positive_phase") == "模態 1 時間係數：正相位案例",
            "negative_phase_exact": strings.get("negative_phase") == "模態 1 時間係數：負相位案例",
            "top_caption_exact": strings.get("top_panel_caption") == expected_top,
            "bottom_caption_exact": expected_bottom is not None and strings.get("bottom_panel_caption") == expected_bottom,
            "info_line_removed": info_line.get("visible") is False,
            "legacy_info_line_absent": "info_line_template" not in strings,
            "arrow_legend_exact": strings.get("arrow_legend") == "1（公尺／秒）",
            "arrow_legend_reference_is_one_mps": quiver.get("legend_reference_mps") == 1.0,
            "arrow_legend_uses_same_quiver_scale": "U=1.0" in str(quiver.get("legend_artist", "")),
            "arrow_key_in_compact_bottom_band": (
                isinstance(quiver.get("legend_position_figure_fraction"), list)
                and len(quiver["legend_position_figure_fraction"]) == 2
                and 0.04 <= float(quiver["legend_position_figure_fraction"][1]) <= 0.09
            ),
            "colorbar_label_exact": strings.get("colorbar_label") == "流速（公尺／秒）",
            "colorbar_unit_layout_single_vertical": colorbar.get("unit_layout") == "single complete vertical label on the outer right side; rotated 90 degrees",
            "colorbar_label_rotation_90": colorbar.get("label_rotation_degrees") == 90,
            "colorbar_label_outer_right_centered": colorbar.get("label_position") == "outer right, vertically centered",
            "colorbar_labelpad_recorded": colorbar.get("labelpad_points") == 12,
            "colorbar_tick_pad_recorded": colorbar.get("tick_pad_points") == 3,
            "colorbar_width_recorded": colorbar.get("colorbar_width_fraction") == 0.025,
            "colorbar_margin_recorded": colorbar.get("right_margin_fraction") == 0.075,
            "visible_text_color_black": text_style.get("visible_text_color") == "#000000"
            and font_and_color.get("text_color") == "#000000",
            "title_fontsize_minimum": float(text_style.get("title_fontsize_points", 0.0)) >= 16.0,
            "phase_utc_fontsize_minimum": float(text_style.get("phase_utc_fontsize_points", 0.0)) >= 11.0,
            "panel_caption_fontsize_minimum": float(text_style.get("panel_caption_fontsize_points", 0.0)) >= 10.5,
            "axis_tick_fontsize_minimum": float(text_style.get("axis_tick_fontsize_points", 0.0)) >= 9.0,
            "colorbar_fontsize_minimum": float(text_style.get("colorbar_fontsize_points", 0.0)) >= 9.0,
            "arrow_legend_fontsize_minimum": float(text_style.get("arrow_legend_fontsize_points", 0.0)) >= 10.5,
            "captions_outside_axes": "outside axes bbox" in str(caption_layout.get("top", ""))
            and "outside axes bbox" in str(caption_layout.get("bottom", "")),
            "captions_clip_disabled": caption_layout.get("clip_on") is False,
            "compact_bottom_layout_recorded": float(panel_layout.get("bottom_margin_fraction", 1.0)) <= 0.08,
            "bottom_white_margin_target_recorded": panel_layout.get("bottom_white_margin_target_px") == [30, 90],
            "axis_labels_with_direction": axis_labels == {"x": "經度（°E）", "y": "緯度（°N）"},
            "x_major_locator_fixed": x_ticks.get("major_locator") == "FixedLocator",
            "display_extent_source_recorded": region.get("display_extent_source") == expected_axis.get("display_extent_source"),
            "display_reference_page_recorded": display_axis_spec.get("reference_page") == expected_axis.get("reference_page"),
            "display_reference_hash_recorded": display_axis_spec.get("reference_image_sha256") == expected_axis.get("reference_image_sha256"),
            "display_extent_exact": display_extent_ok,
            "raw_grid_bbox_retained": raw_grid_bbox_recorded,
            "display_x_major_ticks_exact": display_x_ticks_ok,
            "display_y_major_ticks_exact": display_y_ticks_ok,
            "x_tick_label_spacing_gt_8px": x_tick_label_spacing_ok,
            "quiver_key_right_aligned_to_bottom_axes": quiver_key_right_alignment_ok,
            "x_major_formatter_one_decimal_without_degree": x_ticks.get("major_formatter") == "%.1f",
            "y_major_formatter_one_decimal_without_degree": y_ticks.get("major_formatter") == "%.1f",
            "x_minor_locator_02_degree_without_labels": x_ticks.get("minor_locator") == "MultipleLocator(0.2)"
            and x_ticks.get("minor_labels") is False,
            "y_major_locator_02_degree_without_degree_suffix": y_ticks.get("major_locator") == "FixedLocator"
            and y_ticks.get("major_formatter") == "%.1f"
            and y_ticks.get("major_labels_omit_degree_suffix") is True,
            "top_panel_x_labels_hidden": axis_ticks.get("top_panel_x_labels") is False,
            "bottom_panel_x_labels_visible": axis_ticks.get("bottom_panel_x_labels") is True,
            "vector_land_edge_removed": coastline.get("edgecolor") == "none" and float(coastline.get("linewidth", -1.0)) == 0.0,
            "vector_land_antialiased": coastline.get("antialiased") is True,
            "raster_land_background_hidden": coastline.get("raster_land_background_visible") is False,
            "scientific_semantics_not_in_frame": visual.get("scientific_semantics_in_frame") is False,
            "no_forbidden_visible_tokens": not forbidden_found,
        }
        region_checks.append(
            {
                "region_key": key,
                "strings": strings,
                "forbidden_tokens_found": forbidden_found,
                "checks": checks,
                "passed": all(bool(value) for value in checks.values()),
            }
        )
    return {
        "status": "checked",
        "forbidden_tokens": list(DISPLAY_FORBIDDEN_TOKENS),
        "regions": region_checks,
        "passed": bool(region_checks) and all(item["passed"] for item in region_checks),
    }


def main() -> None:
    """讀取 manifest、驗證四支影片並將 QA 結果寫回 manifest。"""

    parser = argparse.ArgumentParser(description="Validate OCM SVD modal-context MP4 outputs with ffprobe and frame QA.")
    parser.add_argument("--manifest", type=Path, required=True, help="local animation_manifest.json")
    parser.add_argument("--ffprobe", default=None, help="ffprobe executable; default uses PATH")
    parser.add_argument("--expected-width", type=int, default=864)
    parser.add_argument("--expected-height", type=int, default=1080)
    parser.add_argument("--expected-fps", type=float, default=4.0)
    parser.add_argument("--expected-duration", type=float, default=16.0)
    parser.add_argument("--coastline-geojson", type=Path, default=None, help="v2 exact coastline GeoJSON for SHA-256 verification")
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    manifest = read_manifest(manifest_path)
    ffprobe = args.ffprobe or shutil.which("ffprobe")
    if not ffprobe:
        raise FileNotFoundError("找不到 ffprobe；請安裝 ffmpeg 或以 --ffprobe 指定可執行檔")
    manifest_dir = manifest_path.parent
    qa_dir = manifest_dir / "qa"
    results: list[dict[str, Any]] = []
    all_passed = True
    for region in manifest["regions"]:
        if not isinstance(region, dict):
            raise ValueError("manifest regions 內容必須是 object")
        key = str(region.get("region_key", "unknown"))
        video_path = resolve_output_path(region["outputs"], manifest_dir, "mp4")
        poster_path = resolve_output_path(region["outputs"], manifest_dir, "poster")
        poster = Image.open(poster_path)
        poster_size = [int(poster.width), int(poster.height)]
        poster.close()
        poster_bottom = measure_poster_bottom_white_margin(poster_path)
        expected_size = (args.expected_width, args.expected_height)
        contact = qa_dir / f"region_{key}_first_middle_last_contact.png"
        result = validate_video(
            video_path,
            expected_size=expected_size,
            expected_fps=args.expected_fps,
            expected_duration=args.expected_duration,
            contact_sheet_path=contact,
            ffprobe_executable=ffprobe,
        )
        result["video_path_local"] = str(video_path)
        result["poster_path_local"] = str(poster_path)
        result["poster_size_px"] = poster_size
        result["poster_size_ok"] = poster_size == list(expected_size)
        result["poster_bottom_white_margin"] = poster_bottom
        result["video_sha256"] = sha256_file(video_path)
        result["poster_sha256"] = sha256_file(poster_path)
        result["passed"] = bool(result["passed"] and result["poster_size_ok"] and poster_bottom["passed"])
        region["outputs"]["mp4"]["local_path"] = str(video_path)
        region["outputs"]["mp4"]["sha256"] = result["video_sha256"]
        region["outputs"]["mp4"]["ffprobe"] = result["ffprobe"]
        region["outputs"]["poster"]["local_path"] = str(poster_path)
        region["outputs"]["poster"]["sha256"] = result["poster_sha256"]
        region["outputs"]["frame_qa"] = result
        results.append({"region_key": key, "passed": result["passed"]})
        all_passed = all_passed and result["passed"]
    geography = validate_geography(
        manifest,
        manifest_dir=manifest_dir,
        coastline_geojson=args.coastline_geojson,
    )
    all_passed = all_passed and bool(geography["passed"])
    display_text = validate_display_text(manifest)
    all_passed = all_passed and bool(display_text["passed"])
    manifest["qa"] = {
        "validated_at_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z"),
        "ffprobe_executable": ffprobe,
        "expected": {
            "width_px": args.expected_width,
            "height_px": args.expected_height,
            "fps": args.expected_fps,
            "duration_seconds": args.expected_duration,
            "codec": "h264",
            "pixel_format": "yuv420p",
            "audio": False,
        },
        "regions": results,
        "geography": geography,
        "display_text": display_text,
        "all_passed": all_passed,
        "manual_review_required": "請人工觀看 qa/*_first_middle_last_contact.png；自動檢查無法代替投影片縮放後的可讀性判斷",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest["qa"], ensure_ascii=False, indent=2), flush=True)
    if not all_passed:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
