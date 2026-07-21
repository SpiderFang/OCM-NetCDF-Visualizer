# 同尺寸 flow domain bbox 決策紀錄

本文紀錄 2026-07-21 新需求下的 OCM/SCHISM 前處理 bbox 規格。此檔和
`docs/REGION_BBOX_RECORD.md` 分開保存，因為舊檔主要保存各研究區「外海影響分析域」
與 smoke test 決策；本檔只處理「從 raw OCM NetCDF 一次前處理成同尺寸
flow domain `.npy` 快取」的資料產品規格。

## 需求摘要

- 前處理階段只從 raw OCM NetCDF 讀取一次，輸出固定大小矩形 `flow_domain` `.npy`
  快取；後續 EOF、統計與作圖不得再直接回頭讀 raw NetCDF。
- 五個研究區都使用同一物理大小的矩形快取域，以降低不同區域 EOF 或統計比較時的
  資料尺度差異。
- 共用域只用於下列兩種情境：
  - 龜山島與貢寮共用同一份東北台灣 `flow_domain` 快取。
  - 連江縣 7 個子區共用同一份連江 `flow_domain` 快取。
- 新竹與屏東／海生館不和其它研究區共用快取，但仍使用同一物理大小的矩形
  `flow_domain`，讓五個研究區的前處理快取產品維持一致尺度。
- EOF 分析前，必須先從 `flow_domain` `.npy` 依 `analysis_bbox`、`focus_bbox`
  或 AOI polygon 切出真正要分析的空間域；`flow_domain` 不是研究結論邊界。

## bbox 順序與座標系統

本專案 CLI 仍使用下列順序：

```text
lon_min lon_max lat_min lat_max
```

GIS / GeoJSON bbox 若需要轉換，順序為：

```text
[lon_min, lat_min, lon_max, lat_max]
```

所有座標皆為 WGS84 經緯度。本檔表格保留經 geojson.io 目視確認的 6 位小數
bbox，作為目前 v3 候選快取域。原因是本需求沒有唯一「標準答案」，需在資料量、
目標區域完整性與外側流場緩衝之間迭代。正式 smoke test 後，應以
`monthly_summary.json` 回填實際 `lat_count`、`lon_count` 與資料量；若未來要求
四份 `.npy` 陣列 shape 完全一致，需改由同一個 bbox 產生器輸出更多小數，或在
後處理階段明確裁切／補齊到共同 shape。

## 標準 flow domain 尺寸

本版是第三輪縮小後的 v3 候選。使用者在 geojson.io 檢視後，確認 v2 的
`119.750000 121.750000 21.050000 22.450000` 套用到其它區域仍偏大；該尺寸可留作
報告中解釋「小型外海影響框」的視覺參考，但不適合直接作為 raw NetCDF 讀取範圍。
因此 v3 將資料快取框縮到約 `150 km x 100 km`，定位為「後續
`analysis_bbox/focus_bbox` 的來源 tile」，而不是完整外海影響域。

屏東／海生館 v3 錨點 bbox：

```text
120.166710 121.620000 21.550844 22.449156
```

由此得到的標準物理尺寸為：

```text
寬度：約 150 km
高度：約 100 km
```

在 `--target-resolution-km 1` 且沿用現行 `build_target_grid()` 公式時，每份
flow domain 約為：

```text
約 100 x 150-151
水平格點數約 15,000
```

此規格比舊版屏東／海生館 bbox `117.50 124.00 19.00 24.00` 的約 `375,418`
個 1 km 水平格點少約 `96%`；也比 v2 compact 錨點的 `32,292` 格少約一半以上，
更符合「raw 讀取快取，不承擔報告外海敘事」的需求。

### bbox 產生公式

每個區域先指定一個中心點 `(center_lon, center_lat)`，再用同一物理寬高推回 bbox：

```text
lat_half_deg = height_km / (2 * 111.32)
lon_half_deg = width_km / (2 * 111.32 * cos(center_lat))

lon_min = center_lon - lon_half_deg
lon_max = center_lon + lon_half_deg
lat_min = center_lat - lat_half_deg
lat_max = center_lat + lat_half_deg
```

