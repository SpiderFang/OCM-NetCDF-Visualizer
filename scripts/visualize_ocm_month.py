"""從月資料中間檔產生 OCM 流場動畫與三維示意圖。

本腳本讀取 preprocess_ocm_month.py 輸出的規則格點陣列。二維動畫可採用中性
海域底圖，或使用 η/elev 自由水面高度作為底圖色階，再加 quiver 箭頭以箭頭長度
直接表示水平流速大小；三維靜態示意圖採用月平均 zcor 作為垂向位置；若
前處理另外輸出 `zcor.npy`，三維時間動畫會使用逐時 zcor 呈現水位與層位變動，目的是
先期觀察台灣周遭流場與可能的分割區域，而不是取代 ParaView/pyParaOcean
等高階互動式三維分析工具。

說明:
- 讀取由 `preprocess_ocm_month.py` 所輸出的 NumPy 中間檔 (npy + JSON)
- 產生二維底圖並繪製箭頭 (quiver)：底圖可選中性海域色或 η/elev 水位色階，
    箭頭方向表示流向、箭頭長度表示流速大小；或輸出一張三維示意圖，使用月平均 `zcor_mean`
    作為垂向位置來表示層化結構。

註記:
- 此腳本的目的在於快速視覺化與資料檢查，而非替代互動式三維分析軟體。
- 程式註解會說明資料維度、遮罩語意與縮放假設，避免把缺值、陸地或箭頭長度
    誤讀為其他物理量。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import imageio.v2 as imageio
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# 缺值格點顏色：代表該 layer 在該水平位置沒有有效海水資料，常見原因包含
# 目標層位落在局部海底以下、來源非結構網格無法支援該點，或插值結果為 NaN。
# 使用淡灰色而不是預設白色，是為了讓「無資料」與圖面背景清楚分離。
MISSING_DATA_COLOR = "#d9d9d9"
# 陸地格點顏色：代表 `mask.npy` 明確標示為非海域的位置。它和 layer 缺值使用
# 不同色調，讓圖面可同時區分「地理上的陸地」與「該垂向層沒有水體資料」。
LAND_COLOR = "#f1ead8"
# 有效海域底色：二維動畫不再以色階呈現速度大小，因此需要固定且低干擾的
# 海域底色。流速強弱改由箭頭長度表達，避免同時用底圖色彩和向量長度代表同一物理量。
OCEAN_COLOR = "#e7f2f3"
# 二維箭頭顏色：在中性海域、淡灰缺值與淺色陸地上都要保持足夠對比。
QUIVER_COLOR = "#123b5d"
# 2D 底圖模式：neutral 保留舊版固定海色，elev 使用原始 `elev` 插值後的
# η（自由水面高度），elev_anomaly 則先扣除每個格點月平均水位，用於凸顯潮汐
# 或短期水位振盪。這些模式只改變底圖標量，不改變 quiver 的流速來源。
BACKGROUND_NEUTRAL = "neutral"
BACKGROUND_ELEV = "elev"
BACKGROUND_ELEV_ANOMALY = "elev_anomaly"


def load_month(input_dir: Path) -> dict[str, np.ndarray | dict]:
    """讀取月資料中間檔。

    所有主要流場陣列均使用 `time, layer, lat, lon` 排列。metadata 由 JSON
    保留處理參數與流速統計，繪圖時用於標題、箭頭縮放基準與成果追蹤。
    """

    # 以字典回傳各種陣列與 metadata。部分較大的陣列以 `mmap_mode="r"`
    # 方式讀取以節省記憶體：只在需要時載入資料頁面，而不是一次全部複製到 RAM。
    data: dict[str, np.ndarray | dict] = {
        # 經度、緯度：一維座標陣列
        "lon": np.load(input_dir / "lon.npy"),
        "lat": np.load(input_dir / "lat.npy"),
        # 時間字串（ISO 格式）或其他可呈現的時間標記
        "time_iso": np.load(input_dir / "time_iso.npy"),
        # u, v: 四維陣列 arranged as (time, layer, lat, lon)
        # 使用記憶體對映讀取以處理大型資料集
        "u": np.load(input_dir / "u.npy", mmap_mode="r"),
        "v": np.load(input_dir / "v.npy", mmap_mode="r"),
        # speed 同樣為 (time, layer, lat, lon) 或可能為 (time, layer, lat, lon) 的 magnitude
        "speed": np.load(input_dir / "speed.npy", mmap_mode="r"),
        # 月平均的 zcor（垂向座標，通常以層為索引）：用於 3D 靜態示意圖中的 z 值
        "zcor_mean": np.load(input_dir / "zcor_mean.npy"),
        # bathymetry: 海底深度（正值或負值視資料定義而定）
        "bathymetry": np.load(input_dir / "bathymetry.npy"),
        # mask: 用來遮蔽陸地或不存在資料的布林 / 整數遮罩
        "mask": np.load(input_dir / "mask.npy"),
    }
    summary_path = input_dir / "monthly_summary.json"
    if summary_path.exists():
        data["summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
    else:
        data["summary"] = {}
    zcor_path = input_dir / "zcor.npy"
    if zcor_path.exists():
        # zcor.npy 是選擇性的大型逐時垂向座標檔，形狀為 (time, layer, lat, lon)。
        # 使用 mmap 讀取，讓 3D 時間動畫逐幀取用，不必一次把整個月的 zcor 載入記憶體。
        data["zcor"] = np.load(zcor_path, mmap_mode="r")
    elev_path = input_dir / "elev.npy"
    if elev_path.exists():
        # elev.npy 是由原始 NetCDF `elev(time, node)` 插值而來的 η（自由水面高度），
        # 形狀為 (time, lat, lon)。它沒有 layer 維度，代表同一時間的水面標量場；
        # 視覺化時只能作為底圖色階，箭頭仍需來自 surface hvel 或指定 layer hvel。
        data["elev"] = np.load(elev_path, mmap_mode="r")
    return data


def resolve_layer_index(layer_index: int, layer_count: int) -> int:
    """支援 Python 負索引的垂向層選擇。

    OCM/SCHISM 垂向層常由底層到表層排列，因此 `-1` 通常代表表層。實際
    是否如此仍應用 zcor_mean 或 sigma 檢查；這裡只負責轉成合法陣列索引。
    """

    # 支援像 Python 那樣使用負數索引（-1 表示最後一層），並驗證範圍。
    resolved = layer_index if layer_index >= 0 else layer_count + layer_index
    # 若傳入超出範圍的索引，主動拋出錯誤以避免 silent failure。
    if resolved < 0 or resolved >= layer_count:
        raise IndexError(f"layer_index {layer_index} outside layer count {layer_count}")
    return resolved


def choose_quiver_step(lon_count: int, lat_count: int, target_arrows: int) -> tuple[int, int]:
    """依格點大小選擇箭頭抽樣間距。

    2D 動畫改以箭頭長度表示流速大小後，箭頭本身就是主要資訊而不是色階的輔助。
    這裡仍以目標箭頭數估算 x/y 間距，但預設會比早期版本更密，讓較小研究區
    或低解析度輸出能呈現更多有效格點；若資料太密，使用者仍可調低 target_arrows。
    """

    # 根據格點總數與目標箭頭數量估算採樣步長。採用 sqrt(total/target) 的理由是：
    # 當我們在 2D 網格上以均勻間距抽樣時，橫向與縱向的間距約為 sqrt(total/target).
    total = max(lon_count * lat_count, 1)
    step = max(1, int(np.sqrt(total / max(target_arrows, 1))))
    # 回傳 (y_step, x_step)；目前採用方形抽樣間距以維持視覺平均性
    return step, step


def normalize_ocean_mask(mask: np.ndarray, expected_shape: tuple[int, int]) -> np.ndarray:
    """將讀入的海域遮罩整理成 `(lat, lon)` 布林陣列。

    `mask.npy` 由前處理輸出，True 代表該水平格點位於原始水體 mesh 或可用海域。
    舊版本資料可能使用整數或浮點儲存，因此這裡會轉為 bool 並檢查 shape；若
    shape 不符，直接報錯比安靜地畫錯海陸邊界更安全。
    """

    normalized = np.asarray(mask, dtype=bool)
    if normalized.shape != expected_shape:
        raise ValueError(f"mask shape {normalized.shape} does not match expected horizontal grid {expected_shape}")
    return normalized


def apply_ocean_mask(values: np.ndarray, ocean_mask: np.ndarray) -> np.ndarray:
    """把水平海域遮罩套到二維流場或地形陣列。

    輸入 values 代表 `(lat, lon)` 的速度、分量、zcor 或 bathymetry；False 的
    mask 位置會被改成 NaN。這個步驟的目的不是改變原始資料，而是在繪圖前
    防止陸地格點因歷史檔案、外部處理或插值邊界誤差而顯示出流速或箭頭。
    """

    masked = np.asarray(values, dtype=np.float32).copy()
    masked[~ocean_mask] = np.nan
    return masked


def build_background_frames(
    data: dict[str, np.ndarray | dict],
    background_mode: str,
    ocean_mask: np.ndarray,
    expected_time_count: int,
) -> tuple[np.ndarray | None, matplotlib.colors.Normalize | None, str | None]:
    """準備 2D 動畫底圖標量場。

    background_mode 控制底圖物理量：`neutral` 表示不使用色階，只畫固定海域底色；
    `elev` 表示使用 η（原始 NetCDF 變數 `elev`，自由水面高度）；`elev_anomaly`
    表示先扣除每個格點的月平均 η，凸顯逐時水位偏差。輸出 frames 的形狀為
    `(time, lat, lon)`，可直接與動畫 time index 對齊。norm 以 0 為中心，因為
    η 的正負號代表相對基準面的上升或下降；這和流速大小的非負色階不可混用。
    """

    if background_mode == BACKGROUND_NEUTRAL:
        return None, None, None
    if background_mode not in {BACKGROUND_ELEV, BACKGROUND_ELEV_ANOMALY}:
        raise ValueError(f"Unsupported background mode: {background_mode}")
    if "elev" not in data:
        raise FileNotFoundError(
            "Background mode elev/elev_anomaly requires elev.npy. "
            "Re-run preprocess_ocm_month.py with --include-elev."
        )

    elev = np.asarray(data["elev"], dtype=np.float32).copy()
    if elev.ndim != 3 or elev.shape[0] != expected_time_count or elev.shape[1:] != ocean_mask.shape:
        # elev 沒有 layer 維度，必須和 time_iso 與水平格點完全對齊；若 shape 不符，
        # 代表輸入資料夾混用了不同前處理版本或不同 bbox，繪圖會錯位。
        raise ValueError(
            f"elev shape {elev.shape} does not match expected (time, lat, lon) "
            f"= ({expected_time_count}, {ocean_mask.shape[0]}, {ocean_mask.shape[1]})"
        )

    # 將靜態陸域或 mesh 外格點設為 NaN，確保水位底圖只出現在有效海域。
    elev[:, ~ocean_mask] = np.nan
    if background_mode == BACKGROUND_ELEV_ANOMALY:
        # 月平均以每個格點獨立計算，保留空間上不同潮位基準或模式平均水位；
        # 扣除後的值代表該格點相對自己月平均的逐時偏差。
        finite_elev = np.isfinite(elev)
        elev_sum = np.where(finite_elev, elev, 0.0).sum(axis=0, dtype=np.float64)
        elev_count = finite_elev.sum(axis=0)
        mean_elev = np.full(elev.shape[1:], np.nan, dtype=np.float32)
        # 不使用 np.nanmean 的原因是陸地或 mesh 外格點整個月都可能是 NaN；
        # 這些格點應安靜地保留為 NaN，不應在正常繪圖流程中產生空切片警告。
        np.divide(elev_sum, elev_count, out=mean_elev, where=elev_count > 0)
        background = elev - mean_elev[None, :, :]
        label = "η anomaly from monthly mean (m)"
    else:
        background = elev
        label = "η / elev sea-surface elevation (m)"

    finite_abs = np.abs(background[np.isfinite(background)])
    limit = float(np.nanpercentile(finite_abs, 98)) if finite_abs.size else 1.0
    if not np.isfinite(limit) or limit <= 0:
        limit = 1.0
    # 使用對稱色階讓 η=0 具有穩定視覺語意；少數極端值會被色階裁切，但原始
    # 數值仍保留在資料陣列中，只是避免 GIF 因單一離群值而整體對比過低。
    norm = matplotlib.colors.TwoSlopeNorm(vmin=-limit, vcenter=0.0, vmax=limit)
    return background.astype(np.float32, copy=False), norm, label


def frame_to_png(
    lon: np.ndarray,
    lat: np.ndarray,
    speed: np.ndarray,
    u: np.ndarray,
    v: np.ndarray,
    timestamp: str,
    output_path: Path,
    vmax: float,
    quiver_step: tuple[int, int],
    layer_label: str,
    ocean_mask: np.ndarray,
    background: np.ndarray | None = None,
    background_norm: matplotlib.colors.Normalize | None = None,
    background_label: str | None = None,
) -> None:
    """繪製單一時間步水平流場圖。

    二維圖面可使用固定海域底色，或把 `background` 解讀為 η/elev 水位場並用
    色階呈現；不論底圖模式為何，流速大小一律由箭頭長度表示。若使用 η 底圖，
    colorbar 的單位是公尺，不能被解讀成 m/s；箭頭仍來自 u/v 分量。
    此圖是年度快速總覽的基礎，適合檢查黑潮主軸、台灣海峽交換與局部流向轉換。
    layer_label 會寫入標題，避免單獨檢視 PNG/GIF 時無法判斷它代表哪個
    模型垂向層；這裡的 layer 是模式陣列索引，不等同固定水深或公尺數。
    speed/u/v 中的 NaN 代表該 layer 在該格點沒有有效資料，會被畫成淡灰色；
    ocean_mask=False 則代表陸地或原始水體 mesh 外的位置，會被畫成陸地底色並
    排除 quiver 箭頭，避免把陸地誤判為低流速或靜水。
    vmax 不是色階上限，而是箭頭長度的穩定縮放基準；使用全動畫同一基準可避免
    每幀箭頭長短因自動縮放改變而造成時間序列誤讀。
    """

    # 建立圖形：選擇適當的 figsize 與 dpi 以便生成高品質 PNG
    fig, ax = plt.subplots(figsize=(8.5, 7.0), dpi=140)
    # 先把海域遮罩套到速度與分量，這是繪圖端的最後一道保護；即使中間檔來自
    # 舊版前處理或外部修改，也不允許 mask=False 的格點顯示流速或箭頭。
    speed = apply_ocean_mask(speed, ocean_mask)
    u = apply_ocean_mask(u, ocean_mask)
    v = apply_ocean_mask(v, ocean_mask)
    if background is not None:
        # background 目前代表 η/elev 或 η anomaly，維度必須是 (lat, lon)。
        # 再次套用 ocean_mask 是防禦性處理，避免舊版 elev.npy 或外部產製資料
        # 在陸地格點保留數值而被底圖上色。
        background = apply_ocean_mask(background, ocean_mask)

    # 在最底層先畫陸地底色。MISSING_DATA_COLOR 保留給海域內 layer 缺值，
    # LAND_COLOR 則只對應水平 mask=False，讓海洋/陸地/缺值三者可被目視區分。
    land = np.ma.masked_where(ocean_mask, np.zeros_like(ocean_mask, dtype=np.float32))
    land_cmap = matplotlib.colors.ListedColormap([LAND_COLOR])
    ax.pcolormesh(lon, lat, land, shading="auto", cmap=land_cmap, vmin=0, vmax=1)

    # 軸背景設成缺值色，讓完全沒有有效 layer 資料的位置維持淡灰色。
    # 後續的固定海域底圖只畫在 speed 有限的位置，因此不會把缺值區染成海域。
    ax.set_facecolor(MISSING_DATA_COLOR)

    valid_speed = np.isfinite(speed)
    if background is None:
        # 畫固定海域底色：有效速度格點代表該 layer 在此水平位置有可判讀水體。
        # 顏色不承載速度大小，避免把背景色誤解為流速色階；速度大小只由箭頭長度表示。
        ocean_layer = np.ma.masked_where(~valid_speed, np.ones_like(speed, dtype=np.float32))
        ocean_cmap = matplotlib.colors.ListedColormap([OCEAN_COLOR])
        ax.pcolormesh(lon, lat, ocean_layer, shading="auto", cmap=ocean_cmap, vmin=0, vmax=1)
    else:
        # η 底圖使用水位本身的有效值決定上色範圍；若某些格點有速度但沒有 elev，
        # 會維持缺值色，避免把缺少水位資料的位置誤畫成中性海域。
        background_layer = np.ma.masked_where(~np.isfinite(background), background)
        mesh = ax.pcolormesh(lon, lat, background_layer, shading="auto", cmap="RdBu_r", norm=background_norm)
        cbar = fig.colorbar(mesh, ax=ax, shrink=0.84, pad=0.025)
        cbar.set_label(background_label or "η / elev (m)")

    # 計算箭頭採樣位置：傳入的 quiver_step 為 (y_step, x_step)
    sy, sx = quiver_step
    q_lon = lon[::sx]
    q_lat = lat[::sy]

    # quiver 的速度向量矩陣也要以相同步幅抽樣。注意 u 與 v 的索引順序為 [lat, lon]。
    # 箭頭只在 speed/u/v 三者皆有效時顯示；任一分量缺值都代表該格點不適合判讀流向。
    valid_vector = np.isfinite(speed) & np.isfinite(u) & np.isfinite(v)
    sampled_valid_vector = valid_vector[::sy, ::sx]
    sampled_u = np.ma.masked_where(~sampled_valid_vector, u[::sy, ::sx])
    sampled_v = np.ma.masked_where(~sampled_valid_vector, v[::sy, ::sx])
    ax.quiver(
        q_lon,
        q_lat,
        sampled_u,
        sampled_v,
        color=QUIVER_COLOR,
        # scale 參數影響箭頭長度；以整段動畫的速度百分位作為基準，代表相同速度
        # 在每一幀都有相同視覺長度。係數比舊版小，讓使用者能直接從箭頭長度判讀流速強弱。
        scale=max(vmax * 8, 0.1),
        width=0.0026,
        headwidth=3.5,
        headlength=4.5,
        alpha=0.9,
    )

    # 標題與座標標籤。水位異常與原始水位雖然都來自 elev，但學術判讀語意不同；
    # 因此標題也分開標示，避免單看 GIF 時把 η' 誤解為未扣平均的 η。
    background_title = ""
    if background is not None:
        background_title = " over η anomaly" if background_label and "anomaly" in background_label else " over η"
    ax.set_title(f"OCM {layer_label} horizontal current{background_title} | {timestamp}")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    # 使用等比例顯示，以避免長寬比扭曲流向
    ax.set_aspect("equal", adjustable="box")

    # 確保邊界不被裁切，再輸出檔案並關閉圖形以釋放記憶體
    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)


def make_layer_animation(
    data: dict[str, np.ndarray | dict],
    output_path: Path,
    layer_index: int,
    frame_stride: int,
    fps: int,
    target_arrows: int,
    layer_label: str | None = None,
    background_mode: str = BACKGROUND_NEUTRAL,
) -> None:
    """輸出指定垂向層的水平流場 GIF。

    frame_stride 是視覺化階段的再次抽樣，與前處理 time_stride 不同。前者用來
    控制 GIF 幀數，後者決定中間檔保留多少時間解析度。output_path 應由呼叫端
    指定成可辨識用途的檔名，避免多層輸出時互相覆蓋或難以追蹤。
    background_mode 只控制底圖標量場；箭頭來源仍是同一層的 u/v，箭頭長度仍
    使用同一段動畫的速度百分位作為縮放基準。
    """

    # 取出必要欄位
    lon = data["lon"]
    lat = data["lat"]
    times = data["time_iso"]
    u = data["u"]
    v = data["v"]
    speed = data["speed"]
    ocean_mask = normalize_ocean_mask(data["mask"], (len(lat), len(lon)))
    background_frames, background_norm, background_label = build_background_frames(
        data,
        background_mode,
        ocean_mask,
        speed.shape[0],
    )

    # 將使用者傳入的 layer_index 解析為合法索引
    layer = resolve_layer_index(layer_index, speed.shape[1])
    label = layer_label or f"model layer {layer:03d}"

    # vmax 取每個時間點在該層速度的 98 百分位，作為整段動畫共用的箭頭縮放基準。
    # 二維底圖已不再用速度色階；保留此統計是為了避免單一極端流速把所有箭頭壓得過短。
    layer_speed = np.asarray(speed[:, layer], dtype=np.float32).copy()
    layer_speed[:, ~ocean_mask] = np.nan
    vmax = float(np.nanpercentile(layer_speed, 98))
    vmax = vmax if np.isfinite(vmax) and vmax > 0 else 1.0

    # 決定繪製箭頭的抽樣步寬
    quiver_step = choose_quiver_step(len(lon), len(lat), target_arrows)

    # 準備輸出目錄與暫存影格資料夾（以隱藏目錄放置中間 PNG）
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame_paths: list[Path] = []
    temp_dir = output_path.parent / f".{output_path.stem}_frames"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # loop：按照 frame_stride 抽樣時間步，為每一幀呼叫 frame_to_png
    for frame_number, time_index in enumerate(range(0, speed.shape[0], max(frame_stride, 1))):
        frame_path = temp_dir / f"frame_{frame_number:04d}.png"
        frame_to_png(
            lon,
            lat,
            speed[time_index, layer],
            u[time_index, layer],
            v[time_index, layer],
            str(times[time_index]),
            frame_path,
            vmax,
            quiver_step,
            label,
            ocean_mask,
            None if background_frames is None else background_frames[time_index],
            background_norm,
            background_label,
        )
        frame_paths.append(frame_path)

    # 使用 imageio 將多張 PNG 串成 GIF；mode="I" 表示多張影格（image sequence）
    with imageio.get_writer(output_path, mode="I", fps=fps) as writer:
        for frame_path in frame_paths:
            writer.append_data(imageio.imread(frame_path))

    # 清理暫存檔案
    for frame_path in frame_paths:
        frame_path.unlink(missing_ok=True)
    temp_dir.rmdir()


def layer_role_name(layer: int, layer_count: int) -> str:
    """依解析後的垂向層索引產生檔名角色。

    OCM/SCHISM 的垂向層通常由底層往表層排列，因此最小索引標為 bottom，
    最大索引標為 surface，中間層標為 model。這是檔名輔助資訊，真正的
    實際深度仍需由 zcor_mean.npy 判讀。
    """

    if layer == 0:
        return "bottom"
    if layer == layer_count - 1:
        return "surface"
    return "model"


def layer_animation_label(layer: int, layer_count: int) -> str:
    """建立寫入動畫標題的垂向層說明文字。

    標題保留 layer 的數值索引，因為此索引可直接對應 u/v/speed/zcor_mean 的
    第二維；同時加入 bottom/surface/model 角色，協助快速判讀圖面用意。
    """

    role = layer_role_name(layer, layer_count)
    if role == "bottom":
        return f"bottom layer {layer:03d}"
    if role == "surface":
        return f"surface layer {layer:03d}"
    return f"model layer {layer:03d}"


def layer_animation_filename(layer: int, layer_count: int, background_mode: str = BACKGROUND_NEUTRAL) -> str:
    """建立不會互相覆蓋且能看出用途的 GIF 檔名。

    中性底圖沿用舊檔名 `{role}_layer_{index}_horizontal_current_speed_quiver.gif`。
    若底圖改為 η/elev，表層輸出使用 `surface_speed_elev_quiver.gif` 這個較
    直觀的成果名；其它 layer 則在檔名中加入 `eta_background` 或
    `eta_anomaly_background`，避免和中性底圖版本互相覆蓋。
    """

    role = layer_role_name(layer, layer_count)
    if background_mode == BACKGROUND_ELEV and role == "surface":
        return "surface_speed_elev_quiver.gif"
    if background_mode == BACKGROUND_ELEV_ANOMALY and role == "surface":
        return "surface_speed_elev_anomaly_quiver.gif"
    if background_mode == BACKGROUND_ELEV:
        return f"{role}_layer_{layer:03d}_horizontal_current_eta_background_quiver.gif"
    if background_mode == BACKGROUND_ELEV_ANOMALY:
        return f"{role}_layer_{layer:03d}_horizontal_current_eta_anomaly_background_quiver.gif"
    return f"{role}_layer_{layer:03d}_horizontal_current_speed_quiver.gif"


def resolve_unique_layers(layer_indices: list[int], layer_count: int) -> list[int]:
    """解析並去除重複的垂向層索引。

    輸入可混用正索引與 Python 負索引，例如 `-1` 與最後一層正索引會指向
    同一層。為避免重複輸出同一個 GIF，這裡保留第一次出現的解析結果。
    """

    resolved_layers: list[int] = []
    seen: set[int] = set()
    for layer_index in layer_indices:
        layer = resolve_layer_index(layer_index, layer_count)
        if layer in seen:
            continue
        resolved_layers.append(layer)
        seen.add(layer)
    return resolved_layers


def make_multiple_layer_animations(
    data: dict[str, np.ndarray | dict],
    output_dir: Path,
    layer_indices: list[int],
    frame_stride: int,
    fps: int,
    target_arrows: int,
    background_mode: str = BACKGROUND_NEUTRAL,
) -> list[Path]:
    """依指定 layer 清單輸出多個水平流場 GIF。

    此函式是避免只產生單一泛用檔名的主要入口。每個 layer 會輸出成獨立檔案，
    檔名含有角色、三位數 layer index、底圖來源與 quiver 資訊；回傳值則提供
    實際輸出的檔案清單，方便未來接續產生索引頁或報告。background_mode 會
    傳入單層動畫函式，確保多層輸出使用同一種底圖語意。
    """

    speed = data["speed"]
    layer_count = speed.shape[1]
    output_paths: list[Path] = []

    for layer in resolve_unique_layers(layer_indices, layer_count):
        output_path = output_dir / layer_animation_filename(layer, layer_count, background_mode)
        make_layer_animation(
            data,
            output_path,
            layer,
            frame_stride,
            fps,
            target_arrows,
            layer_animation_label(layer, layer_count),
            background_mode,
        )
        output_paths.append(output_path)
    return output_paths


def make_surface_elevation_animation(
    data: dict[str, np.ndarray | dict],
    output_dir: Path,
    frame_stride: int,
    fps: int,
    target_arrows: int,
    background_mode: str,
) -> Path:
    """輸出表層流場搭配 η 類底圖的單一 GIF。

    此函式只允許 `elev` 或 `elev_anomaly` 兩種水位底圖，目的是把學術分析圖
    和原始資料檢查圖拆成兩個獨立產品：`elev_anomaly` 用於研究潮汐/水位變化
    與表層流場耦合，`elev` 用於檢查原始自由水面高度是否合理。它固定使用
    表層 hvel 箭頭，避免把表層水位底圖套到中層或底層流速後造成垂向語意混淆。
    """

    if background_mode not in {BACKGROUND_ELEV, BACKGROUND_ELEV_ANOMALY}:
        raise ValueError("Surface elevation animation requires elev or elev_anomaly background mode.")
    speed = data["speed"]
    layer_count = speed.shape[1]
    surface_layer = resolve_layer_index(-1, layer_count)
    output_path = output_dir / layer_animation_filename(surface_layer, layer_count, background_mode)
    make_layer_animation(
        data,
        output_path,
        surface_layer,
        frame_stride,
        fps,
        target_arrows,
        layer_animation_label(surface_layer, layer_count),
        background_mode,
    )
    return output_path


def make_3d_static(
    data: dict[str, np.ndarray | dict],
    output_path: Path,
    layer_indices: list[int],
    time_index: int,
    xy_step: int,
    vertical_exaggeration: float,
) -> None:
    """輸出三維流場稀疏箭頭示意圖。

    z 軸使用月平均 zcor，並乘上 vertical_exaggeration 以便在經緯度平面上看見
    垂向結構。這張圖用於「看懂資料型態與流場層化」，不應被解讀為真實比例
    的三維地形模型。
    """

    # 取出需要的欄位
    lon = data["lon"]
    lat = data["lat"]
    u = data["u"]
    v = data["v"]
    speed = data["speed"]
    zcor_mean = data["zcor_mean"]
    bathymetry = data["bathymetry"]
    ocean_mask = normalize_ocean_mask(data["mask"], (len(lat), len(lon)))

    layer_count = speed.shape[1]
    # 將使用者想要的層索引解析為合法索引清單
    layers = [resolve_layer_index(index, layer_count) for index in layer_indices]

    # 建立經緯度的二維網格，用於在 3D 中定位箭頭
    mesh_lon, mesh_lat = np.meshgrid(lon, lat)

    # 保護性限制 time_index 在合法範圍內
    t = min(max(time_index, 0), speed.shape[0] - 1)

    # vmax 同樣以 98 百分位作為色階上限（考慮多層時傳入 layers）
    selected_speed = np.asarray(speed[t, layers], dtype=np.float32).copy()
    selected_speed[:, ~ocean_mask] = np.nan
    vmax = float(np.nanpercentile(selected_speed, 98))
    vmax = vmax if np.isfinite(vmax) and vmax > 0 else 1.0

    # 建立 3D 圖形
    fig = plt.figure(figsize=(10.5, 8.0), dpi=160)
    ax = fig.add_subplot(111, projection="3d")

    # 處理海底資料：乘上垂直誇張參數以便在視覺上可見
    bottom = -apply_ocean_mask(bathymetry, ocean_mask) * vertical_exaggeration
    bottom_valid = np.isfinite(bottom)
    bottom_surface = np.where(bottom_valid, bottom, np.nan)

    # 半透明的海底平面提供深度參考，但顏色與透明度盡量低調以免干擾速度色階
    ax.plot_surface(
        mesh_lon,
        mesh_lat,
        bottom_surface,
        color="0.72",
        alpha=0.22,
        linewidth=0,
        antialiased=False,
        shade=False,
    )

    # 為每個指定層繪製稀疏的 3D 箭頭（quiver）
    for layer in layers:
        # zcor_mean 可能的形狀為 (layer, lat, lon)；乘上垂直誇張
        z = apply_ocean_mask(zcor_mean[layer], ocean_mask) * vertical_exaggeration
        # valid 表示該點在 z 與 speed 上皆有有限值
        speed_layer = apply_ocean_mask(speed[t, layer], ocean_mask)
        valid = np.isfinite(z) & np.isfinite(speed_layer)

        # 以 xy_step 做水平抽樣
        yy = slice(0, None, xy_step)
        xx = slice(0, None, xy_step)
        sampled = valid[yy, xx]

        # 顏色以速度相對於 vmax 進行映射
        colors = plt.cm.viridis(np.clip(speed_layer[yy, xx] / vmax, 0, 1))

        # 以 ax.quiver 繪製 3D 向量場：最後一個向量分量為 0（沒有垂直分量）
        ax.quiver(
            mesh_lon[yy, xx][sampled],
            mesh_lat[yy, xx][sampled],
            z[yy, xx][sampled],
            u[t, layer][yy, xx][sampled],
            v[t, layer][yy, xx][sampled],
            np.zeros_like(u[t, layer][yy, xx][sampled]),
            length=0.08,
            normalize=True,
            colors=colors[sampled],
            linewidth=0.8,
            alpha=0.82,
        )

    # 標註與視角
    ax.set_title(f"OCM 3D current-field sketch | time index {t} | z x{vertical_exaggeration:g}")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_zlabel("Mean zcor (m, exaggerated)")
    ax.view_init(elev=27, azim=-58)

    # 建立對應 colorbar（以 mappable 方式，不直接對 quiver 取色階）
    mappable = plt.cm.ScalarMappable(cmap="viridis")
    mappable.set_clim(0, vmax)
    cbar = fig.colorbar(mappable, ax=ax, shrink=0.68, pad=0.08)
    cbar.set_label("Speed (m/s)")

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path)
    plt.close(fig)


def format_layers_for_filename(layers: list[int]) -> str:
    """將解析後的 layer 清單轉成穩定檔名字串。

    3D 時間動畫通常會同時呈現多個垂向層；使用三位數補零可讓檔名直接說明
    動畫包含哪些模型層，並避免 `-1` 這類輸入索引和實際解析後索引混淆。
    """

    return "_".join(f"{layer:03d}" for layer in layers)


def make_3d_time_animation(
    data: dict[str, np.ndarray | dict],
    output_path: Path,
    layer_indices: list[int],
    frame_stride: int,
    fps: int,
    xy_step: int,
    vertical_exaggeration: float,
) -> None:
    """輸出使用逐時 zcor 的三維流場時間動畫。

    這個動畫和 `make_3d_static` 的關鍵差異是 z 軸不使用 `zcor_mean.npy`，而是
    使用 `zcor.npy` 的每個時間步，因此能呈現自由水面與 sigma/z 層位隨水位
    逐時上下移動。若缺少 `zcor.npy`，函式會直接報錯，避免產生看似逐時但
    其實只使用月平均垂向座標的誤導性動畫。
    """

    if "zcor" not in data:
        raise FileNotFoundError(
            "3D time animation requires zcor.npy. Re-run preprocess_ocm_month.py with --include-zcor-time."
        )

    # 取出必要欄位。u/v/speed 與 zcor 皆為 (time, layer, lat, lon)，其中 zcor
    # 是每個時間步、模型層與水平格點的實際垂向座標，單位通常為公尺。
    lon = data["lon"]
    lat = data["lat"]
    u = data["u"]
    v = data["v"]
    speed = data["speed"]
    zcor = data["zcor"]
    bathymetry = data["bathymetry"]
    times = data["time_iso"]
    ocean_mask = normalize_ocean_mask(data["mask"], (len(lat), len(lon)))

    layer_count = speed.shape[1]
    layers = resolve_unique_layers(layer_indices, layer_count)
    surface_layer = resolve_layer_index(-1, layer_count)
    frame_indices = list(range(0, speed.shape[0], max(frame_stride, 1)))
    if not frame_indices:
        raise ValueError("No frames selected for 3D time animation.")

    # 使用所選時間與所選 layer 的速度 98 百分位作為穩定色階，避免每幀色階跳動。
    # 這會讀取部分 mmap 資料，但比逐幀重算不同色階更適合時間動畫判讀。
    selected_speed = np.asarray(speed[frame_indices][:, layers], dtype=np.float32)
    selected_speed[:, :, ~ocean_mask] = np.nan
    vmax = float(np.nanpercentile(selected_speed, 98))
    vmax = vmax if np.isfinite(vmax) and vmax > 0 else 1.0

    # 固定 z 軸範圍，避免動畫因 Matplotlib 自動縮放造成視角抖動。時間動畫的
    # 目標是看見逐時水位與所選層位變化，因此 zlim 聚焦於所選 zcor layer 與
    # 表層水面，而不把數千公尺海底深度納入 z 軸範圍；否則 1 公尺等級水位
    # 變化會被深海尺度壓扁到幾乎不可見。
    bottom = -apply_ocean_mask(bathymetry, ocean_mask) * vertical_exaggeration
    z_selected = np.asarray(zcor[frame_indices][:, layers], dtype=np.float32)
    z_selected[:, :, ~ocean_mask] = np.nan
    z_selected *= vertical_exaggeration
    surface_selected = np.asarray(zcor[frame_indices, surface_layer], dtype=np.float32)
    surface_selected[:, ~ocean_mask] = np.nan
    surface_selected *= vertical_exaggeration
    z_min = float(np.nanmin(z_selected))
    z_max = float(np.nanmax([np.nanmax(surface_selected), np.nanmax(z_selected)]))
    if not np.isfinite(z_min) or not np.isfinite(z_max) or z_min >= z_max:
        z_min, z_max = -1.0, 1.0
    z_margin = 0.08 * (z_max - z_min)
    z_min -= z_margin
    z_max += z_margin

    mesh_lon, mesh_lat = np.meshgrid(lon, lat)
    bottom_surface = np.where(np.isfinite(bottom), bottom, np.nan)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame_paths: list[Path] = []
    temp_dir = output_path.parent / f".{output_path.stem}_frames"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # 逐時間步繪製 3D frame。每幀都重新建立 figure 是較慢但穩定的做法，可避免
    # 3D artist 清除不完整造成殘影；此動畫是檢查用，不追求即時互動效能。
    for frame_number, time_index in enumerate(frame_indices):
        frame_path = temp_dir / f"frame_{frame_number:04d}.png"
        fig = plt.figure(figsize=(10.5, 8.0), dpi=140)
        ax = fig.add_subplot(111, projection="3d")

        ax.plot_surface(
            mesh_lon,
            mesh_lat,
            bottom_surface,
            color="0.72",
            alpha=0.20,
            linewidth=0,
            antialiased=False,
            shade=False,
        )

        # 以逐時表層 zcor 畫出半透明水面，讓水位變化在時間動畫中可被直接觀察。
        water_surface = apply_ocean_mask(zcor[time_index, surface_layer], ocean_mask) * vertical_exaggeration
        ax.plot_surface(
            mesh_lon,
            mesh_lat,
            np.where(np.isfinite(water_surface), water_surface, np.nan),
            color="#6bb6ff",
            alpha=0.18,
            linewidth=0,
            antialiased=False,
            shade=False,
        )

        for layer in layers:
            z = apply_ocean_mask(zcor[time_index, layer], ocean_mask) * vertical_exaggeration
            speed_layer = apply_ocean_mask(speed[time_index, layer], ocean_mask)
            valid = np.isfinite(z) & np.isfinite(speed_layer)
            yy = slice(0, None, xy_step)
            xx = slice(0, None, xy_step)
            sampled = valid[yy, xx]
            colors = plt.cm.viridis(np.clip(speed_layer[yy, xx] / vmax, 0, 1))
            ax.quiver(
                mesh_lon[yy, xx][sampled],
                mesh_lat[yy, xx][sampled],
                z[yy, xx][sampled],
                u[time_index, layer][yy, xx][sampled],
                v[time_index, layer][yy, xx][sampled],
                np.zeros_like(u[time_index, layer][yy, xx][sampled]),
                length=0.08,
                normalize=True,
                colors=colors[sampled],
                linewidth=0.75,
                alpha=0.82,
            )

        surface_mean_m = float(np.nanmean(zcor[time_index, surface_layer]))
        ax.set_title(
            f"OCM 3D current-field time animation | {times[time_index]} | surface mean z={surface_mean_m:+.2f} m | z x{vertical_exaggeration:g}"
        )
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        ax.set_zlabel("Time-varying zcor (m, exaggerated)")
        ax.set_xlim(float(np.nanmin(lon)), float(np.nanmax(lon)))
        ax.set_ylim(float(np.nanmin(lat)), float(np.nanmax(lat)))
        ax.set_zlim(z_min, z_max)
        ax.view_init(elev=27, azim=-58)

        mappable = plt.cm.ScalarMappable(cmap="viridis")
        mappable.set_clim(0, vmax)
        cbar = fig.colorbar(mappable, ax=ax, shrink=0.68, pad=0.08)
        cbar.set_label("Speed (m/s)")

        fig.tight_layout()
        fig.savefig(frame_path)
        plt.close(fig)
        frame_paths.append(frame_path)

    with imageio.get_writer(output_path, mode="I", fps=fps) as writer:
        for frame_path in frame_paths:
            writer.append_data(imageio.imread(frame_path))

    for frame_path in frame_paths:
        frame_path.unlink(missing_ok=True)
    temp_dir.rmdir()


def parse_layer_list(text: str) -> list[int]:
    """解析逗號分隔的垂向層索引。

    這個工具同時供 2D 動畫與 3D 示意圖使用。輸入值代表 NumPy 陣列第二維
    的 layer index，可使用 Python 負索引；空字串會回傳空清單，讓呼叫端
    自行決定預設層或是否報錯。
    """

    # 接受類似 "0,10,20,-1" 的字串，並回傳整數索引清單
    return [int(part.strip()) for part in text.split(",") if part.strip()]


def parse_args() -> argparse.Namespace:
    """解析視覺化命令列參數。"""

    parser = argparse.ArgumentParser(description="Visualize preprocessed monthly OCM current fields.")
    parser.add_argument("--input-dir", required=True, type=Path, help="Directory from preprocess_ocm_month.py.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for figures and animations.")
    parser.add_argument("--surface-animation", action="store_true", help="Create surface-layer GIF.")
    parser.add_argument(
        "--surface-elev-animation",
        action="store_true",
        help="Create surface-current GIF with raw elev/η background for model-output checks.",
    )
    parser.add_argument(
        "--surface-elev-anomaly-animation",
        action="store_true",
        help="Create surface-current GIF with elev anomaly background for research analysis.",
    )
    parser.add_argument("--layer-animation", action="store_true", help="Create GIFs for selected layer indices.")
    parser.add_argument("--layer-index", type=int, default=-1, help="Layer index for layer animation; -1 usually means surface.")
    parser.add_argument(
        "--layer-indices",
        default=None,
        help="Comma-separated layer indices for multiple layer GIFs, for example 0,16,32,-1.",
    )
    parser.add_argument(
        "--all-layers",
        action="store_true",
        help="Create one GIF per model layer. This is expensive for monthly data.",
    )
    parser.add_argument("--frame-stride", type=int, default=1, help="Use every Nth preprocessed frame in GIF.")
    parser.add_argument("--fps", type=int, default=6, help="GIF frames per second.")
    parser.add_argument("--target-arrows", type=int, default=500, help="Approximate number of quiver arrows per frame.")
    parser.add_argument(
        "--background",
        choices=(BACKGROUND_NEUTRAL, BACKGROUND_ELEV, BACKGROUND_ELEV_ANOMALY),
        default=BACKGROUND_NEUTRAL,
        help="2D animation background: neutral ocean fill, elev η field, or elev anomaly from monthly mean.",
    )
    parser.add_argument("--make-3d", action="store_true", help="Create static 3D current-field sketch.")
    parser.add_argument(
        "--make-3d-animation",
        action="store_true",
        help="Create 3D time GIF using time-varying zcor.npy. Requires preprocessing with --include-zcor-time.",
    )
    parser.add_argument("--three-d-layers", default="0,10,20,-1", help="Comma-separated layer indices for 3D sketch.")
    parser.add_argument("--three-d-time-index", type=int, default=0, help="Preprocessed time index for 3D sketch.")
    parser.add_argument(
        "--three-d-frame-stride",
        type=int,
        default=4,
        help="Use every Nth preprocessed frame for 3D time animation.",
    )
    parser.add_argument("--three-d-xy-step", type=int, default=2, help="Horizontal sampling step for 3D arrows.")
    parser.add_argument("--vertical-exaggeration", type=float, default=0.02, help="Scale zcor meters into lon/lat visual space.")
    return parser.parse_args()


def main() -> None:
    """執行動畫與三維示意圖產製。"""

    args = parse_args()
    data = load_month(args.input_dir)
    speed = data["speed"]
    layer_count = speed.shape[1]
    if args.surface_elev_anomaly_animation:
        # 研究分析圖：底圖使用 η' = η - 月平均 η，只呈現相對水位變化；箭頭仍
        # 使用表層 hvel。這和原始 elev 檢查圖分開輸出，避免同一張圖混用兩種水位語意。
        make_surface_elevation_animation(
            data,
            args.output_dir,
            args.frame_stride,
            args.fps,
            args.target_arrows,
            BACKGROUND_ELEV_ANOMALY,
        )
    if args.surface_elev_animation:
        # 原始資料檢查圖：底圖使用未扣平均的 η/elev，用於檢查模式輸出的自由水面
        # 高度是否合理；正式潮汐或流場耦合分析應優先看 elev_anomaly 版本。
        make_surface_elevation_animation(
            data,
            args.output_dir,
            args.frame_stride,
            args.fps,
            args.target_arrows,
            BACKGROUND_ELEV,
        )
    if args.surface_animation:
        surface_layer = resolve_layer_index(-1, layer_count)
        make_layer_animation(
            data,
            args.output_dir / layer_animation_filename(surface_layer, layer_count, args.background),
            surface_layer,
            args.frame_stride,
            args.fps,
            args.target_arrows,
            layer_animation_label(surface_layer, layer_count),
            args.background,
        )
    if args.layer_animation:
        # 多層輸出的優先序：明確要求全部層時使用完整 range；否則使用
        # --layer-indices 的逗號清單；若未提供清單，維持既有 --layer-index 單層行為。
        layer_indices = (
            list(range(layer_count))
            if args.all_layers
            else parse_layer_list(args.layer_indices)
            if args.layer_indices is not None
            else [args.layer_index]
        )
        if args.surface_animation:
            # README 範例會同時輸出表層動畫與代表層清單，其中 `-1` 也會解析成表層。
            # 這裡先去掉已由 --surface-animation 產生的表層，避免同一個 GIF 被重畫一次。
            surface_layer = resolve_layer_index(-1, layer_count)
            layer_indices = [
                layer_index
                for layer_index in layer_indices
                if resolve_layer_index(layer_index, layer_count) != surface_layer
            ]
        make_multiple_layer_animations(
            data,
            args.output_dir,
            layer_indices,
            args.frame_stride,
            args.fps,
            args.target_arrows,
            args.background,
        )
    if args.make_3d:
        make_3d_static(
            data,
            args.output_dir / "flow_field_3d.png",
            parse_layer_list(args.three_d_layers),
            args.three_d_time_index,
            args.three_d_xy_step,
            args.vertical_exaggeration,
        )
    if args.make_3d_animation:
        layers = resolve_unique_layers(parse_layer_list(args.three_d_layers), layer_count)
        make_3d_time_animation(
            data,
            args.output_dir / f"flow_field_3d_time_layers_{format_layers_for_filename(layers)}.gif",
            layers,
            args.three_d_frame_stride,
            args.fps,
            args.three_d_xy_step,
            args.vertical_exaggeration,
        )


if __name__ == "__main__":
    main()
