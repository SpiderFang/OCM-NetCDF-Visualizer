#!/usr/bin/env python3
"""建立不覆寫原始快取的 coastline-corrected surface SVD 輸入。

本程式只複製四個 flow-domain 的小型 ``grid`` 目錄，將其 ``mask_static.npy`` 修正為
``original_static_ocean_mask & ~coastline_land_mask``；兩年逐月 u/v/eta/valid 陣列則
以符號連結指向既有 immutable surface cache，不複製也不修改原始資料。這個設計讓
water-column SVD 核心沿用原本的時間、缺值與深度資料，同時由修正後的靜態 mask 在
候選 feature layout 階段排除 exact-land 表層及深層特徵，並把輸入版本、岸線雜湊、
格點數與原始 mask 雜湊寫入 manifest。

岸線 rasterize 使用 ``coastline_utils.build_coastline_land_mask`` 的保守規則：格點
中心、任一 cell corner 或 GeoJSON ring vertex 接觸 polygon 即標為 land；洞環會扣除。
此規則是 1 km cell-overlap 近似，不宣稱潮汐乾濕線或 polygon 面積比例。目標資料夾
必須是新的版本化路徑；除非明確傳入 ``--overwrite``，程式拒絕碰觸已存在的目錄。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from coastline_utils import build_coastline_land_mask, load_coastline_geojson, sha256_file


REGIONS: dict[str, dict[str, str]] = {
    "A": {"name_zh": "臺灣東北", "flow_domain_id": "northeast_taiwan_common_cache_v3"},
    "B": {"name_zh": "新竹", "flow_domain_id": "hsinchu_cache_v3"},
    "C": {"name_zh": "後灣", "flow_domain_id": "houwan_nmmba_cache_v3"},
    "D": {"name_zh": "連江", "flow_domain_id": "lienchiang_common_cache_v3"},
}


def _read_json(path: Path) -> dict[str, Any]:
    """讀取物件型 JSON；grid metadata 是輸入追溯的一部分，格式錯誤不得靜默略過。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根節點必須是 object：{path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """以 UTF-8 寫出可讀的版本 metadata；不寫入任何認證資訊。"""

    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_one_region(
    key: str,
    info: dict[str, str],
    *,
    source_base: Path,
    output_base: Path,
    coastline_path: Path,
    overwrite: bool,
) -> dict[str, Any]:
    """複製單一 flow-domain 的 grid 並產生 exact-land 排除後的靜態 mask。

    輸入 grid 陣列的座標維度為 ``lon[nlon]``、``lat[nlat]``，mask 為 ``(nlat,nlon)``；
    corrected mask 只改變 feature eligibility，不改變任何速度值、時間軸或網格座標。
    ``months`` 用 symlink 連回原始 cache，因而不會形成第二份兩年大型資料，也能在
    manifest 明確追溯真正讀取的 monthly source。
    """

    domain_id = info["flow_domain_id"]
    source_root = source_base / domain_id
    target_root = output_base / domain_id
    source_grid = source_root / "grid"
    target_grid = target_root / "grid"
    if not source_grid.is_dir():
        raise FileNotFoundError(f"找不到 source grid：{source_grid}")
    if target_root.exists():
        if not overwrite:
            raise FileExistsError(f"corrected input 已存在，為避免覆寫請改用 --overwrite：{target_root}")
        # 只允許清理本程式明確指定的新版 target，不會碰觸 source_base 或既有 v1。
        shutil.rmtree(target_root)
    target_root.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source_grid, target_grid, symlinks=True)

    lon = np.load(source_grid / "lon.npy", allow_pickle=False).astype(np.float64)
    lat = np.load(source_grid / "lat.npy", allow_pickle=False).astype(np.float64)
    original_static = np.load(source_grid / "mask_static.npy", allow_pickle=False).astype(bool)
    land_mask, coastline_summary = build_coastline_land_mask(lon, lat, coastline_path)
    if original_static.shape != land_mask.shape:
        raise ValueError(f"{key} static/coastline shape 不一致：{original_static.shape} vs {land_mask.shape}")
    corrected_static = original_static & ~land_mask
    np.save(target_grid / "mask_static_original.npy", original_static, allow_pickle=False)
    np.save(target_grid / "coastline_land_mask.npy", land_mask, allow_pickle=False)
    np.save(target_grid / "mask_static.npy", corrected_static, allow_pickle=False)

    source_months = source_root / "months"
    target_months = target_root / "months"
    if not source_months.is_dir():
        raise FileNotFoundError(f"找不到 source months：{source_months}")
    os.symlink(str(source_months), str(target_months), target_is_directory=True)

    source_metadata_path = source_grid / "metadata.json"
    target_metadata_path = target_grid / "metadata.json"
    grid_metadata = _read_json(source_metadata_path) if source_metadata_path.is_file() else {}
    grid_metadata["coastline_correction"] = {
        "version": "coastline_corrected_v2",
        "source_grid": str(source_grid),
        "source_mask_static_sha256": sha256_file(source_grid / "mask_static.npy"),
        "coastline_geojson": coastline_summary,
        "policy": "corrected mask_static = original mask_static & ~coastline_land_mask; monthly arrays remain symlinked immutable source",
        "original_static_ocean_cell_count": int(np.count_nonzero(original_static)),
        "corrected_static_ocean_cell_count": int(np.count_nonzero(corrected_static)),
        "excluded_exact_land_from_static_ocean_count": int(np.count_nonzero(original_static & land_mask)),
    }
    _write_json(target_metadata_path, grid_metadata)
    return {
        "region_key": key,
        "region_name_zh": info["name_zh"],
        "flow_domain_id": domain_id,
        "source_root": str(source_root),
        "corrected_root": str(target_root),
        "monthly_source": str(source_months),
        "monthly_storage": "symbolic link; source arrays are not copied or modified",
        "grid_shape_lat_lon": [int(lat.size), int(lon.size)],
        "coastline": coastline_summary,
        "original_mask_static_sha256": sha256_file(source_grid / "mask_static.npy"),
        "corrected_mask_static_sha256": sha256_file(target_grid / "mask_static.npy"),
        "original_static_ocean_cell_count": int(np.count_nonzero(original_static)),
        "corrected_static_ocean_cell_count": int(np.count_nonzero(corrected_static)),
        "excluded_exact_land_from_static_ocean_count": int(np.count_nonzero(original_static & land_mask)),
    }


