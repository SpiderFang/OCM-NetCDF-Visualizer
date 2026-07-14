# OCM NetCDF Visualizer 下一階段加強版 Spec

## 1. 背景與目的

目前專案已完成一月份 OCM/SCHISM NetCDF 的基礎流場處理流程：

- 讀取每日 `*_schout.nc`。
- 以經度 `[119, 123]`、緯度 `[20, 27]` 作為台灣鄰近研究區域。
- 將非結構網格節點資料插值到 10 km 規則經緯度格點。
- 以 3 小時抽樣產出一月份 `hvel` 水平流速中間檔。
- 產出表層流場動畫、指定垂向層動畫與 3D 稀疏箭頭示意圖。

下一階段目標是把目前「只看水平三維流場」擴充成「可支援全年先期觀察與研究區域分割」的分析型產品。重點不是一次把所有欄位塞進同一張圖，而是依照物理意義拆成可判讀、可驗證、可批次處理的資料產品。

## 2. 已確認的 NetCDF 欄位理解

2025-01 日檔為 SCHISM/UGRID 結構，核心維度如下：

- `time = 24`：單日 hourly time steps。
- `nSCHISM_hgrid_node = 508456`：非結構水平節點。
- `nSCHISM_hgrid_face = 988434`：非結構元素。
- `nSCHISM_hgrid_edge = 1497045`：非結構邊。
- `nSCHISM_vgrid_layers = 48`：垂向層。
- `two = 2`：向量分量，通常為東西向與南北向。

主要變數：

- `SCHISM_hgrid_node_x(node)`：節點經度。
- `SCHISM_hgrid_node_y(node)`：節點緯度。
- `depth(node)`：水深，單位公尺，正值向下。
- `zcor(time, node, layer)`：每個時間、節點、垂向層的實際 z 座標，單位通常為公尺。
- `hvel(time, node, layer, two)`：三維水平流速，主要流場來源。
- `vertical_velocity(time, node, layer)`：垂直流速。
- `dahv(time, node, two)`：深度平均水平流速。
- `elev(time, node)`：自由水面高度。
- `temp(time, node, layer)`：水溫。
- `salt(time, node, layer)`：鹽度。
- `water_density(time, node, layer)`：水密度。
- `diffusivity(time, node, layer)`：垂向或湍流擴散相關模型參數。
- `wetdry_elem(time, face)`：元素乾濕狀態。

缺值旗標：

- 多數動態變數使用 `missing_value = 9.969209968386869e+36`。
- 前處理必須在插值與統計前轉成 `NaN`，避免色階與統計被缺值污染。

## 3. 下一階段優先順序

### P1：深度平均流場 `dahv`

目的：

- 提供全年最快速的 2D 流場總覽。
- 讓使用者不必讀取 48 層 `hvel` 就能快速觀察台灣海峽、黑潮、巴士海峽與東北外海的主流向。

資料處理：

- 讀取 `dahv(time, node, two)`。
- 使用既有水平插值權重插值到規則格點。
- 輸出 `dahv_u.npy`、`dahv_v.npy`、`dahv_speed.npy`。
- 陣列形狀為 `time, lat, lon`。

視覺化：

- 新增 `dahv_speed_quiver.gif`。
- 背景為 `dahv_speed` 色階。
- 箭頭為 `dahv_u/dahv_v`。
- 適合作為年度總覽動畫的第一優先產品。

驗收標準：

- 一月份 10 km / 3 小時設定可成功輸出。
- `monthly_summary.json` 增加 `dahv_speed_m_per_s` 統計。
- GIF 時間步與 `time_iso.npy` 對齊。

### P2：水位 `elev` 疊圖

狀態：

- 已在 `preprocess_ocm_month.py` 加入 `--include-elev`，可輸出
  `elev.npy(time, lat, lon)`。
- 已在 `visualize_ocm_month.py` 加入 `--surface-elev-anomaly-animation` 與
  `--surface-elev-animation`，分別輸出研究分析用 `η'` 水位異常圖與原始
  `η/elev` 檢查圖；兩者分開產生，不在同一張圖中混用。
- 仍保留進階選項 `--background elev|elev_anomaly` 給一般 layer 動畫使用，但
  多垂向層流場比較建議維持中性底圖，避免把表層水位色階套到不同 layer。

目的：

- 用於判讀潮汐、水位變化與流向轉換的關聯。
- 支援後續分辨潮汐主導區與外海環流主導區。

資料處理：

- 讀取 `elev(time, node)`。
- 插值到規則格點。
- 輸出 `elev.npy`，形狀為 `time, lat, lon`。

