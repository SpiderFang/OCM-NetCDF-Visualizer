#!/usr/bin/env python3
"""驗證四海域純原始表層流場動畫的檔案、編碼與版面 metadata。

本工具針對 ``render_ocm_raw_surface_only.py`` 產生並同步回本機的成果目錄，
重新執行可攜式的 ffprobe 檢查，避免 SERVER 沒有 ffprobe 時把「工具不可用」
誤判為影片編碼錯誤。它不讀取或修改 OCM/SVD 科學資料，只檢查已輸出的 MP4、
PNG、manifest 與精確岸線檔案；因此不能取代逐幀人工觀看，也不會證明資料本身
的物理正確性。

資料結構與單位：
    - 每支 MP4 應有單一 video stream；寬高、fps、影格數與片長由 manifest 的
      ``render_policy`` 讀取，舊版 864 x 500 px、4 fps、64 幀、16 s 只作向後
      相容的預設值。四區主圖與色條必須使用同一個像素矩形，才能在簡報 2×2
      排列時保持視覺一致。
    - 本次簡報一致版流速色階固定為 0.0--0.8 m/s、每 0.2 m/s 一個主刻度；比例尺
      仍由 renderer 的 U=1.0 QuiverKey 產生。超過 0.8 m/s 的像素飽和是展示
      正規化行為，不代表原始流速被截斷。
    - poster 與代表影格是 864 x 500 px PNG；三幀 contact sheet 是 2592 x 500 px。
    - manifest 仍保留 SERVER 路徑與正式 SVD 追溯欄位；本機 QA 只依相對檔名與
    已同步檔案核對，不重算 SVD、不變更三點時間平滑場。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import struct
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# 這些是 renderer 的固定展示契約；用常數集中管理，避免 QA 只檢查目前某一支影片。
EXPECTED_WIDTH = 864
EXPECTED_HEIGHT = 500
EXPECTED_FPS = "4/1"
EXPECTED_FRAMES = 64
EXPECTED_DURATION_SECONDS = 16.0
EXPECTED_SPEED_VMAX_MPS = 0.8
"""本輪正式成果與簡報靜態圖一致的固定流速色階上限（m/s）。"""

EXPECTED_SPEED_TICK_STEP_MPS = 0.2
"""本輪正式成果的色條主刻度間距（m/s）。"""

EXPECTED_SPEED_TICKS_MPS = (0.0, 0.2, 0.4, 0.6, 0.8)
"""QA 預期在 manifest 與四區 visual_spec 中都出現的固定刻度集合。"""

EXPECTED_COASTLINE_SHA256 = (
    "9e2e0ac9bc527aca87d89332cd428fdcb776eefbf94a85dd70f887f729b95fdd"
)


def _sha256(path: Path) -> str:
    """以串流方式計算檔案雜湊，避免把影片或 PNG 整個載入記憶體。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _png_size(path: Path) -> tuple[int, int] | None:
    """讀取 PNG IHDR 的寬高，檢查圖檔尺寸而不依賴 Pillow。

    PNG 的前 24 bytes 包含固定 signature、IHDR 長度／型別與兩個 big-endian
    32-bit 尺寸欄位。這裡只做格式與尺寸稽核，不解碼像素，因此不能檢查文字是否
    被裁切；文字與岸線仍須搭配 renderer metadata 和人工影格檢視。
    """

    try:
        with path.open("rb") as handle:
            header = handle.read(24)
        if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        if header[12:16] != b"IHDR":
            return None
        return struct.unpack(">II", header[16:24])
    except (OSError, struct.error):
        return None


def _run_ffprobe(path: Path, ffprobe: str) -> tuple[dict[str, Any], str | None]:
    """取得影片 stream/format metadata；回傳 JSON 與可讀錯誤訊息。"""

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return {}, str(exc)
    if completed.returncode != 0:
        return {}, completed.stderr.strip() or f"ffprobe exit={completed.returncode}"
    try:
        return json.loads(completed.stdout), None
    except json.JSONDecodeError as exc:
        return {}, f"ffprobe JSON 無法解析：{exc}"


def _near(value: Any, expected: float, tolerance: float = 0.01) -> bool:
    """安全比較 ffprobe 可能回傳字串的浮點欄位。"""

    try:
        return abs(float(value) - expected) <= tolerance
    except (TypeError, ValueError):
        return False


