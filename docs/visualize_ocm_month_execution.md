# `scripts/visualize_ocm_month.py` 執行流程說明

檔案：scripts/visualize_ocm_month.py

## 概覽
- 功能：由 preprocessing 的 NumPy 中間檔（npy + JSON）產生 2D 水平流場 GIF 與 3D 示意圖 / 時間動畫（PNG / GIF）。
- 主要輸入：`lon.npy`, `lat.npy`, `time_iso.npy`, `u.npy`, `v.npy`, `speed.npy`, `mask.npy`。
- 選用輸入：`elev.npy`, `zcor.npy`, `zcor_mean.npy`, `bathymetry.npy`, `monthly_summary.json`。
- 資料形狀說明：主要陣列多為 `(time, layer, lat, lon)`，`elev` 為 `(time, lat, lon)`，`zcor_mean` 為 `(layer, lat, lon)`。

---
## 程式啟動與主要流程:
- 入口: if __name__ == "__main__": main()。

## 主流程 (main)
1. 解析命令列選項（`parse_args()`）：例如 `--input-dir`, `--output-dir`, `--surface-animation`, `--layer-animation`, `--make-3d-animation`, `--background`, `--frame-stride`, `--fps`, `--target-arrows` 等。（大量旗標，控制輸入/輸出、哪些動畫要產生、抽樣參數、background 模式等）。
2. 使用 `load_month(input_dir)` 讀取所有必要的 npy 與 summary（部分大型檔用 `mmap_mode="r"` 以節省記憶體）。
3. 根據命令列選項依序呼叫下列處理函式（呼叫順序會影響避免覆寫）：
   - `make_surface_elevation_animation(...)`（若 `--surface-elev-anomaly` 或 `--surface-elev-animation`）
     若 --surface-elev-anomaly：呼 make_surface_elevation_animation(..., BACKGROUND_ELEV_ANOMALY)（表層速度 + η anomaly 底圖）。
     若 --surface-elev-animation：呼 make_surface_elevation_animation(..., BACKGROUND_ELEV)（表層速度 + 原始 elev 底圖）。
   - `make_layer_animation(...)`（若 `--surface-animation`）
     若 --surface-animation：解析表層 layer，呼 make_layer_animation(...)（中性或指定 background）。
   - `make_multiple_layer_animations(...)`（若 `--layer-animation`）
     若 --layer_animation：依 --all-layers / --layer-indices / --layer-index 決定要輸出的層清單，並呼 make_multiple_layer_animations(...)（會避免重複輸出已由 --surface-animation 產生的表層）。
   - `make_3d_static(...)`（若 `--make-3d`）
   - `make_3d_time_animation(...)`（若 `--make-3d-animation`，需 `zcor.npy`）
     若 --make-3d：呼 make_3d_static(...) 產生單張 3D 靜態示意圖（使用 zcor_mean 作 z）。
     若 --make-3d-animation：解析 layers、frame indices，呼 make_3d_time_animation(...)（需要 zcor.npy，以時間變動 zcor 產 GIF）。

若缺少必要檔案（例如沒有 `elev.npy` 卻選 `elev` 底圖，或沒有 `zcor.npy` 卻選 3D 時間動畫），程式會拋出錯誤以提示重跑前處理。

---