視覺化：

- 新增 `surface_speed_elev_anomaly_quiver.gif` 作為主要研究分析圖。
- 新增 `surface_speed_elev_quiver.gif` 作為原始模式水位檢查圖。
- 背景選項：
  - `speed`：表層流速。
  - `elev`：水位。
  - `elev_anomaly`：扣除每格點月平均後的水位異常。
- 箭頭仍使用表層 `hvel` 或 `dahv`。

驗收標準：

- 可用 CLI 選擇背景欄位。
- 色階標籤清楚區分 `m/s` 與 `m`。
- 不把水位與流速混用同一個 colorbar。

### P3：垂直流速 `vertical_velocity`

目的：

- 找出可能的上升流、下降流與垂向交換區。
- 輔助判斷分層、混合與研究區域邊界。

資料處理：

- 讀取 `vertical_velocity(time, node, layer)`。
- 插值到規則格點。
- 輸出 `w.npy`，形狀為 `time, layer, lat, lon`。

視覺化：

- 新增 `vertical_velocity_layer.gif`。
- 背景為指定層 `w`，使用正負發散色階。
- 箭頭可選擇疊加同層 `u/v`。
- 3D 圖中不建議直接把 `w` 當箭頭 z 分量，除非明確做比例縮放與標註，避免垂向量級被誤讀。

驗收標準：

- 支援 `--vertical-velocity-animation`。
- 支援 `--w-layer-index`。
- 色階以 0 為中心。

### P4：溫鹽密度與水團特徵

目的：

- 從純流速觀察擴充到水團與分層判讀。
- 為後續研究區域分割提供物理特徵。

資料處理：

- 選擇性讀取 `temp`、`salt`、`water_density`。
- 先不必輸出所有時間、所有層的完整陣列；可先輸出統計特徵：
  - 月平均表層溫度。
  - 月平均表層鹽度。
  - 月平均表底密度差。
  - 指定層溫鹽剖面。

建議輸出：

- `temp_surface_mean.npy`
- `salt_surface_mean.npy`
- `density_surface_mean.npy`
- `density_bottom_mean.npy`
- `density_stratification.npy`

視覺化：

- `temp_surface_mean.png`
- `salt_surface_mean.png`
- `density_stratification.png`
- 後續可增加沿固定經緯線的剖面圖。

驗收標準：

- 不要求第一版做動畫。
- 先產生月平均圖與統計摘要。
- 所有圖需清楚標示單位與層位。

### P5：乾濕遮罩 `wetdry_elem`

目的：

- 改善近岸淺水區與潮間帶的有效資料判斷。
- 避免在乾掉或模型標示無效的區域繪製流速。

資料處理限制：

- `wetdry_elem` 是 face-centered，而目前主要產品是 node/grid-centered。
- 需要決定 face-to-node 或 face-to-grid 的轉換策略。

建議策略：

- 第一版先保留現有 bathymetry/NaN mask。
- 第二版加入元素乾濕比例：
  - 將 face 中心座標插值或最近鄰映射到規則格點。
  - 輸出 `wetdry_fraction.npy`，形狀為 `time, lat, lon`。

驗收標準：

- 可選擇 `--apply-wetdry-mask`。
- metadata 清楚記錄乾濕遮罩來源與轉換方法。

## 4. 建議新增或調整的腳本

### `scripts/preprocess_ocm_month.py`

建議擴充 CLI：

```bash
--include-dahv
--include-elev
--include-vertical-velocity
--include-tracers temp,salt,water_density
--include-wetdry
--surface-layer-index -1
--bottom-layer-index 0
```

設計原則：

- 預設仍只處理必要流場，避免檔案暴增。
- 額外欄位都必須由 CLI 明確啟用。
- 每個新增欄位都要在 `monthly_summary.json` 中記錄：
  - 原始變數名稱。
  - 輸出檔名。
  - 陣列形狀。
  - 單位與物理意義。
  - 缺值處理方式。

### `scripts/visualize_ocm_month.py`

建議擴充 CLI：

```bash
--dahv-animation
--surface-elev-animation
--surface-elev-anomaly-animation
--vertical-velocity-animation
--tracer-map temp_surface_mean
--background speed|elev|elev_anomaly|dahv_speed
--vector-source surface|dahv|layer
```

設計原則：

- 一張動畫只表達一個主變數。
- 若背景和箭頭代表不同物理量，必須有獨立標籤與說明。
- 不在同一張圖中混入過多欄位。