def main() -> None:
    """依指定 A–D 建立版本化 corrected surface cache 輸入並輸出 manifest。"""

    parser = argparse.ArgumentParser(description="Build versioned coastline-corrected OCM surface SVD inputs.")
    parser.add_argument("--source-base", type=Path, required=True)
    parser.add_argument("--output-base", type=Path, required=True)
    parser.add_argument("--coastline-geojson", type=Path, required=True)
    parser.add_argument("--regions", default="A,B,C,D")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    _document, coastline_summary = load_coastline_geojson(args.coastline_geojson)
    selected = [item.strip().upper() for item in args.regions.split(",") if item.strip()]
    unknown = [item for item in selected if item not in REGIONS]
    if unknown:
        raise ValueError(f"未知區域：{unknown}")
    args.output_base.mkdir(parents=True, exist_ok=True)
    regions = [
        build_one_region(
            key,
            REGIONS[key],
            source_base=args.source_base,
            output_base=args.output_base,
            coastline_path=args.coastline_geojson,
            overwrite=args.overwrite,
        )
        for key in selected
    ]
    manifest = {
        "schema_name": "ocm_coastline_corrected_surface_input_manifest",
        "schema_version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_base": str(args.source_base.resolve()),
        "output_base": str(args.output_base.resolve()),
        "coastline_source": coastline_summary,
        "rasterize_semantics": coastline_summary["rasterize_semantics"],
        "regions": regions,
        "limitations": [
            "只修正 SVD static feature eligibility；monthly raw arrays 仍含原始 provider values，並由 SVD mask 排除 exact-land。",
            "cell-center/corners/ring-vertex 規則是保守 1 km cell-overlap 近似，不等同 polygon 面積或潮汐岸線。",
            "corrected input 是新版本目錄，不得回寫原始 preprocessed/ocm_surface 或既有 SVD result。",
        ],
    }
    _write_json(args.output_base / "coastline_corrected_surface_input_manifest.json", manifest)
    print(json.dumps({"output_base": str(args.output_base), "regions": selected}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