這樣做的原因是不同緯度下 1 度經度代表的公里數不同；若所有區域硬套同一個
經緯度跨度，物理寬度與 grid shape 都會隨緯度變化，不符合「同一大小快取域」
的需求。

## 前處理 flow cache 產品

本需求實際需要 4 份前處理快取產品，對應 5 個研究區。龜山島與貢寮共用一份；
連江 7 子區共用一份；新竹與屏東／海生館各自一份。

| flow cache | 涵蓋研究區 | 中心點 `(lon, lat)` | v3 bbox（CLI 順序） | 建議 `DOMAIN_ID` | 1 km 預估格點 |
| --- | --- | --- | --- | --- | --- |
| 東北台灣共用域 | 宜蘭縣龜山島海域、新北市貢寮海域 | `122.050000, 25.050000` | `121.306315 122.793685 24.600844 25.499156` | `northeast-taiwan-common-cache-v3-121306-122794-24601-25499` | 約 `15,000` |
| 新竹單區域 | 新竹縣外海 | `120.450000, 24.750000` | `119.708120 121.191880 24.300844 25.199156` | `hsinchu-cache-v3-119708-121192-24301-25199` | 約 `15,000` |
| 屏東／海生館單區域 | 屏東縣國立海洋生物博物館周邊海域 | `120.893355, 22.000000` | `120.166710 121.620000 21.550844 22.449156` | `houwan-nmmba-cache-v3-120167-121620-21551-22449` | 約 `15,000` |
| 連江共用域 | 連江縣北竿、南竿 7 個候選子區 | `119.950000, 26.200000` | `119.199120 120.700880 25.750844 26.649156` | `lienchiang-common-cache-v3-119199-120701-25751-26649` | 約 `15,000` |

### 研究區對應關係

| 研究區 | 使用的 flow cache | 後續切分方式 | 備註 |
| --- | --- | --- | --- |
| 宜蘭縣龜山島海域 | 東北台灣共用域 | 從共用 `.npy` 切 `analysis_bbox` 或 AOI polygon | `flow_domain` 保留龜山島、宜蘭外海、東北角與黑潮近岸背景；本版已依 geojson.io 檢視結果略往東北平移，以減少西南側陸地並增加外側海域緩衝。EOF 前仍要先切龜山島分析域。 |
| 新北市貢寮海域 | 東北台灣共用域 | 從共用 `.npy` 切 `analysis_bbox` 或新版 compact focus bbox | 舊版貢寮聚焦範圍 `121.40 123.50 24.85 26.20` 已超出本版 compact cache，不適合作為直接切片範圍；後續需另定較小 `analysis_bbox`，或改以 AOI polygon 從 cache 內取樣。 |
| 新竹縣外海 | 新竹單區域 | 從單區域 `.npy` 切新竹 `analysis_bbox` | 本版已將新竹框整體往西移，減少東側陸地浪費，同時保留新竹外海與台灣海峽東側背景。 |
| 屏東縣國立海洋生物博物館周邊海域 | 屏東／海生館單區域 | 從單區域 `.npy` 切後灣／海生館 `analysis_bbox` 或 AOI polygon | 本版東界貼近蘭嶼東側，保留蘭嶼陸地視覺辨識，但不再納入過多蘭嶼東側外洋。 |
| 連江縣海域 | 連江共用域 | 從共用 `.npy` 切 7 個 `focus_bbox` 或 AOI polygon | 連江共用域供北竿 3 區與南竿 4 區共用；7 個子區統計不得直接使用整個 flow domain。 |

## 屏東／海生館縮小範圍判定

本版採用的屏東／海生館 bbox：

```text
120.166710 121.620000 21.550844 22.449156
```

保留內容：

- 北界到 `22.449156N`，包含小琉球與高屏外海必要北側緩衝。
- 東界到 `121.620000E`，貼近蘭嶼東側，保留蘭嶼陸地視覺辨識，但不再延伸到更多東側外洋。
- 南界到 `21.550844N`，只保留屏東南側與巴士海峽北緣的近場脈絡，不再把南界下推到呂宋海峽北部。
- 西界到 `120.166710E`，保留後灣西側外海與小琉球西側交換空間；使用者確認此西側緩衝可接受。

刻意排除內容：

