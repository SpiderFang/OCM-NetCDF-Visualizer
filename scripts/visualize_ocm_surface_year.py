"""由年度 1 km 表層 `.npy` 中間檔產生簡報版與備查版流場動畫。

本腳本不再讀取大型原始 NetCDF，而是讀取
`preprocess_ocm_surface_year.py` 產生的 `u_surface.npy`、`v_surface.npy`、
`speed_surface.npy` 與時間/遮罩 metadata。這樣可以把昂貴的 NetCDF 解碼與網格
插值和 GIF 繪圖分開，後續調整 FPS、箭頭密度或底圖色階時不必重新讀取數 TB
原始檔。

輸出三種用途不同的產品：

- `global_trend_surface_layer_047_four_regions.gif`：每 2 日一幀的簡報總覽，
  以固定且適度密集的 1 km 報告圖箭頭風格標示四個研究區域。
- `surface_layer_047_speed_fixed_scale_four_regions.gif`：同一趨勢抽樣，但以
  全年度固定流速色階作背景，作為流速強區變化的備查產品。
- `annual_full_surface_layer_047_6h.gif`：保留完整 6 小時時間軸的年度備查動畫；
  缺日幀不會在圖面上加註文字，但原始/補值狀態仍保留於 NumPy 標記檔。

箭頭策略保留 1 km 報告圖的箭頭外觀，但將目標數量提高至約 2600 支、抽樣步距
約 11 格，讓完整台灣周邊範圍能看出更連續的局部流向與強流區變化；同時將
`quiver scale multiplier` 降至 16，使箭頭比前一版適度加長。中性海域底圖沿用
報告圖的深藍系箭頭；為避免在灰米色陸地與深灰海岸線旁看起來接近黑色，中性版
使用較明亮但仍低飽和的中深藍青色，固定流速背景的亮色/暗色變化較複雜則改用白色
箭頭提高對比。
陸地使用中度灰米色以分離海陸，但不代表任何物理量；四個研究區域框均取消內部填色，
讓框內速度背景與箭頭保持連續。中性趨勢版維持報告圖深藍框線，固定流速備查版則
使用低飽和磚紅外框，避免與白色箭頭或 viridis 背景混淆；這些框線只表示研究區域
位置，不代表警示、流速強弱或資料品質。
固定流速版另使用較寬的畫布與獨立 colorbar 軸，避免色條壓縮主圖；主圖仍維持
與中性版本相同的經緯度比例與主要資料框高度，單位標籤右側保留足夠留白。
固定流速背景則採產品規格預先指定的 `0.0–2.0 m/s` 範圍與 `0.5 m/s` 固定刻度，
不使用資料衍生的上下限；GIF 另使用整部動畫共用的固定 256 色盤，避免每幀重新量化
造成色條顏色跳動，且不改變任何流速數值或色階上下限。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# SERVER 與本機環境都已具備 imageio/Pillow；這裡直接使用 imageio 舊版 Pillow GIF
# writer 的串流核心，再覆寫其單幀量化方法。原因是 imageio 預設會為每幀建立
# adaptive palette，雖然 Matplotlib 的 m/s 色階固定，GIF 播放時仍可能因每幀 local
# palette 不同而讓色條視覺跳動。使用既有套件的內部 writer 不需在 SERVER 安裝新
# 套件，也能保留逐幀串流，避免把 2924 張 726×1210 影像全部載入記憶體。
try:
    from imageio.plugins.pillowmulti import GifWriter as _ImageIOGifWriter
except ImportError:
    # imageio 2.9 可直接匯入 pillowmulti；較新的 imageio 版本會讓 pillowmulti
    # 與 pillow_legacy 互相匯入，因此先完成 legacy 初始化後再重試。兩個路徑都
    # 指向同一個 GIF 串流核心，目的是同時相容 SERVER 的 Anaconda 與本機虛擬環境。
    try:
        import imageio.plugins.pillow_legacy  # noqa: F401
        from imageio.plugins.pillowmulti import GifWriter as _ImageIOGifWriter
    except ImportError as exc:  # pragma: no cover - 正式執行環境預期已有 imageio
        raise ImportError("固定色盤 GIF 需要 imageio 的 Pillow GIF writer") from exc
import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np


MISSING_DATA_COLOR = "#d9d9d9"
"""無資料/乾地背景色；不代表流速為零。"""

LAND_COLOR = "#9f9a90"
"""陸地的中度灰米色；僅代表 mask=False，不承載海洋物理量。

相較於海域底色與流速色階，這個中性、低飽和色只負責讓台灣本島與離島從海域
中分離出來；它不表示流速、eta、水深或任何其它物理量，避免觀眾把陸地顏色
誤讀成資料值。
"""

LAND_EDGE_COLOR = "#555555"
"""陸海邊界線顏色。"""

OCEAN_COLOR = "#e7f2f3"
"""中性海域底色；中性動畫的速度大小完全由箭頭長度表示。"""

QUIVER_COLOR = "#1f5f83"
"""中性趨勢版的中深藍青色流速箭頭。

這個顏色保留 1 km 報告圖的深藍系視覺語彙，但比原本的 `#123b5d` 稍微提高
亮度與藍綠分量，讓箭頭在淡色海域與中度灰米色陸地、深灰海岸線旁不會視覺上
接近黑色。它仍是固定版面顏色，不承載額外的流速分類或資料品質意義。
"""

FIXED_SPEED_QUIVER_COLOR = "#ffffff"
"""固定流速色階背景上的白色流速箭頭。

固定流速底圖使用 viridis，單幀內可能同時出現深紫、綠色與黃色區域；白色箭頭
可在大部分色階上維持清楚輪廓。此顏色只套用於固定流速備查版，中性趨勢版仍
保留 1 km 報告圖的深藍系視覺語彙。
"""

BBOX_EDGE_COLOR = "#2c2f83"
"""中性趨勢版四個研究區域框線的深藍色；延續 1 km 報告圖視覺語彙。"""

FIXED_SPEED_BBOX_EDGE_COLOR = "#c85a54"
"""固定流速備查版研究區域框線的低飽和磚紅色。