def _render_contract(manifest: dict[str, Any]) -> dict[str, Any]:
    """從 manifest 取得本次 renderer 的影片尺寸、幀率、影格數與片長契約。

    既有 16 秒雙相位版與新增的連續長時窗版共用同一個 validator，但兩者的
    frame count/duration 不同。若 QA 固定寫死 64 幀，長時窗成果即使編碼正確也
    會被錯誤判定為失敗；因此優先使用 manifest 的 render_policy，缺欄位時才回退
    到歷史 raw-only 版本常數。這裡只讀展示 metadata，不改變影片或科學資料。
    """

    policy = manifest.get("render_policy", {})
    return {
        "width": int(policy.get("width_px", EXPECTED_WIDTH)),
        "height": int(policy.get("height_px", EXPECTED_HEIGHT)),
        "fps": str(policy.get("fps", int(EXPECTED_FPS.split("/")[0]))) + "/1",
        "frames": int(policy.get("expected_frame_count", EXPECTED_FRAMES)),
        "duration_seconds": float(policy.get("expected_duration_seconds", EXPECTED_DURATION_SECONDS)),
    }


def _video_checks(
    video_path: Path,
    expected_sha256: str | None,
    ffprobe: str,
    expected: dict[str, Any],
) -> dict[str, Any]:
    """逐支檢查影片 stream、編碼、時間長度、音訊與 manifest 雜湊。

    ``nb_frames`` 在某些容器可能不存在，因此同時保留 ffprobe 原始資料；本次
    renderer 使用 imageio-ffmpeg 寫出的 MP4 會提供此欄位。若欄位缺失，QA 會保守
    判定不通過，避免只依 duration 推測影格數。
    """

    exists = video_path.is_file()
    report: dict[str, Any] = {
        "filename": video_path.name,
        "path": str(video_path),
        "exists": exists,
        "checks": {},
        "ffprobe": {},
    }
    if not exists:
        report["checks"] = {
            "video_exists": False,
            "codec_h264": False,
            "pixel_format_yuv420p": False,
            "width_ok": False,
            "height_ok": False,
            "fps_ok": False,
            "frame_count_ok": False,
            "duration_ok": False,
            "no_audio": False,
            "sha256_matches_manifest": False,
        }
        report["passed"] = False
        return report

    probe, error = _run_ffprobe(video_path, ffprobe)
    report["ffprobe"] = probe
    if error:
        report["ffprobe_error"] = error

    streams = probe.get("streams", [])
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    stream = video_streams[0] if len(video_streams) == 1 else {}
    format_info = probe.get("format", {})
    actual_sha256 = _sha256(video_path)

    checks = {
        "video_exists": True,
        "codec_h264": stream.get("codec_name") == "h264",
        "pixel_format_yuv420p": stream.get("pix_fmt") == "yuv420p",
        "width_ok": stream.get("width") == expected["width"],
        "height_ok": stream.get("height") == expected["height"],
        "fps_ok": stream.get("r_frame_rate") == expected["fps"]
        or stream.get("avg_frame_rate") == expected["fps"],
        "frame_count_ok": str(stream.get("nb_frames")) == str(expected["frames"]),
        "duration_ok": _near(format_info.get("duration"), expected["duration_seconds"]),
        "no_audio": len(audio_streams) == 0,
        "sha256_matches_manifest": bool(expected_sha256)
        and actual_sha256 == expected_sha256,
    }
    report["sha256"] = actual_sha256
    report["checks"] = checks
    report["passed"] = all(checks.values())
    return report


