#!/usr/bin/env python3
"""建立 coastline-corrected v2 水柱 SVD 設定檔的版本化副本。

原始 A–D JSON config 仍是 v1 科學結果的輸入證據，不能就地修改。本程式把設定複製到
新的 config 目錄，僅變更 ``analysis_label``、purpose 與新增的
``coastline_correction`` metadata；真正排除 exact-land 的資料行為由 corrected
surface cache 的 ``grid/mask_static.npy`` 控制。四個 config 的 surface/native root
仍由 SERVER runner 的命令列明確傳入，避免把帳號、密碼或主機環境寫入設定檔。

這樣做的限制是：monthly source arrays 仍保留 provider 原始值，corrected SVD 不會
修改它們；SVD candidate feature layout 會因修正後 static ocean mask 而移除 exact-land
的 u/v/eta 特徵，深層速度也沿用同一個修正後的空間 eligibility。config 內保存岸線
GeoJSON SHA-256 與 corrected input root，方便日後重現與比對 v1/v2。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coastline_utils import load_coastline_geojson


CONFIG_BY_REGION = {
    "A": "guishan_gongliao_northeast_taiwan_flow_domain_water_column_svd_available_2024_2025.json",
    "B": "hsinchu_flow_domain_water_column_svd_available_2024_2025.json",
    "C": "houwan_nmmba_flow_domain_water_column_svd_available_2024_2025.json",
    "D": "lienchiang_flow_domain_water_column_svd_available_2024_2025.json",
}


def _read_json(path: Path) -> dict[str, Any]:
    """讀取物件型 config，避免把格式錯誤的檔案當成可重算設定。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根節點必須是 object：{path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """輸出 UTF-8、繁中不跳脫 JSON；檔案內容不包含任何認證資訊。"""

    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_config(
    key: str,
    source_path: Path,
    output_path: Path,
    *,
    corrected_surface_root: Path,
    coastline_path: Path,
    coastline_summary: dict[str, Any],
) -> dict[str, Any]:
    """從單一 v1 config 建立 coastline-corrected v2 副本。"""

    config = _read_json(source_path)
    original_label = str(config.get("analysis_label", source_path.stem))
    # water-column CLI 要求 analysis_label 最後以 `_vN` 結尾作為不可覆寫保護；因此
    # corrected 語意放在版本尾碼之前，且 renderer 會以同一個完整 suffix 找回結果。
    config["analysis_label"] = f"{original_label}_coastline_corrected_v2_v2"
    config["purpose"] = (
        str(config.get("purpose", ""))
        + " 本版本以 exact coastline conservative cell-overlap mask 排除真實陸地後重算；"
        + "結果與 v1 分開保存，不回寫原始 SVD。"
    )
    domain_id = str(config.get("domain", {}).get("flow_domain_id", ""))
    config["coastline_correction"] = {
        "enabled": True,
        "version": "coastline_corrected_v2",
        "region_key": key,
        "source_config": str(source_path.resolve()),
        "corrected_surface_root": str((corrected_surface_root / domain_id).resolve()),
        "coastline_geojson": coastline_summary,
        "corrected_mask_policy": "surface grid mask_static = original mask_static & ~coastline_land_mask; monthly values remain immutable source arrays",
        "scientific_reason": "exact-land cells with finite source u/v were included in original surface SVD feature layout",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    # figures 區原本可能指向歷史 OSM 路徑；v2 明確改成驗證過的 exact coastline，
    # 讓 SVD 產生的附屬圖與動畫使用同一個地理來源，但不改變分析格點或數值。 
    figures = config.setdefault("figures", {})
    figures["coastline_geojson"] = str(coastline_path.resolve())
    figures["coastline_geojson_sha256"] = coastline_summary["sha256"]
    _write_json(output_path, config)
    return {
        "region_key": key,
        "source_config": str(source_path.resolve()),
        "output_config": str(output_path.resolve()),
        "analysis_label": config["analysis_label"],
        "corrected_surface_root": config["coastline_correction"]["corrected_surface_root"],
        "coastline_sha256": coastline_summary["sha256"],
    }


def main() -> None:
    """建立所選 A–D config 副本並輸出 config manifest。"""

    parser = argparse.ArgumentParser(description="Create versioned coastline-corrected water-column SVD configs.")
    parser.add_argument("--source-config-dir", type=Path, required=True)
    parser.add_argument("--output-config-dir", type=Path, required=True)
    parser.add_argument("--corrected-surface-root", type=Path, required=True)
    parser.add_argument("--coastline-geojson", type=Path, required=True)
    parser.add_argument("--regions", default="A,B,C,D")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    _document, coastline_summary = load_coastline_geojson(args.coastline_geojson)
    selected = [item.strip().upper() for item in args.regions.split(",") if item.strip()]
    unknown = [item for item in selected if item not in CONFIG_BY_REGION]
    if unknown:
        raise ValueError(f"未知區域：{unknown}")
    args.output_config_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for key in selected:
        source = args.source_config_dir / CONFIG_BY_REGION[key]
        output = args.output_config_dir / f"{source.stem}_coastline_corrected_v2.json"
        if output.exists() and not args.overwrite:
            raise FileExistsError(f"config 已存在，為避免覆寫請改用 --overwrite：{output}")
        results.append(
            make_config(
                key,
                source,
                output,
                corrected_surface_root=args.corrected_surface_root,
                coastline_path=args.coastline_geojson,
                coastline_summary=coastline_summary,
            )
        )
    manifest = {
        "schema_name": "ocm_coastline_corrected_svd_config_manifest",
        "schema_version": "1.0.0",
        "source_config_dir": str(args.source_config_dir.resolve()),
        "output_config_dir": str(args.output_config_dir.resolve()),
        "corrected_surface_root": str(args.corrected_surface_root.resolve()),
        "coastline_source": coastline_summary,
        "regions": results,
    }
    _write_json(args.output_config_dir / "config_manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
