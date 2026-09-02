#!/usr/bin/env python3
"""驗證四海域三日 hourly 原始表層流場影片的檔案與時間標示。

本工具只讀取已同步到本機的 MP4、PNG、manifest 與 exact coastline GeoJSON，
不讀取或修改 OCM 原始 NetCDF、hourly `.npy` 或任何 SVD 結果。它補足既有
raw-only validator 對本版本 72 幀／2 fps／36 秒與「只顯示 UTC 時間」標題契約
的專用檢查，讓影片技術規格、來源語意與簡報後製標題規格可由同一份 JSON 追溯。

重要限制：contact sheet 的上方像素差異可證明首、中、末畫面的時間文字確實
隨幀變化，但不能取代人工辨讀文字內容；最終仍應在簡報縮小到 2×2 後確認可讀性。
本版本另明確區分「時間內插」與「空間重網格」：參考區域網格模式允許為了
對齊既有正式區域幾何而做空間重網格，但 72 個畫面仍必須逐一對應實際 hourly
觀測，故 QA 只禁止時間內插，不把展示用空間轉換誤判為時間軸造值。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

try:
    # 新版 imageio 以 v2 命名空間提供穩定的讀圖 API；SERVER 的既有環境
    # 可能只安裝舊版頂層模組，因此保留相容分支，QA 不因套件版本而中斷。
    import imageio.v2 as imageio
except ImportError:  # pragma: no cover - SERVER 舊版 imageio 的相容分支
    import imageio  # type: ignore[no-redef]
import numpy as np


EXPECTED_COASTLINE_SHA256 = (
    "9e2e0ac9bc527aca87d89332cd428fdcb776eefbf94a85dd70f887f729b95fdd"
)
EXPECTED_WIDTH = 864
EXPECTED_HEIGHT = 500
EXPECTED_FPS = "2/1"
EXPECTED_FRAME_COUNT = 72
EXPECTED_DURATION_SECONDS = 36.0
EXPECTED_TITLE_FORMAT = "YYYY-MM-DD HH:MM UTC"


def _sha256(path: Path) -> str:
    """以串流方式計算檔案雜湊，避免將影片整個載入記憶體。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _png_size(path: Path) -> tuple[int, int] | None:
    """讀取 PNG IHDR 尺寸，檢查 poster/contact sheet 而不改寫圖檔。"""

    try:
        with path.open("rb") as handle:
            header = handle.read(24)
        if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            return None
        return struct.unpack(">II", header[16:24])
    except (OSError, struct.error):
        return None