def _colorbar_checks(manifest: dict[str, Any]) -> dict[str, Any]:
    """驗證固定色階是否確實與簡報靜態圖一致。

    這裡同時核對總體 ``render_policy`` 與每一區 ``visual_spec``，因為只改總體
    manifest 而漏改 scene 的 colorbar artist，仍可能產生每區實際刻度不一致的
    影片。此檢查不讀像素，也不判斷高於 0.8 m/s 的色彩飽和是否適合科學分析；它
    只確認 renderer 已把使用者指定的展示契約寫入並套用到四區。
    """

    policy = manifest.get("render_policy", {})
    policy_vmax_ok = _near(policy.get("fixed_speed_vmax_mps"), EXPECTED_SPEED_VMAX_MPS, 1.0e-6)
    policy_step_ok = _near(
        policy.get("fixed_speed_tick_spacing_mps"), EXPECTED_SPEED_TICK_STEP_MPS, 1.0e-6
    )
    policy_ticks = policy.get("fixed_speed_ticks_mps", [])
    policy_ticks_ok = len(policy_ticks) == len(EXPECTED_SPEED_TICKS_MPS) and all(
        _near(value, expected, 1.0e-6) for value, expected in zip(policy_ticks, EXPECTED_SPEED_TICKS_MPS)
    )
    region_checks: list[dict[str, Any]] = []
    for region in manifest.get("regions", []):
        visual = region.get("visual_spec", {})
        ticks = visual.get("colorbar_ticks_mps", [])
        ticks_ok = len(ticks) == len(EXPECTED_SPEED_TICKS_MPS) and all(
            _near(value, expected, 1.0e-6) for value, expected in zip(ticks, EXPECTED_SPEED_TICKS_MPS)
        )
        checks = {
            "label_full": visual.get("colorbar_label") == "流速（公尺／秒）",
            "vmin_zero": _near(visual.get("fixed_speed_vmin_mps"), 0.0, 1.0e-6),
            "vmax_0p8": _near(visual.get("fixed_speed_vmax_mps"), EXPECTED_SPEED_VMAX_MPS, 1.0e-6),
            "tick_spacing_0p2": _near(
                visual.get("colorbar_tick_spacing_mps"), EXPECTED_SPEED_TICK_STEP_MPS, 1.0e-6
            ),
            "ticks_0p0_to_0p8": ticks_ok,
        }
        region_checks.append(
            {
                "region_key": region.get("region_key"),
                "checks": checks,
                "passed": all(checks.values()),
            }
        )
    return {
        "expected_vmax_mps": EXPECTED_SPEED_VMAX_MPS,
        "expected_tick_step_mps": EXPECTED_SPEED_TICK_STEP_MPS,
        "expected_ticks_mps": list(EXPECTED_SPEED_TICKS_MPS),
        "policy": {
            "vmax_0p8": policy_vmax_ok,
            "tick_spacing_0p2": policy_step_ok,
            "ticks_0p0_to_0p8": policy_ticks_ok,
        },
        "regions": region_checks,
        "passed": bool(
            policy_vmax_ok
            and policy_step_ok
            and policy_ticks_ok
            and region_checks
            and all(item["passed"] for item in region_checks)
        ),
    }


def _static_checks(manifest: dict[str, Any], region_index: int) -> dict[str, Any]:
    """合併 renderer 已記錄的固定版面與文字檢查。

    這些資料不是重新從像素 OCR 出來的結果，而是 renderer 在建立 scene 時保存的
    artist bbox、文字 allowlist/denylist 與岸線遮罩 metadata；因此報告會明確列為
    ``renderer_metadata_checks``，不冒充獨立影像辨識。
    """

    old_regions = manifest.get("qa", {}).get("server_validation", {}).get("regions", [])
    if not old_regions:
        old_regions = manifest.get("qa", {}).get("regions", [])
    if region_index >= len(old_regions):
        return {"available": False, "passed": False}
    checks = old_regions[region_index].get("checks", {})
    keys = (
        "colorbar_same_height",
        "arrow_inside_main_axes",
        "arrow_outline_removed",
        "title_and_raw_only_text_passed",
        "no_forbidden_visible_tokens",
        "x_ticks_not_clipped",
        "x_axis_label_not_clipped",
        "shared_main_axes_bbox",
        "shared_colorbar_bbox",
    )
    selected = {key: bool(checks.get(key)) for key in keys}
    selected["available"] = True
    selected["passed"] = all(selected[key] for key in keys)
    return selected


def _arrow_scale_checks(manifest: dict[str, Any]) -> dict[str, Any]:
    """驗證連續長時窗版的四區 quiver scale 是否固定一致。

    新版 renderer 將固定跨區尺度與 legend group bbox 寫入 ``qa``；本機 validator
    只複核該 metadata，不依各區流速 p95 重新決定尺度。舊版 manifest 沒有此欄位時
    保留向後相容並標記為 not_applicable，避免因新增 QA 規則誤否決既有 16 秒成果。
    """

    value = manifest.get("qa", {}).get("arrow_scale_consistency")
    if not isinstance(value, dict):
        return {"available": False, "passed": True, "reason": "not_present_in_legacy_manifest"}
    return {
        "available": True,
        "passed": value.get("passed") is True,
        "reference_spread_mps": value.get("reference_spread_mps"),
        "effective_scale_spread": value.get("effective_scale_spread"),
        "legend_group_width_spread_px": value.get("legend_group_width_spread_px"),
    }