- 完整呂宋海峽與菲律賓北側。
- 澎湖完整範圍與台灣海峽中段以上。
- 更遠的南海北部外海與台灣東側遠洋背景。
- 日本與那國島／八重山群島方向的東北台灣遠場；這是本版縮小標準尺寸的重要原因之一。

此取捨符合新的資料生命週期：`flow_domain` 是 EOF 與統計前的快取範圍，不再承擔
完整外海影響域報告敘事。若未來研究問題改回黑潮入侵南海、呂宋海峽交換或台灣海峽
南段通量，應另建大尺度 flow domain，不應把本 compact cache 重新解讀為完整外海域。

## 快取與 EOF 分析規則

- `flow_domain` `.npy` 是唯一允許 EOF 後處理讀取的流場資料來源；若 EOF 需要的新欄位
  不存在於 cache，應重建對應月份 cache，而不是在 EOF 腳本中直接補讀 raw NetCDF。
- EOF 的空間域必須在分析前切好。流程應為：

```text
load flow_domain .npy
→ 依 lon.npy / lat.npy 切 analysis_bbox 或 focus_bbox
→ 套用 mask.npy 與選用 AOI polygon
→ 建立 EOF 資料矩陣
→ 執行 EOF / PC / loading 分析
```

- 龜山島與貢寮因距離近且共享東北台灣黑潮、陸棚交換與沿岸流背景，`flow_domain`
  重疊或共用是合理的；兩者的研究分離應靠 EOF 前的 `analysis_bbox` 或 AOI mask。
- 連江 7 子區距離更近，整個連江 flow cache 僅作為資料快取。子區熱點統計與 EOF
  應使用既有低重疊 `focus_bbox` 或更精細的 polygon，不得直接用整個連江共用域代表單一子區。
- 新竹與屏東／海生館雖不共用域，但仍使用相同物理尺寸，以維持 cache 資料量、
  grid shape 與後續分析流程一致。

## 建議 smoke test 驗收

每份 flow cache 正式使用前，至少需要完成單日單時刻 smoke test：

```bash
UV_CACHE_DIR=work/uv-cache \
MPLCONFIGDIR=work/matplotlib-cache \
PYTHONDONTWRITEBYTECODE=1 \
uv run python3 scripts/preprocess_ocm_month.py \
  --input-dir /Users/mustlab/Downloads/CWA-OCM/2025/01 \
  --output-dir outputs/ocm_2025_01_<domain_id>_1km_cache_smoke \
  --year 2025 \
  --month 1 \
  --domain-id <domain_id> \
  --bbox <lon_min> <lon_max> <lat_min> <lat_max> \
  --target-resolution-km 1 \
  --source-margin-deg 0.25 \
  --time-stride 999 \
  --max-files 1 \
  --include-elev \
  --land-geojson data/geojson/twCounty2010.geo.json
```

驗收項目：

- `monthly_summary.json` 中的 `grid.lat_count` 與 `grid.lon_count` 應接近
  `100 x 150-151`，實際值需依 smoke test 回填；若任一區域因 bbox 四捨五入造成
  1 格差異，可先視為可接受的候選誤差。
- `lon.npy`、`lat.npy`、`mask.npy`、`u.npy`、`v.npy`、`speed.npy`、`elev.npy`
  的 shape 必須符合 `time, layer, lat, lon` 或 `time, lat, lon` 規格。
- 表層流場圖不得空白，且不能有明顯陸域流速殘留。
- 每個研究區後續要使用的 `analysis_bbox`、`focus_bbox` 或 AOI polygon 必須完全落在
  對應 flow cache 內；若需要 EOF 邊界緩衝，focus bbox 不應直接貼住 flow cache 邊界。
- smoke test 只能證明 bbox、插值、遮罩與圖面流程可用；正式 EOF 或報告結論仍需使用
  完整月份或完整年度時間序列。

## 待後續補充

- 龜山島、貢寮、新竹、屏東／海生館與連江 7 子區的正式 `analysis_bbox` 或 AOI polygon。
- 同尺寸 flow cache 的完整月份批次命名規則與輸出目錄規則。
- EOF 腳本的欄位需求，例如使用 `u/v`、`speed`、`elev_anomaly`、深度平均流或指定 sigma layer。
- 四份 flow cache 的 smoke test 圖面、有效海域比例、資料量與 QC 結果。
