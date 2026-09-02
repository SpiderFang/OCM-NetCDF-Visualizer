#!/usr/bin/env python3
"""產生四海域既有六層聯合水柱 SVD 的表層流場關聯動畫。

本模組是給既有研究簡報第 6–9 頁右側嵌入區使用的獨立 renderer，不會讀取或
修改 PPTX。每個海域輸出一支約 16 秒、864×1080、4 fps、無音訊的 H.264 MP4，
內容由兩個不重疊的代表性第一模態時間係數相位視窗組成：每段 28 個精確 6 小時
資料影格，分別選取第一模態時間係數強正相位與強負相位附近的完整連續 7 日案例；
片頭及片尾各保留約
1 秒靜止畫面，方便 PowerPoint 點擊播放前先顯示 poster。

本模組另外支援 `--temporal-interpolation` 展示模式。此模式不是補造新的觀測資料，
而是為了在固定 4 fps、16 秒與 64 幀下減少畫面跳動，將每段 28 個觀測影格改以
12 小時錨點搭配 6 小時中間線性內插影格播放：保留 15 個真實錨點、產生 13 個
展示用虛擬影格，並維持原始時間標籤順序。內插只在完整連續視窗內、相鄰錨點的
有限 u/v 交集上進行；任一端缺值就保留 NaN，不跨缺口、不外插，也不回寫來源或
SVD。此版本必須與未啟用內插的正式 v3 分開存放，並在 manifest/README 中明確標示
「展示用時間內插」，避免把虛擬影格誤讀為新的 6 小時觀測。

資料關係採「先追溯、再決定」的策略：

1. 先讀取四個既有正式 water-column SVD run 的 config/metadata，確認 surface layer index=0、
   `mean + Σ(mode_per_raw_pc × pc)` 的 raw-PC 重建公式與內部 K90。
2. 再讀取同一 flow-domain 的 `preprocessed/ocm_surface/<flow_domain_id>` 月快取。
   這些快取與 SVD 使用相同的規則網格、同一個 published OCM surface u/v 來源，
   因而可將上半部標成「原始流場」，下半部標成「前 n 個模態重建流場」。K90 只作
   內部選模態追溯，不在觀眾可見文字中呈現。
3. 以全臺 1 km、6 小時產品的 `source_valid`/`imputed` 標記作共同時間稽核；只有
   精確存在於全臺產品、SVD 時間軸及同源月快取，且全臺產品為 source-valid、非
   imputed 的時間才可進入代表性視窗。這個全臺產品在同源模式下只作時間交集稽核，
   不拿來計算殘差或 RMSE。
4. 若指定的同源月快取缺失，才退回全臺 1 km 產品；退回時以雙線性內插把外部
   u/v 重網格到 SVD 規則網格，畫面與 manifest 明確標示「外部表層流場對照；非
   同源重建殘差」，且不輸出嚴格殘差或 RMSE。正式本案預期會走同源路徑。

畫面採垂直雙面板而非整頁研究圖：上方為原始流場、下方為模態重建流場。
兩面板共用每支影片固定的流速色階、固定箭頭尺度、固定空間範圍，才能在播放時
觀察方向、強流區與前 n 個模態重建之間的視覺對照。海域背景使用 viridis 流速色階；分析
域外、模型靜態域外、特徵未納入及逐時缺值各用近白/淡中性色，真實陸地使用中度灰
米色 GeoJSON polygon，使用高解析度抗鋸齒填色且不繪製額外深色描邊。箭頭使用白色
並加低調深色描邊，避免與背景混淆。
畫面只保留適合投影片右側縮小觀看的必要資訊帶：指定海域主標題、UTC 時間、模態
1 時間係數正/負相位、流速色條與 1（公尺／秒）箭頭圖例。前四模態累積流場變異
百分比資訊列已移除。畫面不顯示內部 PC 數值列或 K/K90 符號；六層聯合 SVD 的方法語意、原始欄位
名稱與模態選取數值仍保留在 manifest、README 與程式註解中，但不在影片文字中重複。

輸出與限制：

- `--pilot` 仍依完整 28 影格規則選視窗，但只繪製每段 4 個代表影格及低解析度
  預覽，便於先做 A 區版面與科學一致性檢查；正式執行不帶此旗標。
- 繪圖資料使用公尺/秒；經度、緯度以規則格點實際座標顯示，沒有將度數當作公尺
  重新解釋。quiver 的畫面長度是視覺縮放，不可直接當作地理距離。
- 模態重建採 raw `pc.npy` 搭配 `mode_*_mps_per_raw_pc.npy`，絕不把
  `pc_standardized.npy` 代入 raw mode；標準化 PC 僅用於畫面上的相位資訊與視窗選取。
- 代表性視窗是可追溯的相位案例，不是統計檢定或全年氣候代表性結論；選取規則、
  時間交集、排除數量、遮罩、色階、箭頭尺度與輸出雜湊都寫入 `animation_manifest.json`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.patheffects as patheffects
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np

from coastline_utils import (
    build_coastline_land_mask,
    draw_vector_land_overlay,
    load_outer_rings,
)

try:
    # QA 疊圖只會重用本模組的網格／選窗輔助函式，不會建立 MP4；因此允許在
    # 只有 matplotlib/numpy 而沒有 video writer 的環境中匯入本模組。正式輸出
    # MP4 時仍由 `_require_video_dependencies` 明確檢查 imageio 是否可用，避免
    # 把「QA 不能匯入」與「影片編碼依賴缺少」混成同一種錯誤。
    try:
        import imageio.v2 as imageio
    except ImportError:
        # SERVER 的既有 Anaconda 環境可能只有舊版 imageio；其頂層 API 仍提供
        # get_writer，故保留相容 fallback。優先使用 v2 以維持現代 imageio 行為。
        import imageio  # type: ignore[no-redef]
except ImportError:  # pragma: no cover - 僅在純 QA 最小環境觸發
    imageio = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 產品規格常數：這些數值控制的是動畫呈現與選取規則，不是 OCM 物理資料常數。
# ---------------------------------------------------------------------------


def _require_video_dependencies() -> None:
    """在真正寫入 MP4 前確認 imageio video writer 可用。

    本 renderer 同時被地理 QA overlay 匯入以重用選窗與繪圖工具；若在 QA 環境
    沒有 imageio，匯入本模組仍應成功。然而 MP4 需要 imageio 的 ``get_writer``，
    因此把依賴檢查延後到影片輸出入口，並提供具體錯誤訊息。ffmpeg 本體則由
    `_configure_imageio_ffmpeg` 查找 bundled executable 或系統 executable。
    """

    if imageio is None or not hasattr(imageio, "get_writer"):
        raise ImportError("建立 H.264 MP4 需要 imageio；目前環境只能執行繪圖與地理 QA")

SCRIPT_VERSION = "3.1.0"
"""renderer schema 版本；本版在 v3 display-only 座標對齊上加入可選時間內插。

3.0.1 的關鍵變更是：A、B、C、D 分別使用簡報第 6、7、8、9 頁正式靜態流場圖
核對出的 x/y 顯示範圍與固定一位小數 major ticks，不再把 C 區刻度套用到其他海域，
也不再讓正式四區回退到原始 bbox。這些設定只控制 Matplotlib 顯示裁切與座標標籤，
原始 SVD grid bbox 仍完整保留於 manifest，沒有改寫正式 SVD、模態、時間係數或重建
資料。比例尺群組仍依實際中文字寬度右對齊下方面板右界，並以像素 bbox 寫入 QA。
本版另將簡報端點與「格點中心 bbox」分開處理：若簡報端點落在 pcolormesh cell-edge
外框附近或略向外保留頁面版面邊界，允許其與原始 cell-edge 可繪範圍相交即可，並在
manifest 同時保留中心 bbox、cell-edge bbox 與相交狀態，避免把合法的展示裁切誤判成
資料越界。
保留 2.6.0–2.7.0 的展示性岸線與緊湊版面：conservative exact-land raster mask
只供資料／地理稽核，可見流場由指定 GeoJSON 高解析度 vector polygon 覆蓋真實陸地。
3.1.0 的 `--temporal-interpolation` 只在展示 payload 階段以 u/v 線性內插降低
6 小時觀測影格間的視覺跳動；它不改變既有 formal SVD 的 mean/mode/pc，也不把
內插結果寫回任何 OCM 中間檔。未啟用時，renderer 行為與 v3 相同。
"""

NANOS_PER_HOUR = np.int64(3_600_000_000_000)
"""一小時的奈秒數；SVD 與 surface cache 的時間欄位都以 UTC epoch ns 表示。"""

COMMON_INTERVAL_HOURS = 6
"""代表性動畫只接受全臺產品規格的 6 小時資料間隔。"""

WINDOW_FRAME_COUNT = 28
"""每個代表性相位視窗的資料影格數；28 個 6 小時影格約對應一週觀察段。"""

INTRO_HOLD_FRAMES = 4
"""4 fps 下的片頭靜止影格數，約為 1 秒。"""

OUTRO_HOLD_FRAMES = 4
"""4 fps 下的片尾靜止影格數，約為 1 秒。"""

DEFAULT_FPS = 4
"""簡報播放速率；每一資料影格代表 6 小時而非即時流速。"""

DEFAULT_WIDTH = 864
"""正式 MP4 寬度，適配投影片右側約 35% 的直式嵌入區。"""

DEFAULT_HEIGHT = 1080
"""正式 MP4 高度；寬高皆為偶數以符合 yuv420p 的編碼限制。"""

DEFAULT_RENDER_DPI = 150
"""內部 Matplotlib raster DPI；透過固定 pixel canvas 搭配較高 DPI 保留中文字筆畫。

輸出仍由 `width`/`height` 決定為 864×1080（或同比例 pilot），DPI 只改善 rasterize
與 H.264 每幀輸入的字邊緣／細線品質，不改變地理資料解析度或座標範圍。
"""

PILOT_WIDTH = 432
"""A 區 pilot 的低解析度寬度；減少首輪驗證的檔案量與等待時間。"""

PILOT_HEIGHT = 540
"""A 區 pilot 的低解析度高度；與正式 4:5 版面保持相同幾何比例。"""

PILOT_SAMPLES_PER_WINDOW = 4
"""pilot 每一相位視窗只繪製四個代表時間點，但視窗選取仍依 28 影格規則。"""

TEMPORAL_INTERPOLATION_ANCHOR_STRIDE = 2
"""時間內插模式的錨點間距，以原始 6 小時資料影格數表示。

值 2 代表每 12 小時取一個真實錨點，再在兩錨點中間建立一個 6 小時展示影格。
這個設定使每段 28 格仍輸出 28 格：15 格為真實錨點、13 格為線性內插，而不需
改變影片 4 fps 或 16 秒長度。最後一個 6 小時端點保留真實值，不做外插。
"""

TEMPORAL_INTERPOLATION_ALPHA = 0.5
"""每個虛擬影格在左右 12 小時錨點間的線性內插比例；0.5 代表正中央。"""

DEFAULT_TARGET_ARROWS = 420
"""每個面板的目標箭頭數；比 10 km 參考 GIF 稀疏，仍能顯示 1 km 區域流向。"""

DEFAULT_QUIVER_SCALE_MULTIPLIER = 20.0
"""箭頭長度縮放倍數；數值越小箭頭越長，20 是局部區域中等偏保守的畫面尺度。

實際畫面圖例不再把資料 p95 當作圖例值，而是直接呼叫 Matplotlib quiverkey 的
``U=1.0``。因此圖例箭頭會依同一個 quiver scale 計算，標示確實代表 1 m/s；
``quiver_reference_mps`` 仍只用來決定畫面密度與長度，不會出現在影片文字中。
"""

DEFAULT_SPEED_ROUNDING_MPS = 0.2
"""固定色階上限取 0.2 m/s 的向上整數倍，讓色條刻度保持簡潔且固定。"""

DEFAULT_CROSS_REGION_VMAX_MPS = 2.2
"""v2 跨四區固定流速色階的基準上限；只有資料 p99.5 超過它才客觀上調。"""

TEXT_COLOR = "#000000"
"""所有觀眾可見文字的固定黑色；避免深藍灰在 864×1080 或簡報縮放後變淡。"""

TITLE_FONT_SIZE_PT = 16.0
"""海域主標題的 point size；在正式 864×1080 輸出中維持明確層級。"""

PHASE_FONT_SIZE_PT = 11.0
"""相位與 UTC 同列資訊的 point size；不再另設第三列資訊文字。"""

CAPTION_FONT_SIZE_PT = 10.5
"""圖外面板 caption 的 point size；單行重建說明仍需在投影片縮放後辨識。"""

AXIS_FONT_SIZE_PT = 9.0
"""經緯度軸名、刻度與色階刻度的最低 point size。"""

ARROW_KEY_FONT_SIZE_PT = 10.5
"""1 m/s 箭頭比例尺文字的 point size，與下方面板 caption 同一水平帶。"""

ARROW_KEY_X = 0.64
"""quiver key 初始錨點；建立 figure 後會依實際文字寬度自動右對齊下方面板。"""

ARROW_KEY_Y = 0.065
"""quiver key 在 figure 座標中的垂直位置；與下方面板圖外 caption 同一底部帶。

此位置與單行下方面板 caption 同列，刻意置於經度軸名下方但靠近畫布底部；在
864×1080 成品中預留約 30–50 px 的下緣安全距離，避免舊版 0.175 位置造成大面積
空白，同時不讓比例尺或文字貼到畫布邊緣。
"""

ARROW_KEY_RIGHT_TOLERANCE_PX = 4.0
"""比例尺群組右緣與下方面板右緣允許的像素差；用於 renderer 與 post-render QA。"""

LAND_COLOR = "#a29d93"
"""中度灰米色真實陸地；只由高解析度向量 polygon 填色，不由保守 raster mask 填色。"""

LAND_EDGE_COLOR = "none"
"""真實陸地不繪製額外深色描邊；海岸輪廓由 anti-aliased vector fill 邊界呈現。"""

LAND_EDGE_WIDTH = 0.0
"""向量陸地邊界線寬度；0 代表不建立深色 artificial coastline stroke。"""

LAND_ANTIALIASED = True
"""向量 polygon 開啟抗鋸齒；只改善指定 GeoJSON 高解析度邊界的像素過渡。"""

QUIVER_COLOR = "#ffffff"
"""固定流速底圖上的白色箭頭主色；箭頭仍代表 u/v 向量而非額外分類。"""

QUIVER_SHADOW_COLOR = "#26343d"
"""白箭頭的低透明度描邊色，提升 viridis 深淺區域中的辨識度。"""

QUIVER_KEY_COLOR = TEXT_COLOR
"""1 m/s 箭頭圖例的文字與箭頭顏色；所有可見文字統一為純黑。"""

ARROW_LEGEND_LABEL = "1（公尺／秒）"
"""影片底部箭頭圖例的固定文字；括號內單位採使用者指定的全形中文格式。"""

COLORBAR_LABEL = "流速（公尺／秒）"
"""右側固定流速色階的完整單一標籤；整行旋轉 90 度並置於色條最外側。"""

DISPLAY_REGION_TITLES = {
    "A": "海域 A（東北角）",
    "B": "海域 B（新竹外海）",
    "C": "海域 C（後灣海域）",
    "D": "海域 D（連江海域）",
}
"""影片畫面使用的四個正式海域主標題；與 manifest 中的科學方法欄位分離。"""

DISPLAY_FORBIDDEN_TOKENS = (
    "PC",
    "K",
    "箭頭基準",
    "六層聯合 SVD 模態之表層分量",
    "K90",
    "K=",
    "解釋變異",
    "主成分",
    "時間振幅",
    "時間權重",
    "前四模態累積流場變異百分比",
    "臺灣東北海域",
    "新竹海域",
)
"""畫面文字自動稽核的禁用字串；底層資料欄位與 README 仍可保留 PC/K90/方法術語。"""

PANEL_BORDER_COLOR = "#d6dde0"
"""雙面板外框的淡灰色，只用於分隔版面。"""

ANALYSIS_OUTSIDE_COLOR = "#ffffff"
"""分析幾何域外的白色底色；它不是海岸線，也不代表陸地。"""

MODEL_OUTSIDE_COLOR = "#e3e8ea"
"""分析幾何域內但未通過模型靜態海洋遮罩的淡中性色。"""

FEATURE_UNAVAILABLE_COLOR = "#eef2f3"
"""靜態海洋格點中未納入表層速度特徵的淡灰色。"""

DYNAMIC_MISSING_COLOR = "#f5f6f6"
"""逐時資料缺值或 invalid 的近白色；與固定陸地色保持明確區別。"""

SEMANTIC_BACKGROUND_COLORS = (
    ANALYSIS_OUTSIDE_COLOR,
    MODEL_OUTSIDE_COLOR,
    FEATURE_UNAVAILABLE_COLOR,
    DYNAMIC_MISSING_COLOR,
    (1.0, 1.0, 1.0, 0.0),
)
"""背景分類順序；exact land 分類保留於資料語意但設為透明，避免 raster mask 形成灰色階梯暈邊。

真實陸地的可見填色只由 exact GeoJSON vector polygon 提供；因此 raster mask 仍可
阻止流速／箭頭進入 land cell 並供 audit 計數，但不再冒充可見海岸線。
"""

FONT_CANDIDATES = (
    "/data/OCM-Preprocessed-Data/animations_svd_modal_context_v1/assets/STHeiti_Medium.ttc",
    "/data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_v1/assets/STHeiti_Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)
"""SERVER 可能存在的 CJK 字型；正式執行可用 `--font-path` 明確指定。"""

REGION_SPECS: dict[str, dict[str, str]] = {
    "A": {
        "name_zh": "臺灣東北",
        "short_name": "northeast_taiwan",
        "svd_dir": "guishan_gongliao_northeast_taiwan_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1",
        "flow_domain_id": "northeast_taiwan_common_cache_v3",
    },
    "B": {
        "name_zh": "新竹",
        "short_name": "hsinchu",
        "svd_dir": "hsinchu_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1",
        "flow_domain_id": "hsinchu_cache_v3",
    },
    "C": {
        "name_zh": "後灣",
        "short_name": "houwan",
        "svd_dir": "houwan_nmmba_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1",
        "flow_domain_id": "houwan_nmmba_cache_v3",
    },
    "D": {
        "name_zh": "連江",
        "short_name": "lienchiang",
        "svd_dir": "lienchiang_flow_domain_surface_z010_020_030_040_050_u_v_eta_available_2024_2025_v1",
        "flow_domain_id": "lienchiang_common_cache_v3",
    },
}
"""簡報四頁對應的 SVD run 與同源 surface cache 對照表；實際 K90 仍從陣列重算。"""


DISPLAY_AXIS_SPECS: dict[str, dict[str, Any]] = {
    "A": {
        "display_extent": [121.3, 122.8, 24.6, 25.5],
        "x_major_values": [121.3, 121.7, 122.0, 122.4, 122.8],
        "y_major_values": [24.6, 24.8, 25.0, 25.3, 25.5],
        "x_major_formatter": "%.1f",
        "y_major_formatter": "%.1f",
        "display_extent_source": "slide_6_static_flow_figures",
        "reference_page": 6,
        "reference_image_path": (
            "/Users/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/"
            "2026-08-17_water_column_report_no_map_gridlines/report_assets/"
            "water_column_svd_surface_pc_pairs_20_modes/cropped_unlabeled/"
            "northeast_taiwan/water_column_mode_01_surface_spatial_report.png"
        ),
        "reference_image_sha256": "d5e4bb0bb8abf2284e20ed6f71006e62e12c7353fabcc55fe8f98fda8290cd51",
    },
    "B": {
        "display_extent": [119.7, 121.2, 24.3, 25.2],
        "x_major_values": [119.7, 120.1, 120.4, 120.8, 121.2],
        "y_major_values": [24.3, 24.5, 24.8, 25.0, 25.2],
        "x_major_formatter": "%.1f",
        "y_major_formatter": "%.1f",
        "display_extent_source": "slide_7_static_flow_figures",
        "reference_page": 7,
        "reference_image_path": (
            "/Users/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/"
            "2026-08-17_water_column_report_no_map_gridlines/report_assets/"
            "water_column_svd_surface_pc_pairs_20_modes/cropped_unlabeled/"
            "hsinchu/water_column_mode_01_surface_spatial_report.png"
        ),
        "reference_image_sha256": "f4aba297d8bd3027506f0b2302dccea0b398818e20500be3f519575bdd770b2c",
    },
    "C": {
        "display_extent": [120.2, 121.6, 21.6, 22.4],
        "x_major_values": [120.2, 120.5, 120.9, 121.3, 121.6],
        "y_major_values": [21.6, 21.8, 22.0, 22.2, 22.4],
        "x_major_formatter": "%.1f",
        "y_major_formatter": "%.1f",
        "display_extent_source": "slide_8_static_flow_figures",
        "reference_page": 8,
        "reference_image_path": (
            "/Users/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/"
            "2026-08-17_water_column_report_no_map_gridlines/report_assets/"
            "water_column_svd_surface_pc_pairs_20_modes/cropped_unlabeled/"
            "houwan_nmmba/water_column_mode_01_surface_spatial_report.png"
        ),
        "reference_image_sha256": "7042267618ff547ec2d432dbaed964422472e63bc3fcea6e222ae8281467398e",
    },
    "D": {
        "display_extent": [119.2, 120.7, 25.8, 26.6],
        "x_major_values": [119.2, 119.6, 119.9, 120.3, 120.7],
        "y_major_values": [25.8, 26.0, 26.2, 26.4, 26.6],
        "x_major_formatter": "%.1f",
        "y_major_formatter": "%.1f",
        "display_extent_source": "slide_9_static_flow_figures",
        "reference_page": 9,
        "reference_image_path": (
            "/Users/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/"
            "2026-08-17_water_column_report_no_map_gridlines/report_assets/"
            "water_column_svd_surface_pc_pairs_20_modes/cropped_unlabeled/"
            "lienchiang/water_column_mode_01_surface_spatial_report.png"
        ),
        "reference_image_sha256": "f45448277268d30179d50337900e5f66dec5cca24f92b3800a8e0a98fb3d9fa5",
    },
}
"""簡報第 6–9 頁各自核對的 display-only 座標規格。