def validate(output_dir: Path, coastline_path: Path, ffprobe: str) -> dict[str, Any]:
    """執行整個本機成果目錄 QA，並回傳可序列化的報告。

    驗證範圍包含 manifest 之正式 SVD／岸線旗標、四區 MP4 技術編碼、PNG 尺寸、
    exact coastline SHA，以及 renderer 保存的版面檢查。此流程不會修改資料陣列，
    也不會把 raw-only 動畫誤當作模態重建動畫。
    """

    manifest_path = output_dir / "animation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    generated_at = datetime.now(timezone.utc).isoformat()
    expected = _render_contract(manifest)

    coastline_ok = coastline_path.is_file() and _sha256(coastline_path) == EXPECTED_COASTLINE_SHA256
    region_reports: list[dict[str, Any]] = []
    required_flags = {
        "raw_surface_only": manifest.get("raw_surface_only") is True,
        "reconstruction_rendered_false": manifest.get("reconstruction_rendered") is False,
        "svd_source_unchanged": manifest.get("svd_source_unchanged") is True,
        "coastline_visualization_only": manifest.get("coastline_correction_scope")
        == "visualization_only",
        "formal_svd_source_present": bool(manifest.get("formal_svd_source")),
        "uniform_2x2_panel_layout": manifest.get("render_policy", {})
        .get("panel_layout", {})
        .get("mode")
        == "uniform_2x2_fixed_axes_rectangle",
    }
    colorbar_report = _colorbar_checks(manifest)
    arrow_scale_report = _arrow_scale_checks(manifest)

    for index, region in enumerate(manifest.get("regions", [])):
        outputs = region.get("outputs", {})
        mp4 = outputs.get("mp4", {})
        video_path = output_dir / mp4.get("filename", "")
        video_report = _video_checks(video_path, mp4.get("sha256"), ffprobe, expected)
        poster_path = output_dir / outputs.get("poster", {}).get("filename", "")
        qa_frames = outputs.get("qa_frames", {})
        # 舊版兩段相位成果使用 positive/negative_window；連續長時窗改用
        # window_start/middle/end。validator 依語意尋找檔案，不要求兩版共用誤導
        # 的 phase 檔名，讓 manifest 能如實表達播放結構。
        first_frame = qa_frames.get("window_start", qa_frames.get("positive_window", {}))
        middle_frame = qa_frames.get("window_middle", qa_frames.get("negative_window", {}))
        last_frame = qa_frames.get("window_end", qa_frames.get("negative_window", {}))
        positive_path = output_dir / first_frame.get("filename", "")
        middle_path = output_dir / middle_frame.get("filename", "")
        negative_path = output_dir / last_frame.get("filename", "")
        # contact sheet 的 filename 是檔名，實體檔案位於成果目錄下的 qa/；
        # MP4、poster 與正／負代表幀則位於成果目錄根層，兩種路徑語意需分開處理。
        contact_path = output_dir / "qa" / outputs.get("contact_sheet", {}).get("filename", "")
        png_paths = {
            "poster": poster_path,
            "positive_window": positive_path,
            "negative_window": negative_path,
            "contact_sheet": contact_path,
        }
        png_paths["window_middle"] = middle_path
        png_sizes = {name: _png_size(path) if path.is_file() else None for name, path in png_paths.items()}
        png_checks = {
            "poster_exists_and_size": png_sizes["poster"] == (expected["width"], expected["height"]),
            "window_start_exists_and_size": png_sizes["positive_window"] == (expected["width"], expected["height"]),
            "window_middle_exists_and_size": png_sizes["window_middle"] == (expected["width"], expected["height"]),
            "window_end_exists_and_size": png_sizes["negative_window"] == (expected["width"], expected["height"]),
            "contact_sheet_exists_and_size": png_sizes["contact_sheet"] == (expected["width"] * 3, expected["height"]),
        }
        renderer_checks = _static_checks(manifest, index)
        report = {
            "region_key": region.get("region_key"),
            "display_title": region.get("display_title"),
            "video": video_report,
            "png_sizes": png_sizes,
            "png_checks": png_checks,
            "renderer_metadata_checks": renderer_checks,
        }
        report["passed"] = bool(
            video_report.get("passed")
            and all(png_checks.values())
            and renderer_checks.get("passed", False)
        )
        region_reports.append(report)

    report = {
        "schema_name": "ocm_raw_surface_only_local_validation",
        "schema_version": 1,
        "generated_at_utc": generated_at,
        "validation_scope": "local_synced_artifacts",
        "ffprobe": {"path": shutil.which(ffprobe) or ffprobe, "available": bool(shutil.which(ffprobe) or Path(ffprobe).exists())},
        "render_contract": expected,
        "coastline": {
            "path": str(coastline_path),
            "sha256": _sha256(coastline_path) if coastline_path.is_file() else None,
            "expected_sha256": EXPECTED_COASTLINE_SHA256,
            "sha256_matches": coastline_ok,
        },
        "manifest_flags": required_flags,
        "colorbar_spec": colorbar_report,
        "arrow_scale_consistency": arrow_scale_report,
        "regions": region_reports,
    }
    report["all_passed"] = bool(
        coastline_ok
        and all(required_flags.values())
        and colorbar_report["passed"]
        and arrow_scale_report["passed"]
        and all(item["passed"] for item in region_reports)
    )
    return report