### 建議新增 `scripts/summarize_ocm_month.py`

用途：

- 從月資料中間檔產生統計與分割特徵。
- 避免把所有分析都塞進前處理或視覺化腳本。

建議輸出：

- `features/monthly_flow_features.npz`
- `features/monthly_flow_summary.json`

建議特徵：

- 表層平均流速。
- 深度平均平均流速。
- 主流向角度。
- 流速標準差。
- 表層與底層流速差。
- 垂向剪切。
- 水位振幅。
- 溫鹽密度分層指標。

## 5. 輸出資料夾規劃

建議下一階段輸出結構：

```text
outputs/
  ocm_2025_01_taiwan_10km_3h/
    lon.npy
    lat.npy
    time_iso.npy
    bathymetry.npy
    mask.npy
    u.npy
    v.npy
    speed.npy
    zcor_mean.npy
    zcor.npy
    dahv_u.npy
    dahv_v.npy
    dahv_speed.npy
    elev.npy
    w.npy
    monthly_summary.json
    figures/
      surface_layer_047_horizontal_current_speed_quiver.gif
      bottom_layer_000_horizontal_current_speed_quiver.gif
      model_layer_016_horizontal_current_speed_quiver.gif
      model_layer_032_horizontal_current_speed_quiver.gif
      dahv_speed_quiver.gif
      surface_speed_elev_anomaly_quiver.gif
      surface_speed_elev_quiver.gif
      vertical_velocity_layer.gif
      flow_field_3d.png
      flow_field_3d_time_layers_032_040_047.gif
    features/
      monthly_flow_features.npz
      monthly_flow_summary.json
```

## 6. 年度批次處理需求

下一階段應避免只為一月份設計。所有新增欄位與圖像都需能逐月執行：

```bash
for m in 01 02 03 04 05 06 07 08 09 10 11 12; do
  UV_CACHE_DIR=work/uv-cache uv run python3 scripts/preprocess_ocm_month.py \
    --input-dir /server/path/CWA-OCM/2025/$m \
    --output-dir outputs/ocm_2025_${m}_taiwan_10km_3h \
    --year 2025 \
    --month $m \
    --domain-id taiwan-nearby \
    --bbox 119.0 123.0 20.0 27.0 \
    --target-resolution-km 10 \
    --time-stride 3 \
    --include-dahv \
    --include-elev
done
```

年度合併建議另做腳本，不要讓月處理腳本同時負責全年合併。

## 7. 效能與檔案大小注意事項

目前 2025-01、10 km、3 小時、48 層輸出約略特徵：

- `time_count = 248`
- `grid = 78 x 41`
- `u.npy`、`v.npy`、`speed.npy` 各約 145 MB。
- 完整 3 小時 GIF 約 10 MB 等級。

新增欄位後的風險：

- 若加入 `w.npy`，檔案大小會接近另一個 145 MB。
- 若加入 `temp/salt/water_density` 全時間全層，單月可能再增加數百 MB。
- 全年處理時必須優先考慮：
  - 月份分批。
  - 可選欄位。
  - 統計特徵優先於完整陣列。
  - 必要時改用 chunked 格式，例如 zarr。

## 8. 驗收標準

下一階段完成時，至少應符合：

1. 一月份 10 km / 3 小時設定可重新產生所有既有產品。
2. `dahv` 深度平均流場可輸出 `.npy` 與 GIF。
3. `elev` 可輸出 `.npy`，並可產生水位背景或水位異常背景圖。
4. `monthly_summary.json` 明確列出每個新增欄位的來源、形狀、單位與統計。
5. README 更新執行指令與輸出說明。
6. 所有新增或修改的 Python 模組、公開函式、重要流程都要有繁體中文註解或 docstring。
7. `py_compile` 通過。

## 9. 建議實作順序

已完成：

1. 擴充前處理支援 `--include-elev`。
2. 擴充視覺化支援 `--surface-elev-anomaly-animation` 與
   `--surface-elev-animation`，其中研究圖輸出為
   `surface_speed_elev_anomaly_quiver.gif`，檢查圖輸出為
   `surface_speed_elev_quiver.gif`。

後續建議順序：

1. 擴充前處理支援 `--include-dahv`。
2. 擴充視覺化支援 `--dahv-animation`。
3. 新增月特徵摘要腳本 `scripts/summarize_ocm_month.py`。
4. 評估是否加入 `vertical_velocity`。
5. 評估是否加入溫鹽密度月平均產品。
6. 最後再處理 `wetdry_elem` 的 face-to-grid 遮罩轉換。