## 重要子函式與角色
- `load_month(input_dir)`：讀取並回傳所有必要陣列與 metadata；大型檔使用 mmap。
- `normalize_ocean_mask(mask, expected_shape)`：檢查並轉為 `(lat, lon)` 的 boolean 遮罩。
- `apply_ocean_mask(values, ocean_mask)`：把非海域位置設成 NaN，避免陸地顯示流速或色階。
- `build_background_frames(data, background_mode, ocean_mask, expected_time_count)`：處理底圖（`neutral` / `elev` / `elev_anomaly`），回傳 background frames、norm、label。
- `resolve_layer_index(layer_index, layer_count)`：支援 Python 風格負索引並驗證範圍。
- `resolve_unique_layers(layer_indices, layer_count)`：解析並去重層索引。
- `choose_quiver_step(lon_count, lat_count, target_arrows)`：估算 quiver 抽樣步長 (y_step, x_step)。
- `choose_quiver_reference_speed(vmax)` / `add_quiver_scale_key(ax, quiver, vmax)`：計算與繪製箭頭比例尺。
- `frame_to_png(...)`：繪製單一時間步的 2D 流場 PNG（包含背景、陸地覆蓋、箭頭、比例尺、標題），並關閉 figure 以釋放記憶體。
- `make_layer_animation(...)`：為單一層建立暫存 PNG（按 `frame_stride` 抽樣）並用 `imageio` 合成 GIF，最後清理暫存。
- `make_multiple_layer_animations(...)`：對多層逐一呼 `make_layer_animation`。
- `make_surface_elevation_animation(...)`：表層專用封裝（只允許 `elev` 或 `elev_anomaly` 底圖）。
- `make_3d_static(...)`：使用 `zcor_mean` 畫 3D 靜態示意圖（海底、層位箭頭），輸出 PNG。
- `make_3d_time_animation(...)`：使用 `zcor.npy` 的逐時 zcor 每幀畫 3D 圖並合成 GIF，固定 z 軸範圍以避免視覺抖動。

---

## 錯誤處理與防護
- 針對 shape、維度與 finite 值有多處檢查，發現不符即 raise，避免繪圖錯位或產生誤導圖。
- 使用 `mmap_mode='r'` 在讀取大型檔案時降低記憶體壓力。
- 使用 `matplotlib.use('Agg')` 與每次 `plt.close(fig)` 在無頭環境穩定產圖。

---

## 輸出行為
- 2D 與 3D 時間動畫會先在 `output_dir/.<stem>_frames/` 產生每幀 PNG，完成後用 `imageio` 合成 GIF，最後刪除暫存 PNG 與目錄。
- 3D 靜態圖直接輸出單張 PNG。

---

## 常用 CLI 範例
```bash
python scripts/visualize_ocm_month.py --input-dir /path/to/month_out --output-dir /path/to/out --surface-animation
python scripts/visualize_ocm_month.py --input-dir /path/to/month_out --output-dir /path/to/out --surface-elev-anomaly
python scripts/visualize_ocm_month.py --input-dir /path/to/month_out --output-dir /path/to/out --layer-animation --layer-indices 0,16,32,-1
python scripts/visualize_ocm_month.py --input-dir /path/to/month_out --output-dir /path/to/out --make-3d-animation --three-d-layers 0,10,-1 --three-d-frame-stride 2
```

---

## 流程圖（Mermaid）

```mermaid
flowchart TD
  A[Start: parse_args()] --> B[load_month(input_dir)]
  B --> C{Options}
  C -->|--surface-elev-anomaly| D[make_surface_elevation_animation (surface + elev_anomaly)]
  C -->|--surface-elev-animation| E[make_surface_elevation_animation (surface + elev)]
  C -->|--surface-animation| F[make_layer_animation (surface layer)]
  C -->|--layer-animation| G[determine layer list -> make_multiple_layer_animations]
  C -->|--make-3d| H[make_3d_static (uses zcor_mean)]
  C -->|--make-3d-animation| I[make_3d_time_animation (requires zcor.npy)]
  subgraph "make_layer_animation"
    M1[normalize_ocean_mask] --> M2[build_background_frames]
    M2 --> M3[compute vmax (98th pct)]
    M3 --> M4[choose_quiver_step]
    M4 --> M5[for each time frame -> frame_to_png -> save PNG]
    M5 --> M6[imageio combine PNGs -> GIF]
    M6 --> M7[cleanup temps]
  end
  subgraph "frame_to_png"
    F1[apply_ocean_mask to speed/u/v] --> F2[draw background (neutral or elev)]
    F2 --> F3[draw_land_overlay]
    F3 --> F4[sample and plot quiver]
    F4 --> F5[add_quiver_scale_key + title + save PNG]
  end
  subgraph "make_3d_time_animation"
    T1[require zcor.npy] --> T2[select frames & layers]
    T2 --> T3[for each time -> build 3D figure -> save PNG]
    T3 --> T4[imageio combine PNGs -> GIF]
  end
  D & E & F & G & H & I --> Z[End]
```

---

如需我把此檔案轉為 PDF 或把 Mermaid 直接渲染成 PNG，也可以幫你做（會需要額外工具或外部渲染服務）。