def _update_manifest(manifest_path: Path, report: dict[str, Any]) -> None:
    """把本機驗證結果寫入 manifest，同時保留 SERVER 原始 QA 快照。

    SERVER 沒有 ffprobe，原始 ``qa`` 內的技術欄位會是工具不可用造成的 false；
    這裡先以 ``server_validation`` 保存該快照，再以相同輸出檔案的本機 ffprobe
    結果更新 ``qa.all_passed``。這不改變影片位元組或科學來源，只釐清驗證環境。
    """

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    qa = manifest.setdefault("qa", {})
    if "server_validation" not in qa:
        qa["server_validation"] = copy.deepcopy(qa)
        qa["server_validation"].pop("server_validation", None)
        qa["server_validation"].pop("local_validation", None)
    qa["local_validation"] = {
        "report_path": "qa/local_validation.json",
        "validation_scope": report["validation_scope"],
        "ffprobe": report["ffprobe"],
        "coastline_sha256_matches": report["coastline"]["sha256_matches"],
        "colorbar_spec": report["colorbar_spec"],
        "arrow_scale_consistency": report["arrow_scale_consistency"],
        "region_count": len(report["regions"]),
        "all_passed": report["all_passed"],
    }
    # SERVER 的 renderer 可能因沒有安裝 ffprobe 而在 `outputs.mp4.ffprobe` 留下
    # 工具不可用摘要；同步回本機後，validator 已對實際 MP4 重新取得完整 stream
    # metadata。將本機結果寫回目前 manifest，讓交付檔案直接記錄 H.264、yuv420p、
    # 尺寸、fps、影格數與無音訊狀態；原本的遠端摘要另存為 server_ffprobe，避免
    # 混淆 QA 執行環境，也不改變 MP4 或任何科學資料。
    for region, item in zip(manifest.get("regions", []), report["regions"]):
        outputs = region.setdefault("outputs", {})
        mp4 = outputs.setdefault("mp4", {})
        if "server_ffprobe" not in mp4 and isinstance(mp4.get("ffprobe"), dict):
            server_probe = mp4.get("ffprobe")
            if server_probe.get("available") is False:
                mp4["server_ffprobe"] = copy.deepcopy(server_probe)
        mp4["ffprobe"] = copy.deepcopy(item["video"].get("ffprobe", {}))
        mp4["ffprobe_source"] = "local_validator_after_sync"
    # 將 manifest 目前使用中的 regions 檢查同步成「本機實體檔案」結果；
    # 早先 SERVER 缺少 ffprobe 的 false 結果仍完整保存在 server_validation，
    # 避免同一份 manifest 同時顯示頂層通過、區域卻仍沿用遠端工具缺失狀態。
    qa["regions"] = [
        {
            "region_key": item["region_key"],
            "checks": {
                **item["video"]["checks"],
                **item["png_checks"],
                "renderer_metadata_checks": item["renderer_metadata_checks"],
            },
            "passed": item["passed"],
        }
        for item in report["regions"]
    ]
    qa["validation_scope"] = "local_synced_artifacts; server_ffprobe_unavailable_snapshot_preserved"
    qa["all_passed"] = report["all_passed"]
    manifest["local_validation_report"] = "qa/local_validation.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    """解析命令列參數、產生 QA JSON 並回寫本機 manifest。"""

    parser = argparse.ArgumentParser(description="驗證四海域純原始表層流場動畫")
    parser.add_argument("--output-dir", type=Path, required=True, help="已同步的動畫成果目錄")
    parser.add_argument(
        "--coastline",
        type=Path,
        default=Path("data/coastline/taiwan_exact_coastline.geojson"),
        help="本機精確岸線 GeoJSON；預設為專案 data/coastline",
    )
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe 可執行檔路徑")
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    coastline_path = args.coastline.resolve()
    manifest_path = output_dir / "animation_manifest.json"
    report = validate(output_dir, coastline_path, args.ffprobe)
    qa_path = output_dir / "qa" / "local_validation.json"
    qa_path.parent.mkdir(parents=True, exist_ok=True)
    qa_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _update_manifest(manifest_path, report)
    print(json.dumps({"qa_path": str(qa_path), "all_passed": report["all_passed"]}, ensure_ascii=False))
    raise SystemExit(0 if report["all_passed"] else 1)


if __name__ == "__main__":
    main()
