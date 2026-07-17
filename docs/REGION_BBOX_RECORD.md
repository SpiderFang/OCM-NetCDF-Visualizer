# 區域 bbox 決策紀錄

本文紀錄各研究區域使用的 OCM/SCHISM 前處理 bbox。此檔放在 `docs/`，
原因是 bbox 屬於研究設定與資料產品決策，應和一次性 `outputs/` 產物分開保存，
也不應埋在 Python 腳本預設值中，避免後續四個區域比較時失去決策脈絡。

## 使用格式

本專案 `scripts/preprocess_ocm_month.py` 與 `scripts/run_ocm_2025_year.sh`
使用的 bbox 順序為：

```text
lon_min lon_max lat_min lat_max
```

若需要轉成一般 GIS / GeoJSON bbox，順序為：

```text
[lon_min, lat_min, lon_max, lat_max]
```

## 已確認區域

### NMMBA / 後灣與南台灣外海

- 區域名稱：屏東縣國立海洋生物博物館、後灣與南台灣外海影響區
- 主要目標：涵蓋可能影響後灣與恆春半島西北側的外海流場，同時避免把菲律賓北岸與台灣中北部外海納入過多，降低圖面干擾與計算量。
- 建議 bbox（專案 CLI 順序）：

```text
117.50 124.00 19.00 24.00
```

- GIS / GeoJSON bbox：

```text
[117.50, 19.00, 124.00, 24.00]
```

- 對應 `DOMAIN_ID`：

```text
nmmba-south-taiwan-outer-current-11750-12400-1900-2400
```

- 已產生 QC 輸出：

```text
outputs/ocm_2025_01_nmmba_bbox_11750_12400_1900_2400_1km_smoke
```

- QC 設定：
  - 原始檔：`/Users/mustlab/Downloads/CWA-OCM/2025/01/20250101_schout.nc`
  - 時間：只取第一個抽樣時間 `2025-01-01T01:00:00`
  - 解析度：`1 km`
  - 格點：`557 x 674`
  - 有效海域比例：約 `0.795`
  - 陸域遮罩：`data/geojson/twCounty2010.geo.json`

- 主要檢視圖：

```text
outputs/ocm_2025_01_nmmba_bbox_11750_12400_1900_2400_1km_smoke/figures/surface_layer_047_horizontal_current_speed_quiver_first_frame.png
outputs/ocm_2025_01_nmmba_bbox_11750_12400_1900_2400_1km_smoke/figures/surface_speed_elev_quiver_first_frame.png
```

- 決策理由：
  - 西界 `117.50E` 保留台灣西南外海與台灣海峽南段的背景流場。
  - 東界 `124.00E` 保留台灣東南側黑潮主軸與外海背景流，不讓東側邊界太貼近台灣。
  - 南界 `19.00N` 保留巴士海峽北側與南海北部外海訊號，但避免直接納入菲律賓北岸。
  - 北界 `24.00N` 保留高屏外海、澎湖附近與台灣海峽南段；相較 `24.80N` 或 `25.50N`，可減少台灣中北部與東北外海對圖面與計算量的干擾。
  - 曾測試較大範圍 `117.50 124.00 18.00 24.80`，可涵蓋更完整巴士海峽與外海流場，但圖面包含菲律賓北岸且資料量較大，因此不作為目前正式建議。

- 建議前處理命令範例：

```bash
UV_CACHE_DIR=work/uv-cache \
MPLCONFIGDIR=work/matplotlib-cache \
PYTHONDONTWRITEBYTECODE=1 \
uv run python3 scripts/preprocess_ocm_month.py \
  --input-dir /Users/mustlab/Downloads/CWA-OCM/2025/01 \
  --output-dir outputs/ocm_2025_01_nmmba_bbox_11750_12400_1900_2400_1km_smoke \
  --year 2025 \
  --month 1 \
  --domain-id nmmba-south-taiwan-outer-current-11750-12400-1900-2400 \
  --bbox 117.50 124.00 19.00 24.00 \
  --target-resolution-km 1 \
  --source-margin-deg 0.25 \
  --time-stride 999 \
  --max-files 1 \
  --include-elev \
  --land-geojson data/geojson/twCounty2010.geo.json
```

## 待確認區域

後續四個區域確認後，請依同一格式補上 bbox、GIS bbox、`DOMAIN_ID`、QC 輸出位置、
格點大小、有效海域比例、主要檢視圖與決策理由。

### 區域 2

- 狀態：待確認
- 建議 bbox：待補

### 區域 3

- 狀態：待確認
- 建議 bbox：待補

### 區域 4

- 狀態：待確認
- 建議 bbox：待補

### 區域 5

- 狀態：待確認
- 建議 bbox：待補