固定版的流速背景使用 viridis、箭頭使用白色，因此以不過度刺眼的磚紅只畫外框，
可以同時避開白色箭頭與深色/亮色流速背景；此框線是研究區域位置標示，不是警示
色，也不代表流速強弱或資料品質。
"""

DEFAULT_TARGET_ARROWS = 2600
"""提高後的每幀目標箭頭數量。

完整規則格點約為 780×409；目前正式設定為約 2600 支、抽樣步距約 11 格，能
補足大範圍圖面中過大的空白，同時以 `quiver_scale_multiplier=16` 適度加長箭頭。
實際數量會受規則步距取整及海域有效遮罩影響，因此這是目標值而非嚴格固定值。
"""

DEFAULT_QUIVER_SCALE_MULTIPLIER = 16.0
"""箭頭縮放倍數；相較前版 20 降為 16，使箭頭約加長而仍避免過度重疊。"""

DEFAULT_FIXED_SPEED_VMIN = 0.0
"""固定流速背景色階的下限，0 m/s 代表靜止流體。"""

DEFAULT_FIXED_SPEED_VMAX = 2.0
"""固定流速背景色階的上限；超過此值的罕見強流會以最深色呈現。"""

DEFAULT_FIXED_SPEED_TICKS = (0.0, 0.5, 1.0, 1.5, 2.0)
"""固定流速 colorbar 的刻度，避免每次依資料範圍自動產生不同刻度。"""

FIXED_SPEED_COLORBAR_LABEL = "流速(公尺/秒)"
"""備查版固定流速 colorbar 的繁體中文單位標籤。"""

X_AXIS_LABEL = "經度"
"""所有年度表層動畫 X 軸的中文標籤；軸刻度仍以實際經度數值顯示。"""

Y_AXIS_LABEL = "緯度"
"""所有年度表層動畫 Y 軸的中文標籤；軸刻度仍以實際緯度數值顯示。"""

NORMAL_FIGURE_SIZE_INCHES = (6.6, 11.0)
"""中性趨勢與年度備查版的固定畫布尺寸，單位為英吋。"""

FIXED_SPEED_FIGURE_SIZE_INCHES = (7.2, 11.0)
"""固定流速版的畫布尺寸，單位為英吋。

固定流速版需要同時容納主圖、垂直 colorbar 與繁體中文單位標籤；若沿用中性版
寬度，colorbar 會被 Matplotlib 從主圖右側自動扣除，配合等比例經緯度座標後使
主圖上下縮小。增加右側畫布寬度只提供版面空間，不改變主圖的經緯度範圍或資料比例。
"""

FIGURE_LEFT_FRACTION = 0.085
"""三種動畫共用的主圖左側邊界，為畫布寬度的比例值。

左側需要容納經度/緯度軸標籤與刻度；此值只控制圖面留白，不會改變資料的經緯度
範圍。將邊界集中成常數，可讓三個版本在調整垂直版面時維持相同的左側基準。
"""

FIGURE_BOTTOM_FRACTION = 0.055
"""三種動畫共用的主圖下側邊界，為畫布高度的比例值。

下側留白需保留經度軸名稱與刻度，因此本次只收縮上側空白，不任意壓縮下側軸文字
空間，避免簡報縮放後底部標籤與畫布邊緣重疊。
"""

FIGURE_TOP_FRACTION = 0.9675
"""三種動畫共用的主圖上側邊界，為畫布高度的比例值。

舊設定 `top=0.935` 代表上側約 6.5% 為空白；本值使上側約 3.25% 為空白，約縮減
一半，讓地圖在縱向畫布中更集中。這是圖面裁切/排版設定，不會裁掉台灣周邊地理範圍；
由於主圖仍使用等比例經緯度座標，實際資料框會依可用寬高取較小者自動置中。
"""

FIXED_SPEED_MAIN_RIGHT_FRACTION = 0.883
"""固定流速版主圖右側邊界，為畫布寬度的比例值。

上側空白縮減後，主圖可用高度會增加；固定版若仍使用過窄的右界，等比例座標可能
由寬度限制主圖高度，造成它再次略小於中性版。因此將右界由 `0.88` 微調至 `0.883`，
在色條左界 `0.895` 前保留約 1.2% 畫布寬度的間距，讓主圖高度可與另外兩版對齊。
"""

FIXED_SPEED_COLORBAR_AXES = (
    0.895,
    FIGURE_BOTTOM_FRACTION,
    0.025,
    FIGURE_TOP_FRACTION - FIGURE_BOTTOM_FRACTION,
)
"""固定流速版 colorbar 軸在整張畫布中的相對位置 `(left, bottom, width, height)`。