每一組 extent/tick 只控制對應動畫 axes 的展示裁切與座標標籤，資料陣列仍保留正式
SVD 的完整規則網格；`raw_grid_bbox` 會由 `_display_axis_spec_for_region` 動態寫入
manifest，讓研究者同時看到實際資料範圍與簡報版顯示範圍。reference path/hash 是
靜態圖追溯證據，不在 SERVER 渲染時重新讀取，也不會修改 PPTX。A–D 不能共用 C 的
座標；若未來新增區域，才會進入明確標示的 raw-bbox fallback。
"""


@dataclass(frozen=True)
class RegionSpec:
    """一個簡報海域的固定識別資訊。

    `key` 是簡報頁面的 A–D 標記；`svd_dir` 是 server 上 water-column SVD 的實際
    子目錄；`flow_domain_id` 是同源 surface/native cache 的共同 domain 名稱。這些
    值只負責找檔案與寫 manifest，不能取代從 metadata 驗證資料內容。
    """

    key: str
    name_zh: str
    short_name: str
    svd_dir: str
    flow_domain_id: str


@dataclass(frozen=True)
class CacheFrameRef:
    """同源 surface cache 中一個 UTC 時刻的位置。

    月快取採 `time, lat, lon`，`month_dir` 指向單一月份，`offset` 是該月時間軸
    的 row。只保存索引而不把所有月份讀進 RAM，避免 2024–2025 快取在 render 時
    產生不必要的複本。
    """

    month_dir: Path
    offset: int


@dataclass(frozen=True)
class FrameRecord:
    """共同時間軸上的一個可繪製資料影格。

    `svd_index` 用於取 PC 與模態，`full_index` 用於時間稽核，`cache_ref` 用於
    同源原始 u/v 讀取。三者必須指向精確相同的 UTC epoch ns；不能以鄰近時間代替。
    """

    common_order: int
    time_ns: int
    svd_index: int
    full_index: int
    cache_ref: CacheFrameRef | None
    phase: str


@dataclass
class Payload:
    """一個畫面所需的兩組表層向量與速度。

    所有陣列都是 `lat, lon` 二維格點、單位 m/s。`raw` 可以是同源 surface cache
    或 fallback 的外部全臺產品重網格結果；`reconstruction` 一律是 SVD surface
    level=0 的 raw-PC 重建。這些供 renderer 使用的陣列只套用 SVD／分析／逐時
    有效性遮罩；保守的 exact-land raster mask 仍保留在 `RegionDataset.plot_mask`
    供 audit，而可見真實陸地由高解析度 vector polygon 在最上層覆蓋。這個分離
    允許海色延伸到 vector coastline，不會在 polygon 外側留下一公里格點白階梯，
    同時不把正式 SVD 的係數或原始 cache 回寫。
    """

    record: FrameRecord
    pc_standardized: np.ndarray
    raw_u: np.ndarray
    raw_v: np.ndarray
    raw_speed: np.ndarray
    reconstruction_u: np.ndarray
    reconstruction_v: np.ndarray
    reconstruction_speed: np.ndarray
    # 展示用時間內插只改變 renderer 看到的 payload；原始資料、SVD 與 FrameRecord
    # 仍保持可追溯。`interpolation_source_times_ns` 保存左右真實錨點，便於 QA 判斷
    # 虛擬影格是否真的落在相鄰觀測之間，而不是不透明地複製或外插數值。
    is_temporal_interpolated: bool = False
    interpolation_alpha: float | None = None
    interpolation_source_times_ns: tuple[int, int] | None = None


@dataclass
class RegionDataset:
    """一個區域的 SVD、同源快取、時間交集與視窗選取結果。

    大型 SVD mode 與月快取陣列都以 memory-map 保留；只有代表性 56 影格在建立
    `Payload` 時 materialize。啟用時間內插時，仍只在這 56 個影格的記憶體複本上
    建立展示 payload，不需要將 17,000 個小時的完整速度矩陣載入記憶體，也不會
    改寫 server 原始檔或正式 SVD。
    """

    spec: RegionSpec
    svd_dir: Path
    cache_root: Path
    svd_metadata: dict[str, Any]
    cache_metadata: dict[str, Any]
    lon: np.ndarray
    lat: np.ndarray
    static_ocean_mask: np.ndarray
    analysis_geometry_mask: np.ndarray
    velocity_feature_mask_surface: np.ndarray
    coastline_land_mask: np.ndarray
    land_rings: list[np.ndarray]
    coastline_summary: dict[str, Any]
    display_axis_spec: dict[str, Any]
    plot_mask: np.ndarray
    render_mask: np.ndarray
    """供可見流速／箭頭使用的 SVD 有效域遮罩；真實岸線由 vector overlay 定義。

    它刻意不扣除 conservative `coastline_land_mask`，因為把整個相交 cell 變成
    NaN 會在高解析度 polygon 外側產生階梯空白。向量 polygon 會在 rasterize 後
    的最高 z-order 覆蓋真實陸地；`plot_mask` 則仍負責資料／land audit 語意。
    """
    svd_time_ns: np.ndarray
    full_index_by_svd_index: dict[int, int]
    cache_ref_by_time_ns: dict[int, CacheFrameRef]
    common_svd_indices: np.ndarray
    common_full_indices: np.ndarray
    common_time_ns: np.ndarray
    mean_u_surface: np.ndarray
    mean_v_surface: np.ndarray
    mode_u_surface: np.ndarray
    mode_v_surface: np.ndarray
    pc_raw: np.ndarray
    pc_standardized: np.ndarray
    cumulative_explained: np.ndarray
    k90: int
    cache_meta_hashes: dict[str, str]
    source_mode: str
    interpolation_method: str
    selected_records: list[FrameRecord]
    payloads: list[Payload]
    selection_details: dict[str, Any]
    speed_scale_vmax: float = 0.0
    speed_scale_p995: float = 0.0
    quiver_reference_mps: float = 0.0
    temporal_interpolation_summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class RenderScene:
    """可重複更新的 Matplotlib 圖層。

    為了避免每個時間點重新建立 figure，scene 建立一次後只更新兩個 pcolormesh、
    兩組 quiver 與 phase/UTC 文字。前四模態資訊列不建立，避免把靜態研究圖的
    累積量誤套到動畫；這不改變資料，只降低 64 影格 MP4 的繪圖開銷並確保所有
    影格使用完全相同的色階、座標範圍與箭頭尺度。
    """

    fig: Any
    axes: tuple[Any, Any]
    background_meshes: tuple[Any, Any]
    meshes: tuple[Any, Any]
    quivers: tuple[Any, Any]
    phase_text: Any
    top_label: Any
    bottom_label: Any
    colorbar: Any
    arrow_key: Any
    arrow_key_layout: dict[str, Any]
    axis_tick_layout: dict[str, Any]
    cmap: Any
    norm: Any
    land_patch_counts: tuple[int, int]


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """以串流方式計算檔案 SHA-256，避免大型 MP4 產生額外記憶體複本。

    Args:
        path: 已存在的本機或 server 路徑。
        chunk_size: 每次讀取的位元組數；只影響 I/O，不影響雜湊結果。

    Returns:
        64 字元十六進位 SHA-256 字串，可放入 manifest 作為成果完整性指紋。
    """

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    """讀取 UTF-8 JSON 並在路徑錯誤時提供可追溯訊息。"""

    if not path.is_file():
        raise FileNotFoundError(f"找不到必要 JSON：{path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根節點必須是 object：{path}")
    return value


def parse_epoch_ns(values: np.ndarray | Sequence[Any]) -> np.ndarray:
    """將 ISO UTC 字串或 int64 epoch ns 統一為一維 int64 UTC 時間軸。

    全臺產品使用 `time_iso.npy` 字串，SVD/cache 使用 `time_utc_ns.npy` 整數；若
    不先統一型別，直接用字串或 Python datetime 比對可能掩蓋奈秒級差異。本函式
    不修補時間、不排序也不去重，排序/去重責任留在上游 metadata 已記錄的 canonical
    時間軸，避免在動畫端偷偷改變研究資料。
    """

    array = np.asarray(values)
    if array.ndim != 1:
        raise ValueError(f"時間陣列必須是一維，收到 {array.shape}")
    if np.issubdtype(array.dtype, np.integer):
        return array.astype(np.int64, copy=False)
    result = np.empty(array.size, dtype=np.int64)
    for index, value in enumerate(array.tolist()):
        text = str(value)
        # numpy 可解析尾端 Z；若來源沒有時區，依 OCM 既有時間慣例視為 UTC。
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        result[index] = np.datetime64(text, "ns").astype("int64")
    return result


def find_cjk_font(explicit_path: Path | None) -> Any | None:
    """尋找可繪製繁體中文地名、軸名與單位的字型。

    SERVER base 環境不一定預裝 CJK 字型；若指定 `--font-path`，函式先驗證該路徑，
    再嘗試 renderer 內建候選。找不到時仍繼續輸出，以便用英文 fallback 做資料 QA，
    但 manifest 會記錄空字型路徑，提醒使用者成果需在有 CJK 字型環境重繪。
    """

    from matplotlib.font_manager import FontProperties

    candidates: list[Path] = []
    if explicit_path is not None:
        candidates.append(explicit_path)
    candidates.extend(Path(value) for value in FONT_CANDIDATES)
    for candidate in candidates:
        if candidate.is_file():
            return FontProperties(fname=str(candidate))
    return None


def font_with_size(font: Any | None, size_pt: float) -> Any:
    """回傳套用指定 point size 的 CJK FontProperties。

    Matplotlib 的 ``fig.text`` 可以直接接受 ``fontsize``，但 ``QuiverKey`` 主要
    透過 ``fontproperties`` 設定文字，若沿用預設字級會使比例尺在縮小投影片中
    變得過小。此 helper 複製既有字型而不改動共享物件，讓標題、相位、caption、
    軸與比例尺各自維持明確字級；找不到 CJK 字型時仍回傳指定大小的 fallback。
    """

    from matplotlib.font_manager import FontProperties

    if font is None:
        return FontProperties(size=size_pt)
    sized = font.copy()
    sized.set_size(size_pt)
    return sized


def build_region_specs(selected: Iterable[str]) -> list[RegionSpec]:
    """依使用者指定順序建立 A–D 區域規格，並拒絕未定義代號。"""

    specs: list[RegionSpec] = []
    for raw_key in selected:
        key = raw_key.strip().upper()
        if key not in REGION_SPECS:
            raise ValueError(f"未知海域代號 {raw_key!r}；可用 A、B、C、D")
        info = REGION_SPECS[key]
        specs.append(RegionSpec(key=key, **info))
    if not specs:
        raise ValueError("至少要指定一個海域")
    return specs


def load_full_product_audit(full_product_dir: Path) -> dict[str, Any]:
    """讀取全臺 1 km 6 小時產品的時間/QC 欄位，建立共同時間稽核基準。

    本函式刻意不載入大型全臺 u/v 矩陣；同源模式只需要 `time_iso`、`source_valid`、
    `imputed` 和 `time_status` 來驗證 2024–2025 的精確共同時刻。若後續切換到外部
    fallback，`u_surface/v_surface` 才由專用函式按單幀 memory-map 讀取。全臺產品的
    6 小時缺日與 40 個線性補值時刻因此不會被誤當成可用原始觀測。
    """

    metadata = read_json(full_product_dir / "metadata.json")
    time_iso = np.load(full_product_dir / "time_iso.npy", allow_pickle=False)
    time_ns = parse_epoch_ns(time_iso)
    source_valid = np.load(full_product_dir / "source_valid.npy", allow_pickle=False).astype(bool)
    imputed = np.load(full_product_dir / "imputed.npy", allow_pickle=False).astype(bool)
    time_status = np.load(full_product_dir / "time_status.npy", allow_pickle=False)
    lengths = {len(time_ns), len(source_valid), len(imputed), len(time_status)}
    if len(lengths) != 1:
        raise ValueError("全臺產品 time_iso/source_valid/imputed/time_status 長度不一致")
    if metadata.get("time_axis", {}).get("time_step_hours") != COMMON_INTERVAL_HOURS:
        raise ValueError("全臺產品不是規格要求的 6 小時時間軸")
    return {
        "dir": full_product_dir,
        "metadata": metadata,
        "metadata_sha256": sha256_file(full_product_dir / "metadata.json"),
        "time_ns": time_ns,
        "source_valid": source_valid,
        "imputed": imputed,
        "time_status": np.asarray(time_status).astype(str),
        "time_count": int(time_ns.size),
    }


def load_month_cache_index(cache_root: Path) -> tuple[dict[int, CacheFrameRef], dict[str, str], dict[str, Any]]:
    """掃描同源 surface cache 的 2024–2025 月資料並建立 UTC→row 索引。

    每個月的必要欄位是 `time_utc_ns.npy`、`u_surface_mps.npy`、`v_surface_mps.npy`、
    `valid_mask_surface.npy`；這些欄位的 shape 由該月 metadata 驗證。索引只儲存
    Path/row，真正的速度資料仍以 memory-map 延後到選定的 56 影格才讀取。若不同月
    出現重複 UTC 時刻，保留排序後較晚月份 row 並記錄 duplicate count；正式 SVD
    metadata 已採 prefer-last canonical policy，這裡沿用相同方向且不靜默掩蓋重複。
    """

    if not cache_root.is_dir():
        raise FileNotFoundError(f"找不到同源 surface cache 根目錄：{cache_root}")
    refs: dict[int, CacheFrameRef] = {}
    metadata_hashes: dict[str, str] = {}
    month_summary: dict[str, Any] = {"ready_months": [], "skipped_months": [], "duplicate_time_count": 0}
    month_dirs = sorted(
        path for path in cache_root.glob("months/20*") if path.is_dir() and path.name.isdigit()
    )
    for month_dir in month_dirs:
        metadata_path = month_dir / "metadata.json"
        if not metadata_path.is_file():
            month_summary["skipped_months"].append({"month": month_dir.name, "reason": "metadata_missing"})
            continue
        metadata = read_json(metadata_path)
        if metadata.get("status") != "ready":
            month_summary["skipped_months"].append({"month": month_dir.name, "reason": metadata.get("status")})
            continue
        arrays = metadata.get("arrays", {})
        required = (
            "time_utc_ns.npy",
            "u_surface_mps.npy",
            "v_surface_mps.npy",
            "valid_mask_surface.npy",
        )
        missing = [name for name in required if not (month_dir / name).is_file()]
        if missing:
            month_summary["skipped_months"].append({"month": month_dir.name, "reason": "arrays_missing", "files": missing})
            continue
        time_ns = parse_epoch_ns(np.load(month_dir / "time_utc_ns.npy", allow_pickle=False))
        expected_shape = tuple(arrays.get("u_surface_mps.npy", {}).get("shape", []))
        if expected_shape and expected_shape[0] != time_ns.size:
            raise ValueError(f"{month_dir} u_surface 時間長與 metadata 不一致")
        for offset, value in enumerate(time_ns.tolist()):
            if int(value) in refs:
                month_summary["duplicate_time_count"] += 1
            refs[int(value)] = CacheFrameRef(month_dir=month_dir, offset=offset)
        metadata_hashes[month_dir.name] = sha256_file(metadata_path)
        month_summary["ready_months"].append(month_dir.name)
    if not refs:
        raise ValueError(f"同源 surface cache 沒有可用月份：{cache_root}")
    month_summary["time_count_indexed"] = len(refs)
    return refs, metadata_hashes, month_summary


def load_region_dataset(
    spec: RegionSpec,
    *,
    svd_base: Path,
    surface_cache_base: Path,
    full_audit: dict[str, Any],
    coastline_geojson: Path,
    svd_directory_suffix: str = "",
) -> RegionDataset:
    """載入並驗證一個區域的 SVD、同源快取與精確共同時間軸。

    驗證重點包括：SVD surface index=0 的 `mean/mode` 維度、raw PC 與標準化 PC 的
    time 長度、K90 是否由 cumulative explained variance 重新計算、cache grid 的
    lon/lat/mask 是否與 SVD grid 相同，以及全臺產品只保留 source-valid 且非 imputed
    時刻。岸線遮罩由同一份 WGS84 GeoJSON 以 cell center/corner/ring-vertex 接觸規則
    rasterize；`plot_mask` 以 conservative land mask 供資料／稽核，`render_mask` 則
    保留 SVD 有效海洋格點，讓可見岸線完全由高解析度 vector polygon 決定。若直接
    把相交 cell 整格變成 NaN，會在 polygon 外側產生階梯白邊，因此兩種遮罩不能在
    renderer 中混為同一個角色。若 grid 不完全相同，本函式會將狀態設為 fallback 由
    外部產品處理；正式同源 cache 需通過以下的同網格檢查，因而不會以錯位像素直接相減。
    """

    # 正式動畫必須保留既有簡報所依據的 SVD；因此正式命令以空後綴讀取
    # 2026-08-13 water_column_svd。非空後綴僅保留給診斷性試驗，不能混入正式 manifest。
    svd_dir = svd_base / f"{spec.svd_dir}{svd_directory_suffix}"
    cache_root = surface_cache_base / spec.flow_domain_id
    config = read_json(svd_dir / "config.json")
    metadata = read_json(svd_dir / "metadata.json")
    if config.get("vertical_sampling", {}).get("surface_velocity_source") != "published_ocm_surface_u_v":
        raise ValueError(f"{spec.key} SVD 的 surface velocity source 不符合 published OCM surface u/v")

    lon = np.load(svd_dir / "lon.npy", allow_pickle=False).astype(np.float64, copy=False)
    lat = np.load(svd_dir / "lat.npy", allow_pickle=False).astype(np.float64, copy=False)
    # 顯示座標與正式資料網格分離：C 區 display extent 依簡報第 8 頁靜態流場圖核對，
    # 但原始 lon/lat 仍完整保留供 pcolormesh、岸線 audit 與 SVD 重建使用。此步驟
    # 只驗證簡報裁切範圍沒有超出資料 bbox，不會改寫或重採樣任何速度陣列。
    display_axis_spec = _display_axis_spec_for_region(spec.key, lon, lat)
    static_mask = np.load(svd_dir / "static_ocean_mask.npy", allow_pickle=False).astype(bool)
    geometry_mask = np.load(svd_dir / "analysis_geometry_mask.npy", allow_pickle=False).astype(bool)
    velocity_mask_all = np.load(svd_dir / "velocity_feature_mask.npy", allow_pickle=False).astype(bool)
    if velocity_mask_all.ndim != 3 or velocity_mask_all.shape[0] < 1:
        raise ValueError(f"{spec.key} velocity_feature_mask 維度不含 surface level")
    velocity_mask_surface = velocity_mask_all[0]
    expected_grid_shape = (lat.size, lon.size)
    for name, array in (
        ("static_ocean_mask", static_mask),
        ("analysis_geometry_mask", geometry_mask),
        ("velocity_feature_mask[0]", velocity_mask_surface),
    ):
        if array.shape != expected_grid_shape:
            raise ValueError(f"{spec.key} {name} shape={array.shape} != {expected_grid_shape}")
    coastline_land_mask, coastline_summary = build_coastline_land_mask(lon, lat, coastline_geojson)
    land_rings = load_outer_rings(coastline_geojson)
    # `plot_mask` 是保守的科學／稽核 mask：凡 cell center、角點或 ring vertex 接觸
    # exact polygon 就整格排除，確保 land audit 可回答「保守 exact-land cell 上不留
    # 有限流速」。但它若直接拿來畫 pcolormesh，會把 polygon 外側海水一併挖空。
    plot_mask = static_mask & geometry_mask & velocity_mask_surface & ~coastline_land_mask
    # `render_mask` 只排除 SVD 有效域外；在可見畫布上由高解析度 vector polygon
    # 覆蓋真實陸地，讓 polygon 外側的部分 cell 保留海色與流場，不再出現 raster 白階梯。
    render_mask = static_mask & geometry_mask & velocity_mask_surface

    svd_time_ns = parse_epoch_ns(np.load(svd_dir / "time_utc_ns.npy", allow_pickle=False))
    pc_raw = np.load(svd_dir / "pc.npy", mmap_mode="r")
    pc_standardized = np.load(svd_dir / "pc_standardized.npy", mmap_mode="r")
    if pc_raw.ndim != 2 or pc_standardized.shape != pc_raw.shape or pc_raw.shape[1] != svd_time_ns.size:
        raise ValueError(f"{spec.key} PC/time 維度不一致：pc={pc_raw.shape}, time={svd_time_ns.shape}")
    cumulative = np.load(svd_dir / "cumulative_explained_variance.npy", allow_pickle=False).astype(np.float64)
    if cumulative.ndim != 1 or cumulative.size < 4:
        raise ValueError(f"{spec.key} cumulative explained variance 不足四個模態")
    k90_indices = np.flatnonzero(cumulative >= 0.90)
    if k90_indices.size == 0:
        raise ValueError(f"{spec.key} 前 {cumulative.size} 個模態未達 90%")
    k90 = int(k90_indices[0] + 1)
    mean_u = np.load(svd_dir / "mean_u_mps.npy", mmap_mode="r")
    mean_v = np.load(svd_dir / "mean_v_mps.npy", mmap_mode="r")
    mode_u = np.load(svd_dir / "mode_u_mps_per_raw_pc.npy", mmap_mode="r")
    mode_v = np.load(svd_dir / "mode_v_mps_per_raw_pc.npy", mmap_mode="r")
    for name, array in (("mean_u", mean_u), ("mean_v", mean_v)):
        if array.ndim != 3 or array.shape[0] < 1 or tuple(array.shape[-2:]) != expected_grid_shape:
            raise ValueError(f"{spec.key} {name} 不是 [layer, lat, lon]：{array.shape}")
    for name, array in (("mode_u", mode_u), ("mode_v", mode_v)):
        if array.ndim != 4 or array.shape[0] < k90 or array.shape[1] < 1 or tuple(array.shape[-2:]) != expected_grid_shape:
            raise ValueError(f"{spec.key} {name} 不是 [mode, layer, lat, lon]：{array.shape}")

    cache_grid_dir = cache_root / "grid"
    cache_refs: dict[int, CacheFrameRef] = {}
    cache_hashes: dict[str, str] = {}
    cache_summary: dict[str, Any] = {
        "ready_months": [],
        "skipped_months": [],
        "duplicate_time_count": 0,
        "time_count_indexed": 0,
    }
    same_grid = False
    # 同源快取是首選，但 fallback 的責任是讓缺 cache 的情況仍能輸出「外部對照」；
    # 因此先容許 cache root 缺失，再以全臺產品的 u/v 做明確重網格，而不是半途
    # 把外部資料誤標成同源原始流場。
    if cache_root.is_dir() and (cache_grid_dir / "lon.npy").is_file():
        cache_refs, cache_hashes, cache_summary = load_month_cache_index(cache_root)
        cache_lon = np.load(cache_grid_dir / "lon.npy", allow_pickle=False).astype(np.float64)
        cache_lat = np.load(cache_grid_dir / "lat.npy", allow_pickle=False).astype(np.float64)
        cache_static = np.load(cache_grid_dir / "mask_static.npy", allow_pickle=False).astype(bool)
        same_grid = (
            cache_lon.shape == lon.shape
            and cache_lat.shape == lat.shape
            and cache_static.shape == static_mask.shape
            and np.allclose(cache_lon, lon, rtol=0.0, atol=1e-9)
            and np.allclose(cache_lat, lat, rtol=0.0, atol=1e-9)
            and np.array_equal(cache_static, static_mask)
        )
    source_mode = "same_source_surface_cache" if same_grid else "external_full_taiwan_product"
    interpolation_method = (
        "none; same flow-domain surface cache grid and SVD grid verified identical"
        if same_grid
        else "bilinear interpolation of external full-Taiwan u/v onto SVD grid; finite-corner mask required"
    )

    full_time_ns = full_audit["time_ns"]
    full_valid = full_audit["source_valid"] & ~full_audit["imputed"]
    svd_index_by_time: dict[int, int] = {}
    for index, value in enumerate(svd_time_ns.tolist()):
        svd_index_by_time[int(value)] = index
    common_svd: list[int] = []
    common_full: list[int] = []
    common_time: list[int] = []
    for full_index, value in enumerate(full_time_ns.tolist()):
        value_int = int(value)
        if not bool(full_valid[full_index]):
            continue
        svd_index = svd_index_by_time.get(value_int)
        if svd_index is None or (source_mode == "same_source_surface_cache" and value_int not in cache_refs):
            continue
        common_full.append(full_index)
        common_svd.append(svd_index)
        common_time.append(value_int)
    order = np.argsort(np.asarray(common_time, dtype=np.int64), kind="stable")
    common_full_array = np.asarray(common_full, dtype=np.int64)[order]
    common_svd_array = np.asarray(common_svd, dtype=np.int64)[order]
    common_time_array = np.asarray(common_time, dtype=np.int64)[order]
    if common_time_array.size < WINDOW_FRAME_COUNT * 2:
        raise ValueError(f"{spec.key} 精確共同時間不足兩段 28 影格視窗：{common_time_array.size}")

    grid_metadata_path = cache_grid_dir / "metadata.json"
    cache_metadata = {
        "grid_dir": str(cache_grid_dir),
        "grid_metadata_sha256": sha256_file(grid_metadata_path) if grid_metadata_path.is_file() else None,
        "mask_static_sha256": sha256_file(cache_grid_dir / "mask_static.npy")
        if (cache_grid_dir / "mask_static.npy").is_file()
        else None,
        "index_summary": cache_summary,
    }
    return RegionDataset(
        spec=spec,
        svd_dir=svd_dir,
        cache_root=cache_root,
        svd_metadata={"config": config, "metadata": metadata},
        cache_metadata=cache_metadata,
        lon=lon,
        lat=lat,
        static_ocean_mask=static_mask,
        analysis_geometry_mask=geometry_mask,
        velocity_feature_mask_surface=velocity_mask_surface,
        coastline_land_mask=coastline_land_mask,
        land_rings=land_rings,
        coastline_summary=coastline_summary,
        display_axis_spec=display_axis_spec,
        plot_mask=plot_mask,
        render_mask=render_mask,
        svd_time_ns=svd_time_ns,
        full_index_by_svd_index={int(s): int(f) for s, f in zip(common_svd_array.tolist(), common_full_array.tolist())},
        cache_ref_by_time_ns=cache_refs,
        common_svd_indices=common_svd_array,
        common_full_indices=common_full_array,
        common_time_ns=common_time_array,
        mean_u_surface=mean_u[0],
        mean_v_surface=mean_v[0],
        mode_u_surface=mode_u[:, 0],
        mode_v_surface=mode_v[:, 0],
        pc_raw=pc_raw,
        pc_standardized=pc_standardized,
        cumulative_explained=cumulative,
        k90=k90,
        cache_meta_hashes=cache_hashes,
        source_mode=source_mode,
        interpolation_method=interpolation_method,
        selected_records=[],
        payloads=[],
        selection_details={},
    )


def _window_is_contiguous(time_ns: np.ndarray, start: int, count: int) -> bool:
    """檢查候選視窗內每個時間點是否都是精確 6 小時連續資料。"""

    window = time_ns[start : start + count]
    if window.size != count:
        return False
    return bool(np.all(np.diff(window) == COMMON_INTERVAL_HOURS * NANOS_PER_HOUR))


def select_phase_windows(dataset: RegionDataset) -> list[FrameRecord]:
    """依 PC1 標準化值選取互不重疊的強正/強負相位 28 影格視窗。

    候選視窗的中心取 28 影格中央偏後的第 15 個 frame，評分為該中心時刻的
    `abs(pc_standardized[0])`；先依絕對值由大到小，再以 UTC 時間排序。先選最佳
    正相位，再選與其不重疊的最佳負相位；若候選靠邊、跨過缺口或與前一段重疊，
    就依確定性順序取下一候選。這是代表性相位案例的畫面取樣規則，不是全年統計
    驗證；所有候選數與選定起訖時間會寫入 manifest。
    """

    time_ns = dataset.common_time_ns
    svd_indices = dataset.common_svd_indices
    pc1 = np.asarray(dataset.pc_standardized[0, svd_indices], dtype=np.float64)
    candidates: dict[str, list[dict[str, Any]]] = {"positive": [], "negative": []}
    center_offset = WINDOW_FRAME_COUNT // 2
    for start in range(0, time_ns.size - WINDOW_FRAME_COUNT + 1):
        if not _window_is_contiguous(time_ns, start, WINDOW_FRAME_COUNT):
            continue
        center = start + center_offset
        value = float(pc1[center])
        phase = "positive" if value > 0.0 else "negative" if value < 0.0 else "zero"
        if phase == "zero":
            continue
        candidates[phase].append(
            {
                "start": start,
                "end": start + WINDOW_FRAME_COUNT - 1,
                "center": center,
                "pc1_standardized": value,
                "abs_pc1_standardized": abs(value),
                "start_time_ns": int(time_ns[start]),
                "end_time_ns": int(time_ns[start + WINDOW_FRAME_COUNT - 1]),
            }
        )
    for phase in candidates:
        candidates[phase].sort(key=lambda item: (-item["abs_pc1_standardized"], item["center"]))
    if not candidates["positive"] or not candidates["negative"]:
        raise ValueError(f"{dataset.spec.key} 找不到完整正/負 PC1 7 日候選視窗")
    positive = candidates["positive"][0]
    try:
        negative = next(
            item
            for item in candidates["negative"]
            if item["end"] < positive["start"] or item["start"] > positive["end"]
        )
    except StopIteration as exc:
        raise ValueError(f"{dataset.spec.key} 的正/負 PC1 候選視窗無法排成兩段互不重疊的 28 影格") from exc
    selected: list[FrameRecord] = []
    for phase, item in (("positive", positive), ("negative", negative)):
        for order in range(item["start"], item["end"] + 1):
            time_value = int(time_ns[order])
            selected.append(
                FrameRecord(
                    common_order=order,
                    time_ns=time_value,
                    svd_index=int(svd_indices[order]),
                    full_index=int(dataset.common_full_indices[order]),
                    cache_ref=dataset.cache_ref_by_time_ns.get(time_value),
                    phase=phase,
                )
            )
    dataset.selected_records = selected
    dataset.selection_details = {
        "candidate_count_positive": len(candidates["positive"]),
        "candidate_count_negative": len(candidates["negative"]),
        "center_offset_frame": center_offset,
        "rule": "complete 28-frame contiguous 6-hour window; PC1 abs priority; positive first; negative non-overlap",
        "positive": positive,
        "negative": negative,
    }
    return selected


def _read_same_source_frame(
    dataset: RegionDataset,
    record: FrameRecord,
    valid_domain_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """從同源 surface 月快取讀取單一 `lat,lon` u/v 並套用指定有效域。

    `valid_domain_mask` 預設使用保守 `plot_mask`，可供需要嚴格 land audit 的呼叫端
    使用；動畫 materialization 則傳入 `render_mask`，讓資料在 vector coastline 外側
    的部分 cell 保留，避免整格 NaN 形成白色階梯。兩者都會再與逐時 valid、u/v
    finite 交集，絕不以零值填補缺值，也不修改月快取檔案。
    """

    if record.cache_ref is None:
        raise ValueError("同源表層影格缺少 cache reference")
    month_dir = record.cache_ref.month_dir
    offset = record.cache_ref.offset
    u_array = np.load(month_dir / "u_surface_mps.npy", mmap_mode="r")
    v_array = np.load(month_dir / "v_surface_mps.npy", mmap_mode="r")
    valid_array = np.load(month_dir / "valid_mask_surface.npy", mmap_mode="r")
    if u_array.ndim != 3 or tuple(u_array.shape[1:]) != dataset.plot_mask.shape:
        raise ValueError(f"{dataset.spec.key} 同源 u shape 不符：{u_array.shape}")
    if v_array.shape != u_array.shape or valid_array.shape != u_array.shape:
        raise ValueError(f"{dataset.spec.key} 同源 u/v/valid shape 不一致")
    u = np.asarray(u_array[offset], dtype=np.float32).copy()
    v = np.asarray(v_array[offset], dtype=np.float32).copy()
    # memory-map row 是 read-only；這裡必須建立可寫複本，才能把靜態 SVD mask 與
    # 逐時 valid mask 合併。若直接對 mmap view 做 `&=`，會在第一個影格就拋出
    # read-only assignment，且不會留下可追溯的 partial payload。
    valid = np.asarray(valid_array[offset], dtype=bool).copy()
    domain_mask = dataset.plot_mask if valid_domain_mask is None else np.asarray(valid_domain_mask, dtype=bool)
    if domain_mask.shape != dataset.plot_mask.shape:
        raise ValueError(f"{dataset.spec.key} valid_domain_mask shape 不符：{domain_mask.shape}")
    valid &= domain_mask & np.isfinite(u) & np.isfinite(v)
    u[~valid] = np.nan
    v[~valid] = np.nan
    return u, v


def _bilinear_external_frame(
    full_product: dict[str, Any],
    full_index: int,
    target_lon: np.ndarray,
    target_lat: np.ndarray,
    target_mask: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """將外部全臺規則網格的 u/v 以有限四角雙線性內插至 SVD 網格。

    這是同源快取缺失時的明確 fallback，不能拿來與 SVD 重建做嚴格殘差。四個
    包圍格點必須都有限且落在外部 mask 內，否則輸出 NaN；寧可保留缺值也不以近鄰
    或零值跨越海岸線填補。程式仍以 memory-map 讀取全臺年度陣列，單次只 materialize
    一個 780×409 影格。
    """

    from scipy.interpolate import RegularGridInterpolator

    full_dir = full_product["dir"]
    lon = np.load(full_dir / "lon.npy", allow_pickle=False).astype(np.float64)
    lat = np.load(full_dir / "lat.npy", allow_pickle=False).astype(np.float64)
    static_mask = np.load(full_dir / "mask.npy", allow_pickle=False).astype(bool)
    u_source = np.asarray(np.load(full_dir / "u_surface.npy", mmap_mode="r")[full_index], dtype=np.float64).copy()
    v_source = np.asarray(np.load(full_dir / "v_surface.npy", mmap_mode="r")[full_index], dtype=np.float64).copy()
    u_source[~static_mask] = np.nan
    v_source[~static_mask] = np.nan
    # 明確建立 [lat, lon] 點列，確保與 target_mask 的 lat/lon 展平順序一致。
    lat_mesh, lon_mesh = np.meshgrid(target_lat, target_lon, indexing="ij")
    points = np.column_stack((lat_mesh.ravel(), lon_mesh.ravel()))
    u_interp = RegularGridInterpolator((lat, lon), u_source, bounds_error=False, fill_value=np.nan)(points).reshape(target_mask.shape)
    v_interp = RegularGridInterpolator((lat, lon), v_source, bounds_error=False, fill_value=np.nan)(points).reshape(target_mask.shape)
    valid = target_mask & np.isfinite(u_interp) & np.isfinite(v_interp)
    u_interp[~valid] = np.nan
    v_interp[~valid] = np.nan
    return u_interp.astype(np.float32), v_interp.astype(np.float32)


def _reconstruct_surface(
    dataset: RegionDataset,
    record: FrameRecord,
    valid_domain_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """使用 raw PC 與 surface mode=0 重建 K90 表層 u/v。

    公式明確為 `mean_u[0] + Σ(mode_u[:K90,0] * pc[:K90,t])`，v 分量同理；
    `pc_standardized` 不會進入計算。用 `einsum` 只 materialize 單一時間影格，並
    依呼叫端指定的 domain mask 將無有效物理資料的網格設為 NaN。動畫傳入
    `render_mask`，由高解析度 vector land overlay 隱藏真實陸地；稽核呼叫端可傳入
    conservative `plot_mask`。這個選擇只改變展示邊界，不改正式 SVD 模態或係數。
    """

    coefficients = np.asarray(dataset.pc_raw[: dataset.k90, record.svd_index], dtype=np.float32)
    u = np.asarray(dataset.mean_u_surface, dtype=np.float32).copy()
    v = np.asarray(dataset.mean_v_surface, dtype=np.float32).copy()
    u += np.einsum("k,kyx->yx", coefficients, np.asarray(dataset.mode_u_surface[: dataset.k90], dtype=np.float32))
    v += np.einsum("k,kyx->yx", coefficients, np.asarray(dataset.mode_v_surface[: dataset.k90], dtype=np.float32))
    domain_mask = dataset.plot_mask if valid_domain_mask is None else np.asarray(valid_domain_mask, dtype=bool)
    if domain_mask.shape != dataset.plot_mask.shape:
        raise ValueError(f"{dataset.spec.key} reconstruction valid_domain_mask shape 不符：{domain_mask.shape}")
    valid = domain_mask & np.isfinite(u) & np.isfinite(v)
    u[~valid] = np.nan
    v[~valid] = np.nan
    return u, v


def _speed(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """由東西/南北分量計算水平流速大小，輸出單位為 m/s。"""

    result = np.hypot(u, v).astype(np.float32)
    result[~np.isfinite(u) | ~np.isfinite(v)] = np.nan
    return result


def materialize_payloads(dataset: RegionDataset, full_product: dict[str, Any]) -> list[Payload]:
    """依已選取的兩段視窗 materialize 56 個雙面板資料影格。

    原始面板的來源依 `source_mode` 決定：同源時直接讀 monthly surface cache；
    fallback 時才讀全臺 1 km u/v 並雙線性內插。重建面板均由 SVD raw-PC 公式建立。
    `pc_standardized[0:4]` 仍只保留在 payload 的內部資料結構，不會替代 raw PC 或
    產生觀眾可見數值列。正式畫面使用 `render_mask` 保留 vector coastline 外側的
    部分 cell；conservative `plot_mask` 仍由 manifest/audit 記錄。
    """

    payloads: list[Payload] = []
    for ordinal, record in enumerate(dataset.selected_records, start=1):
        if dataset.source_mode == "same_source_surface_cache":
            raw_u, raw_v = _read_same_source_frame(dataset, record, dataset.render_mask)
        else:
            raw_u, raw_v = _bilinear_external_frame(
                full_product,
                record.full_index,
                dataset.lon,
                dataset.lat,
                dataset.render_mask,
            )
        rec_u, rec_v = _reconstruct_surface(dataset, record, dataset.render_mask)
        pc_std = np.asarray(dataset.pc_standardized[:4, record.svd_index], dtype=np.float32).copy()
        payloads.append(
            Payload(
                record=record,
                pc_standardized=pc_std,
                raw_u=raw_u,
                raw_v=raw_v,
                raw_speed=_speed(raw_u, raw_v),
                reconstruction_u=rec_u,
                reconstruction_v=rec_v,
                reconstruction_speed=_speed(rec_u, rec_v),
            )
        )
        if ordinal % 8 == 0 or ordinal == len(dataset.selected_records):
            print(f"{dataset.spec.key} materialized {ordinal}/{len(dataset.selected_records)} frames", flush=True)
    dataset.payloads = payloads
    return payloads


def _linear_interpolate_field(
    left: np.ndarray,
    right: np.ndarray,
    *,
    alpha: float,
    valid_domain_mask: np.ndarray,
) -> np.ndarray:
    """在兩個有限向量場之間建立一個展示用線性內插場。

    Args:
        left: 左側真實錨點的 `[lat, lon]` u 或 v，單位為 m/s。
        right: 右側真實錨點的 `[lat, lon]` u 或 v，單位為 m/s。
        alpha: 右側錨點權重；本 renderer 固定為 0.5，代表兩錨點正中央。
        valid_domain_mask: SVD/分析有效域的固定遮罩；不在此域內的格點維持 NaN。

    Returns:
        新的 float32 `[lat, lon]` 陣列。只有左右兩端都有限、且位於有效域內的
        格點才會產生內插值；任一端缺值時不以零、鄰近值或外插補洞。

    限制：這是為降低影片影格跳動的展示轉換，不是新增觀測值，也不會寫回任何
    OCM cache。以兩個 12 小時錨點估計中間時間的作法可能無法表達短於 12 小時
    的真實變化，因此必須在 manifest 與 README 明確標示為 display-only。
    """

    left_array = np.asarray(left, dtype=np.float32)
    right_array = np.asarray(right, dtype=np.float32)
    domain_mask = np.asarray(valid_domain_mask, dtype=bool)
    if left_array.shape != right_array.shape or left_array.shape != domain_mask.shape:
        raise ValueError(
            "時間內插陣列與有效域遮罩 shape 不一致："
            f"left={left_array.shape}, right={right_array.shape}, mask={domain_mask.shape}"
        )
    if not 0.0 <= float(alpha) <= 1.0:
        raise ValueError(f"時間內插 alpha 必須在 [0, 1]：{alpha}")
    valid = domain_mask & np.isfinite(left_array) & np.isfinite(right_array)
    # 即使乘法遇到 NaN 會自然產生 NaN，也先建立獨立可寫複本，讓後續遮罩行為
    # 明確且不會意外修改來自 memory-map 或既有 payload 的陣列。
    result = ((1.0 - float(alpha)) * left_array + float(alpha) * right_array).astype(np.float32, copy=True)
    result[~valid] = np.nan
    return result


def _interpolation_difference_summary(
    interpolated_u: np.ndarray,
    interpolated_v: np.ndarray,
    observed_u: np.ndarray,
    observed_v: np.ndarray,
    valid_domain_mask: np.ndarray,
) -> dict[str, Any]:
    """量化虛擬場與被跳過的原始觀測場之差，僅作版本對照診斷。

    這個統計不被解讀為模型誤差或預測誤差；它只回答「用線性展示內插替代該
    6 小時觀測影格時，畫面中的向量改變幅度大約多少」。比較時只取四個向量
    分量都有限且通過 SVD render mask 的格點，避免缺值或分析域外主導結果。
    """

    valid = (
        np.asarray(valid_domain_mask, dtype=bool)
        & np.isfinite(interpolated_u)
        & np.isfinite(interpolated_v)
        & np.isfinite(observed_u)
        & np.isfinite(observed_v)
    )
    if not np.any(valid):
        return {
            "finite_comparison_cell_count": 0,
            "mean_abs_component_difference_mps": None,
            "p95_vector_difference_mps": None,
            "max_vector_difference_mps": None,
        }
    du = np.abs(np.asarray(interpolated_u, dtype=np.float64)[valid] - np.asarray(observed_u, dtype=np.float64)[valid])
    dv = np.abs(np.asarray(interpolated_v, dtype=np.float64)[valid] - np.asarray(observed_v, dtype=np.float64)[valid])
    vector_difference = np.hypot(
        np.asarray(interpolated_u, dtype=np.float64)[valid] - np.asarray(observed_u, dtype=np.float64)[valid],
        np.asarray(interpolated_v, dtype=np.float64)[valid] - np.asarray(observed_v, dtype=np.float64)[valid],
    )
    return {
        "finite_comparison_cell_count": int(np.count_nonzero(valid)),
        "mean_abs_component_difference_mps": float(np.mean(np.concatenate((du, dv)))),
        "p95_vector_difference_mps": float(np.percentile(vector_difference, 95.0)),
        "max_vector_difference_mps": float(np.max(vector_difference)),
    }


def set_temporal_interpolation_disabled(dataset: RegionDataset) -> None:
    """記錄未啟用內插時的時間語意，讓 v3 基準 manifest 也具有明確對照欄位。

    未啟用模式完全沿用每一個精確 6 小時 source-valid payload；這個函式只寫入
    記錄，不複製或修改任何資料陣列。將基準模式也顯式記錄，能避免讀者只看到
    新版本的 `enabled` 欄位而誤以為 v3 的時間步階沒有被驗證。
    """

    dataset.temporal_interpolation_summary = {
        "enabled": False,
        "method": "none; exact 6-hour observed payload display",
        "visualization_only": True,
        "source_observation_interval_hours": COMMON_INTERVAL_HOURS,
        "anchor_interval_hours": COMMON_INTERVAL_HOURS,
        "virtual_frame_interval_hours": None,
        "anchor_stride_source_frames": 1,
        "alpha": None,
        "input_observation_frame_count": int(len(dataset.payloads)),
        "display_payload_frame_count": int(len(dataset.payloads)),
        "displayed_real_anchor_frame_count": int(len(dataset.payloads)),
        "virtual_frame_count": 0,
        "not_used_as_anchor_observation_count": 0,
        "preserves_frame_count_and_duration": True,
        "no_interpolation_across_gap": True,
        "input_data_unchanged": True,
        "note_zh": "未啟用時間內插；每個畫面直接使用精確 6 小時共同時間的原始與重建 payload。",
    }


def apply_temporal_interpolation(dataset: RegionDataset) -> None:
    """將 56 個真實 payload 轉為同長度的展示用時間內插 payload 序列。

    每段 28 個 6 小時觀測影格採固定確定性順序：`t0` 真實錨點、`t1` 由
    `t0/t2` 線性內插、`t2` 真實錨點、依此類推，最後保留 `t26` 與 `t27` 的真實
    端點。因此每段仍輸出 28 格、時間標籤仍為原始 6 小時序列，但其中 13 格
    的向量場是展示用虛擬值；原本的奇數索引觀測只作 QA 對照，不直接畫出。

    raw 面板與既有正式 SVD 的 K90 reconstruction 面板採同一 alpha 與同一有限
    格點規則。重建場因 `mean + mode × pc` 對 pc 是線性的，先對兩端重建場做
    u/v 內插，與對兩端 raw PC 做相同比例內插後再套用既有 mode 在數學上等價；
    renderer 選擇前者以保留「不改正式 SVD」的界線。這裡不跨越視窗、缺值或
    分析域外，不產生科學分析結果，只為同時間長度的視覺平滑比較。
    """

    expected_count = WINDOW_FRAME_COUNT * 2
    if len(dataset.payloads) != expected_count:
        raise ValueError(
            f"時間內插需要正相位 {WINDOW_FRAME_COUNT} + 負相位 {WINDOW_FRAME_COUNT} 個 payload，"
            f"目前為 {len(dataset.payloads)}"
        )
    source_payloads = list(dataset.payloads)
    output_payloads: list[Payload] = []
    window_summaries: list[dict[str, Any]] = []
    for window_start, phase in ((0, "positive"), (WINDOW_FRAME_COUNT, "negative")):
        window = source_payloads[window_start : window_start + WINDOW_FRAME_COUNT]
        if len(window) != WINDOW_FRAME_COUNT:
            raise ValueError(f"{dataset.spec.key} {phase} 時間內插視窗長度不符")
        window_output: list[Payload] = []
        difference_records: list[dict[str, Any]] = []
        virtual_times: list[int] = []
        for anchor_index in range(0, WINDOW_FRAME_COUNT - 2, TEMPORAL_INTERPOLATION_ANCHOR_STRIDE):
            left = window[anchor_index]
            middle = window[anchor_index + 1]
            right = window[anchor_index + 2]
            left_to_right_hours = (int(right.record.time_ns) - int(left.record.time_ns)) / float(NANOS_PER_HOUR)
            if left_to_right_hours != COMMON_INTERVAL_HOURS * TEMPORAL_INTERPOLATION_ANCHOR_STRIDE:
                raise ValueError(
                    f"{dataset.spec.key} {phase} 內插錨點非連續 12 小時："
                    f"{_format_time_utc(left.record.time_ns)} -> {_format_time_utc(right.record.time_ns)}"
                )
            if int(middle.record.time_ns) * 2 != int(left.record.time_ns) + int(right.record.time_ns):
                raise ValueError(f"{dataset.spec.key} {phase} 中間時間不是兩錨點正中央")
            raw_u = _linear_interpolate_field(
                left.raw_u,
                right.raw_u,
                alpha=TEMPORAL_INTERPOLATION_ALPHA,
                valid_domain_mask=dataset.render_mask,
            )
            raw_v = _linear_interpolate_field(
                left.raw_v,
                right.raw_v,
                alpha=TEMPORAL_INTERPOLATION_ALPHA,
                valid_domain_mask=dataset.render_mask,
            )
            reconstruction_u = _linear_interpolate_field(
                left.reconstruction_u,
                right.reconstruction_u,
                alpha=TEMPORAL_INTERPOLATION_ALPHA,
                valid_domain_mask=dataset.render_mask,
            )
            reconstruction_v = _linear_interpolate_field(
                left.reconstruction_v,
                right.reconstruction_v,
                alpha=TEMPORAL_INTERPOLATION_ALPHA,
                valid_domain_mask=dataset.render_mask,
            )
            virtual = Payload(
                record=middle.record,
                pc_standardized=(
                    (1.0 - TEMPORAL_INTERPOLATION_ALPHA) * left.pc_standardized
                    + TEMPORAL_INTERPOLATION_ALPHA * right.pc_standardized
                ).astype(np.float32, copy=True),
                raw_u=raw_u,
                raw_v=raw_v,
                raw_speed=_speed(raw_u, raw_v),
                reconstruction_u=reconstruction_u,
                reconstruction_v=reconstruction_v,
                reconstruction_speed=_speed(reconstruction_u, reconstruction_v),
                is_temporal_interpolated=True,
                interpolation_alpha=TEMPORAL_INTERPOLATION_ALPHA,
                interpolation_source_times_ns=(int(left.record.time_ns), int(right.record.time_ns)),
            )
            window_output.extend((left, virtual))
            virtual_times.append(int(middle.record.time_ns))
            difference_records.append(
                {
                    "display_time_utc": _format_time_utc(middle.record.time_ns),
                    "source_left_time_utc": _format_time_utc(left.record.time_ns),
                    "source_right_time_utc": _format_time_utc(right.record.time_ns),
                    "raw": _interpolation_difference_summary(
                        raw_u,
                        raw_v,
                        middle.raw_u,
                        middle.raw_v,
                        dataset.render_mask,
                    ),
                    "reconstruction": _interpolation_difference_summary(
                        reconstruction_u,
                        reconstruction_v,
                        middle.reconstruction_u,
                        middle.reconstruction_v,
                        dataset.render_mask,
                    ),
                }
            )
        # 28 格視窗的最後兩個來源影格沒有右側 12 小時錨點，不能外插；保留真實
        # t26/t27 端點，既維持案例的精確起訖時間，也避免產生未受資料支持的值。
        window_output.extend((window[-2], window[-1]))
        if len(window_output) != WINDOW_FRAME_COUNT:
            raise RuntimeError(f"{dataset.spec.key} {phase} 內插後影格數錯誤：{len(window_output)}")
        output_payloads.extend(window_output)
        window_summaries.append(
            {
                "phase": phase,
                "input_observation_frame_count": WINDOW_FRAME_COUNT,
                "displayed_real_anchor_frame_count": 15,
                "virtual_frame_count": len(difference_records),
                "not_used_as_anchor_observation_count": len(difference_records),
                "display_payload_frame_count": len(window_output),
                "start_utc": _format_time_utc(window[0].record.time_ns),
                "end_utc": _format_time_utc(window[-1].record.time_ns),
                "virtual_frame_first_time_utc": _format_time_utc(virtual_times[0]) if virtual_times else None,
                "virtual_frame_last_time_utc": _format_time_utc(virtual_times[-1]) if virtual_times else None,
                "interpolation_diagnostics_vs_skipped_observations": difference_records,
            }
        )
    dataset.payloads = output_payloads
    dataset.temporal_interpolation_summary = {
        "enabled": True,
        "method": "piecewise_linear_display_only",
        "visualization_only": True,
        "source_observation_interval_hours": COMMON_INTERVAL_HOURS,
        "anchor_interval_hours": COMMON_INTERVAL_HOURS * TEMPORAL_INTERPOLATION_ANCHOR_STRIDE,
        "virtual_frame_interval_hours": COMMON_INTERVAL_HOURS,
        "anchor_stride_source_frames": TEMPORAL_INTERPOLATION_ANCHOR_STRIDE,
        "alpha": TEMPORAL_INTERPOLATION_ALPHA,
        "input_observation_frame_count": int(len(source_payloads)),
        "display_payload_frame_count": int(len(output_payloads)),
        "displayed_real_anchor_frame_count": 30,
        "virtual_frame_count": int(sum(item["virtual_frame_count"] for item in window_summaries)),
        "not_used_as_anchor_observation_count": int(sum(item["not_used_as_anchor_observation_count"] for item in window_summaries)),
        "preserves_frame_count_and_duration": len(output_payloads) == len(source_payloads),
        "no_interpolation_across_gap": True,
        "input_data_unchanged": True,
        "formula": "field_virtual(t_mid) = (1-alpha) * field_left(t_left) + alpha * field_right(t_right)",
        "mask_policy": "interpolate only where both anchor u/v pairs are finite and render_mask is true; otherwise NaN",
        "window_summaries": window_summaries,
        "note_zh": "為保持 4 fps、16 秒與 64 幀，部分 6 小時畫面以相鄰 12 小時真實錨點的線性展示內插取代；虛擬影格不代表新的觀測資料。",
    }


def choose_speed_and_arrow_scales(dataset: RegionDataset) -> None:
    """以兩段代表性視窗固定一支影片的色階與箭頭參考尺度。

    p99.5 使用 raw 與 K90 reconstruction 兩個面板的所有有效像素，避免單一極端
    網格值決定畫面；色階上限向上取 0.2 m/s 倍數，並在整支影片所有影格固定。箭頭
    參考取同一批向量的 p95，scale multiplier 只負責畫面長度，兩者都寫入 manifest。
    這個固定尺度服務視覺比較，不是新的物理統計量。
    """

    speed_values: list[np.ndarray] = []
    vector_values: list[np.ndarray] = []
    for payload in dataset.payloads:
        for speed, u, v in (
            (payload.raw_speed, payload.raw_u, payload.raw_v),
            (payload.reconstruction_speed, payload.reconstruction_u, payload.reconstruction_v),
        ):
            finite_speed = speed[np.isfinite(speed)]
            finite_vector = np.hypot(u[np.isfinite(u) & np.isfinite(v)], v[np.isfinite(u) & np.isfinite(v)])
            if finite_speed.size:
                speed_values.append(finite_speed.astype(np.float64, copy=False))
            if finite_vector.size:
                vector_values.append(finite_vector.astype(np.float64, copy=False))
    if not speed_values or not vector_values:
        raise ValueError(f"{dataset.spec.key} 兩段視窗沒有有限流速值")
    all_speed = np.concatenate(speed_values)
    all_vectors = np.concatenate(vector_values)
    p995 = float(np.nanpercentile(all_speed, 99.5))
    vmax = max(DEFAULT_SPEED_ROUNDING_MPS, math.ceil(max(p995, 0.05) / DEFAULT_SPEED_ROUNDING_MPS) * DEFAULT_SPEED_ROUNDING_MPS)
    dataset.speed_scale_p995 = p995
    dataset.speed_scale_vmax = float(vmax)
    dataset.quiver_reference_mps = float(max(np.nanpercentile(all_vectors, 95.0), 0.05))


def choose_global_vmax(datasets: Sequence[RegionDataset]) -> float:
    """決定跨 A–D 共用的固定色階上限，預設保留 v1 的 0–2.2 m/s。

    v2 的用途是四區簡報比較，因此不再因區域 p99.5 比例差異而切換成每區色階。
    先以選定視窗所有 raw/reconstruction 像素的最大 p99.5 估計是否需要容納更大的
    強流；若該估計不超過 2.2 m/s，固定回 2.2；若超過，才向上取 0.2 m/s 倍數並
    由 manifest/比較報告說明色階調整。此函式只決定 Normalize 上限，不改變 u/v。
    """

    p995_values = [max(dataset.speed_scale_p995, 0.05) for dataset in datasets]
    maximum = max(p995_values)
    shared = max(
        DEFAULT_CROSS_REGION_VMAX_MPS,
        math.ceil(maximum / DEFAULT_SPEED_ROUNDING_MPS) * DEFAULT_SPEED_ROUNDING_MPS,
    )
    for dataset in datasets:
        dataset.speed_scale_vmax = float(shared)
    return float(shared)


def choose_quiver_step(lon_count: int, lat_count: int, target_arrows: int) -> tuple[int, int]:
    """依規則格點數估算等距箭頭抽樣步距，回傳 `(step_y, step_x)`。

    這是純視覺抽樣，不會降低 pcolormesh 或 SVD 資料解析度；有效海域遮罩再決定
    實際畫出多少支箭頭。局部區域約 15,000 格點、目標 420 支時通常每 6–7 格取一支。
    """

    total = max(lon_count * lat_count, 1)
    step = max(1, int(math.sqrt(total / max(target_arrows, 1))))
    return step, step


def _mask_array(values: np.ndarray) -> np.ma.MaskedArray:
    """把 NaN 轉成 Matplotlib 可更新的 masked array，保留 NaN 不代表零流速。"""

    return np.ma.masked_where(~np.isfinite(values), values)


def _format_time_utc(time_ns: int) -> str:
    """將 epoch ns 顯示成簡潔 UTC ISO 時間，避免使用本機時區。"""

    value = np.datetime64(int(time_ns), "ns")
    text = np.datetime_as_string(value, unit="m")
    return text.replace("T", " ") + " UTC"


def _x_major_tick_values(lon: np.ndarray) -> np.ndarray:
    """建立未完成簡報核對區域的 fallback X 軸 major tick。

    經度資料是 SVD 規則格點的一維中心座標，單位為 °E。為避免每 0.2° 都顯示
    數字造成端點與色階擁擠，major tick 以資料的精確首末值加上以 0.4° 為間距的
    內部刻度；0.2° 間距則交給 minor locator。這個 fallback 只供尚未逐頁核對
    簡報靜態圖的海域，不能取代正式簡報版的 display extent 與 tick 規格；目前
    A–D 會由 `_display_axis_spec_for_region` 使用第 6–9 頁各自的固定刻度。
    """

    values = np.asarray(lon, dtype=np.float64)
    if values.ndim != 1 or values.size < 2 or not np.all(np.diff(values) > 0.0):
        raise ValueError("X 軸 major tick 需要至少兩個嚴格遞增的經度中心座標")
    lower = float(values[0])
    upper = float(values[-1])
    step = 0.4
    first_internal = math.ceil((lower + 1.0e-9) / step) * step
    internal = np.arange(first_internal, upper - 1.0e-9, step, dtype=np.float64)
    # 當規則內部刻度距離精確 bbox 上限只剩很小間隔時，雖然數值上仍在範圍內，
    # 其標籤卻會與末端標籤重疊；這是 fallback 對任意原始 bbox 的一般性保護。
    # 這裡只刪除靠近端點的顯示刻度，不改變資料 bbox、不影響 0.2° minor tick，
    # 並保留由輸入 lon 首末值產生的精確端點；0.1° 是版面可讀性的顯示門檻，
    # 不是任何物理或分析網格的重新取樣規則。
    endpoint_clearance = 0.1
    internal = internal[
        (internal > lower + 1.0e-8)
        & (internal < upper - endpoint_clearance)
    ]
    return np.concatenate(
        (
            np.asarray([lower], dtype=np.float64),
            np.round(internal, 8),
            np.asarray([upper], dtype=np.float64),
        )
    )


def _display_axis_spec_for_region(
    region_key: str | None,
    lon: np.ndarray,
    lat: np.ndarray,
) -> dict[str, Any]:
    """回傳單區 display-only 座標規格並驗證其落在原始網格範圍內。

    `lon`/`lat` 是正式 SVD 的完整規則格點中心座標；本函式不改變它們，也不裁切
    任何重建資料，只建立 Matplotlib 使用的顯示視窗。A–D 已由簡報第 6–9 頁靜態圖
    分別核對，因此各自使用該頁的 x/y 範圍、固定 major tick 與格式；未來未核對的
    區域才暫時記錄原始 SVD bbox 作為 fallback。若簡報規格超出原始網格，立即失敗
    以防 QA 在錯誤空間範圍上通過。
    """

    raw_bbox = [float(lon[0]), float(lon[-1]), float(lat[0]), float(lat[-1])]
    # `raw_bbox` 描述的是格點中心；Matplotlib pcolormesh 的實際可繪範圍還包括
    # 最外側半格 cell edge。簡報靜態圖可能以略超出 cell edge 的整潔端點標示，
    # 因此不能用中心 bbox 或 cell-edge bbox 的「完全包含」條件拒絕合法 display
    # extent。這裡只要求顯示範圍與資料可繪範圍相交，允許外側留下簡報版面留白；
    # 不會補值、外插或改變任何 SVD／cache 格點。
    if lon.size < 2 or lat.size < 2:
        raise ValueError("建立 display extent 需要至少兩個 lon/lat 格點")
    lon_step_left = float(lon[1] - lon[0])
    lon_step_right = float(lon[-1] - lon[-2])
    lat_step_bottom = float(lat[1] - lat[0])
    lat_step_top = float(lat[-1] - lat[-2])
    if min(lon_step_left, lon_step_right, lat_step_bottom, lat_step_top) <= 0.0:
        raise ValueError("正式 SVD lon/lat 必須嚴格遞增，才能建立 cell-edge bbox")
    raw_cell_edge_bbox = [
        float(lon[0] - 0.5 * lon_step_left),
        float(lon[-1] + 0.5 * lon_step_right),
        float(lat[0] - 0.5 * lat_step_bottom),
        float(lat[-1] + 0.5 * lat_step_top),
    ]
    configured = DISPLAY_AXIS_SPECS.get(str(region_key)) if region_key is not None else None
    if configured is None:
        x_major = _x_major_tick_values(lon)
        y_major = np.arange(
            math.ceil((float(lat[0]) + 1.0e-9) / 0.2) * 0.2,
            float(lat[-1]) + 1.0e-9,
            0.2,
            dtype=np.float64,
        )
        return {
            "display_extent": raw_bbox.copy(),
            "x_major_values": [float(value) for value in x_major],
            "y_major_values": [float(value) for value in np.round(y_major, 8)],
            "x_major_formatter": "%.2f",
            "y_major_formatter": "%.2f",
            "display_extent_source": "formal_svd_grid_bbox_fallback_pending_slide_crosscheck",
            "reference_page": None,
            "reference_image_path": None,
            "reference_image_sha256": None,
            "raw_grid_bbox": raw_bbox,
            "raw_grid_cell_edge_bbox": raw_cell_edge_bbox,
            "display_extent_intersects_raw_grid_cell_edges": True,
        }

    extent = [float(value) for value in configured["display_extent"]]
    intersects_raw_cell_edges = bool(
        extent[1] > raw_cell_edge_bbox[0]
        and extent[0] < raw_cell_edge_bbox[1]
        and extent[3] > raw_cell_edge_bbox[2]
        and extent[2] < raw_cell_edge_bbox[3]
    )
    if not intersects_raw_cell_edges:
        raise ValueError(
            f"{region_key} display extent 與正式 SVD grid cell-edge bbox 無相交："
            f"display={extent}, raw_center={raw_bbox}, raw_cell_edge={raw_cell_edge_bbox}"
        )
    x_major = [float(value) for value in configured["x_major_values"]]
    y_major = [float(value) for value in configured["y_major_values"]]
    if not (
        all(extent[0] <= value <= extent[1] for value in x_major)
        and all(extent[2] <= value <= extent[3] for value in y_major)
    ):
        raise ValueError(f"{region_key} display major tick 超出 display extent：{configured}")
    return {
        **configured,
        "display_extent": extent,
        "x_major_values": x_major,
        "y_major_values": y_major,
        "raw_grid_bbox": raw_bbox,
        "raw_grid_cell_edge_bbox": raw_cell_edge_bbox,
        "display_extent_intersects_raw_grid_cell_edges": intersects_raw_cell_edges,
    }


def _apply_geographic_axis_style(
    ax: Any,
    lon: np.ndarray,
    lat: np.ndarray,
    *,
    font: Any | None,
    show_x_labels: bool,
    display_axis_spec: dict[str, Any] | None = None,
) -> None:
    """套用單區 display-only 經緯度範圍、locator、formatter 與端點對齊規則。

    已核對的 A–D 分別使用第 6–9 頁靜態流場圖的 xlim/ylim 與固定一位小數 major
    tick；完整原始 SVD bbox 仍由 manifest 的 `grid`/`display_axis_spec.raw_grid_bbox`
    保存。上方面板保留 tick mark 但隱藏 X 數字，讓下方面板成為唯一 X 軸閱讀位置；
    刻度文字不再逐一附加度數符號，方向由軸名表示。
    """

    spec = display_axis_spec or _display_axis_spec_for_region(None, lon, lat)
    extent = [float(value) for value in spec["display_extent"]]
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_aspect("equal", adjustable="box")
    x_major = np.asarray(spec["x_major_values"], dtype=np.float64)
    y_major = np.asarray(spec["y_major_values"], dtype=np.float64)
    ax.xaxis.set_major_locator(mticker.FixedLocator(x_major))
    ax.xaxis.set_major_formatter(mticker.FormatStrFormatter(str(spec["x_major_formatter"])))
    ax.xaxis.set_minor_locator(mticker.MultipleLocator(0.2))
    ax.yaxis.set_major_locator(mticker.FixedLocator(y_major))
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter(str(spec["y_major_formatter"])))
    ax.tick_params(
        axis="both",
        which="major",
        labelsize=AXIS_FONT_SIZE_PT,
        labelcolor=TEXT_COLOR,
        colors=TEXT_COLOR,
        length=2.5,
        pad=2.0,
    )
    ax.tick_params(
        axis="x",
        which="minor",
        labelbottom=False,
        colors=TEXT_COLOR,
        length=1.5,
        pad=2.0,
    )
    ax.set_xlabel(
        "經度（°E）",
        fontproperties=font_with_size(font, AXIS_FONT_SIZE_PT),
        labelpad=3,
        color=TEXT_COLOR,
    )
    ax.set_ylabel(
        "緯度（°N）",
        fontproperties=font_with_size(font, AXIS_FONT_SIZE_PT),
        labelpad=3,
        color=TEXT_COLOR,
    )
    if not show_x_labels:
        ax.tick_params(axis="x", which="both", labelbottom=False)
        ax.xaxis.label.set_visible(False)
    else:
        # 端點 tick 若使用預設的水平置中，文字會有一半落在 axes 外側；明確改成
        # 左／右對齊可保留各頁靜態圖指定的端點標籤，同時不把色階推入主圖範圍。
        labels = ax.get_xticklabels()
        if labels:
            labels[0].set_ha("left")
            labels[-1].set_ha("right")


def _phase_label(phase: str) -> str:
    """將內部 phase key 轉為簡報採用的時間係數相位描述。

    選窗本身仍由 `pc_standardized[0]` 的正負相位決定；這裡只改變畫面用語，避免
    將簡報中的「時間係數」重新命名為 PC。manifest 仍保留原始 pc 欄位與選窗規則，
    讓展示術語和科學追溯彼此分離。
    """

    return {
        "positive": "模態 1 時間係數：正相位案例",
        "negative": "模態 1 時間係數：負相位案例",
    }.get(phase, phase)


def _display_title(region_key: str) -> str:
    """回傳影片畫面指定的海域主標題。

    主標題只服務簡報辨識，不攜帶 SVD 方法名稱或模態個數；方法、資料來源與
    K90 等追溯資訊會寫入 manifest/README，避免畫面在投影片縮小後變得擁擠。
    """

    try:
        return DISPLAY_REGION_TITLES[region_key]
    except KeyError as exc:
        raise ValueError(f"未定義的畫面海域主標題：{region_key}") from exc


def _reconstruction_panel_label(dataset: RegionDataset) -> str:
    """建立不含 K/K90 符號的下方面板標籤。

    `dataset.k90` 仍由既有正式 SVD 的累積流場變異陣列計算；畫面只保留「前 n 個
    模態重建流場」這個簡潔標籤，不把單一模態的 EVₖ 或累積百分比塞入下方面板
    caption，避免文字離圖過遠或讓展示資訊過密。完整的累積達 90% 條件仍保留在
    manifest/README 的內部追溯欄位。
    """

    return f"前 {dataset.k90} 個模態重建流場"


def _display_text_spec(dataset: RegionDataset, *, external_note: bool) -> dict[str, Any]:
    """建立並檢查畫面文字規格，回傳可序列化的 manifest 證據。

    此檢查針對 renderer 實際會送入 Matplotlib 的標題、phase、panel caption、色條
    與箭頭圖例字串；頂部前四模態資訊列刻意不建立，避免把靜態研究圖的累積量誤認
    成動畫重建的說明。這裡不是 OCR，無法取代人工觀看 rasterized poster，但能在
    renderer/模板修改時自動阻擋 PC、K90 或舊方法副標題重新混入畫面。科學原始欄位
    名稱仍可在 manifest 的 `svd` 與 `source_products` 區段保留。
    """

    strings = {
        "main_title": _display_title(dataset.spec.key),
        "positive_phase": _phase_label("positive"),
        "negative_phase": _phase_label("negative"),
        "top_panel_caption": "原始流場" if not external_note else "外部 1 km 流場對照",
        "bottom_panel_caption": _reconstruction_panel_label(dataset),
        "arrow_legend": ARROW_LEGEND_LABEL,
        "colorbar_label": COLORBAR_LABEL,
    }
    forbidden_found = {
        token: [name for name, value in strings.items() if token in str(value)]
        for token in DISPLAY_FORBIDDEN_TOKENS
    }
    forbidden_found = {token: names for token, names in forbidden_found.items() if names}
    if forbidden_found:
        raise ValueError(f"畫面文字違反展示術語規格：{forbidden_found}")
    return {
        "strings": strings,
        "forbidden_tokens": list(DISPLAY_FORBIDDEN_TOKENS),
        "forbidden_tokens_found": forbidden_found,
        "passed": not forbidden_found,
        "info_line": {
            "visible": False,
            "reason": "前四模態累積量只屬簡報靜態研究圖；為避免誤導，動畫不顯示第三列資訊。",
        },
        "font_and_color": {
            "text_color": TEXT_COLOR,
            "title_fontsize_points": TITLE_FONT_SIZE_PT,
            "phase_utc_fontsize_points": PHASE_FONT_SIZE_PT,
            "panel_caption_fontsize_points": CAPTION_FONT_SIZE_PT,
            "axis_tick_color": TEXT_COLOR,
            "axis_fontsize_points": AXIS_FONT_SIZE_PT,
            "colorbar_fontsize_points": AXIS_FONT_SIZE_PT,
            "arrow_legend_fontsize_points": ARROW_KEY_FONT_SIZE_PT,
        },
        "caption_layout": {
            "top": "figure-level caption strip below the upper axes, outside axes bbox",
            "bottom": "figure-level caption strip below the lower axes, outside axes bbox; single line",
            "clip_on": False,
        },
        "scientific_semantics_manifest_only": "六層聯合 SVD 模態之表層分量；不是 surface-only SVD",
    }


def _speed_ticks(vmax: float) -> np.ndarray:
    """產生一位小數、等距且包含上下限的固定流速色條刻度。"""

    count = max(2, int(round(vmax / DEFAULT_SPEED_ROUNDING_MPS)))
    return np.linspace(0.0, vmax, count + 1)


def _make_masked_speed(speed: np.ndarray, vmax: float) -> np.ma.MaskedArray:
    """建立共用固定 Normalize 的速度背景；超過 vmax 只飽和顯示，不截斷陣列。"""

    finite = np.where(np.isfinite(speed), speed, np.nan)
    return np.ma.masked_invalid(np.clip(finite, 0.0, vmax))


def _semantic_background(
    dataset: RegionDataset,
    speed: np.ndarray,
) -> np.ndarray:
    """建立非流速分類背景，明確分開地理與資料遮罩的三種語意。

    回傳 `(lat, lon)` 的整數分類陣列：0=分析域外、1=模型靜態海洋域外、
    2=表層速度特徵未納入、3=逐時無效/缺值。exact-land raster mask 仍由
    `dataset.coastline_land_mask` 保存，供科學遮罩與 QA 追溯，但刻意不在此處把
    整個保守 cell 改成「透明的 exact land」類別。原因是
    `cell_overlap_center_corners_vertices` 只表示 cell 與 polygon 有接觸；若把
    這種整格分類畫成透明，polygon 外的 cell 部分會露出白色 figure 背景，形成
    一圈 1 km 格點階梯白邊。可見陸地只由上層高解析度 GeoJSON vector polygon
    覆蓋，polygon 外的部分則保留原本的模型域／逐時缺值語意；因此既不把 raster
    mask 畫成假海岸線，也不會用白色透明格製造接縫。只有 `render_mask` 且當幀
    速度有限的格點由上層 viridis 流速 mesh 覆蓋；exact polygon 的最高 z-order
    patch 再完整遮住其內部色塊與箭頭。
    """

    category = np.full(dataset.plot_mask.shape, 0, dtype=np.int8)
    geometry = dataset.analysis_geometry_mask
    static = dataset.static_ocean_mask
    feature = dataset.velocity_feature_mask_surface
    category[geometry & ~static] = 1
    category[geometry & static & ~feature] = 2
    # 逐時缺值以 renderer 的有效域判定。這裡不使用 conservative exact-land raster
    # mask 覆蓋既有分類：整格透明會在 vector polygon 外側留下白色階梯，整格灰色又
    # 會把稽核用近似 mask 誤畫成可見海岸線。polygon 外側保留原始分類，polygon
    # 內部則由最高 z-order vector land patch 覆蓋，這才符合兩種資料語意的分工。
    category[dataset.render_mask & ~np.isfinite(speed)] = 3
    # `coastline_land_mask` 不在可見分類陣列中直接寫入；它仍存在於 dataset，且
    # manifest/audit 會照原規則記錄格點數。此保留是刻意的：exact coastline 的
    # 顯示邊界必須由原始高解析度 polygon 決定，而不是由 1 km cell overlap 近似
    # 決定。幾何域外也不會因為 polygon 恰好相交而被改稱為陸地。
    return category


def _semantic_background_mesh(
    ax: Any,
    dataset: RegionDataset,
    speed: np.ndarray,
    cmap: Any,
    norm: Any,
) -> Any:
    """把 `_semantic_background` 畫成可逐幀更新的分類底層 mesh。"""

    return ax.pcolormesh(
        dataset.lon,
        dataset.lat,
        _semantic_background(dataset, speed),
        shading="auto",
        cmap=cmap,
        norm=norm,
        zorder=1,
    )


def _quiver_key_group_bbox_px(key: Any, renderer: Any) -> tuple[float, float, float, float]:
    """量測 QuiverKey 箭頭與文字的聯合像素 bounding box。

    Matplotlib 的 ``QuiverKey`` 本身沒有可靠的單一 ``get_window_extent``；它由一個
    PolyCollection 箭頭和一個 Text artist 組成。因此這裡在 renderer 已初始化後，
    分別將箭頭 path／offset 與文字 bbox 轉成畫布像素，再取聯集。這個量測只服務
    版面 QA，不改變 U=1.0 的箭頭尺度或流場資料。
    """

    text_bbox = key.text.get_window_extent(renderer)
    x0, y0, x1, y1 = (
        float(text_bbox.x0),
        float(text_bbox.y0),
        float(text_bbox.x1),
        float(text_bbox.y1),
    )
    vector = key.vector
    offsets = np.asarray(vector.get_offsets(), dtype=np.float64)
    path_transform = vector.get_transform()
    offset_transform = vector.get_offset_transform()
    for path in vector.get_paths():
        path_vertices_px = path_transform.transform(path.vertices)
        for offset in offsets:
            offset_px = offset_transform.transform(offset)
            vertices_px = path_vertices_px + offset_px
            if vertices_px.size == 0:
                continue
            x0 = min(x0, float(np.min(vertices_px[:, 0])))
            y0 = min(y0, float(np.min(vertices_px[:, 1])))
            x1 = max(x1, float(np.max(vertices_px[:, 0])))
            y1 = max(y1, float(np.max(vertices_px[:, 1])))
    return x0, y0, x1, y1


def _measure_quiver_key_layout(fig: Any, ax: Any, key: Any) -> dict[str, Any]:
    """回傳 QuiverKey 群組與下方面板 bbox 的實際像素位置。

    使用 figure canvas 已繪製的最終字型與 dpi 量測，避免只依 figure fraction 推算
    中文標籤寬度。`right_edge_diff_px` 定義為下方面板右緣減去比例尺群組右緣；
    正值表示群組尚未靠右，負值表示越過面板右緣。此值會寫入 manifest 並由
    validator 要求絕對值不超過 4 px。
    """

    renderer = fig.canvas.get_renderer()
    group_bbox = _quiver_key_group_bbox_px(key, renderer)
    axes_bbox = ax.get_window_extent(renderer)
    figure_bbox = fig.bbox
    right_edge_diff = float(axes_bbox.x1 - group_bbox[2])
    return {
        "group_bbox_px": [float(value) for value in group_bbox],
        "bottom_axes_bbox_px": [
            float(axes_bbox.x0),
            float(axes_bbox.y0),
            float(axes_bbox.x1),
            float(axes_bbox.y1),
        ],
        "figure_bbox_px": [
            float(figure_bbox.x0),
            float(figure_bbox.y0),
            float(figure_bbox.x1),
            float(figure_bbox.y1),
        ],
        "right_edge_diff_px": right_edge_diff,
        "right_edge_tolerance_px": ARROW_KEY_RIGHT_TOLERANCE_PX,
        "right_aligned": abs(right_edge_diff) <= ARROW_KEY_RIGHT_TOLERANCE_PX,
        "key_figure_anchor": [float(key.X), float(key.Y)],
        "reference_mps": float(key.U),
        "label": ARROW_LEGEND_LABEL,
    }


def _align_quiver_key_to_axes_right(fig: Any, ax: Any, key: Any) -> dict[str, Any]:
    """以實際文字寬度把 QuiverKey 群組右緣貼齊下方面板右緣。

    ``X`` 只是 QuiverKey 的箭頭頭端錨點，不能直接等同於整個箭頭加文字群組的
    右邊界；若只硬編碼 X，換字型或 dpi 後就會再次偏移。此函式先繪製一次取得
    中文標籤的真實 bbox，再以像素差換算回 figure fraction，最多迭代兩次，最後
    再量測並回傳 QA 證據。箭頭仍由同一個 quiver artist、U=1.0 與原 scale 產生。
    """

    for _ in range(2):
        fig.canvas.draw()
        layout = _measure_quiver_key_layout(fig, ax, key)
        figure_width_px = float(fig.bbox.width)
        if figure_width_px <= 0.0:
            raise ValueError("figure canvas 寬度無法用於 QuiverKey 對齊")
        correction = float(layout["right_edge_diff_px"]) / figure_width_px
        if abs(float(layout["right_edge_diff_px"])) <= ARROW_KEY_RIGHT_TOLERANCE_PX:
            break
        key.X += correction
        key.stale = True
    fig.canvas.draw()
    return _measure_quiver_key_layout(fig, ax, key)


def _measure_x_tick_label_layout(fig: Any, ax: Any) -> dict[str, Any]:
    """量測下方面板 X 軸標籤的相鄰水平間距與畫布裁切狀態。

    這裡讀取 Matplotlib 實際產生的 Text bbox，而不是以數值刻度間距估算；因此
    能捕捉中文字型、dpi、端點對齊與輸出尺寸造成的真實擁擠。`minimum_gap_px`
    必須大於 8 px，且每個 bbox 都要落在 figure 內，才可把該區正式動畫視為通過。
    上方面板的 X 標籤刻意隱藏，不納入這項檢查。
    """

    renderer = fig.canvas.get_renderer()
    figure_bbox = fig.bbox
    labels = [label for label in ax.get_xticklabels() if label.get_visible() and label.get_text()]
    boxes = [
        (label.get_text(), label.get_window_extent(renderer))
        for label in labels
    ]
    boxes.sort(key=lambda item: float(item[1].x0))
    bboxes = [
        [float(box.x0), float(box.y0), float(box.x1), float(box.y1)]
        for _text, box in boxes
    ]
    gaps = [
        float(boxes[index + 1][1].x0 - boxes[index][1].x1)
        for index in range(max(len(boxes) - 1, 0))
    ]
    minimum_gap = min(gaps) if gaps else None
    clipped = any(
        box[0] < float(figure_bbox.x0)
        or box[2] > float(figure_bbox.x1)
        or box[1] < float(figure_bbox.y0)
        or box[3] > float(figure_bbox.y1)
        for box in bboxes
    )
    no_overlap = bool(gaps) and all(gap >= 0.0 for gap in gaps)
    return {
        "texts": [text for text, _box in boxes],
        "bbox_px": bboxes,
        "adjacent_horizontal_gaps_px": gaps,
        "minimum_gap_px": float(minimum_gap) if minimum_gap is not None else None,
        "minimum_required_gap_px": 8.0,
        "no_overlap": no_overlap,
        "clipped": clipped,
        "passed": bool(
            len(boxes) >= 2
            and minimum_gap is not None
            and minimum_gap > 8.0
            and no_overlap
            and not clipped
        ),
    }


def _draw_arrow_key(ax: Any, quiver: Any, font: Any | None) -> Any:
    """用同一組 quiver artist 繪製真正代表 1 m/s 的箭頭圖例。

    不能以固定 figure 座標手動畫一支箭頭再任意換文字，因為那會使圖例長度與
    面板內的 `scale` 脫鉤。`QuiverKey` 直接引用下方面板的 quiver，並以 `U=1.0`
    傳入 1 m/s；因此其幾何長度由與資料箭頭相同的 scale 計算。圖例放在下方面板
    圖外 caption 的同一水平帶，避免在 figure 底部留下與主圖無關的大面積空白。
    圖例文字與示意箭頭均採純黑，與所有其他觀眾可見文字一致。
    """

    return ax.quiverkey(
        quiver,
        X=ARROW_KEY_X,
        Y=ARROW_KEY_Y,
        U=1.0,
        label=ARROW_LEGEND_LABEL,
        labelpos="E",
        coordinates="figure",
        color=QUIVER_KEY_COLOR,
        labelcolor=QUIVER_KEY_COLOR,
        labelsep=0.015,
        fontproperties=font_with_size(font, ARROW_KEY_FONT_SIZE_PT),
        zorder=30,
    )


def create_scene(
    dataset: RegionDataset,
    *,
    width: int,
    height: int,
    dpi: int,
    target_arrows: int,
    quiver_scale_multiplier: float,
    font: Any | None,
    external_note: bool,
) -> RenderScene:
    """建立雙面板固定尺度畫面與可更新的 Matplotlib artists。

    版面以 4:5 直式 figure 配置：上、下兩個同範圍 axes，右側獨立 colorbar，面板
    圖框外另設 caption strip 與箭頭圖例。`pcolormesh` 兩面板使用同一
    `Normalize(0, vmax)`；quiver 使用
    同一 scale，故每個時間幀與兩個面板都可直接做視覺比較。畫面不放大標題，必要
    語意以小型 panel label 與資訊帶呈現。
    """

    vmax = dataset.speed_scale_vmax
    norm = mcolors.Normalize(vmin=0.0, vmax=vmax, clip=True)
    cmap = plt.get_cmap("viridis").copy()
    # 速度 mesh 的無效格點必須透明，讓底下的分類背景顯示「分析域外／逐時缺值」；
    # exact-land 則由下層分類色與最上層 GeoJSON polygon 共同覆蓋，不能再把所有 NaN
    # 統一設成陸地色。
    cmap.set_bad((0.0, 0.0, 0.0, 0.0))
    semantic_cmap = mcolors.ListedColormap(SEMANTIC_BACKGROUND_COLORS, name="ocm_mask_semantics")
    semantic_norm = mcolors.BoundaryNorm(np.arange(-0.5, 5.5, 1.0), semantic_cmap.N)
    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi, facecolor="white")
    # 左側／右側外框及上方標題的外邊界固定不動：上方面板頂界維持 y=0.91、
    # 兩個 axes 的 x 起點與寬度仍為 0.10/0.75，右側色階 x 起點與寬度仍為
    # 0.875/0.025。只把上下兩面板等比例增高，並將下方面板的底界下移；這樣
    # 中間間距與底部 caption 帶都能在不縮減地圖寬度的前提下重新分配。
    ax_top = fig.add_axes([0.10, 0.56, 0.75, 0.35])
    ax_bottom = fig.add_axes([0.10, 0.15, 0.75, 0.35])
    # 色階保持原本右側 x 位置與上界，垂直範圍只隨兩面板的新共同高度延伸，
    # 避免下方面板被拉長後與色階失去對齊；因此右側留白與色階標籤的外側位置不變。
    colorbar_axis = fig.add_axes([0.875, 0.15, 0.025, 0.76])
    axes = (ax_top, ax_bottom)
    first = dataset.payloads[0]
    speed_values = (first.raw_speed, first.reconstruction_speed)
    vector_values = ((first.raw_u, first.raw_v), (first.reconstruction_u, first.reconstruction_v))
    meshes: list[Any] = []
    background_meshes: list[Any] = []
    quivers: list[Any] = []
    land_patch_counts: list[int] = []
    sy, sx = choose_quiver_step(dataset.lon.size, dataset.lat.size, target_arrows)
    for index, (ax, speed, (u, v)) in enumerate(zip(axes, speed_values, vector_values)):
        ax.set_facecolor(ANALYSIS_OUTSIDE_COLOR)
        background_mesh = _semantic_background_mesh(ax, dataset, speed, semantic_cmap, semantic_norm)
        background_meshes.append(background_mesh)
        mesh = ax.pcolormesh(
            dataset.lon,
            dataset.lat,
            _make_masked_speed(speed, vmax),
            shading="auto",
            cmap=cmap,
            norm=norm,
            zorder=2,
        )
        meshes.append(mesh)
        # 可見 arrows 依 render_mask 取樣；vector exact coastline 隨後位於 zorder=30，
        # 因此 polygon 內的箭頭會被完整覆蓋，而 polygon 外側不會因保守 raster cell
        # 被整格挖空。
        valid = np.isfinite(u) & np.isfinite(v) & dataset.render_mask
        sampled_u = np.ma.masked_where(~valid[::sy, ::sx], u[::sy, ::sx])
        sampled_v = np.ma.masked_where(~valid[::sy, ::sx], v[::sy, ::sx])
        quiver = ax.quiver(
            dataset.lon[::sx],
            dataset.lat[::sy],
            sampled_u,
            sampled_v,
            color=QUIVER_COLOR,
            scale=max(quiver_scale_multiplier * dataset.quiver_reference_mps, 0.1),
            width=0.0023,
            headwidth=3.1,
            headlength=4.2,
            headaxislength=3.5,
            alpha=0.94,
            zorder=10,
        )
        quiver.set_path_effects(
            [patheffects.withStroke(linewidth=0.8, foreground=QUIVER_SHADOW_COLOR, alpha=0.52)]
        )
        quivers.append(quiver)
        # 最高 z-order 的 exact coastline polygon 蓋住流速色塊及箭頭，避免岸線附近
        # 因格點抽樣或箭頭 head 延伸而產生「陸地上有流場」的視覺假象。此 polygon
        # 使用原始 GeoJSON 高解析度頂點、抗鋸齒填色且不繪製深色 edge；保守 raster
        # mask 只負責資料遮罩／audit，不再作為可見陸地邊界。
        land_patch_counts.append(
            draw_vector_land_overlay(
                ax,
                dataset.land_rings,
                tuple(float(value) for value in dataset.display_axis_spec["display_extent"]),
                facecolor=LAND_COLOR,
                edgecolor=LAND_EDGE_COLOR,
                linewidth=LAND_EDGE_WIDTH,
                antialiased=LAND_ANTIALIASED,
                zorder=30,
            )
        )
        _apply_geographic_axis_style(
            ax,
            dataset.lon,
            dataset.lat,
            font=font,
            show_x_labels=index == 1,
            display_axis_spec=dataset.display_axis_spec,
        )
        for spine in ax.spines.values():
            spine.set_color(PANEL_BORDER_COLOR)
            spine.set_linewidth(0.7)

    colorbar = fig.colorbar(meshes[0], cax=colorbar_axis)
    colorbar.set_ticks(_speed_ticks(vmax))
    colorbar.ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f"))
    colorbar.ax.tick_params(
        labelsize=AXIS_FONT_SIZE_PT,
        labelcolor=TEXT_COLOR,
        colors=TEXT_COLOR,
        pad=3,
        length=2.5,
    )
    colorbar.outline.set_linewidth(0.55)
    # 色條刻度維持直向排列；完整標籤只建立一個 Text artist，整行旋轉 90 度後
    # 放在色條右側。`labelpad`、刻度 `pad` 與色階寬度均固定，避免標籤碰到刻度、
    # 主圖或畫布邊界；rotation=90 表示整行旋轉，不是逐字直排。
    colorbar.ax.yaxis.set_label_position("right")
    colorbar.set_label(
        COLORBAR_LABEL,
        rotation=90,
        rotation_mode="anchor",
        labelpad=12,
        fontproperties=font_with_size(font, AXIS_FONT_SIZE_PT),
        color=TEXT_COLOR,
    )
    colorbar.ax.yaxis.label.set_va("center")
    colorbar.ax.yaxis.label.set_ha("center")

    # 主標題與 phase/UTC 同列資訊均使用純黑與較大字級。前四模態累積百分比列
    # 已完全移除，避免把簡報左側靜態圖的數字誤解成下方面板的重建條件。
    fig.text(
        0.10,
        0.975,
        _display_title(dataset.spec.key),
        ha="left",
        va="top",
        fontproperties=font_with_size(font, TITLE_FONT_SIZE_PT),
        color=TEXT_COLOR,
    )
    phase_text = fig.text(
        0.10,
        0.940,
        "",
        ha="left",
        va="top",
        fontproperties=font_with_size(font, PHASE_FONT_SIZE_PT),
        color=TEXT_COLOR,
    )
    top_label = fig.text(
        0.10,
        0.545,
        "原始流場" if not external_note else "外部 1 km 流場對照",
        ha="left",
        va="top",
        fontproperties=font_with_size(font, CAPTION_FONT_SIZE_PT),
        color=TEXT_COLOR,
        clip_on=False,
    )
    bottom_label = fig.text(
        0.10,
        0.065,
        _reconstruction_panel_label(dataset),
        ha="left",
        va="top",
        linespacing=1.08,
        fontproperties=font_with_size(font, CAPTION_FONT_SIZE_PT),
        color=TEXT_COLOR,
        clip_on=False,
    )
    # 圖例與下方面板使用同一個 Quiver artist，U=1.0 對應資料單位中的 1 m/s；
    # 建立後再依實際中文字寬度把整個群組右緣對齊 axes，而不是猜測錨點 X。
    arrow_key = _draw_arrow_key(ax_bottom, quivers[1], font)
    arrow_key_layout = _align_quiver_key_to_axes_right(fig, ax_bottom, arrow_key)
    axis_tick_layout = _measure_x_tick_label_layout(fig, ax_bottom)
    return RenderScene(
        fig=fig,
        axes=(ax_top, ax_bottom),
        background_meshes=(background_meshes[0], background_meshes[1]),
        meshes=(meshes[0], meshes[1]),
        quivers=(quivers[0], quivers[1]),
        phase_text=phase_text,
        top_label=top_label,
        bottom_label=bottom_label,
        colorbar=colorbar,
        arrow_key=arrow_key,
        arrow_key_layout=arrow_key_layout,
        axis_tick_layout=axis_tick_layout,
        cmap=cmap,
        norm=norm,
        land_patch_counts=(land_patch_counts[0], land_patch_counts[1]),
    )


def update_scene(scene: RenderScene, dataset: RegionDataset, payload: Payload) -> np.ndarray:
    """更新兩面板速度/向量、語意背景及 UTC/相位資訊，回傳 RGB uint8 畫面。

    每幀都重新建立逐時缺值分類，但 exact-land、分析幾何與模型靜態分類只取自
    dataset 的固定布林遮罩。流速與箭頭的 valid 條件使用 `dataset.render_mask`，
    真實 exact-land 的可見邊界由最上層 vector polygon 定義；保守 `plot_mask` 不會
    再把 polygon 外側的相交 cell 整格變成白色。資料／land audit 仍以 plot_mask
    另行記錄，不把展示性 mask 變更解讀成 SVD 重算。
    """

    speeds = (payload.raw_speed, payload.reconstruction_speed)
    vectors = (
        (payload.raw_u, payload.raw_v),
        (payload.reconstruction_u, payload.reconstruction_v),
    )
    sy, sx = choose_quiver_step(dataset.lon.size, dataset.lat.size, DEFAULT_TARGET_ARROWS)
    # scene 的 quiver 抽樣步距由主程式固定為同一 target；此處由座標長度反推只在
    # DEFAULT_TARGET_ARROWS 下成立，因此正式 render 另由 scene 動態屬性覆寫。
    if hasattr(scene, "_quiver_step"):  # type: ignore[attr-defined]
        sy, sx = scene._quiver_step  # type: ignore[attr-defined]
    for background_mesh, mesh, quiver, speed, (u, v) in zip(
        scene.background_meshes,
        scene.meshes,
        scene.quivers,
        speeds,
        vectors,
    ):
        background_mesh.set_array(_semantic_background(dataset, speed).ravel())
        mesh.set_array(_make_masked_speed(speed, dataset.speed_scale_vmax).ravel())
        valid = np.isfinite(u) & np.isfinite(v) & dataset.render_mask
        quiver.set_UVC(
            np.ma.masked_where(~valid[::sy, ::sx], u[::sy, ::sx]),
            np.ma.masked_where(~valid[::sy, ::sx], v[::sy, ::sx]),
        )
    phase = _phase_label(payload.record.phase)
    scene.phase_text.set_text(f"{phase}  ·  {_format_time_utc(payload.record.time_ns)}")
    scene.fig.canvas.draw()
    rgba = np.asarray(scene.fig.canvas.buffer_rgba(), dtype=np.uint8)
    width, height = scene.fig.canvas.get_width_height()
    if rgba.shape[:2] != (height, width):
        raise RuntimeError(f"Matplotlib canvas shape={rgba.shape} != {(height, width)}")
    return rgba[:, :, :3].copy()


def _frame_plan(dataset: RegionDataset, pilot: bool) -> list[int]:
    """建立正式或 pilot 的 payload index 播放順序，維持片頭/片尾停留規格。"""

    if len(dataset.payloads) != WINDOW_FRAME_COUNT * 2:
        raise ValueError("payload 必須先包含正相位 28 + 負相位 28 個資料影格")
    if pilot:
        picks = np.linspace(0, WINDOW_FRAME_COUNT - 1, PILOT_SAMPLES_PER_WINDOW, dtype=int).tolist()
        data_indices = picks + [WINDOW_FRAME_COUNT + value for value in picks]
    else:
        data_indices = list(range(WINDOW_FRAME_COUNT * 2))
    return [0] * INTRO_HOLD_FRAMES + data_indices + [WINDOW_FRAME_COUNT * 2 - 1] * OUTRO_HOLD_FRAMES


def _output_names(
    dataset: RegionDataset,
    pilot: bool,
    temporal_interpolated: bool,
) -> tuple[str, str, str, str]:
    """回傳 MP4、poster、正/負相位 QA 的版本清楚檔名。

    內插版與 v3 即使分存在不同資料夾，也在檔名加入
    ``temporal_interpolated``，避免把單支 MP4 複製到簡報素材資料夾後失去時間
    語意。`pilot` 仍保留既有命名相容性；本次正式四區輸出不使用 pilot。
    """

    suffix_parts = []
    if pilot:
        suffix_parts.append("pilot")
    if temporal_interpolated:
        suffix_parts.append("temporal_interpolated")
    suffix = "_" + "_".join(suffix_parts) if suffix_parts else ""
    stem = f"region_{dataset.spec.key}_{dataset.spec.short_name}_modal_context{suffix}"
    return f"{stem}.mp4", f"{stem}_poster.png", f"{stem}_positive_phase.png", f"{stem}_negative_phase.png"


def _configure_imageio_ffmpeg() -> str | None:
    """若 imageio-ffmpeg 有 bundled ffmpeg，先設定其路徑並回傳供 manifest 追蹤。"""

    try:
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        os.environ.setdefault("IMAGEIO_FFMPEG_EXE", executable)
        return executable
    except Exception:
        return shutil.which("ffmpeg")


def _render_rgb_frames(
    dataset: RegionDataset,
    scene: RenderScene,
    frame_indices: Sequence[int],
    *,
    target_arrows: int,
) -> list[np.ndarray]:
    """繪製指定 payload index；pilot 使用小列表，正式模式直接串流寫入 MP4。"""

    # create_scene 後把實際抽樣步距掛在 scene，確保 update_scene 不會因預設 target
    # 與 CLI target 不同而改變箭頭位置；這是 renderer 內部狀態，不寫回資料。
    scene._quiver_step = choose_quiver_step(dataset.lon.size, dataset.lat.size, target_arrows)  # type: ignore[attr-defined]
    return [update_scene(scene, dataset, dataset.payloads[index]) for index in frame_indices]


def render_region_animation(
    dataset: RegionDataset,
    *,
    output_dir: Path,
    fps: int,
    width: int,
    height: int,
    dpi: int,
    target_arrows: int,
    quiver_scale_multiplier: float,
    font: Any | None,
    font_path: Path | None,
    pilot: bool,
    temporal_interpolated: bool,
    overwrite: bool,
) -> dict[str, Any]:
    """輸出單區 MP4、poster、兩張代表相位 QA 幀及檔案摘要。

    MP4 以 imageio-ffmpeg 串流編碼，輸入每幀為 RGB，輸出固定 4:5 畫布、H.264、
    yuv420p、無音訊；不把 64 張完整影像同時堆入 RAM。poster 是正式播放第一個
    frame（正相位視窗起始影格）的 PNG；QA 幀是兩段視窗的起始影格，方便不播放影片
    也能檢查雙面板、遮罩、colorbar、文字與箭頭是否被裁切。
    """

    # 只有在此真正需要 video writer 的入口才檢查 imageio；地理 QA overlay 會匯入
    # 本模組的資料工具，但不應因缺少影片編碼套件而無法產出地理稽核證據。
    _require_video_dependencies()
    output_dir.mkdir(parents=True, exist_ok=True)
    mp4_name, poster_name, positive_name, negative_name = _output_names(dataset, pilot, temporal_interpolated)
    mp4_path = output_dir / mp4_name
    poster_path = output_dir / poster_name
    positive_path = output_dir / positive_name
    negative_path = output_dir / negative_name
    targets = (mp4_path, poster_path, positive_path, negative_path)
    if not overwrite:
        existing = [str(path) for path in targets if path.exists()]
        if existing:
            raise FileExistsError("輸出已存在，為避免覆寫請改用 --overwrite：" + ", ".join(existing))

    ffmpeg_executable = _configure_imageio_ffmpeg()
    external_note = dataset.source_mode != "same_source_surface_cache"
    scene = create_scene(
        dataset,
        width=width,
        height=height,
        dpi=dpi,
        target_arrows=target_arrows,
        quiver_scale_multiplier=quiver_scale_multiplier,
        font=font,
        external_note=external_note,
    )
    scene._quiver_step = choose_quiver_step(dataset.lon.size, dataset.lat.size, target_arrows)  # type: ignore[attr-defined]
    plan = _frame_plan(dataset, pilot)
    first_rgb = update_scene(scene, dataset, dataset.payloads[plan[0]])
    scene.fig.savefig(poster_path, dpi=dpi, facecolor="white")
    update_scene(scene, dataset, dataset.payloads[0])
    scene.fig.savefig(positive_path, dpi=dpi, facecolor="white")
    update_scene(scene, dataset, dataset.payloads[WINDOW_FRAME_COUNT])
    scene.fig.savefig(negative_path, dpi=dpi, facecolor="white")
    # 恢復第一幀後再開始串流，確保 poster 與 MP4 的第一個畫面完全一致。
    update_scene(scene, dataset, dataset.payloads[plan[0]])
    try:
        writer = imageio.get_writer(
            str(mp4_path),
            mode="I",
            fps=fps,
            codec="libx264",
            quality=10,
            macro_block_size=1,
            ffmpeg_log_level="error",
            # CRF 16 + slow preset 優先保留黑色中文字與白色箭頭的細邊；仍固定
            # yuv420p、無音訊與 faststart，確保 PowerPoint 相容與點擊播放反應。
            output_params=[
                "-pix_fmt",
                "yuv420p",
                "-an",
                "-movflags",
                "+faststart",
                "-crf",
                "16",
                "-preset",
                "slow",
            ],
        )
    except Exception:
        plt.close(scene.fig)
        raise
    try:
        for ordinal, payload_index in enumerate(plan, start=1):
            if ordinal == 1:
                rgb = first_rgb
            else:
                rgb = update_scene(scene, dataset, dataset.payloads[payload_index])
            writer.append_data(rgb)
            if ordinal % 8 == 0 or ordinal == len(plan):
                print(f"{dataset.spec.key} rendered {ordinal}/{len(plan)} frames", flush=True)
    finally:
        writer.close()
        plt.close(scene.fig)

    return {
        "mp4": {
            "path": str(mp4_path),
            "frame_count_expected": len(plan),
            "fps_requested": fps,
            "duration_expected_seconds": len(plan) / fps,
            "codec_requested": "libx264",
            "pixel_format_requested": "yuv420p",
            "quality_requested": 10,
            "h264_crf_requested": 16,
            "h264_preset_requested": "slow",
            "audio": False,
            "ffmpeg_executable": ffmpeg_executable,
            "sha256": sha256_file(mp4_path),
        },
        "poster": {"path": str(poster_path), "sha256": sha256_file(poster_path)},
        "qa_frames": {
            "positive_phase": {"path": str(positive_path), "sha256": sha256_file(positive_path)},
            "negative_phase": {"path": str(negative_path), "sha256": sha256_file(negative_path)},
        },
        "render_size_px": [width, height],
        "render_dpi": dpi,
        "pilot": pilot,
        "temporal_interpolated": temporal_interpolated,
        "temporal_interpolation": dataset.temporal_interpolation_summary,
        "frame_plan": {
            "intro_hold_frames": INTRO_HOLD_FRAMES,
            "positive_data_frames": WINDOW_FRAME_COUNT if not pilot else PILOT_SAMPLES_PER_WINDOW,
            "negative_data_frames": WINDOW_FRAME_COUNT if not pilot else PILOT_SAMPLES_PER_WINDOW,
            "outro_hold_frames": OUTRO_HOLD_FRAMES,
            "total_frames": len(plan),
        },
        "font_path": str(font_path) if font_path is not None and font_path.is_file() else None,
        "font_available": font is not None,
        "quiver_key_alignment": scene.arrow_key_layout,
        "x_tick_label_layout": scene.axis_tick_layout,
    }


def _selection_manifest(dataset: RegionDataset) -> dict[str, Any]:
    """把內部的兩段 FrameRecord 轉為不依賴 numpy dtype 的 JSON 內容。"""

    details = dataset.selection_details
    windows: list[dict[str, Any]] = []
    for phase, records in (
        ("positive", dataset.selected_records[:WINDOW_FRAME_COUNT]),
        ("negative", dataset.selected_records[WINDOW_FRAME_COUNT:]),
    ):
        if not records:
            continue
        center_record = records[WINDOW_FRAME_COUNT // 2]
        windows.append(
            {
                "phase": phase,
                "label_zh": _phase_label(phase),
                "frame_count": len(records),
                "start_utc": _format_time_utc(records[0].time_ns),
                "end_utc": _format_time_utc(records[-1].time_ns),
                "center_utc": _format_time_utc(center_record.time_ns),
                "center_pc1_standardized": float(dataset.pc_standardized[0, center_record.svd_index]),
                "all_frames_source_valid_non_imputed": True,
                "contiguous_interval_hours": COMMON_INTERVAL_HOURS,
            }
        )
    return {
        "rule": details.get("rule"),
        "candidate_count_positive": details.get("candidate_count_positive"),
        "candidate_count_negative": details.get("candidate_count_negative"),
        "center_offset_frame": details.get("center_offset_frame"),
        "windows": windows,
    }


def _region_manifest(
    dataset: RegionDataset,
    *,
    full_audit: dict[str, Any],
    render_summary: dict[str, Any],
    target_arrows: int,
    quiver_scale_multiplier: float,
    shared_vmax: bool,
) -> dict[str, Any]:
    """建立單區完整 manifest：來源、時間、遮罩、SVD、視覺規格與輸出檔案。

    正式成果的 `svd_dir` 必須指向既有 2026-08-13 water-column SVD；exact coastline
    的 conservative `plot_mask` 只供資料／稽核，觀眾可見流場則使用 SVD 有效域的
    `render_mask`，由高解析度 vector polygon 在最上層定義實際陸地輪廓。這是為了
    避免整格排除相交 cell 所造成的白階梯，不代表重算或改寫正式 SVD 係數。
    """

    metadata = dataset.svd_metadata["metadata"]
    config = dataset.svd_metadata["config"]
    bbox = [float(dataset.lon[0]), float(dataset.lon[-1]), float(dataset.lat[0]), float(dataset.lat[-1])]
    return {
        "region_key": dataset.spec.key,
        "region_name_zh": dataset.spec.name_zh,
        "region_short_name": dataset.spec.short_name,
        "svd_run_dir": str(dataset.svd_dir),
        "formal_svd_source": str(dataset.svd_dir),
        "svd_source_unchanged": True,
        "coastline_correction_scope": "visualization_only",
        "surface_cache_root": str(dataset.cache_root),
        "coastline": dataset.coastline_summary,
        "source_mode": dataset.source_mode,
        "source_relationship": (
            "同源 published OCM surface u/v cache；可做原始與 K90 重建之畫面對照"
            if dataset.source_mode == "same_source_surface_cache"
            else "外部全臺 1 km surface product；不可稱為同源重建殘差，不輸出 RMSE"
        ),
        "svd_surface_semantics": "六層聯合 SVD 模態之表層分量；不是 surface-only SVD",
        "display_extent_source": dataset.display_axis_spec["display_extent_source"],
        "display_extent": [float(value) for value in dataset.display_axis_spec["display_extent"]],
        "display_axis_spec": dataset.display_axis_spec,
        "source_products": {
            "full_taiwan_1km_6h_audit_dir": str(full_audit["dir"]),
            "full_taiwan_metadata_sha256": full_audit["metadata_sha256"],
            "svd_config_surface_velocity_source": config.get("vertical_sampling", {}).get("surface_velocity_source"),
            "svd_metadata_surface_velocity_source": metadata.get("vertical_sampling", {}).get("surface_velocity_source"),
            "surface_cache_grid_metadata_sha256": dataset.cache_metadata["grid_metadata_sha256"],
            "surface_cache_grid_mask_static_sha256": dataset.cache_metadata.get("mask_static_sha256"),
            "surface_month_metadata_sha256": dataset.cache_meta_hashes,
        },
        "grid": {
            "shape_lat_lon": [int(dataset.lat.size), int(dataset.lon.size)],
            "bbox_lon_min_lon_max_lat_min_lat_max": bbox,
            "lon_min": float(np.min(dataset.lon)),
            "lon_max": float(np.max(dataset.lon)),
            "lat_min": float(np.min(dataset.lat)),
            "lat_max": float(np.max(dataset.lat)),
            "grid_match": "cache grid lon/lat/static mask verified identical to SVD grid" if dataset.source_mode == "same_source_surface_cache" else "external grid remeshed to SVD grid",
        },
        "mask": {
            "semantic_definitions": {
                "exact_land": "GeoJSON rasterized cell-center/corner/ring-vertex contact; true geographic land",
                "analysis_geometry_outside": "outside the rectangular analysis geometry; not land",
                "model_static_outside": "inside analysis geometry but outside SVD static ocean mask",
                "surface_feature_unavailable": "static ocean cell not selected as a surface velocity feature",
                "temporal_invalid": "selected surface feature cell is NaN or invalid at this frame",
            },
            "static_ocean_fraction": float(np.mean(dataset.static_ocean_mask)),
            "analysis_geometry_fraction": float(np.mean(dataset.analysis_geometry_mask)),
            "surface_velocity_feature_fraction": float(np.mean(dataset.velocity_feature_mask_surface)),
            "exact_coastline_land_cell_count": int(np.count_nonzero(dataset.coastline_land_mask)),
            "exact_coastline_land_fraction": float(np.mean(dataset.coastline_land_mask)),
            "analysis_geometry_outside_cell_count": int(np.count_nonzero(~dataset.analysis_geometry_mask)),
            "exact_land_outside_analysis_geometry_cell_count": int(
                np.count_nonzero(dataset.coastline_land_mask & ~dataset.analysis_geometry_mask)
            ),
            "plot_mask_fraction": float(np.mean(dataset.plot_mask)),
            "plot_mask_definition": "conservative audit/data mask: analysis_geometry & static_ocean & surface_velocity_feature & ~exact_coastline_land",
            "render_mask_fraction": float(np.mean(dataset.render_mask)),
            "render_mask_definition": "visible field mask: analysis_geometry & static_ocean & surface_velocity_feature; high-resolution vector coastline overlay hides real land",
            "invalid_values_rendered_as": "semantic background categories; exact land is gray-beige GeoJSON overlay and raster land category is transparent",
            "land_quiver_expected_count": 0,
            "land_finite_render_expected_count": 0,
            "dynamic_surface_valid_mask_used_for_same_source_raw": dataset.source_mode == "same_source_surface_cache",
        },
        "time_intersection": {
            "full_product_time_count": int(full_audit["time_count"]),
            "full_product_source_valid_count": int(np.count_nonzero(full_audit["source_valid"])),
            "full_product_imputed_count": int(np.count_nonzero(full_audit["imputed"])),
            "full_product_source_valid_non_imputed_count": int(np.count_nonzero(full_audit["source_valid"] & ~full_audit["imputed"])),
            "svd_time_count": int(dataset.svd_time_ns.size),
            "surface_cache_indexed_time_count": int(dataset.cache_metadata["index_summary"]["time_count_indexed"]),
            "exact_common_source_valid_non_imputed_count": int(dataset.common_time_ns.size),
            "first_common_utc": _format_time_utc(int(dataset.common_time_ns[0])),
            "last_common_utc": _format_time_utc(int(dataset.common_time_ns[-1])),
            "interval_hours": COMMON_INTERVAL_HOURS,
            "excluded_invalid_or_imputed_count": int(full_audit["time_count"] - dataset.common_time_ns.size),
            "common_time_definition": "exact int64 epoch-ns equality among full product, SVD and same-source cache; full source_valid=True and imputed=False",
        },
        "interpolation": {
            "method": dataset.interpolation_method,
            "same_source_remesh_required": dataset.source_mode != "same_source_surface_cache",
            "coastline_policy": "finite/valid mask required; no zero fill or extrapolation",
        },
        "temporal_interpolation": dataset.temporal_interpolation_summary,
        "svd": {
            "surface_layer_index": 0,
            "pc_for_reconstruction": "pc.npy (raw coefficients)",
            "mode_for_reconstruction": "mode_u/v_mps_per_raw_pc.npy",
            "formula": "mean + sum(mode_per_raw_pc[k, surface] * pc[k, t])",
            "k90": dataset.k90,
            "cumulative_explained_variance_first4": [float(value) for value in dataset.cumulative_explained[:4]],
            "cumulative_explained_variance_first4_percent": float(dataset.cumulative_explained[3] * 100.0),
            "k90_cross_check_expected": {"A": 16, "B": 3, "C": 19, "D": 2}[dataset.spec.key],
            "k90_matches_expected": dataset.k90 == {"A": 16, "B": 3, "C": 19, "D": 2}[dataset.spec.key],
            "pc_standardized_display_only": True,
        },
        "phase_windows": _selection_manifest(dataset),
            "visual_spec": {
            "figure_size_px": render_summary["render_size_px"],
            "render_dpi": render_summary.get("render_dpi"),
            "text_style": {
                "visible_text_color": TEXT_COLOR,
                "title_fontsize_points": TITLE_FONT_SIZE_PT,
                "phase_utc_fontsize_points": PHASE_FONT_SIZE_PT,
                "panel_caption_fontsize_points": CAPTION_FONT_SIZE_PT,
                "axis_tick_color": TEXT_COLOR,
                "axis_tick_fontsize_points": AXIS_FONT_SIZE_PT,
                "colorbar_text_color": TEXT_COLOR,
                "colorbar_fontsize_points": AXIS_FONT_SIZE_PT,
                "arrow_legend_fontsize_points": ARROW_KEY_FONT_SIZE_PT,
            },
            "fps": DEFAULT_FPS,
            "data_frames_per_window": WINDOW_FRAME_COUNT,
            "data_interval_hours": COMMON_INTERVAL_HOURS,
            "temporal_interpolation_enabled": bool(dataset.temporal_interpolation_summary.get("enabled", False)),
            "temporal_interpolation_method": dataset.temporal_interpolation_summary.get("method"),
            "speed_units": "m/s",
            "fixed_speed_colorbar": {
                "vmin_mps": 0.0,
                "vmax_mps": float(dataset.speed_scale_vmax),
                "ticks_mps": [float(value) for value in _speed_ticks(dataset.speed_scale_vmax)],
                "tick_format": "one decimal",
                "scope": "fixed for both panels and all frames in this region video",
                "scope_shared_across_four_regions": shared_vmax,
                "selection_p995_mps": float(dataset.speed_scale_p995),
                "label": COLORBAR_LABEL,
                "unit_layout": "single complete vertical label on the outer right side; rotated 90 degrees",
                "label_rotation_degrees": 90,
                "label_position": "outer right, vertically centered",
                "labelpad_points": 12,
                "tick_pad_points": 3,
                "colorbar_width_fraction": 0.025,
                "right_margin_fraction": 0.075,
            },
            "panel_layout": {
                "top_axes_fraction": [0.10, 0.56, 0.75, 0.35],
                "bottom_axes_fraction": [0.10, 0.15, 0.75, 0.35],
                "colorbar_fraction": [0.875, 0.15, 0.025, 0.76],
                "top_caption_y_fraction": 0.545,
                "bottom_caption_y_fraction": 0.065,
                "arrow_legend_y_fraction": ARROW_KEY_Y,
                "bottom_margin_fraction": 0.04,
                "bottom_white_margin_target_px": [30, 90],
                "caption_and_legend_band": "bottom caption and 1 m/s legend share the same horizontal band",
            },
            "quiver": {
                "target_arrows": target_arrows,
                "step_yx": list(choose_quiver_step(dataset.lon.size, dataset.lat.size, target_arrows)),
                "scale_multiplier": quiver_scale_multiplier,
                "reference_mps": float(dataset.quiver_reference_mps),
                "legend_reference_mps": 1.0,
                "legend_label": ARROW_LEGEND_LABEL,
                "legend_artist": "Matplotlib QuiverKey using the same quiver scale with U=1.0",
                "legend_position_figure_fraction": (
                    render_summary.get("quiver_key_alignment", {}).get(
                        "key_figure_anchor", [ARROW_KEY_X, ARROW_KEY_Y]
                    )
                ),
                "legend_alignment": render_summary.get("quiver_key_alignment"),
                "legend_fontsize_points": ARROW_KEY_FONT_SIZE_PT,
                "color": QUIVER_COLOR,
                "drawn_over": "speed background; white with subdued dark outline",
            },
            "coastline_overlay": {
                "source": dataset.coastline_summary["path"],
                "sha256": dataset.coastline_summary["sha256"],
                "polygon_count": dataset.coastline_summary["polygon_count"],
                "zorder": 30,
                "facecolor": LAND_COLOR,
                "edgecolor": LAND_EDGE_COLOR,
                "linewidth": LAND_EDGE_WIDTH,
                "antialiased": LAND_ANTIALIASED,
                "visible_land_source": "high-resolution GeoJSON vector polygon fill",
                "raster_land_background_visible": False,
                "rasterize_semantics": dataset.coastline_summary["rasterize_semantics"],
            },
            "display_text": _display_text_spec(
                dataset,
                external_note=dataset.source_mode != "same_source_surface_cache",
            ),
            "display_extent_source": dataset.display_axis_spec["display_extent_source"],
            "display_extent": [float(value) for value in dataset.display_axis_spec["display_extent"]],
            "axis_labels": {"x": "經度（°E）", "y": "緯度（°N）"},
            "axis_ticks": {
                "x": {
                    "major_locator": "FixedLocator",
                    "major_values": [float(value) for value in dataset.display_axis_spec["x_major_values"]],
                    "major_formatter": str(dataset.display_axis_spec["x_major_formatter"]),
                    "minor_locator": "MultipleLocator(0.2)",
                    "minor_labels": False,
                    "endpoint_semantics": "display ticks follow the核對簡報靜態圖; raw SVD bbox is retained separately and is not displayed as endpoint labels",
                    "label_bbox_qa": render_summary.get("x_tick_label_layout"),
                },
                "y": {
                    "major_locator": "FixedLocator",
                    "major_values": [float(value) for value in dataset.display_axis_spec["y_major_values"]],
                    "major_formatter": str(dataset.display_axis_spec["y_major_formatter"]),
                    "major_labels_omit_degree_suffix": True,
                },
                "top_panel_x_labels": False,
                "bottom_panel_x_labels": True,
            },
            "large_title": True,
            "academic_info_strip": False,
            "phase_utc_strip": True,
            "scientific_semantics_in_frame": False,
        },
        "outputs": render_summary,
        "limitations": [
            "兩段視窗是 PC1 正/負相位的代表性案例，不宣稱為全年統計驗證。",
            "同源模式沒有計算或輸出 RMSE/殘差結論；兩面板用於視覺對照。",
            "箭頭抽樣與 scale 是展示用視覺設定，不代表流速資料被降採樣或改寫。",
            "exact coastline 只阻止真實陸地上的流速色塊與箭頭被展示，未重新定義或重算正式 SVD。",
            *(
                [
                    "時間內插版是展示用線性轉換：部分 6 小時畫面由相鄰 12 小時真實錨點估計，虛擬影格不代表新的觀測資料。",
                    "時間內插不跨越缺值或視窗邊界，若兩錨點任一端無有限 u/v，對應格點仍維持 NaN。",
                ]
                if dataset.temporal_interpolation_summary.get("enabled")
                else []
            ),
        ],
    }


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    """以 UTF-8、繁中不跳脫 JSON 寫出 manifest，並保留可讀縮排。"""

    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _build_temporal_interpolation_comparison(
    candidate_manifest: dict[str, Any],
    baseline_manifest_path: Path,
    candidate_manifest_path: Path,
) -> dict[str, Any]:
    """建立內插版與既有 v3 的可追溯對照摘要。

    對照只比較播放層面的條件：正式 SVD 路徑、岸線 scope、phase window、顯示
    extent、fps、總影格數與預期片長。它不把「內插場與被跳過觀測場的差」當成
    SVD 誤差，也不重新計算任何模態；各區更細的展示診斷已保留在 manifest 的
    `regions[*].temporal_interpolation.window_summaries`。
    """

    comparison: dict[str, Any] = {
        "schema_name": "ocm_svd_animation_temporal_interpolation_comparison",
        "schema_version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "candidate_manifest_path": str(candidate_manifest_path.resolve()),
        "baseline_manifest_path": str(baseline_manifest_path.resolve()),
        "baseline_available": baseline_manifest_path.is_file(),
        "comparison_semantics": "同一正式 SVD/岸線/選窗/畫面規格下，對照是否加入展示用時間內插；不是模型誤差評估",
        "regions": [],
        "limitations": [
            "內插版為保持 4 fps、16 秒與 64 幀而減少部分真實觀測影格的直接顯示；虛擬場不代表新觀測。",
            "本摘要不以像素差或向量差推導預測能力、SVD 誤差或物理結論。",
        ],
    }
    if not baseline_manifest_path.is_file():
        comparison["note_zh"] = "找不到既有 v3 manifest；候選影片仍可單獨驗證，但無法完成檔案級並列摘要。"
        return comparison
    baseline = read_json(baseline_manifest_path)
    comparison["baseline_svd_source"] = baseline.get("formal_svd_source")
    comparison["candidate_svd_source"] = candidate_manifest.get("formal_svd_source")
    comparison["same_formal_svd_source"] = baseline.get("formal_svd_source") == candidate_manifest.get("formal_svd_source")
    baseline_by_key = {str(item.get("region_key")): item for item in baseline.get("regions", []) if isinstance(item, dict)}
    for candidate_region in candidate_manifest.get("regions", []):
        if not isinstance(candidate_region, dict):
            continue
        key = str(candidate_region.get("region_key"))
        baseline_region = baseline_by_key.get(key, {})
        base_outputs = baseline_region.get("outputs", {}) if isinstance(baseline_region, dict) else {}
        cand_outputs = candidate_region.get("outputs", {})
        base_plan = base_outputs.get("frame_plan", {}) if isinstance(base_outputs, dict) else {}
        cand_plan = cand_outputs.get("frame_plan", {}) if isinstance(cand_outputs, dict) else {}
        base_windows = baseline_region.get("phase_windows", {}).get("windows", []) if isinstance(baseline_region, dict) else []
        cand_windows = candidate_region.get("phase_windows", {}).get("windows", [])
        comparison["regions"].append(
            {
                "region_key": key,
                "baseline_video": base_outputs.get("mp4", {}).get("path") if isinstance(base_outputs, dict) else None,
                "candidate_video": cand_outputs.get("mp4", {}).get("path") if isinstance(cand_outputs, dict) else None,
                "same_phase_windows": base_windows == cand_windows,
                "same_display_extent": baseline_region.get("display_extent") == candidate_region.get("display_extent"),
                "same_formal_svd_run": baseline_region.get("formal_svd_source") == candidate_region.get("formal_svd_source"),
                "baseline_frame_count": base_plan.get("total_frames"),
                "candidate_frame_count": cand_plan.get("total_frames"),
                "frame_count_delta": (
                    cand_plan.get("total_frames") - base_plan.get("total_frames")
                    if isinstance(cand_plan.get("total_frames"), int) and isinstance(base_plan.get("total_frames"), int)
                    else None
                ),
                "baseline_expected_duration_seconds": base_outputs.get("mp4", {}).get("duration_expected_seconds") if isinstance(base_outputs, dict) else None,
                "candidate_expected_duration_seconds": cand_outputs.get("mp4", {}).get("duration_expected_seconds") if isinstance(cand_outputs, dict) else None,
                "candidate_temporal_interpolation": candidate_region.get("temporal_interpolation"),
            }
        )
    comparison["all_same_phase_windows"] = all(item["same_phase_windows"] for item in comparison["regions"])
    comparison["all_same_display_extent"] = all(item["same_display_extent"] for item in comparison["regions"])
    return comparison


def _write_output_readme(path: Path, manifest: dict[str, Any]) -> None:
    """寫出本次 renderer 版本的繁體中文 README，避免沿用 v3 的舊時間語意。

    這份 README 是輸出資料夾內的研究追溯文件，不是 PPTX 說明文字。內容刻意把
    觀眾可見術語、內部 `pc.npy`/`K90` 欄位、exact coastline 展示遮罩與時間內插
    的限制分開，讓後續比較時不會把展示用虛擬影格誤認成上游分析重算。
    """

    temporal = manifest.get("temporal_interpolation", {})
    comparison = manifest.get("comparison_reference", {})
    regions = ", ".join(str(value) for value in manifest.get("render_scope", []))
    lines = [
        "# 四海域表層流場關聯動畫—時間內插對照版",
        "",
        "本目錄是既有 `formal_abcd_slide_aligned_v3` 的並列展示版本；不修改 PPTX，",
        "也不覆寫 v3 或正式 SVD。畫面仍是四海域簡報右側輔助動畫，上方為原始流場、",
        "下方為既有六層聯合 SVD 模態之表層分量的前 n 個模態重建流場。",
        "",
        "## 來源與科學範圍",
        "",
        f"- 輸出區域：{regions}。",
        f"- 正式 SVD：`{manifest.get('formal_svd_source')}`；`svd_source_unchanged=true`。",
        "- SVD 重建仍使用 `mean + Σ(mode_u/v_mps_per_raw_pc × pc.npy)`，表層層位 index=0；未使用標準化係數替代 raw PC。",
        "- `coastline_correction_scope=visualization_only`：exact coastline 僅用於展示階段的陸地遮蔽與向量 polygon 疊加，不改寫 SVD、原始 cache 或遮罩來源。",
        "- 兩段視窗仍是第一模態時間係數正／負相位的代表性案例，不代表全年統計驗證。",
        "",
        "## 時間內插版本",
        "",
        f"- 方法：`{temporal.get('method')}`。",
        f"- 來源觀測間隔：{temporal.get('source_observation_interval_hours')} 小時；錨點間隔：{temporal.get('anchor_interval_hours')} 小時；內插比例 alpha：{temporal.get('region_summaries', {}).get(next(iter(temporal.get('region_summaries', {})), ''), {}).get('alpha', temporal.get('alpha')) if isinstance(temporal.get('region_summaries'), dict) else temporal.get('alpha')}。",
        "- 每段 28 格輸出中，15 格為真實錨點、13 格為左右錨點正中央的展示用線性內插；每段起訖端點保留真實值。",
        "- 內插只在完整連續視窗、兩端 u/v 均有限且通過 render mask 的格點進行；不跨缺口、不外插、不回寫資料。",
        "- 為公平比較，內插版仍維持 4 fps、64 幀、約 16 秒；因此它改善的是影格間的轉換平滑度，不會增加實際播放時間或原始時間解析度。",
        f"- v3 對照目錄：`{comparison.get('directory')}`；詳見 `temporal_interpolation_v3_comparison.json`。",
        "",
        "## 輸出與 QA",
        "",
        "每區包含帶有 `_temporal_interpolated` 後綴的 H.264/yuv420p MP4、poster、正／負相位影格；`qa/` 內有首／中／末 contact sheet、exact-land 地理 QA overlay 與摘要。",
        "請以 `animation_manifest.json` 核對實際輸出雜湊、固定流速色階、簡報頁面座標、quiver 1 m/s 圖例、地理遮罩與時間內插每區診斷；再用 `temporal_interpolation_v3_comparison.json` 與 v3 並列觀看。",
        "",
        "## 主要限制",
        "",
        "時間內插是 presentation-only 的場平滑處理，不是新的 OCM 觀測、預報、SVD 重算或誤差校正；若研究上需要保留每一筆 6 小時觀測的原貌，應使用未啟用內插的 v3。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    """解析 server/local renderer 共用的 CLI 參數。"""

    parser = argparse.ArgumentParser(description="Render four-region SVD modal-context surface-current MP4 animations.")
    parser.add_argument("--svd-base", type=Path, required=True, help="water-column SVD run parent directory")
    parser.add_argument("--surface-cache-base", type=Path, required=True, help="preprocessed/ocm_surface parent directory")
    parser.add_argument("--full-product-dir", type=Path, required=True, help="full Taiwan 1 km 6h product for exact time audit")
    parser.add_argument("--coastline-geojson", type=Path, required=True, help="exact coastline GeoJSON used for rasterize and vector overlay")
    parser.add_argument("--output-dir", type=Path, required=True, help="new versioned output directory")
    parser.add_argument("--svd-directory-suffix", default="", help="version suffix appended to each region SVD directory, e.g. _coastline_corrected_v2")
    parser.add_argument("--regions", default="A,B,C,D", help="comma-separated region keys, e.g. A or A,B")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS, help="MP4 frames per second")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="output width in pixels")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help="output height in pixels")
    parser.add_argument("--dpi", type=int, default=DEFAULT_RENDER_DPI, help="Matplotlib raster DPI; width/dpi and height/dpi define figure size")
    parser.add_argument("--target-arrows", type=int, default=DEFAULT_TARGET_ARROWS, help="approximate arrows per panel")
    parser.add_argument("--quiver-scale-multiplier", type=float, default=DEFAULT_QUIVER_SCALE_MULTIPLIER, help="larger values make arrows shorter")
    parser.add_argument("--font-path", type=Path, default=None, help="CJK font file for Chinese labels")
    parser.add_argument("--pilot", action="store_true", help="A low-resolution/few-frame pilot; still selects complete 28-frame windows")
    parser.add_argument(
        "--temporal-interpolation",
        action="store_true",
        help="use display-only linear interpolation between 12-hour anchors while preserving 4 fps/16 seconds/64 frames",
    )
    parser.add_argument("--overwrite", action="store_true", help="allow overwrite only inside the explicitly supplied new output directory")
    return parser.parse_args()


def main() -> None:
    """執行時間追溯、兩段相位選取、MP4/QA 輸出與 manifest 寫入。"""

    args = parse_args()
    if args.fps <= 0 or args.width <= 0 or args.height <= 0 or args.dpi <= 0 or args.target_arrows <= 0 or args.quiver_scale_multiplier <= 0:
        raise ValueError("fps/width/height/dpi/target-arrows/quiver-scale-multiplier 必須為正值")
    if args.width % 2 or args.height % 2:
        raise ValueError("H.264 yuv420p 輸出要求 width/height 為偶數")
    if args.pilot:
        if args.width == DEFAULT_WIDTH:
            args.width = PILOT_WIDTH
        if args.height == DEFAULT_HEIGHT:
            args.height = PILOT_HEIGHT
    specs = build_region_specs(args.regions.split(","))
    full_audit = load_full_product_audit(args.full_product_dir)
    font = find_cjk_font(args.font_path)
    print(f"font={args.font_path if args.font_path else 'auto'} available={font is not None}", flush=True)
    datasets: list[RegionDataset] = []
    for spec in specs:
        dataset = load_region_dataset(
            spec,
            svd_base=args.svd_base,
            surface_cache_base=args.surface_cache_base,
            full_audit=full_audit,
            coastline_geojson=args.coastline_geojson,
            svd_directory_suffix=args.svd_directory_suffix,
        )
        print(
            f"{spec.key} source_mode={dataset.source_mode} common_6h={dataset.common_time_ns.size} k90={dataset.k90} cum4={dataset.cumulative_explained[3] * 100:.2f}%",
            flush=True,
        )
        select_phase_windows(dataset)
        materialize_payloads(dataset, full_audit)
        if args.temporal_interpolation:
            apply_temporal_interpolation(dataset)
        else:
            set_temporal_interpolation_disabled(dataset)
        choose_speed_and_arrow_scales(dataset)
        print(
            f"{spec.key} positive={_format_time_utc(dataset.selected_records[0].time_ns)}..{_format_time_utc(dataset.selected_records[27].time_ns)} "
            f"negative={_format_time_utc(dataset.selected_records[28].time_ns)}..{_format_time_utc(dataset.selected_records[55].time_ns)} "
            f"p995={dataset.speed_scale_p995:.3f} vmax={dataset.speed_scale_vmax:.1f} arrow_ref={dataset.quiver_reference_mps:.3f}",
            flush=True,
        )
        datasets.append(dataset)
    shared_vmax_value = choose_global_vmax(datasets)
    shared_vmax = shared_vmax_value > 0.0
    if shared_vmax:
        print(f"shared fixed speed vmax={shared_vmax_value:.1f} m/s across selected regions", flush=True)
    else:
        print("fixed speed vmax is region-specific because selected p99.5 ranges differ by >3x", flush=True)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    render_summaries: list[dict[str, Any]] = []
    for dataset in datasets:
        summary = render_region_animation(
            dataset,
            output_dir=args.output_dir,
            fps=args.fps,
            width=args.width,
            height=args.height,
            dpi=args.dpi,
            target_arrows=args.target_arrows,
            quiver_scale_multiplier=args.quiver_scale_multiplier,
            font=font,
            font_path=args.font_path,
            pilot=args.pilot,
            temporal_interpolated=args.temporal_interpolation,
            overwrite=args.overwrite,
        )
        render_summaries.append(summary)
    manifest = {
        "schema_name": "ocm_svd_modal_context_animation_manifest",
        "schema_version": SCRIPT_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "renderer": str(Path(__file__).resolve()),
        "purpose": "四海域簡報右側輔助動畫；不取代第6–9頁左側空間模態與完整時間序列研究圖",
        "academic_semantics": "六層聯合 SVD 模態之表層分量；不是 surface-only SVD",
        "svd_source_unchanged": True,
        "coastline_correction_scope": "visualization_only",
        "formal_svd_source": str(args.svd_base.resolve()),
        "formal_svd_source_policy": "正式動畫只讀取既有 2026-08-13 water-column SVD；不使用 coastline-corrected 重算結果",
        # `coastline_source` 是本版完整欄位；另保留同內容的 `coastline` 頂層別名，
        # 讓獨立 QA 工具不必依賴特定 schema 命名即可直接核對岸線 hash、polygon
        # 數與 rasterize 語意。兩者都只描述展示階段來源，不代表 SVD 上游已修正。
        "coastline": datasets[0].coastline_summary if datasets else None,
        "coastline_source": datasets[0].coastline_summary if datasets else None,
        "coastline_usage": "同一份 exact coastline GeoJSON 僅在展示階段用於 cell-overlap land mask 與最高 z-order 向量陸地覆蓋；不以 SVD/分析遮罩邊界代替岸線，也不改寫正式 SVD",
        "display_extent_source": (
            datasets[0].display_axis_spec["display_extent_source"]
            if len(datasets) == 1
            else "per_region_static_slide_crosscheck"
        ),
        "svd_directory_suffix": args.svd_directory_suffix,
        "pilot": bool(args.pilot),
        "temporal_interpolation": {
            "enabled": bool(args.temporal_interpolation),
            "method": (
                "piecewise_linear_display_only"
                if args.temporal_interpolation
                else "none; exact 6-hour observed payload display"
            ),
            "source_observation_interval_hours": COMMON_INTERVAL_HOURS,
            "anchor_interval_hours": (
                COMMON_INTERVAL_HOURS * TEMPORAL_INTERPOLATION_ANCHOR_STRIDE
                if args.temporal_interpolation
                else COMMON_INTERVAL_HOURS
            ),
            "virtual_frame_interval_hours": COMMON_INTERVAL_HOURS if args.temporal_interpolation else None,
            "preserves_4fps_16seconds_64frames": True,
            "scope": "display payload only; formal SVD, source cache, masks and coastline are unchanged",
            "region_summaries": {
                dataset.spec.key: dataset.temporal_interpolation_summary for dataset in datasets
            },
        },
        "comparison_reference": {
            "label": "現有未啟用時間內插的 formal_abcd_slide_aligned_v3",
            "directory": str((args.output_dir.parent / "formal_abcd_slide_aligned_v3").resolve()),
            "manifest_path": str((args.output_dir.parent / "formal_abcd_slide_aligned_v3" / "animation_manifest.json").resolve()),
            "exists_at_render_time": (args.output_dir.parent / "formal_abcd_slide_aligned_v3" / "animation_manifest.json").is_file(),
            "comparison_scope": "同一正式 SVD、同一岸線、同一選窗與同一畫面規格；只比較 temporal payload 播放差異",
        },
        # C pilot 為完整 64 幀的 display-only gate，並不等同於 renderer 的低影格
        # `--pilot` 模式；額外記錄 scope 讓獨立檢查者不會把單區 gate 誤認成 A–D
        # 正式成果。正式四區輸出會以 render_scope=["A", "B", "C", "D"] 並將
        # pilot_gate 設為 false。
        "render_scope": [dataset.spec.key for dataset in datasets],
        "pilot_gate": bool(len(datasets) == 1 and datasets[0].spec.key == "C"),
        "pilot_gate_requires_independent_review": bool(len(datasets) == 1 and datasets[0].spec.key == "C"),
        "source_audit": {
            "full_taiwan_product_dir": str(full_audit["dir"]),
            "full_taiwan_metadata_sha256": full_audit["metadata_sha256"],
            "full_taiwan_time_count": full_audit["time_count"],
            "full_taiwan_source_valid_count": int(np.count_nonzero(full_audit["source_valid"])),
            "full_taiwan_imputed_count": int(np.count_nonzero(full_audit["imputed"])),
            "full_taiwan_source_valid_non_imputed_count": int(np.count_nonzero(full_audit["source_valid"] & ~full_audit["imputed"])),
        },
        "render_policy": {
            "fps": args.fps,
            "width_px": args.width,
            "height_px": args.height,
            "raster_dpi": args.dpi,
            "expected_formal_duration_seconds": (WINDOW_FRAME_COUNT * 2 + INTRO_HOLD_FRAMES + OUTRO_HOLD_FRAMES) / args.fps,
            "formal_data_frame_count": WINDOW_FRAME_COUNT * 2,
            "window_frame_count": WINDOW_FRAME_COUNT,
            "window_interval_hours": COMMON_INTERVAL_HOURS,
            "temporal_interpolation_enabled": bool(args.temporal_interpolation),
            "temporal_interpolation_method": (
                "piecewise_linear_display_only"
                if args.temporal_interpolation
                else "none; exact 6-hour observed payload display"
            ),
            "temporal_interpolation_preserves_frame_count": True,
            "target_arrows": args.target_arrows,
            "quiver_scale_multiplier": args.quiver_scale_multiplier,
            "no_audio": True,
            "codec": "libx264",
            "pixel_format": "yuv420p",
            "h264_quality": 10,
            "h264_crf": 16,
            "h264_preset": "slow",
            "font_path": str(args.font_path) if args.font_path else None,
            "font_available": font is not None,
            "shared_fixed_vmax_mps": shared_vmax_value if shared_vmax else None,
            "fixed_colorbar_tick_format": "%.1f",
        },
        "regions": [
            _region_manifest(
                dataset,
                full_audit=full_audit,
                render_summary=summary,
                target_arrows=args.target_arrows,
                quiver_scale_multiplier=args.quiver_scale_multiplier,
                shared_vmax=shared_vmax,
            )
            for dataset, summary in zip(datasets, render_summaries)
        ],
    }
    manifest_path = args.output_dir / "animation_manifest.json"
    if args.temporal_interpolation:
        comparison_path = args.output_dir / "temporal_interpolation_v3_comparison.json"
        comparison = _build_temporal_interpolation_comparison(
            manifest,
            args.output_dir.parent / "formal_abcd_slide_aligned_v3" / "animation_manifest.json",
            manifest_path,
        )
        comparison_path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest["temporal_interpolation_comparison"] = {
            "path": str(comparison_path),
            "baseline_manifest_path": comparison["baseline_manifest_path"],
            "baseline_available": comparison["baseline_available"],
            "same_formal_svd_source": comparison.get("same_formal_svd_source"),
            "all_same_phase_windows": comparison.get("all_same_phase_windows"),
            "all_same_display_extent": comparison.get("all_same_display_extent"),
        }
    _write_manifest(manifest_path, manifest)
    _write_output_readme(args.output_dir / "README.md", manifest)
    render_scope = ",".join(dataset.spec.key for dataset in datasets)
    completion_label = "C display-only pilot" if manifest["pilot_gate"] else "A–D formal display-only animations"
    (args.output_dir / "RENDER_COMPLETE").write_text(
        f"{completion_label}（scope={render_scope}）已完成 renderer 輸出；ffprobe/frame QA 由 validate_ocm_svd_modal_context.py 補寫。\n",
        encoding="utf-8",
    )
    print(f"manifest={manifest_path}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