def _ffprobe(path: Path) -> tuple[dict[str, Any], str | None]:
    """取得單支 MP4 的 video/audio stream 與 container metadata。"""

    executable = shutil.which("ffprobe")
    if executable is None:
        return {}, "找不到本機 ffprobe"
    command = [
        executable,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        return {}, completed.stderr.strip() or f"ffprobe exit={completed.returncode}"
    try:
        return json.loads(completed.stdout), None
    except json.JSONDecodeError as exc:
        return {}, f"ffprobe JSON 無法解析：{exc}"


def _near(value: Any, expected: float, tolerance: float = 0.01) -> bool:
    """比較 ffprobe 可能以字串回傳的浮點欄位。"""

    try:
        return abs(float(value) - expected) <= tolerance
    except (TypeError, ValueError):
        return False


def _video_report(path: Path, expected_sha256: str | None) -> dict[str, Any]:
    """驗證影片編碼、尺寸、幀率、幀數、片長、無音訊與 manifest 雜湊。"""

    probe, error = _ffprobe(path)
    streams = probe.get("streams", [])
    video_streams = [item for item in streams if item.get("codec_type") == "video"]
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    stream = video_streams[0] if len(video_streams) == 1 else {}
    format_info = probe.get("format", {})
    actual_sha256 = _sha256(path) if path.is_file() else None
    checks = {
        "exists": path.is_file(),
        "ffprobe_ok": error is None,
        "single_video_stream": len(video_streams) == 1,
        "codec_h264": stream.get("codec_name") == "h264",
        "pixel_format_yuv420p": stream.get("pix_fmt") == "yuv420p",
        "width_864": stream.get("width") == EXPECTED_WIDTH,
        "height_500": stream.get("height") == EXPECTED_HEIGHT,
        "fps_2": stream.get("r_frame_rate") == EXPECTED_FPS
        or stream.get("avg_frame_rate") == EXPECTED_FPS,
        "frame_count_72": str(stream.get("nb_frames")) == str(EXPECTED_FRAME_COUNT),
        "duration_36_seconds": _near(format_info.get("duration"), EXPECTED_DURATION_SECONDS),
        "no_audio": len(audio_streams) == 0,
        "sha256_matches_manifest": bool(expected_sha256) and actual_sha256 == expected_sha256,
    }
    return {
        "path": str(path),
        "sha256": actual_sha256,
        "ffprobe": probe,
        "ffprobe_error": error,
        "checks": checks,
        "passed": all(checks.values()),
    }


def _contact_title_report(path: Path) -> dict[str, Any]:
    """檢查 contact sheet 三個時間影格的頂部文字區確實有變化。

    renderer 將首／中／末影格依序水平拼成 3×864×500 PNG；只比較每一格上方
    40 像素，避免流場本體的變化影響判定。此為像素層級輔助 QA，不對中文字做 OCR。
    """

    report: dict[str, Any] = {"path": str(path), "checks": {}}
    if not path.is_file():
        report["checks"] = {"exists": False, "size": False, "title_pixels_change": False}
        report["passed"] = False
        return report
    image = np.asarray(imageio.imread(path))
    expected_shape = (EXPECTED_HEIGHT, EXPECTED_WIDTH * 3)
    size_ok = image.shape[:2] == expected_shape
    title_change = False
    pair_differences: list[int] = []
    if size_ok:
        crops = [image[:40, index * EXPECTED_WIDTH : (index + 1) * EXPECTED_WIDTH] for index in range(3)]
        pair_differences = [int(np.count_nonzero(crops[index] != crops[index + 1])) for index in range(2)]
        title_change = all(value > 0 for value in pair_differences)
    report["checks"] = {
        "exists": True,
        "size": size_ok,
        "title_pixels_change": title_change,
    }
    report["pairwise_top_strip_changed_pixels"] = pair_differences
    report["passed"] = all(report["checks"].values())
    return report


def validate(output_dir: Path, coastline_path: Path) -> dict[str, Any]:
    """執行 hourly raw-only 交付目錄的完整本機技術與文字契約 QA。"""

    manifest_path = output_dir / "animation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    policy = manifest.get("render_policy", {})
    selection = manifest.get("selection", {}).get("target_animation_window", {})
    expected_times = selection.get("expected_time_ns", [])
    time_diffs = [int(expected_times[index + 1]) - int(expected_times[index]) for index in range(len(expected_times) - 1)]
    time_checks = {
        "frame_count_72": selection.get("frame_count") == EXPECTED_FRAME_COUNT,
        "hourly_interval": len(time_diffs) == EXPECTED_FRAME_COUNT - 1 and all(value == 3_600_000_000_000 for value in time_diffs),
        "actual_source_observations": all(
            region.get("source", {}).get("all_frames_actual_source_observations") is True
            for region in manifest.get("regions", [])
        ),
        # `spatial_interpolation_used` 在參考區域網格模式可為 True；此處真正要
        # 驗證的是影格時間軸沒有產生非觀測時間，因此讀取明確的時間內插旗標。
        "no_temporal_interpolation": all(
            region.get("source", {}).get("temporal_interpolation_used") is False
            for region in manifest.get("regions", [])
        ),
        "no_smoothing": all(region.get("source", {}).get("smoothing_used") is False for region in manifest.get("regions", [])),
    }
    title_checks = {
        "title_content_utc_only": policy.get("title_content") == "UTC timestamp only",
        "region_name_hidden": policy.get("region_name_visible_in_title") is False,
        "raw_surface_label_hidden": policy.get("raw_surface_label_visible_in_title") is False,
        "title_format": policy.get("title_time_format") == EXPECTED_TITLE_FORMAT,
        "center_aligned": policy.get("title_alignment") == "center",
    }
    coastline_report = {
        "path": str(coastline_path),
        "sha256": _sha256(coastline_path) if coastline_path.is_file() else None,
        "expected_sha256": EXPECTED_COASTLINE_SHA256,
        "sha256_matches": coastline_path.is_file() and _sha256(coastline_path) == EXPECTED_COASTLINE_SHA256,
    }
    regions: list[dict[str, Any]] = []
    for region in manifest.get("regions", []):
        outputs = region.get("outputs", {})
        mp4_info = outputs.get("mp4", {})
        video = _video_report(output_dir / mp4_info.get("filename", ""), mp4_info.get("sha256"))
        poster_path = output_dir / outputs.get("poster", {}).get("filename", "")
        contact_path = output_dir / outputs.get("contact_sheet", {}).get("filename", "")
        png_checks = {
            "poster_864x500": _png_size(poster_path) == (EXPECTED_WIDTH, EXPECTED_HEIGHT),
            "contact_2592x500": _png_size(contact_path) == (EXPECTED_WIDTH * 3, EXPECTED_HEIGHT),
        }
        title_report = _contact_title_report(contact_path)
        region_title_checks = {
            "manifest_title_utc_only": region.get("title") == "UTC timestamp only",
            "region_name_hidden": region.get("region_name_visible_in_title") is False,
            "raw_surface_label_hidden": region.get("raw_surface_label_visible_in_title") is False,
            "coastline_sha256": region.get("coastline", {}).get("sha256") == EXPECTED_COASTLINE_SHA256,
        }
        passed = bool(video["passed"] and all(png_checks.values()) and title_report["passed"] and all(region_title_checks.values()))
        regions.append({
            "region_key": region.get("region_key"),
            "video": video,
            "png_checks": png_checks,
            "contact_title": title_report,
            "title_checks": region_title_checks,
            "passed": passed,
        })
    policy_checks = {
        "width_864": policy.get("width_px") == EXPECTED_WIDTH,
        "height_500": policy.get("height_px") == EXPECTED_HEIGHT,
        "fps_2": policy.get("fps") == 2,
        "frame_count_72": policy.get("expected_frame_count") == EXPECTED_FRAME_COUNT,
        "duration_36": _near(policy.get("expected_duration_seconds"), EXPECTED_DURATION_SECONDS),
        "no_audio": policy.get("no_audio") is True,
    }
    result = {
        "schema_name": "ocm_raw_surface_hourly_local_validation",
        "schema_version": "1.0.0",
        "output_dir": str(output_dir),
        "policy_checks": policy_checks,
        "time_checks": time_checks,
        "title_checks": title_checks,
        "coastline": coastline_report,
        "regions": regions,
    }
    result["all_passed"] = bool(
        all(policy_checks.values())
        and all(time_checks.values())
        and all(title_checks.values())
        and coastline_report["sha256_matches"]
        and all(region["passed"] for region in regions)
        and len(regions) == 4
    )
    return result


def main() -> None:
    """執行本機 QA，並把結果回寫成 manifest 的 local_validation 區段。"""

    parser = argparse.ArgumentParser(description="Validate actual-hourly raw-only OCM animations")
    parser.add_argument("--output-dir", type=Path, required=True, help="本機已同步的正式 hourly 動畫目錄")
    parser.add_argument("--coastline", type=Path, default=Path("data/coastline/taiwan_exact_coastline.geojson"))
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    coastline_path = args.coastline.resolve()
    report = validate(output_dir, coastline_path)
    qa_dir = output_dir / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    report_path = qa_dir / "local_validation_hourly.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    manifest_path = output_dir / "animation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    qa = manifest.setdefault("qa", {})
    qa["local_validation"] = {
        "report_path": "qa/local_validation_hourly.json",
        "validation_scope": "local_synced_actual_hourly_artifacts",
        "all_passed": report["all_passed"],
        "title_policy": report["title_checks"],
        "time_policy": report["time_checks"],
        "regions": [{"region_key": item["region_key"], "passed": item["passed"]} for item in report["regions"]],
    }
    qa["all_passed"] = report["all_passed"]
    manifest["local_validation_report"] = "qa/local_validation_hourly.json"
    for manifest_region, report_region in zip(manifest.get("regions", []), report["regions"]):
        mp4_info = manifest_region.setdefault("outputs", {}).setdefault("mp4", {})
        mp4_info["ffprobe"] = report_region["video"].get("ffprobe", {})
        mp4_info["ffprobe_source"] = "local_validator_after_sync"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"qa_path": str(report_path), "all_passed": report["all_passed"]}, ensure_ascii=False))
    raise SystemExit(0 if report["all_passed"] else 1)


if __name__ == "__main__":
    main()