這裡使用獨立的 axes，而不是把 colorbar 綁定到主圖後讓 Matplotlib 自動縮圖；主圖
可維持與中性版相同的垂直資料高度，右側另保留色條與單位標籤所需的留白；色條的
上下界與主圖共用新的上/下側邊界，因此三個版本的視覺重心一致。座標是畫布比例值，
不是流速資料座標，也不會改變色階上下限。色條略向主圖側配置，讓單位標籤遠離畫布
右邊界，同時保留色條與主圖之間的可辨識間距。
"""

FIXED_SPEED_COLORBAR_LABELPAD = 8
"""固定流速版 colorbar 單位標籤與色條的間距，單位為 points。"""

CJK_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)
"""SERVER 常見的繁體中文字型路徑；找不到時仍允許中性版動畫正常輸出。"""

GIF_PALETTE_SIZE = 256
"""GIF 每部動畫共用的固定色盤大小；GIF 格式最多支援 256 個色盤項目。"""

GIF_VIRIDIS_PALETTE_SAMPLES = 224
"""固定色盤中保留給 viridis 流速色階的取樣數，讓 0–2 m/s 漸層仍保持平滑。"""

GIF_LAYOUT_PALETTE_COLORS = (
    MISSING_DATA_COLOR,
    LAND_COLOR,
    LAND_EDGE_COLOR,
    OCEAN_COLOR,
    QUIVER_COLOR,
    FIXED_SPEED_QUIVER_COLOR,
    BBOX_EDGE_COLOR,
    FIXED_SPEED_BBOX_EDGE_COLOR,
    "#000000",
    "#101010",
    "#202020",
    "#303030",
    "#404040",
    "#505050",
    "#606060",
    "#707070",
    "#808080",
    "#909090",
    "#a0a0a0",
    "#b0b0b0",
    "#c0c0c0",
    "#d0d0d0",
    "#e0e0e0",
    "#f0f0f0",
    "#f8f8f8",
    "#ffffff",
)
"""固定色盤中的版面保護色，確保陸地、岸線、區域框線、箭頭與文字不被流速色階取代。"""

GIF_DITHER_NONE = 0
"""Pillow 的無抖動量化選項；避免相鄰時間幀產生會被誤認為色階跳動的噪點。"""


@dataclass(frozen=True)
class RegionBox:
    """四個研究區域的報告圖 flow-domain bbox。

    bbox 順序為 `(lon_min, lon_max, lat_min, lat_max)`，與前處理 CLI 和既有
    1 km 報告 JSON 一致。框線只是視覺標示，不會改變流場資料或 mask。
    """

    id: str
    name: str
    bbox_lonlat: tuple[float, float, float, float]


REGION_BOXES = (
    # 動畫標籤採 ASCII 英文，因 SERVER 的既有 Matplotlib 字型不一定包含中文字形；
    # 區域中文正式名稱仍保留在本專案報告 JSON 與 README，避免 GIF 出現空方框。
    RegionBox("lienchiang_common", "Lienchiang common", (119.199120, 120.700880, 25.750844, 26.649156)),
    RegionBox("hsinchu", "Hsinchu", (119.708120, 121.191880, 24.300844, 25.199156)),
    RegionBox("northeast_taiwan_common", "Northeast Taiwan", (121.306315, 122.793685, 24.600844, 25.499156)),
    RegionBox("houwan_nmmba", "Houwan / NMMBA", (120.166710, 121.620000, 21.550844, 22.449156)),
)
"""直接取自 1 km 報告圖 metadata 的四個同物理尺度研究區域。"""


def load_surface_product(input_dir: Path) -> dict[str, Any]:
    """以 memory-map 讀取年度表層中間檔與追溯標記。

    大型 `u/v/speed` 陣列只讀取需要的時間幀與格點切片；`mmap_mode='r'` 可避免
    產生數 GB 的 RAM 複本。函式也驗證檔案 shape、時間數量與固定 1 km bbox，
    防止把不同解析度或不同年度產品誤送入此 renderer。
    """

    metadata_path = input_dir / "metadata.json"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"metadata.json not found: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    lon = np.load(input_dir / "lon.npy")
    lat = np.load(input_dir / "lat.npy")
    u = np.load(input_dir / "u_surface.npy", mmap_mode="r")
    v = np.load(input_dir / "v_surface.npy", mmap_mode="r")
    speed = np.load(input_dir / "speed_surface.npy", mmap_mode="r")
    mask = np.load(input_dir / "mask.npy")
    times = np.load(input_dir / "time_iso.npy")
    source_valid = np.load(input_dir / "source_valid.npy")
    imputed = np.load(input_dir / "imputed.npy")
    status = np.load(input_dir / "time_status.npy")
    expected_shape = (len(times), len(lat), len(lon))
    for name, array in (("u_surface", u), ("v_surface", v), ("speed_surface", speed)):
        if tuple(array.shape) != expected_shape:
            raise ValueError(f"{name} shape {array.shape} != expected {expected_shape}")
    if tuple(mask.shape) != (len(lat), len(lon)):
        raise ValueError(f"mask shape {mask.shape} does not match {(len(lat), len(lon))}")
    if not (len(source_valid) == len(times) == len(imputed) == len(status)):
        raise ValueError("time/status arrays have inconsistent lengths")
    domain = metadata.get("domain", {})
    if (float(domain.get("lon_min", 0.0)), float(domain.get("lon_max", 0.0)), float(domain.get("lat_min", 0.0)), float(domain.get("lat_max", 0.0))) != (119.0, 123.0, 20.0, 27.0):
        raise ValueError("input product is not the required 119–123E / 20–27N full Taiwan surrounding domain")
    if tuple(u.shape[1:]) != (780, 409):
        raise ValueError(f"input product is not the required 780x409 1 km grid: {u.shape[1:]}")
    return {
        "metadata": metadata,
        "lon": lon.astype(np.float32, copy=False),
        "lat": lat.astype(np.float32, copy=False),
        "u": u,
        "v": v,
        "speed": speed,
        "mask": mask.astype(bool, copy=False),
        "times": times,
        "source_valid": source_valid.astype(bool, copy=False),
        "imputed": imputed.astype(bool, copy=False),
        "status": status,
    }


def choose_quiver_step(lon_count: int, lat_count: int, target_arrows: int) -> tuple[int, int]:
    """依報告圖目標箭頭數估算規則抽樣步距。"""

    total = max(lon_count * lat_count, 1)
    step = max(1, int(np.sqrt(total / max(target_arrows, 1))))
    return step, step


def draw_land_overlay(ax: plt.Axes, lon: np.ndarray, lat: np.ndarray, ocean_mask: np.ndarray) -> None:
    """在背景與箭頭下方建立陸地/缺值視覺層。

    `ocean_mask=False` 同時可能代表陸地、原始 mesh 外或 GeoJSON 遮罩區；在年度
    展示圖中統一畫成陸地色，並以有限流速資料決定箭頭是否出現。此做法避免把
    無資料位置誤讀為低速海域。
    """

    land = np.ma.masked_where(ocean_mask, np.ones_like(ocean_mask, dtype=np.float32))
    land_cmap = mcolors.ListedColormap([LAND_COLOR])
    ax.pcolormesh(lon, lat, land, shading="auto", cmap=land_cmap, vmin=0, vmax=1, zorder=5)
    if np.any(ocean_mask) and np.any(~ocean_mask):
        ax.contour(
            lon,
            lat,
            ocean_mask.astype(np.float32),
            levels=[0.5],
            colors=LAND_EDGE_COLOR,
            linewidths=0.45,
            zorder=6,
        )


def draw_region_boxes(ax: plt.Axes, *, show_labels: bool, edge_color: str) -> None:
    """繪製四個研究區域框，並在趨勢動畫中維持固定位置。

    框線不參與資料計算，只是讓簡報觀眾能在整張台灣周邊圖上辨識四個研究域。
    所有區域都取消內部填色，避免遮蔽固定流速背景、降低局部流向辨識度，或讓
    觀眾誤以為框內是一個不同的物理分類。`edge_color` 由背景模式決定：中性趨勢
    版使用既有深藍色，固定流速版使用低飽和磚紅色；兩者都只畫外框。

    Args:
        ax: 已建立且使用經緯度座標的 Matplotlib 軸。
        show_labels: 是否顯示診斷用 ASCII 區域名稱；正式動畫預設為 False。
        edge_color: 本次背景模式的區域框線顏色。
    """

    for region in REGION_BOXES:
        lon_min, lon_max, lat_min, lat_max = region.bbox_lonlat
        ax.add_patch(
            mpatches.Rectangle(
                (lon_min, lat_min),
                lon_max - lon_min,
                lat_max - lat_min,
                # 框內刻意保持透明，讓背景速度與箭頭在四個研究區域內仍可直接觀察；
                # `none` 不會覆蓋 pcolormesh，也不會引入任何額外的資料色彩。
                facecolor="none",
                edgecolor=edge_color,
                linewidth=1.2,
                zorder=8,
            )
        )
        if show_labels:
            ax.text(
                lon_min + 0.025,
                lat_max - 0.025,
                region.name,
                fontsize=6.5,
                color=edge_color,
                ha="left",
                va="top",
                bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.70, "pad": 1.2},
                zorder=9,
            )


def find_cjk_font() -> Any | None:
    """尋找可繪製繁體中文色條與 XY 軸標籤的字型。

    SERVER 的預設 Matplotlib 字型通常不含中文字形，直接繪製「流速(公尺/秒)」、
    「經度」或「緯度」可能變成空方框。這裡為中文 colorbar 與 XY 軸標籤尋找既有
    系統字型，不下載或安裝新字型；若環境沒有候選字型，回傳 None，renderer 仍會
    保留中文字串並完成輸出，方便在有相容字型的環境重新渲染。
    """

    from matplotlib.font_manager import FontProperties

    for candidate in CJK_FONT_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return FontProperties(fname=str(path))
    return None


def build_fixed_gif_palette() -> Any:
    """建立所有時間幀共用的 GIF 固定色盤。

    GIF 每幀最多只能使用 256 個顏色；若讓 Pillow 對每幀各自執行 adaptive
    quantization，雖然 Matplotlib 的 `Normalize(vmin=0, vmax=2)` 沒有改變，
    播放器仍可能因 local palette 不同而使 colorbar 看起來跳動。因此這裡預先
    建立一個與資料無關的固定色盤：224 個等距 viridis 顏色負責 0–2 m/s 流速
    漸層，其餘色盤項目保護海陸、岸線、研究區域框線、白色/深藍箭頭與灰階文字。

    回傳的 1×1 `P` 模式影像只承載 palette，不承載任何動畫畫面；後續每一幀
    都以同一個 palette 量化，故色階的物理上下限與 GIF 編碼顏色不再混在一起。
    """

    from PIL import Image

    viridis_values = np.linspace(0.0, 1.0, GIF_VIRIDIS_PALETTE_SAMPLES)
    viridis_rgb = np.rint(plt.get_cmap("viridis")(viridis_values)[:, :3] * 255.0).astype(np.uint8)
    layout_rgb = np.asarray(
        [np.rint(np.asarray(mcolors.to_rgb(color)) * 255.0) for color in GIF_LAYOUT_PALETTE_COLORS],
        dtype=np.uint8,
    )
    palette_rgb = np.vstack((viridis_rgb, layout_rgb))
    if palette_rgb.shape[0] < GIF_PALETTE_SIZE:
        # 由於某些版面色可能與 viridis 取樣重複，仍補足 GIF 要求的 256 個
        # 三通道項目；重複項不改變任何顏色，只是確保 palette bytes 長度固定。
        padding = np.repeat(palette_rgb[-1:, :], GIF_PALETTE_SIZE - palette_rgb.shape[0], axis=0)
        palette_rgb = np.vstack((palette_rgb, padding))
    palette_rgb = palette_rgb[:GIF_PALETTE_SIZE]
    palette_image = Image.new("P", (1, 1), color=0)
    palette_image.putpalette(palette_rgb.reshape(-1).tolist())
    return palette_image


class FixedPaletteGifWriter(_ImageIOGifWriter):
    """以同一個固定色盤串流寫入多幀 GIF。

    imageio 的 `GifWriter` 原本在 `converToPIL()`（套件內既有拼字）中對每幀
    重新產生 palette；這個子類別只替換量化步驟，保留 imageio 原有的 GIF header、
    frame duration、loop 與串流寫入邏輯。輸入影像預期為 renderer 產生的
    `height×width×3` uint8 RGB 陣列；若收到 RGBA，會丟棄透明通道，因為本產品
    的背景與遮罩已在 Matplotlib 畫布中完成合成。
    """

    def __init__(self, file: Any, *, fixed_palette: Any, **kwargs: Any) -> None:
        super().__init__(file, **kwargs)
        self._fixed_palette = fixed_palette

    def converToPIL(self, im: np.ndarray, quantizer: Any, palette_size: int = 256) -> Any:
        """將單幀 RGB 影像映射到固定色盤，而非重新學習單幀色盤。"""

        from PIL import Image

        frame = np.asarray(im)
        if frame.ndim != 3 or frame.shape[-1] not in (3, 4):
            raise ValueError(f"GIF frame must be HxWx3 or HxWx4 RGB(A), got {frame.shape}")
        if frame.shape[-1] == 4:
            frame = frame[:, :, :3]
        frame = np.clip(frame, 0, 255).astype(np.uint8, copy=False)
        rgb_image = Image.fromarray(frame, mode="RGB")
        dither_none = getattr(getattr(Image, "Dither", Image), "NONE", GIF_DITHER_NONE)
        return rgb_image.quantize(palette=self._fixed_palette, dither=dither_none)


def build_scene(
    data: dict[str, Any],
    *,
    background_mode: str,
    fixed_vmax: float,
    quiver_vmax: float,
    target_arrows: int,
    quiver_scale_multiplier: float,
    show_region_labels: bool,
    dpi: int,
) -> dict[str, Any]:
    """建立可重複更新的 Matplotlib 圖層。

    GIF 需要數百到數千個時間幀；固定 figure、pcolormesh 與 quiver artist 後只更新
    數值，可以顯著降低每幀建立/銷毀 figure 的成本。`background_mode='neutral'`
    不讓色彩承載速度，`fixed_speed` 則使用所有動畫共用的固定 m/s 色階。固定版
    以獨立 colorbar axes 和較寬畫布保存主圖高度，避免 colorbar 自動壓縮等比例
    經緯度資料框；色條標籤的留白也由固定畫布空間保證。三個版本的 XY 軸標籤統一
    使用「經度」與「緯度」，刻度本身仍是數值座標。
    """

    lon = data["lon"]
    lat = data["lat"]
    ocean_mask = data["mask"]
    # memory-map 以 `mmap_mode='r'` 開啟，直接對其套 mask 會觸發 read-only assignment；
    # 這裡只複製第一個 780×409 影格，後續年度影格仍由 update_scene 逐幀取用，
    # 不會把數 GB 的年度陣列載入記憶體。
    speed = np.asarray(data["speed"][0], dtype=np.float32).copy()
    u = np.asarray(data["u"][0], dtype=np.float32).copy()
    v = np.asarray(data["v"][0], dtype=np.float32).copy()
    speed[~ocean_mask] = np.nan
    u[~ocean_mask] = np.nan
    v[~ocean_mask] = np.nan

    # 使用與 1 km 報告圖相近的縱向畫布比例；dpi 可調低以控制年度完整 GIF 檔案量。
    # 固定流速版另外增加右側畫布寬度，讓 colorbar 與繁體中文單位標籤有獨立空間；
    # 主圖的可用高度維持與中性版一致，配合 equal aspect 後就不會因色條而縮短上下
    # 資料框。這是版面配置調整，不是對經緯度、速度或網格做任何重新取樣。
    figure_size_inches = (
        FIXED_SPEED_FIGURE_SIZE_INCHES
        if background_mode == "fixed_speed"
        else NORMAL_FIGURE_SIZE_INCHES
    )
    fig, ax = plt.subplots(figsize=figure_size_inches, dpi=dpi)
    fig.subplots_adjust(
        left=FIGURE_LEFT_FRACTION,
        # 固定版主圖預留右側獨立 colorbar 軸；中性版維持原本報告圖留白。
        right=(
            FIXED_SPEED_MAIN_RIGHT_FRACTION
            if background_mode == "fixed_speed"
            else 0.965
        ),
        bottom=FIGURE_BOTTOM_FRACTION,
        top=FIGURE_TOP_FRACTION,
    )
    ax.set_facecolor(MISSING_DATA_COLOR)
    # 色條與座標軸都需要中文；在建立任何文字 artist 前先選定同一個既有系統字型，
    # 讓三個版本的中文顯示一致，也避免固定版與中性版各自採用不同字型造成字寬差異。
    cjk_font = find_cjk_font()
    if background_mode == "fixed_speed":
        cmap = plt.get_cmap("viridis").copy()
        cmap.set_bad(MISSING_DATA_COLOR)
        background = np.ma.masked_where(~np.isfinite(speed), speed)
        mesh = ax.pcolormesh(
            lon,
            lat,
            background,
            shading="auto",
            cmap=cmap,
            vmin=DEFAULT_FIXED_SPEED_VMIN,
            vmax=fixed_vmax,
            zorder=1,
        )
        # 不使用 `fig.colorbar(mesh, ax=ax)`，因為該寫法會自動從主圖扣除寬度；
        # 以固定相對座標建立獨立 cax，讓主圖可在等比例座標下維持完整高度。
        colorbar_axis = fig.add_axes(FIXED_SPEED_COLORBAR_AXES)
        colorbar = fig.colorbar(mesh, cax=colorbar_axis)
        # 色條上下限與刻度由產品規格固定，不依當次輸入資料的 min/max、percentile
        # 或單幀極值自動改變；因此不同年度或不同缺日補值狀況仍可直接比較顏色。
        colorbar.set_ticks(DEFAULT_FIXED_SPEED_TICKS)
        from matplotlib.ticker import FormatStrFormatter

        colorbar.ax.yaxis.set_major_formatter(FormatStrFormatter("%.1f"))
        if cjk_font is not None:
            colorbar.set_label(
                FIXED_SPEED_COLORBAR_LABEL,
                fontproperties=cjk_font,
                labelpad=FIXED_SPEED_COLORBAR_LABELPAD,
            )
        else:
            # 缺少 CJK 字型時仍保留文字語意；正式 SERVER 環境預期會使用 Noto CJK。
            colorbar.set_label(FIXED_SPEED_COLORBAR_LABEL, labelpad=FIXED_SPEED_COLORBAR_LABELPAD)
        colorbar.ax.tick_params(labelsize=7, pad=2)
        colorbar.outline.set_linewidth(0.6)
    elif background_mode == "neutral":
        ocean_layer = np.ma.masked_where(~np.isfinite(speed), np.ones_like(speed, dtype=np.float32))
        ocean_cmap = mcolors.ListedColormap([OCEAN_COLOR])
        mesh = ax.pcolormesh(lon, lat, ocean_layer, shading="auto", cmap=ocean_cmap, vmin=0, vmax=1, zorder=1)
        colorbar = None
    else:
        raise ValueError(f"unsupported background mode: {background_mode}")

    draw_land_overlay(ax, lon, lat, ocean_mask)
    # 中性版保留報告圖深藍框線；固定流速版改用低飽和磚紅，只畫外框且不填色，
    # 以兼顧研究區域定位、白色箭頭辨識與 viridis 背景的視覺分離。這是版面規格，
    # 不會改變任何流速、mask 或研究區域 bbox 的數值。
    region_box_color = (
        FIXED_SPEED_BBOX_EDGE_COLOR if background_mode == "fixed_speed" else BBOX_EDGE_COLOR
    )
    draw_region_boxes(ax, show_labels=show_region_labels, edge_color=region_box_color)
    sy, sx = choose_quiver_step(len(lon), len(lat), target_arrows)
    valid_vector = np.isfinite(speed) & np.isfinite(u) & np.isfinite(v)
    sampled_valid = valid_vector[::sy, ::sx]
    sampled_u = np.ma.masked_where(~sampled_valid, u[::sy, ::sx])
    sampled_v = np.ma.masked_where(~sampled_valid, v[::sy, ::sx])
    # 固定流速底圖同時包含深色與高亮色區域；改用白色箭頭，讓流向在強流區與
    # 低流速區都能被辨識。中性底圖沒有速度色彩，因此使用較明亮的深藍青箭頭，
    # 既保留報告圖色系，也和灰米色陸地及深灰海岸線拉開距離；這只改變視覺呈現，
    # 不會改變 u/v 的物理量或比例尺。
    quiver_color = FIXED_SPEED_QUIVER_COLOR if background_mode == "fixed_speed" else QUIVER_COLOR
    quiver = ax.quiver(
        lon[::sx],
        lat[::sy],
        sampled_u,
        sampled_v,
        color=quiver_color,
        # 報告圖的箭頭 scale 以有效流速 P98 為基準，而不是固定 colorbar 上限；
        # 這讓 neutral 動畫與 fixed-speed 備查動畫都能沿用相同的 1 km 箭頭長度。
        scale=max(quiver_scale_multiplier * max(quiver_vmax, 0.1), 0.1),
        width=0.00155,
        headwidth=2.8,
        headlength=3.5,
        # 密度提高後仍保留略高於舊版的透明度，避免白色箭頭在 viridis 的亮色
        # 區域中變得過淡；透明度只影響視覺呈現，不會改變資料值。
        alpha=0.90,
        zorder=10,
    )
    # 中文軸標籤只描述座標軸的物理意義，刻度仍保留數值經緯度，方便教授與研究人員
    # 直接讀取 119–123°E、20–27°N 的地理位置。與 colorbar 共用同一既有 CJK 字型，
    # 可避免 SERVER 預設字型缺字；若未找到候選字型仍不改寫資料或座標，只退回
    # Matplotlib 預設字型處理，讓程式在不同環境保持可執行。
    if cjk_font is not None:
        ax.set_xlabel(X_AXIS_LABEL, fontsize=8, fontproperties=cjk_font)
        ax.set_ylabel(Y_AXIS_LABEL, fontsize=8, fontproperties=cjk_font)
    else:
        ax.set_xlabel(X_AXIS_LABEL, fontsize=8)
        ax.set_ylabel(Y_AXIS_LABEL, fontsize=8)
    ax.tick_params(labelsize=7)
    ax.set_xlim(float(lon[0]), float(lon[-1]))
    ax.set_ylim(float(lat[0]), float(lat[-1]))
    ax.set_aspect("equal", adjustable="box")
    return {
        "fig": fig,
        "ax": ax,
        "mesh": mesh,
        "quiver": quiver,
        "sy": sy,
        "sx": sx,
        "background_mode": background_mode,
        "quiver_color": quiver_color,
        "region_box_color": region_box_color,
        "region_box_fill": False,
        "figure_size_inches": tuple(float(value) for value in figure_size_inches),
        "fixed_vmin": DEFAULT_FIXED_SPEED_VMIN,
        "fixed_vmax": fixed_vmax,
    }


def update_scene(scene: dict[str, Any], data: dict[str, Any], frame_index: int) -> np.ndarray:
    """更新單一時間幀並回傳 RGB 影像。

    動畫畫面不顯示標題、時間、補值狀態或區域名稱，以維持 1 km 報告圖的乾淨
    版面；缺日補值狀態仍完整保存在 `time_status.npy`、`source_valid.npy` 與
    `imputed.npy`，不會因為隱藏文字而失去追溯能力。
    """

    speed = np.asarray(data["speed"][frame_index], dtype=np.float32).copy()
    u = np.asarray(data["u"][frame_index], dtype=np.float32).copy()
    v = np.asarray(data["v"][frame_index], dtype=np.float32).copy()
    ocean_mask = data["mask"]
    speed[~ocean_mask] = np.nan
    u[~ocean_mask] = np.nan
    v[~ocean_mask] = np.nan
    sy = scene["sy"]
    sx = scene["sx"]
    valid_vector = np.isfinite(speed) & np.isfinite(u) & np.isfinite(v)
    sampled_valid = valid_vector[::sy, ::sx]
    scene["quiver"].set_UVC(
        np.ma.masked_where(~sampled_valid, u[::sy, ::sx]),
        np.ma.masked_where(~sampled_valid, v[::sy, ::sx]),
    )
    if scene["background_mode"] == "fixed_speed":
        background = np.ma.masked_where(~np.isfinite(speed), speed)
        scene["mesh"].set_array(background.ravel())
    scene["fig"].canvas.draw()
    width, height = scene["fig"].canvas.get_width_height()
    rgba = np.asarray(scene["fig"].canvas.buffer_rgba(), dtype=np.uint8)
    if rgba.shape[:2] != (height, width):
        raise RuntimeError(f"unexpected canvas shape {rgba.shape} for {(height, width)}")
    return rgba[:, :, :3].copy()


def write_animation(
    data: dict[str, Any],
    output_path: Path,
    frame_indices: np.ndarray,
    *,
    fps: int,
    background_mode: str,
    fixed_vmax: float,
    quiver_vmax: float,
    target_arrows: int,
    quiver_scale_multiplier: float,
    show_region_labels: bool,
    dpi: int,
    first_frame_png: Path | None = None,
) -> dict[str, Any]:
    """以固定色盤串流寫出一個 GIF，避免色階跳動與記憶體暴增。

    `frame_indices` 可代表每 2 日一幀的簡報趨勢，或完整 6 小時軸。GIF 每幀由
    固定 figure 轉成 RGB 後立即交給固定色盤 writer，輸出完成後不會留下大量逐幀
    PNG。固定色盤對整部 GIF 共用，因此 fixed-speed colorbar 的顏色不會因每幀
    adaptive palette 不同而跳動；這只改變 8-bit GIF 的編碼方式，不改變原始
    `speed_surface.npy` 或 Matplotlib 的 0–2 m/s 色階映射。
    """

    if frame_indices.size == 0:
        raise ValueError("cannot write animation with zero frame indices")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    scene = build_scene(
        data,
        background_mode=background_mode,
        fixed_vmax=fixed_vmax,
        quiver_vmax=quiver_vmax,
        target_arrows=target_arrows,
        quiver_scale_multiplier=quiver_scale_multiplier,
        show_region_labels=show_region_labels,
        dpi=dpi,
    )
    first_frame_png = Path(first_frame_png) if first_frame_png is not None else None
    fixed_palette = build_fixed_gif_palette()
    # 先繪製第一幀，再用同一個固定 palette 建立串流 writer；不把後續 2924 幀
    # 暫存於記憶體，並以 dispose=2 讓每幀完整畫布都使用同一個 global palette。
    first_image = update_scene(scene, data, int(frame_indices[0]))
    with output_path.open("wb") as output_file:
        writer = FixedPaletteGifWriter(
            output_file,
            fixed_palette=fixed_palette,
            opt_subrectangle=False,
            opt_loop=0,
            opt_quantizer=0,
            opt_palette_size=GIF_PALETTE_SIZE,
        )
        try:
            writer.add_image(first_image, 1.0 / max(fps, 1), 2)
            if first_frame_png is not None:
                first_frame_png.parent.mkdir(parents=True, exist_ok=True)
                scene["fig"].savefig(first_frame_png, dpi=scene["fig"].dpi)
            print(f"rendered 1/{frame_indices.size}: {output_path.name}", flush=True)
            for ordinal, frame_index in enumerate(frame_indices[1:], start=1):
                image = update_scene(scene, data, int(frame_index))
                writer.add_image(image, 1.0 / max(fps, 1), 2)
                if (ordinal + 1) % 25 == 0 or ordinal + 1 == frame_indices.size:
                    print(f"rendered {ordinal + 1}/{frame_indices.size}: {output_path.name}", flush=True)
        finally:
            writer.close()
    plt.close(scene["fig"])
    duration_seconds = float(frame_indices.size) / max(fps, 1)
    return {
        "path": str(output_path),
        "frame_count": int(frame_indices.size),
        "fps": int(fps),
        "duration_seconds": duration_seconds,
        "background_mode": background_mode,
        "fixed_colorbar_vmin_m_per_s": (
            float(DEFAULT_FIXED_SPEED_VMIN) if background_mode == "fixed_speed" else None
        ),
        "fixed_colorbar_vmax_m_per_s": float(fixed_vmax) if background_mode == "fixed_speed" else None,
        "fixed_colorbar_ticks_m_per_s": (
            list(DEFAULT_FIXED_SPEED_TICKS) if background_mode == "fixed_speed" else None
        ),
        "fixed_colorbar_label": FIXED_SPEED_COLORBAR_LABEL if background_mode == "fixed_speed" else None,
        "gif_palette": {
            "mode": "single_global_palette",
            "size": GIF_PALETTE_SIZE,
            "dither": "none",
        },
        "quiver_color": scene["quiver_color"],
        "region_box_color": scene["region_box_color"],
        "region_box_fill": bool(scene["region_box_fill"]),
        "figure_size_inches": list(scene["figure_size_inches"]),
        "target_arrows": int(target_arrows),
        "quiver_step_yx": list(choose_quiver_step(len(data["lon"]), len(data["lat"]), target_arrows)),
        "quiver_scale_multiplier": float(quiver_scale_multiplier),
        "first_frame_index": int(frame_indices[0]),
        "last_frame_index": int(frame_indices[-1]),
    }


def parse_args() -> argparse.Namespace:
    """解析年度動畫 CLI 參數。"""

    parser = argparse.ArgumentParser(description="Render annual 1 km OCM surface GIFs from NumPy arrays.")
    parser.add_argument("--input-dir", type=Path, required=True, help="Directory produced by preprocess_ocm_surface_year.py.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory for GIFs and animation manifest.")
    parser.add_argument("--fps", type=int, default=4, help="GIF playback frames per second.")
    parser.add_argument("--trend-frame-stride", type=int, default=8, help="Input-frame stride for the short trend animation; 8 means one frame per 48 hours.")
    parser.add_argument("--target-arrows", type=int, default=DEFAULT_TARGET_ARROWS, help="Approximate arrows per frame; default 2600 improves local flow visibility while retaining a presentation-safe density.")
    parser.add_argument("--quiver-scale-multiplier", type=float, default=DEFAULT_QUIVER_SCALE_MULTIPLIER, help="Arrow shortening multiplier; smaller values make arrows longer.")
    parser.add_argument("--dpi", type=int, default=110, help="Raster output DPI; lower values reduce annual GIF size.")
    parser.add_argument("--show-region-labels", action="store_true", help="Optional diagnostic mode; default animation output keeps the report-style no-text region boxes.")
    parser.add_argument("--skip-annual-full", action="store_true", help="Only render the short trend and fixed-speed backup GIFs.")
    return parser.parse_args()


def main() -> None:
    """讀取年度中間檔並產生三種用途的動畫產品。"""

    args = parse_args()
    if args.fps <= 0 or args.trend_frame_stride <= 0 or args.target_arrows <= 0 or args.dpi <= 0:
        raise ValueError("fps, trend-frame-stride, target-arrows and dpi must be positive")
    data = load_surface_product(args.input_dir)
    metadata = data["metadata"]
    # 固定流速版的色階是視覺化產品規格，不從 metadata 的資料統計欄位讀取；metadata
    # 中仍可保留 p98/p99 作為箭頭比例與品質查核依據，但不能讓它改變色條上下限。
    fixed_vmax = DEFAULT_FIXED_SPEED_VMAX
    quiver_vmax = float(metadata["speed_statistics_observed_sample"]["p98_m_per_s"])
    total_frames = len(data["times"])
    trend_indices = np.arange(0, total_frames, args.trend_frame_stride, dtype=np.int64)
    full_indices = np.arange(0, total_frames, 1, dtype=np.int64)
    # 報告圖版面不含區域名稱；只有明確指定診斷旗標時才加入 ASCII 區域標籤。
    labels = bool(args.show_region_labels)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "input_dir": str(args.input_dir),
        "source_metadata": "metadata.json",
        "render_policy": {
            "fps": int(args.fps),
            "trend_frame_stride": int(args.trend_frame_stride),
            "trend_time_interval_hours": int(args.trend_frame_stride * metadata["time_axis"]["time_step_hours"]),
            "target_arrows": int(args.target_arrows),
            "quiver_scale_multiplier": float(args.quiver_scale_multiplier),
            "quiver_colors": {
                "neutral": QUIVER_COLOR,
                "fixed_speed": FIXED_SPEED_QUIVER_COLOR,
            },
            "region_box_colors": {
                "neutral": BBOX_EDGE_COLOR,
                "fixed_speed": FIXED_SPEED_BBOX_EDGE_COLOR,
            },
            "region_box_fill": False,
            "axis_labels": {
                "x": X_AXIS_LABEL,
                "y": Y_AXIS_LABEL,
            },
            "figure_sizes_inches": {
                "neutral": list(NORMAL_FIGURE_SIZE_INCHES),
                "fixed_speed": list(FIXED_SPEED_FIGURE_SIZE_INCHES),
            },
            "figure_top_fraction": FIGURE_TOP_FRACTION,
            "figure_bottom_fraction": FIGURE_BOTTOM_FRACTION,
            "fixed_speed_main_right_fraction": FIXED_SPEED_MAIN_RIGHT_FRACTION,
            "fixed_speed_colorbar_axes": list(FIXED_SPEED_COLORBAR_AXES),
            "fixed_speed_colorbar_labelpad_points": FIXED_SPEED_COLORBAR_LABELPAD,
            "dpi": int(args.dpi),
            "region_labels": bool(labels),
        },
        "fixed_speed_colorbar": {
            "vmin_m_per_s": DEFAULT_FIXED_SPEED_VMIN,
            "vmax_m_per_s": fixed_vmax,
            "ticks_m_per_s": list(DEFAULT_FIXED_SPEED_TICKS),
            "label": FIXED_SPEED_COLORBAR_LABEL,
            "data_derived_limits": False,
        },
        "quiver_reference_p98_m_per_s": quiver_vmax,
        "products": [],
    }

    trend_path = args.output_dir / "global_trend_surface_layer_047_four_regions.gif"
    manifest["products"].append(
        write_animation(
            data,
            trend_path,
            trend_indices,
            fps=args.fps,
            background_mode="neutral",
            fixed_vmax=fixed_vmax,
            quiver_vmax=quiver_vmax,
            target_arrows=args.target_arrows,
            quiver_scale_multiplier=args.quiver_scale_multiplier,
            show_region_labels=labels,
            dpi=args.dpi,
        )
    )

    fixed_path = args.output_dir / "surface_layer_047_speed_fixed_scale_four_regions.gif"
    manifest["products"].append(
        write_animation(
            data,
            fixed_path,
            trend_indices,
            fps=args.fps,
            background_mode="fixed_speed",
            fixed_vmax=fixed_vmax,
            quiver_vmax=quiver_vmax,
            target_arrows=args.target_arrows,
            quiver_scale_multiplier=args.quiver_scale_multiplier,
            show_region_labels=labels,
            dpi=args.dpi,
            first_frame_png=args.output_dir / "surface_layer_047_speed_fixed_scale_first_frame.png",
        )
    )

    if not args.skip_annual_full:
        annual_path = args.output_dir / "annual_full_surface_layer_047_6h.gif"
        manifest["products"].append(
            write_animation(
                data,
                annual_path,
                full_indices,
                fps=args.fps,
                background_mode="neutral",
                fixed_vmax=fixed_vmax,
                quiver_vmax=quiver_vmax,
                target_arrows=args.target_arrows,
                quiver_scale_multiplier=args.quiver_scale_multiplier,
                show_region_labels=labels,
                dpi=args.dpi,
            )
        )
    (args.output_dir / "animation_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "RENDER_COMPLETE").write_text(
        "This marker means the requested animation products were rendered.\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
