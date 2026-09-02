# OCM 海流資料三維流場示意與月資料動畫流程

本專案以中央氣象署 OCM NetCDF 月資料為起點，建立可從「單月資料」逐步擴充到「整年時間序列」的處理與視覺化管線。現階段先針對本機一月份資料夾 `/Users/mustlab/Downloads/CWA-OCM/2025/01` 實作；搬到 server 後建議維持月份為單位，一個月處理、檢查完成後再啟動下一批月份。

## 目標

- 讀取每日 `*_schout.nc` NetCDF 檔案，盤點時間、水平網格、垂向層、海流與水位資料。
- 將非結構網格 OCM/SCHISM 節點資料，插值到研究區域的規則經緯度格點。
- 產出月資料中間檔，作為年度動畫、三維示意圖與後續研究區域分割的共同輸入。
- 產出表層流場動畫、固定垂向層動畫與三維流場示意圖。

## 研究區參考圖

五個研究區的原始範圍、等深線與作業海域參考圖統一存放於 `data/reference/`，
避免文件依賴個人電腦的 `Downloads` 絕對路徑。這些圖片只用於 bbox 判讀、視覺比對與報告追溯，
不是前處理腳本的輸入，也不代表 OCM 網格解析度或模式計算邊界。

- `data/reference/屏東縣國立海洋生物博物館周邊海域.png`
- `data/reference/宜蘭縣龜山島海域.png`
- `data/reference/新北市貢寮海域.png`
- `data/reference/新竹縣外海.png`
- `data/reference/連江縣海域.png`

各區正式 bbox、檢視成果與邊界決策理由記錄於
[`docs/REGION_BBOX_RECORD.md`](docs/REGION_BBOX_RECORD.md)。

## 後續實作規格

- `docs/NEXT_PHASE_ENHANCED_SPEC.md`：記錄下一階段加強版實作規格，包含 `dahv` 深度平均流場、`elev` 水位疊圖、`vertical_velocity` 垂直流速、溫鹽密度特徵、乾濕遮罩、年度批次處理與驗收標準。後續開發新欄位或新動畫前，應先依此 spec 拆分任務與更新 README。
- `docs/VECTOR_LAND_OVERLAY.md`：記錄成果圖使用的「1 km OCM 規則格點資料 + GeoJSON 向量陸地疊圖」做法。此做法只提升陸地、離島與海岸線的視覺辨識度，不改變流場資料解析度或統計結果；後續成果報告若需要讓北竿、南竿、龜山島、貢寮岬角等小尺度陸地更清楚，應先參考此文件。

## 選用外部 GeoJSON 陸域遮罩

前處理預設會優先使用 OCM/SCHISM 原始 `SCHISM_hgrid_face_nodes` 建立靜態海域遮罩，並在來源資料提供 `wetdry_elem` 時套用逐時乾濕遮罩。若原始 mesh 在台灣本島、離島或海岸附近仍殘留少量被視為有效水體的格點，可額外提供本機 GeoJSON 面狀圖資給 `--land-geojson`，把落在陸域 polygon 內的規則格點從 `mask.npy` 扣除。

已確認可用的來源：

- OXXO.STUDIO 的「Google Maps API - 顯示台灣縣市 ( GeoJSON )」文章整理了台灣縣市行政區 GeoJSON/TopoJSON 來源，並示範使用縣市 GeoJSON 繪製台灣行政區。
- `g0v/twgeojson` 提供台灣行政區 GeoJSON/TopoJSON；README 標示資料為 CC0 1.0 Universal。`json/twCounty2010.geo.json` 是可直接作為 `--land-geojson` 的 GeoJSON 檔，GitHub API 顯示大小約 9 MB。

下載 g0v county-level GeoJSON 到本機資料夾：

```bash
mkdir -p data/geojson
curl -L https://raw.githubusercontent.com/g0v/twgeojson/master/json/twCounty2010.geo.json \
  -o data/geojson/twCounty2010.geo.json
```

`data/geojson/` 已列入 `.gitignore`。這些外部圖資應由下載指令與 `monthly_summary.json` 追蹤版本與路徑，不直接提交到專案。若改用 Sheethub 或其它正式岸線資料，只要檔案是 GeoJSON `Polygon` 或 `MultiPolygon`，也可用相同參數套用。

## 資料假設

目前腳本針對常見 SCHISM NetCDF 結構設計：

- `SCHISM_hgrid_node_x`：節點經度，單位通常為 degree east。
- `SCHISM_hgrid_node_y`：節點緯度，單位通常為 degree north。
- `depth`：每個水平節點的水深，單位通常為公尺，正值代表海床深度。
- `time`：每個檔案內的時間軸，會優先使用 NetCDF `units` 與 `calendar` 轉成 ISO 時間字串。
- `hvel`：水平流速，預期維度包含時間、節點、垂向層與東西/南北兩個分量。
- `zcor`：每個時間、節點與垂向層的實際垂向座標，單位通常為公尺，表層接近水面、負值朝向海底。
- `elev(time, node)`：自由水面高度 η（eta），原始資料位於 SCHISM 水平節點。
  單位通常依 SCHISM 慣例為公尺；目前檢查到的 2025-01 檔案未明寫 `units`，
  但數值範圍約為公尺等級，適合作為海表面高度變化與潮汐訊號的底圖欄位。

若其它月份資料的變數名稱或維度順序不同，應先用檢查腳本確認，再調整變數參數或程式中的維度解析邏輯。

## 安裝

請先切到專案根目錄；後續所有相對路徑都以此資料夾為基準：

```bash
cd /Users/mustlab/Workspace/OCM-NetCDF-Visualizer
```

```bash
UV_CACHE_DIR=work/uv-cache uv sync
```

## Server 操作

Server / VS Code Remote SSH 的前處理、月份批次、畫圖、3D 與監看指令已移到
[`README_SERVER.md`](README_SERVER.md)。主 README 只保留本機與共通流程，避免操作
指令混在一起。

## 1. 檢查一月份 NetCDF 結構

```bash
UV_CACHE_DIR=work/uv-cache uv run python3 scripts/inspect_ocm_netcdf.py \
  --input /Users/mustlab/Downloads/CWA-OCM/2025/01/20250101_schout.nc \
  --output outputs/inspect_20250101.json
```

此步驟會輸出維度、變數、屬性、座標範圍與主要流場變數資訊，用於人工確認後續前處理是否讀對資料。`outputs/inspect_20250101.json` 是檢查紀錄與資料假設參考，不是 `preprocess_ocm_month.py` 的程式輸入；目前前處理腳本不會自動讀取這個 JSON。

## 2. 前處理一月份資料

以下範例先取台灣鄰近海域，經度 `[119, 123]`、緯度 `[20, 27]`，並使用 10 km 解析度與 3 小時抽樣。這個設定比早期 smoke test 更細，適合作為一月份正式 demo 與後續其它月份處理的基準設定：

`preprocess_ocm_month.py` 會直接讀取 `--input-dir` 內排序後的 `*_schout.nc` 原始 NetCDF 日檔，並從第一個檔案推斷 `hvel` 維度順序、垂向層數、`depth`、`sigma` 等必要資訊。若前一步產生了 inspect JSON，該檔只用來讓開發者比對資料結構與腳本假設，不會參與本步驟運算。

```bash
UV_CACHE_DIR=work/uv-cache uv run python3 scripts/preprocess_ocm_month.py \
  --input-dir /Users/mustlab/Downloads/CWA-OCM/2025/01 \
  --output-dir outputs/ocm_2025_01_taiwan_10km_3h \
  --year 2025 \
  --month 1 \
  --domain-id taiwan-surrounding \
  --bbox 119.0 123.0 20.0 27.0 \
  --target-resolution-km 10 \
  --time-stride 3 \
  --include-zcor-time \
  --include-elev
```

若需要額外套用外部 GeoJSON 陸域遮罩，才在同一個前處理指令中加入以下參數。
未加入此參數時，流程會維持原本的 OCM/SCHISM mesh mask 與 `wetdry_elem` 行為，
不會讀取 GeoJSON，也不會改變 `mask.npy` 的靜態海域定義：

```bash
  --land-geojson data/geojson/twCounty2010.geo.json
```

輸出內容包含：

- `lon.npy`、`lat.npy`：規則格點經緯度。
- `sigma.npy`：垂向層代表值，若原檔有 sigma 變數則沿用。
- `time_iso.npy`：抽樣後時間字串。
- `u.npy`、`v.npy`：插值後東西向與南北向流速，形狀為 `time, layer, lat, lon`。
- `speed.npy`：水平流速大小。
- `elev.npy`：自由水面高度 η，由原始 `elev(time, node)` 插值到規則格點，
  形狀為 `time, lat, lon`，用於 2D 水位底圖或水位異常底圖。
- `zcor_mean.npy`：每層在每個格點的月平均垂向座標。
- `zcor.npy`：逐時垂向座標，形狀為 `time, layer, lat, lon`，用於需要呈現水位與 sigma/z 層位隨時間變動的 3D 動畫。此檔約與單一流速分量同等大小，只有加上 `--include-zcor-time` 才會輸出。
- `bathymetry.npy`：插值後水深。
- `mask.npy`：有效海域遮罩。新版前處理會優先依原始 `SCHISM_hgrid_face_nodes`
  水平元素判斷靜態海域，避免 Delaunay 凸包把陸地洞補成流場；若原始資料提供
  `wetdry_elem`，逐時乾出元素會在輸出的 `u/v/speed/zcor` 中直接寫成 NaN。若提供
  `--land-geojson`，與 GeoJSON 陸域 polygon 接觸的目標格點 cell 也會從 `mask.npy`
  扣除，並在 `bathymetry/u/v/speed/elev/zcor` 等已輸出的欄位寫成 NaN。這個
  cell-overlap 判斷不改變格點數或輸出陣列大小，但能避免澎湖、蘭嶼等小島因
  10 km 格點中心沒有落在 polygon 內而被誤保留為海域。
- `monthly_summary.json`：流速統計、時間範圍、輸入檔案與參數。

## 3. 產生 2D 動畫

前處理完成後，`visualize_ocm_month.py` 會讀取月份資料夾內的 `lon/lat/time_iso/u/v/speed/elev/mask`
等 `.npy` 中間檔，並把 GIF/PNG 寫到該月份的 `figures/` 子資料夾。以下指令會重跑
目前建議的主要 2D 成果圖，不會輸出 `flow_field_3d.png`。
研究分析圖與原始水位檢查圖分開輸出，不把 `elev` 與 `elev_anomaly` 混在同一張圖：

- `--surface-elev-anomaly-animation`：主要研究圖，底圖為
  `η'(x,y,t)=η(x,y,t)-monthly_mean(η)(x,y)`，適合看潮汐、水位變化與表層流場耦合。
- `--surface-elev-animation`：原始資料檢查圖，底圖為未扣平均的 `η/elev`，適合確認模式水位輸出是否合理。
- `--layer-animation --layer-indices 0,16,32,-1`：多垂向層流場比較，建議維持中性底圖，避免把表層水位色階套到中層或底層流速後造成解讀混淆。
- `--surface-animation`：輸出中性底圖的表層流場，用於只看箭頭流向與流速相對強弱。

兩種 η 圖的 colorbar 單位都是公尺，右側色條會固定標示實際繪圖資料推算出的
最小值與最大值；若資料範圍跨過 0，會額外標示 0 作為正負水位變化的判讀中心。
流速大小仍由深藍色箭頭長度表示，箭頭方向表示流向；圖面右下角會顯示 m/s
參考箭頭比例尺，讀者可用它判讀實際流速量級。
`--target-arrows 1000` 會讓箭頭比早期版本更密，適合目前台灣 10 km / 3 小時 demo。

以下是一月份 demo 的同等指令，可用來重畫本機或既有一月主要成果圖：

```bash
UV_CACHE_DIR=work/uv-cache MPLCONFIGDIR=work/matplotlib-cache \
  uv run python3 scripts/visualize_ocm_month.py \
  --input-dir outputs/ocm_2025_01_taiwan_10km_3h \
  --output-dir outputs/ocm_2025_01_taiwan_10km_3h/figures \
  --surface-elev-anomaly-animation \
  --surface-elev-animation \
  --surface-animation \
  --layer-animation \
  --layer-indices 0,16,32,-1 \
  --background neutral \
  --frame-stride 1 \
  --fps 2 \
  --target-arrows 1000
```

### GeoJSON 陸地遮罩 QC 範例

以下範例使用整月、10 km、台灣周邊 bbox，並套用 GeoJSON 陸地遮罩，輸出到指定位置：

前處理資料:

```bash
uv run python3 scripts/preprocess_ocm_month.py \
  --input-dir /Users/mustlab/Downloads/CWA-OCM/2025/01 \
  --output-dir outputs/ocm_2025_01_taiwan_10km_geojson_qc \
  --year 2025 \
  --month 1 \
  --domain-id taiwan-surrounding-geojson-qc \
  --bbox 119.0 123.0 20.0 27.0 \
  --target-resolution-km 10 \
  --time-stride 3 \
  --land-geojson data/geojson/twCounty2010.geo.json \
  --include-zcor-time \
  --include-elev
```

產生 2D 表層動畫：

```bash
uv run python3 scripts/visualize_ocm_month.py \
  --input-dir outputs/ocm_2025_01_taiwan_10km_geojson_qc \
  --output-dir outputs/ocm_2025_01_taiwan_10km_geojson_qc/figures \
  --surface-elev-anomaly-animation \
  --surface-elev-animation \
  --surface-animation \
  --layer-animation \
  --layer-indices 0,16,32,-1 \
  --background neutral \
  --frame-stride 1 \
  --fps 2 \
  --target-arrows 1000
```

### 投影片用乾淨區域圖

若只需要後續簡報排版用的乾淨 PNG，使用獨立腳本
`scripts/plot_ocm_clean_region_maps.py`，不要改動一般動畫腳本
`scripts/visualize_ocm_month.py`。此腳本會讀取既有 `.npy` 中間檔，輸出：

- 四個等物理尺寸 flow-domain bbox 主圖：連江共用域、東北台灣共用域、新竹單區域、屏東/海生館單區域。
- 三張獨立放大圖：龜山島、貢寮、南北竿。
- 一份 JSON metadata，記錄時間、layer、bbox、zoom extent 與箭頭縮放參數。

PNG 圖面刻意只保留經緯度刻度數字與 `Longitude`、`Latitude`；不放標題、圖例、
比例尺文字、區域名稱或註解。流速箭頭使用同一個 98 百分位流速作為縮放基準，
但預設 `--quiver-scale-multiplier 20`，因此箭頭比一般動畫更短、更細。三張獨立
放大圖只顯示 GeoJSON 向量陸地輪廓；`mask.npy` 仍用於排除陸地箭頭，但不再把
1 km 方格陸地畫出來，避免小島岸線外側出現階梯狀灰塊。所有主圖與放大圖都會
強制把圖面經緯度左右/上下邊界加入座標軸主要刻度；這個設計是為了讓報告截圖能
直接讀出裁切範圍，不會因 Matplotlib 自動刻度省略 bbox 上下限而被誤認為邊界
沒有切齊。三張獨立放大圖預設使用 `--zoom-coordinate-tick-interval 0.1`，讓龜山、
貢寮與南北竿的經緯度主要刻度維持相同尺度。放大圖顯示範圍也採一位小數邊界：
龜山 `121.80-122.20E, 24.60-25.00N`、貢寮 `121.70-122.20E, 24.80-25.30N`、
南北竿 `119.80-120.20E, 26.00-26.40N`。這些是報告圖的 display extent，
不是分析用 bbox；龜山與南北竿都採四個 0.1 度格距，避免 3 格圖面顯得偏小，
貢寮則維持可完整涵蓋岬角與龜山島的 0.5 度 display extent。放大圖預設
`--zoom-target-arrows 300`，保留足夠箭頭密度呈現局地流向。

目前 1 km GeoJSON QC 第一幀可用以下指令重畫：

```bash
UV_CACHE_DIR=work/uv-cache MPLCONFIGDIR=work/matplotlib-cache \
  uv run python3 scripts/plot_ocm_clean_region_maps.py \
  --input-dir outputs/ocm_2025_01_taiwan_1km_geojson_qc \
  --output-dir outputs/ocm_2025_01_taiwan_1km_geojson_qc/figures \
  --land-geojson data/geojson/twCounty2010.geo.json \
  --layer-index -1 \
  --time-index 0 \
  --zoom-coordinate-tick-interval 0.1
```

主要輸出檔名：

```text
surface_layer_047_first_frame_four_region_equal_bbox_clean.png
surface_layer_047_first_frame_guishan_zoom_clean.png
surface_layer_047_first_frame_gongliao_zoom_clean.png
surface_layer_047_first_frame_lienchiang_nangan_beigan_zoom_clean.png
surface_layer_047_first_frame_four_region_equal_bbox_clean.json
```

若圖面要直接用於正式報告，且需要消除小島或岬角陸地上仍可見流場箭頭的視覺疑慮，
使用報告安全版腳本 `scripts/plot_ocm_report_safe_region_maps.py`。此腳本保留舊
乾淨圖流程與既有 `.npy` 中間檔，不回寫 `mask.npy`；它會在繪圖階段用指定岸線
GeoJSON 重新建立 cell-overlap 報告視覺遮罩，排除落在陸地 cell 的箭頭 anchor，
並在箭頭上方再覆蓋向量陸地 polygon，避免箭頭線段因長度伸進南北竿、龜山島或
貢寮岬角等陸域。

報告安全版第一幀範例：

```bash
UV_CACHE_DIR=work/uv-cache MPLCONFIGDIR=work/matplotlib-cache \
  uv run python3 scripts/plot_ocm_report_safe_region_maps.py \
  --input-dir outputs/ocm_2025_01_taiwan_1km_geojson_qc \
  --output-dir outputs/ocm_2025_01_taiwan_1km_geojson_qc/figures_report_safe_exact_coastline \
  --land-geojson data/coastline/taiwan_exact_coastline.geojson \
  --layer-index -1 \
  --time-index 0 \
  --zoom-coordinate-tick-interval 0.1
```

報告安全版輸出檔名會使用 `_report_safe` 後綴，避免覆蓋舊版 `_clean` 成果；
sidecar JSON 會記錄使用的岸線 GeoJSON、額外移除的報告遮罩格點數、每張圖的
有效箭頭數、實際顯示的邊界刻度，以及「原始 `mask.npy` 未被修改」的語意。

若需要和主圖相同範圍、相同時間與相同岸線遮罩，但不顯示四個 flow-domain 視覺框，
可加入 `--hide-main-region-boxes`。此參數只會略過主圖 PNG 上的半透明 bbox 與外框，
不會改變 `mask.npy`、流速箭頭抽樣、岸線遮罩或任何輸入資料；輸出檔名會改用
`surface_layer_047_first_frame_no_region_bbox_report_safe.png` 與對應 JSON，方便和
原本 `surface_layer_047_first_frame_four_region_equal_bbox_report_safe.png` 並存。

## 4. 串接全年 2D GIF

若 12 個月份的同一種 2D GIF 都已完成，可用 `concat_ocm_year_gifs.py` 直接把每月
GIF 依月份順序接成年度 GIF。此做法不重跑前處理、不重畫每月影格，適合先快速產生
全年連續動畫：

```bash
UV_CACHE_DIR=work/uv-cache \
  uv run python3 scripts/concat_ocm_year_gifs.py \
  --year 2025 \
  --suffix taiwan_10km_3h \
  --figure-name surface_speed_elev_anomaly_quiver.gif \
  --fps 2
```

預設會讀取：

```text
outputs/ocm_2025_01_taiwan_10km_3h/figures/surface_speed_elev_anomaly_quiver.gif
...
outputs/ocm_2025_12_taiwan_10km_3h/figures/surface_speed_elev_anomaly_quiver.gif
```

並輸出：

```text
outputs/ocm_2025_year_taiwan_10km_3h/figures/surface_speed_elev_anomaly_quiver.gif
outputs/ocm_2025_year_taiwan_10km_3h/figures/surface_speed_elev_anomaly_quiver.manifest.json
```

若要串接其它 2D 圖，只改 `--figure-name`。例如原始水位檢查圖：

```bash
UV_CACHE_DIR=work/uv-cache \
  uv run python3 scripts/concat_ocm_year_gifs.py \
  --year 2025 \
  --suffix taiwan_10km_3h \
  --figure-name surface_speed_elev_quiver.gif \
  --fps 2
```

此年度 GIF 是「每月 GIF 接起來」的快速成果。每個月份原本的色階、標題與
`elev_anomaly` 月平均基準會維持各月設定；若需要全年統一色階或全年平均水位異常，
應另外用 12 個月份的 `.npy` 中間檔重畫年度圖。
若 `imageio` 讀取某些 GIF 影格時混用 RGB 與 RGBA channel，工具會先把影格合成為
RGB 後再串接；這只處理透明通道差異，不會改變月份圖的高寬與流場內容。

## 5. 選用：產生三維示意圖

若需要靜態 3D 示意圖，再額外執行以下指令。這一步會輸出 `flow_field_3d.png`；
若只想快速驗證 2D 動畫，可先跳過。

```bash
UV_CACHE_DIR=work/uv-cache MPLCONFIGDIR=work/matplotlib-cache \
  uv run python3 scripts/visualize_ocm_month.py \
  --input-dir outputs/ocm_2025_01_taiwan_10km_3h \
  --output-dir outputs/ocm_2025_01_taiwan_10km_3h/figures \
  --make-3d \
  --three-d-layers 0,16,32,-1 \
  --three-d-time-index 0 \
  --three-d-xy-step 3
```

近表層 3D 時間動畫需要前處理時有輸出 `zcor.npy`，也就是前處理曾使用
`--include-zcor-time` 或 `INCLUDE_ZCOR_TIME=1`。此動畫會比 2D GIF 更耗時，建議在
主要 2D 圖完成後再產生：

```bash
UV_CACHE_DIR=work/uv-cache MPLCONFIGDIR=work/matplotlib-cache \
  uv run python3 scripts/visualize_ocm_month.py \
  --input-dir outputs/ocm_2025_01_taiwan_10km_3h \
  --output-dir outputs/ocm_2025_01_taiwan_10km_3h/figures \
  --make-3d-animation \
  --three-d-layers 32,40,-1 \
  --three-d-frame-stride 4 \
  --three-d-xy-step 4 \
  --vertical-exaggeration 0.02 \
  --fps 2
```

輸出內容包含：

- `surface_speed_elev_anomaly_quiver.gif`：由 `--surface-elev-anomaly-animation` 產生，是主要研究分析圖；底圖色彩代表同一格點相對月平均的 η 水位異常。
- `surface_speed_elev_quiver.gif`：由 `--surface-elev-animation` 產生，是原始資料檢查圖；底圖色彩代表未扣平均的 η/elev 自由水面高度。
- `bottom_layer_000_horizontal_current_speed_quiver.gif`、`model_layer_016_horizontal_current_speed_quiver.gif`、`model_layer_032_horizontal_current_speed_quiver.gif`：由 `--layer-animation --layer-indices 0,16,32,-1 --background neutral` 產生，代表多個指定模型垂向層的水平流場。這些 layer index 是 Python 陣列索引，不是固定水深，也不是公尺；實際深度需參考 `zcor_mean.npy`。
- `flow_field_3d.png`：由選用的 `--make-3d --three-d-layers 0,16,32,-1` 指令產生，代表同一時間點的多個模型層三維示意。`0` 是底部附近模型層，`-1` 通常是表層，`16` 與 `32` 是中間指定層。圖中的 z 軸使用 `zcor_mean.npy` 並套用垂向縮放，因此是結構示意，不是真實比例的三維場景。
- `flow_field_3d_time_layers_032_040_047.gif`：由 `--make-3d-animation --three-d-layers 32,40,-1` 產生，使用 `zcor.npy` 的逐時垂向座標，因此水面與所選 layer 會隨時間上下變動。此動畫刻意選近表層 layer，避免深海底層把 z 軸尺度拉大而看不出 1 公尺等級的水位起伏。

動畫圖面判讀：

- `surface_speed_elev_anomaly_quiver.gif` 的底圖色彩代表 `η'` 水位異常，單位為公尺；正負號代表相對該格點月平均水位的升降。
- `surface_speed_elev_quiver.gif` 的底圖色彩代表原始 `η/elev` 自由水面高度，單位為公尺；此圖主要用於資料檢查。
- η 類 colorbar 會明確標出實際繪圖資料的 `min` 與 `max`；這些上下限直接由
  `elev.npy` 或由 `elev.npy` 推算出的 `η'` 有效格點取得，不使用自訂範圍或
  百分位裁切。若資料範圍跨過 0，colorbar 會同時標示 0。
- 深藍色箭頭代表有效格點的水平流向與流速大小；方向表示流向，長度可和右下角 m/s 參考箭頭比例尺比較，以判讀實際流速量級。若 `speed/u/v` 任一分量缺值，該格點不畫箭頭。
- 淡灰色區域代表該 layer 在該水平位置沒有有效資料，常見原因是該模型層位於局部海底以下或插值後為 NaN；淡灰色不是低流速，也不應解讀為靜水。
- 灰褐色格點與細灰色邊界線代表 `mask.npy=False` 的非海域位置，可能來自原始 mesh 外、GeoJSON 陸域遮罩或其它無效格點。這些陸地標記會覆蓋在 `η/elev` 底圖之上，目的是避免澎湖、綠島、蘭嶼等小島被 `RdBu_r` 色階的近零淡色吃掉；灰褐色本身不代表水位、流速或缺值大小。

流速箭頭比例尺與 98 百分位縮放：

- 2D 圖中的 `vmax` 不是資料最大流速，也不是底圖 colorbar 上限；它是同一段動畫、
  同一個 layer、有效海域格點的水平流速大小 `speed=sqrt(u^2+v^2)` 第 98 百分位。
  計算前會先把 `mask.npy=False` 的陸地、原始 mesh 外格點，以及 `speed/u/v`
  任一分量缺值的位置排除，避免無效資料影響箭頭長度。
- 第 98 百分位的意義是：把有效流速由小到大排序後，約 98% 的有效流速小於或等於
  此值，約 2% 的最大流速高於此值。它提供一個「代表性高流速」作為箭頭縮放基準。
- 不直接使用最大值，是因為海流資料可能有少數局部強流、邊界插值尖峰、瞬間極端值
  或資料雜訊。若用最大值縮放，絕大多數箭頭會被壓得過短，圖面難以判讀主流向與
  空間差異。使用第 98 百分位可保留大部分高流速尺度，同時降低少數極端值主導圖面的風險。
- 選 98 而不是 100，是為了避免最大值主導；選 98 而不是較低的 90 或 95，則是為了
  仍保留強流區的尺度，不讓高流速區箭頭過度放大。這是視覺化上的保守折衷，適合
  報告中說明「箭頭長度使用穩定、可比較的代表性高流速縮放」。
- Matplotlib `quiver` 的 `scale` 設為 `max(vmax * 8, 0.1)`。同一個動畫所有時間幀
  共用同一個 `vmax`，因此相同 m/s 的箭頭在不同時間會有相同視覺長度，不會因每一幀
  自動縮放而產生誤導。
- 右下角的 m/s 參考箭頭由 `quiverkey` 繪製，並綁定同一個 `quiver` 物件，所以比例尺
  與主圖箭頭使用完全相同的縮放規則。參考箭頭的標示值不是任意指定，而是先取
  `0.5 * vmax` 作為目標，再轉成易讀的 `1/2/5 × 10^n` 數值，例如 `0.6 m/s`
  會標成 `1 m/s`，方便讀者比較箭頭長度。
- 以目前一月台灣 10 km / 3 小時表層第一幀檢查圖為例，整段表層資料的
  `vmax_98pct=1.22541 m/s`，`0.5 * vmax=0.612705 m/s`，轉成易讀刻度後圖上
  顯示 `1 m/s` 參考箭頭。圖中箭頭若接近比例尺長度，可判讀為約 `1 m/s`；
  若約為比例尺一半，則約為 `0.5 m/s`。

重要參數意義：

- `--input-dir`：讀取 `preprocess_ocm_month.py` 產生的月資料中間檔。
- `--output-dir`：輸出 GIF 與 PNG 的資料夾。
- `--layer-index`：指定單一 layer 動畫使用哪一個模型垂向層。未提供 `--layer-indices` 時，`--layer-animation` 會使用此參數；預設值是 `-1`，通常代表表層。
- `--surface-elev-anomaly-animation`：輸出表層流速箭頭搭配 `η'` 水位異常底圖，是建議的主要研究圖。`η'` 目前定義為每個格點扣除該月平均 `elev`。
- `--surface-elev-animation`：輸出表層流速箭頭搭配原始 `η/elev` 底圖，主要用於確認模式水位輸出。
- `--layer-indices`：指定多個 2D layer 動畫要輸出的模型層，逗號分隔，可混用正索引與負索引。例如 `0,16,32,-1` 會輸出底層、中間層與表層；多層流場比較建議搭配 `--background neutral`。
- `--all-layers`：輸出每一個模型垂向層的 2D GIF。此選項會讓工作量約等於單層動畫乘上 layer 數；以本專案 48 層、248 幀資料為例，會繪製 11,904 張暫存 PNG 並合成 48 個 GIF，因此只建議在需要完整垂向檢查時使用。
- `--target-arrows`：控制 2D 動畫每幀目標箭頭數，預設為 `1000`。數值越大，抽樣間距越小、箭頭越密；新版圖面以箭頭長度與 m/s 參考箭頭比例尺代表流速大小，因此預設比早期版本更密。
- `--background`：控制一般 2D layer 動畫底圖。`neutral` 是固定海域底色；`elev` 使用 `elev.npy` 的 η 自由水面高度；`elev_anomaly` 會先扣除每個格點月平均 η。正式表層水位研究建議使用上方兩個專用旗標，讓研究圖與檢查圖分開產生。使用 `elev` 或 `elev_anomaly` 前，前處理必須加上 `--include-elev`。
- `--three-d-layers`：指定 3D 示意圖要畫哪些模型層，逗號分隔，可混用正索引與負索引。
- `--three-d-time-index`：指定 3D 示意圖使用哪個時間步。`0` 代表月資料中的第一個 3 小時抽樣時間。
- `--make-3d-animation`：輸出 3D 時間動畫，必須有 `zcor.npy`。此動畫使用逐時 zcor，不會用 `zcor_mean.npy` 假裝水位變動。
- `--three-d-frame-stride`：3D 時間動畫的時間降採樣。範例使用 `4`，代表 248 個 3 小時時間步會輸出約 62 幀，方便快速檢視。
- `--three-d-xy-step`：3D 箭頭的水平抽樣間距。數值越大，箭頭越稀疏，圖面越清楚。
- `--frame-stride`：視覺化階段的時間降採樣。`1` 代表使用所有已前處理時間步；本專案 10 km / 3 小時一月份資料共有 248 幀。
- `--fps`：GIF 每秒幀數，只影響播放速度，不改變原始資料時間間隔。
fps 說明, 播放時間大概會變成：
fps 8：約 31 秒
fps 4：約 62 秒
fps 2：約 124 秒
fps 1：約 248 秒

## 擴充到整年資料

一月份流程穩定後，建議維持月份為單位逐步處理與檢查。Server 端的單月、多月補跑、
背景執行、畫圖與監看指令請見 [`README_SERVER.md`](README_SERVER.md)。

年度研究區域分割建議在月資料中間檔上計算特徵，例如月平均流速、主流向、季節變化、渦度、散度、垂直剪切與粒子停留時間。這些特徵比單純影片更適合後續分群與區域邊界判讀。

### Server 完整台灣周邊 1 km 表層流場產品

本專案另提供一條針對簡報展示與年度備查的完整台灣周邊流程。它直接讀取 Server
`/CWA-OCM/2024` 與 `/CWA-OCM/2025` 內的原始日 NetCDF，使用與 1 km 報告圖相同的
經度 `[119, 123]`、緯度 `[20, 27]` 範圍，將原始 SCHISM/UGRID 非結構網格插值到約
1 km 的規則經緯度格點，固定使用模型表層 `layer 047`，再以每 6 小時一幀產生年度
動畫。所有中間檔與動畫均寫入下列新建、隔離的資料夾；既有
`/data/OCM-Preprocessed-Data/preprocessed` 不會被覆寫或修改：

```text
/data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/
```

#### 時間軸修復與缺日處理

原始檔案的 `time` units 在部分月份存在日期偏移，因此年度產品不把 NetCDF 的
`time` units 當作最終日期來源，而是以檔名 `YYYYMMDD_schout.nc` 的日期為權威，
將每日四個輸出時間固定定義為 `01:00、07:00、13:00、19:00 UTC`。這個規則保留原始
檔案內的 1、7、13、19、25 小時相對位置，同時消除跨檔案的日期偏移。

2025 年原始資料缺少以下 10 個日期：
`2025-03-03`、`2025-03-14`、`2025-03-19`、`2025-05-23`、`2025-07-21`、
`2025-11-02`、`2025-11-05`、`2025-11-19`、`2025-11-20`、`2025-11-27`。
產品仍建立完整的規則時間軸；缺日的 40 個 6 小時時間步以相鄰有效時間步逐格線性
補值，並在 `source_valid.npy`、`imputed.npy` 與 `time_status.npy` 中留下可追溯標記。
這些補值是為了維持年度動畫時間連續，不應被解讀為提供者重新補回的觀測或模式輸出。

#### Server 執行方式

所有長時間工作均應在 Server `tmux` 中執行，以避免 SSH 斷線中止前處理或渲染：

```bash
tmux new -s ocm_surface_2024_2025
```

前處理指令如下；`--output-dir` 必須指向新的空資料夾，程式會拒絕把結果寫入非空
資料夾，以降低誤覆寫風險：

```bash
python3 /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/scripts/preprocess_ocm_surface_year.py \
  --source-root /CWA-OCM \
  --output-dir /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/intermediate_v2 \
  --years 2024 2025 \
  --bbox 119 123 20 27 \
  --target-resolution-km 1 \
  --layer-index 47 \
  --time-step-hours 6 \
  --first-hour-utc 1 \
  --land-geojson /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/config/taiwan_exact_coastline.geojson \
  > /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/logs/formal_preprocess_v2.log 2>&1
```

輸出的 `u_surface.npy`、`v_surface.npy` 與 `speed_surface.npy` 形狀為
`time, latitude, longitude = 2924, 780, 409`，其中 `speed_surface.npy` 的單位是
`m/s`，定義為 `sqrt(u_surface**2 + v_surface**2)`。`eta/elev` 沒有參與本產品的
底圖；`eta` 是自由水面高度，若未來要製作水位動畫，必須另外設計單位為公尺的色階，
不可與流速的 `m/s` 色階混用。

前處理完成後，使用下列指令產生三個用途不同的 GIF。預設不顯示標題、區域名稱、
日期、時間或狀態文字；版面只保留 1 km 報告圖使用的經緯度軸、刻度與四個研究區域
透明內部的框線。陸地使用中度灰米色，僅用於辨識 mask=False 的位置；中性趨勢版
保留深藍框線，固定流速備查版使用低飽和磚紅框線。固定流速備查版額外保留固定刻度的
數值 colorbar，並標示
`流速(公尺/秒)`，以便核對不同時間的色彩是否使用相同的 `m/s` 尺度：

```bash
MPLCONFIGDIR=/tmp/ocm-surface-mplconfig PYTHONUNBUFFERED=1 \
python3 /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/scripts/visualize_ocm_surface_year.py \
  --input-dir /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/intermediate_v2 \
  --output-dir /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_v8 \
  --fps 4 \
  --trend-frame-stride 8 \
  --target-arrows 2600 \
  --quiver-scale-multiplier 16 \
  --dpi 110 \
  > /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/logs/formal_render_v8.log 2>&1
```

輸出檔案與時間長度如下：

- `global_trend_surface_layer_047_four_regions.gif`：每 48 小時取一幀，約 366 幀；
  `fps=4` 時約 92 秒，作為簡報主動畫，用於呈現兩年大趨勢與四區域流向變化；
  每幀目標約 2600 支箭頭、抽樣步距約 11 格，較前版約 1800 支箭頭更容易辨識
  大範圍流場的連續性；箭頭縮放倍數降為 16，使箭頭適度加長。
- `surface_layer_047_speed_fixed_scale_four_regions.gif`：與主動畫相同時間抽樣，
  背景使用全資料共用的固定流速色階；色彩代表 `m/s`，不是 `eta`，箭頭改為白色
  以提高在 viridis 色階上的可見度，並以整部 GIF 共用的固定 256 色盤避免色條跳動。
- `annual_full_surface_layer_047_6h.gif`：保留完整 6 小時時間軸的年度備查動畫，
  共約 2924 幀；`fps=4` 時約 12.2 分鐘，不建議直接放入簡報播放。
- `surface_layer_047_speed_fixed_scale_first_frame.png`：固定色階版第一幀，供報告、
  色階與岸線品質查核。
- `global_trend_surface_layer_047_four_regions.mp4`、
  `surface_layer_047_speed_fixed_scale_four_regions.mp4` 與
  `annual_full_surface_layer_047_6h.mp4`：由同一 v8 GIF 轉製的 H.264 MP4，維持
  原本的影格順序、4 fps、解析度與無標題版面；影片不含音訊，可直接插入簡報。
- `animation_manifest.json`：記錄輸入版本、時間抽樣、固定色階上限、箭頭參數、
  影格數與輸出檔名。

固定流速備查版的色階不是 data-derived：產品規格固定為 `0.0–2.0 m/s`，刻度固定為
`0.0、0.5、1.0、1.5、2.0`，每個刻度顯示一位小數，色條標籤為
`流速(公尺/秒)`；固定色階版箭頭為白色，中性趨勢版箭頭採用深藍系中深藍青色
`#1f5f83`，以和深灰海岸線區分。
為避免垂直色條壓縮等比例主圖，固定流速版使用獨立 colorbar 軸與較寬的
`7.2×11.0` 英吋畫布（中性版為 `6.6×11.0` 英吋）；主圖資料框高度與經緯度比例
維持一致，右側另保留單位標籤的安全留白。這個尺寸差異只屬版面配置，不代表資料
解析度或地理範圍不同。三個版本的上側版面邊界由 `top=0.935` 調整為 `top=0.9675`，
使上方空白約縮減一半；固定版的 colorbar 同步使用相同上界，避免色條與主圖上下錯位。
三個版本的 X/Y 軸標籤統一使用中文「經度」與「緯度」，數值刻度仍保留實際地理座標。
GIF 編碼採單一 global palette 且不使用抖動量化，因此所有動畫幀的顏色均可直接比較；
超過 `2.0 m/s` 的罕見強流
會以色階最深端呈現。前處理 metadata 另記錄 P98/P99 與抽樣最大值，但那些數值只
用於箭頭長度與資料品質查核，不會改變背景色階上下限。

正式動畫成果已同步回本機主專案的下列路徑，便於簡報與報告直接取用；此資料夾由
`.gitignore` 排除，不會把大型 GIF/MP4 自動納入版本控制：

```text
outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations/
```

本次新版（固定版主圖與色條重新配置、三版本上方空白縮減、XY 軸改為中文經緯度、中性版箭頭改為 `#1f5f83`、中度灰米色陸地、區域框取消填色、固定流速版採低飽和磚紅框線）先在 Server
的
`/data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_v8/`
獨立完成並驗證，再同步至上述本機路徑；Server 原有 `animations/`、`animations_v2/`、
`animations_v3/`、`animations_v4/`、`animations_v5/`、`animations_v6/` 與 `animations_v7/` 均保留作為歷次成果備查，沒有刪除或覆寫。

## 四海域 modal-context display-only coastline v2

本版正式動畫以既有簡報相同的六層聯合水柱 SVD 結果，補充四個海域的表層流場與
模態重建關聯。`svd_source_unchanged: true`，且 `coastline_correction_scope:
visualization_only`：正式 SVD 的模態、時間係數、流場變異百分比與達累積 90% 所需
模態數均不重算、不改寫。exact coastline 只在渲染階段阻止陸地上的流速色塊與箭頭被
展示，並以向量 polygon 覆蓋底圖；因此本版不宣稱重新定義 SVD、殘差或 RMSE。

正式 SVD 根目錄為：

```text
/home/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/2026-08-13_water_column_four_regions/water_column_svd/
```

四區正式結果是該目錄下的既有 A–D run。原始表層流場使用其 metadata 追溯出的同源
cache `/data/OCM-Preprocessed-Data/preprocessed/ocm_surface`；完整 1 km 產品只用於
時間交集與展示網格查核。重建仍為 `mean + Σ(mode_u/v_mps_per_raw_pc × pc.npy)`，
`pc_standardized.npy` 只在內部用於模態 1 相位案例選取，不能與 raw-PC 模態係數混用。

精確岸線檔案為：

```text
/Users/mustlab/Workspace/OCM-NetCDF-Visualizer/data/coastline/taiwan_exact_coastline.geojson
/data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/config/taiwan_exact_coastline.geojson
```

兩者 SHA-256 均為
`9e2e0ac9bc527aca87d89332cd428fdcb776eefbf94a85dd70f887f729b95fdd`；資料為
FeatureCollection，1,905 個原始 features、1,912 個可 rasterize polygon groups。
岸線 rasterize 採保守 cell-overlap 語意：cell center、四角或 ring vertex 接觸
polygon 即標示 `coastline_land_mask`，洞環扣除。真實陸地、分析域外與逐時缺值在
渲染與 QA 中保持不同語意。

早期建立的 `2026-08-27_coastline_corrected_v2` SVD 與 C pilot comparison 只作
diagnostic／方法敏感度檢查，保留於版本化目錄，絕不納入正式動畫 manifest，也不作為
正式動畫來源。正式輸出目錄另行保留，且不覆寫 `animations_svd_modal_context_v1`。

正式渲染命令如下；SERVER 的長時間工作必須在 `tmux` 內執行：

```bash
MPLCONFIGDIR=work/matplotlib-cache PYTHONDONTWRITEBYTECODE=1 \
python3 scripts/visualize_ocm_svd_modal_context.py \
  --svd-base /home/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/2026-08-13_water_column_four_regions/water_column_svd \
  --surface-cache-base /data/OCM-Preprocessed-Data/preprocessed/ocm_surface \
  --full-product-dir /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/intermediate_v2 \
  --coastline-geojson /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/config/taiwan_exact_coastline.geojson \
  --output-dir /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2 \
  --regions A,B,C,D --fps 4 --width 864 --height 1080 --target-arrows 420
```

正式影片各取兩段不重疊的 7 日、6 小時代表視窗，每段 28 個資料影格，片頭與片尾各
停留約 1 秒，總計 64 幀、4 fps、約 16 秒。上下兩面板分別為原始表層流場與前 n 個
模態重建流場；四區共用固定 0–2.2 m/s 色階與真正代表 1 m/s 的箭頭圖例。畫面只使用
簡報名詞：標題為 `海域 A（東北角）`、`海域 B（新竹外海）`、`海域 C（後灣海域）`、
`海域 D（連江海域）`；相位列為「模態 1 時間係數：正／負相位案例」；面板 caption、
單一完整的 `流速（公尺／秒）` 直式色條標籤與中文 `經度`／`緯度` 均置於不遮蔽資料圖框的位置。
觀眾可見畫面不使用 `PC`、`K`、`K90`、`解釋變異` 或非簡報同義詞；manifest 與 README
可保留 `pc.npy`、`pc_standardized.npy`、K90 等內部資料／演算法名稱供追溯。

正式輸出位於：

```text
SERVER  /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/
LOCAL   /Users/mustlab/Workspace/OCM-NetCDF-Visualizer/outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/
```

其中包含四支 MP4、poster、正／負相位 QA 幀、首／中／末 contact sheet、exact coastline
疊圖、A–D 陸地稽核摘要、`animation_manifest.json` 與本輸出目錄的 README。manifest
記錄正式 SVD 路徑、精確共同時間、mask 語意、岸線雜湊、色階、箭頭尺度、文字規格、
ffprobe 資訊與輸出雜湊；正式版不需要也不產生 v1/v2 SVD comparison。

QA 必須同時通過 ffprobe 技術檢查、exact coastline 地理檢查與畫面文字檢查：H.264/
`yuv420p`、864×1080、4 fps、無音訊、首中末幀、land finite render=0、land-arrow=0、
分析域外不被標作陸地、兩岸線雜湊一致，以及四區標題／caption／相位／色條／箭頭圖例
完全符合規格。最終 `qa.all_passed=true` 已在本機以抽取影格及 contact sheet 驗證；
仍建議在簡報端以右側約 35% 尺寸人工確認可讀性。PPTX 未修改，播放方式仍由簡報端
設定為點擊播放、不循環。

### 四海域時間內插對照版

另產製 `formal_abcd_slide_aligned_v4_temporal_interpolated` 與 v3 並列比較。此版仍唯讀
使用同一正式六層聯合水柱 SVD、同一岸線與同一組代表時段；只在 renderer 的展示 payload
階段，以相鄰 12 小時真實錨點的 `alpha=0.5` 線性內插形成 6 小時中間畫面。每段仍輸出
28 幀、四區仍為 64 幀／4 fps／約 16 秒，因此不增加影片時間，也不提高原始資料時間解析度。
輸出目錄為：

```text
outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_slide_aligned_v4_temporal_interpolated/
```

`temporal_interpolation_v3_comparison.json` 記錄同一選窗、座標與正式 SVD 的對照，
`temporal_interpolation_frame_transition_qa.json` 記錄解碼影格的相鄰 RGB 轉換差異。此
內插版適合簡報視覺展示；若要逐筆保留每個 6 小時觀測影格，應使用未啟用內插的 v3。

### 四海域純原始表層流場動畫—緊湊 2×2 版

依簡報展示版面另製作只含「原始流場」單一主圖的 A–D 四區動畫，供四支影片在
簡報中以 2×2 配置排列。此版本不顯示模態重建、相位、UTC 或內部 SVD 文字，
不修改 PPTX，也不覆寫既有 v3／v4 雙面板成果；本次依使用者要求直接覆寫同一目錄
中的舊 `0.0–2.2 m/s` 純原始版，未另保留舊色階副本。renderer 為
`scripts/render_ocm_raw_surface_only.py`，正式輸出目錄如下：

```text
SERVER /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_compact_v1_temporal_interpolated/
LOCAL  /Users/mustlab/Workspace/OCM-NetCDF-Visualizer/outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_compact_v1_temporal_interpolated/
```

四區均採 864×500 px、4 fps、64 幀、16 秒、H.264／`yuv420p`、無音訊。畫面主標題
分別為 `海域 A（東北角） 原始流場`、`海域 B（新竹外海） 原始流場`、
`海域 C（後灣海域） 原始流場` 與 `海域 D（連江海域） 原始流場`；固定流速色階為
`0.0–0.8 m/s`，刻度每 `0.2 m/s`（`0.0, 0.2, 0.4, 0.6, 0.8`），色條標籤為直式
`流速（公尺／秒）`。主圖內的 `1 公尺／秒` 是由同一 raw-surface QuiverKey、
`U=1.0` 產生，非手工繪製的裝飾符號。

`0.0–0.8 m/s` 是為與簡報靜態圖一致而設定的展示色階；超過 0.8 m/s 的原始
速度會在色階頂端飽和，原始資料、箭頭方向與箭頭尺度不被截斷或修改。renderer
可透過 `--fixed-speed-vmax` 與 `--speed-tick-step` 明確記錄此展示契約。

為使四支影片縮放後放入簡報 2×2 版面時外觀一致，主圖採共用的固定 axes rectangle
`[0.10, 0.14, 0.75, 0.74]`，色條與主圖共用 y/height；不再讓 Matplotlib 依 A–D
不同的經緯度範圍比例自動縮短 C/D 的圖框。這是 presentation-only 的畫面配置，
不改變各區 xlim/ylim、原始 u/v 或正式 SVD；代價是各區地理縱橫顯示比例可能有有限
差異，因此不應以此版面作距離或角度量測。manifest 的 `qa.layout_consistency` 會
以最終像素 bbox 驗證四區主圖與色條一致，且額外檢查經度軸名稱未裁切。

資料仍唯讀使用既有正式六層聯合水柱 SVD 所追溯的同源表層 cache；SVD 模態與時間
係數不被改寫，`coastline_correction_scope=visualization_only`。精確 GeoJSON 岸線
只在渲染階段遮蔽真實陸地內的流速色塊與箭頭，再以無描邊高解析度 polygon 覆蓋；
保守 1 km raster land mask 僅供資料／地理稽核，不作可見階梯海岸線。啟用
`--temporal-interpolation` 時，以相鄰 12 小時真實錨點線性產生展示用 6 小時中間場，
維持 64 幀與 16 秒，不增加原始資料時間解析度。

SERVER 長時間渲染需在 `tmux` 內執行，例如：

```bash
tmux new -s ocm_raw_only_compact_v1
PYTHONPATH=/tmp/ocm_raw_surface_only_v1 \
/home/mustlab/Workspace/OCM-SVD-Analysis/.venv/bin/python \
/tmp/ocm_raw_surface_only_v1/render_ocm_raw_surface_only.py \
  --svd-base /home/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/2026-08-13_water_column_four_regions/water_column_svd \
  --surface-cache-base /data/OCM-Preprocessed-Data/preprocessed/ocm_surface \
  --coastline-geojson /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/config/taiwan_exact_coastline.geojson \
  --output-dir /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_compact_v1_temporal_interpolated \
  --regions A,B,C,D --fps 4 --width 864 --height 500 --target-arrows 420 \
  --quiver-scale-multiplier 20 --fixed-speed-vmax 0.8 --speed-tick-step 0.2 \
  --temporal-interpolation --overwrite
```

每區另保留 poster、正／負時間窗代表幀、首／中／末 contact sheet、
`animation_manifest.json` 與 `qa/local_validation.json`。本機驗證程式
`scripts/validate_ocm_raw_surface_only.py` 以可用的 `ffprobe` 重新檢查影片編碼、
尺寸、影格數、時長、無音訊、PNG 尺寸、精確岸線 SHA-256、固定版面 metadata 與
輸出雜湊；本機同步成果的 `qa.all_passed=true`。SERVER 端未安裝 `ffprobe`，其原始
遠端 QA 快照會保留 `ffprobe_not_found`，不代表同步後影片本身編碼失敗。

### 四海域純原始表層流場動畫—連續 30 日／30 秒平滑版

為觀察較長時間範圍且避免每 6 小時直接切換造成跳動，另以
`scripts/render_ocm_raw_surface_only_continuous.py` 產生四區同一段連續長時窗版本。
本版固定使用 120 個實際觀測時間位置（每 6 小時一筆，時槽覆蓋 30 日），輸出
120 幀、4 fps、精確 30 秒；不增加片頭／片尾、不加入虛擬影格，也不是七日內插。
四區共用的實際 UTC 時窗、source-valid／非 imputed 驗證結果寫在輸出 manifest。

為降低相鄰觀測在影片中的瞬間變化，僅對中間 118 個展示位置套用
`0.25×前一筆 + 0.50×當筆 + 0.25×下一筆` 的三點時間平滑，首尾影格保留原始值。
平滑只存在於 renderer 的展示 payload，不寫回原始 OCM cache，不改動正式 SVD，
也不代表新增觀測資料；若需逐筆觀看原始觀測，可在同一 renderer 關閉
`--temporal-smoothing`。

本版四區使用同一個固定 quiver scale 作為比例尺與流場箭頭的展示示意，不依 A–D
各自流速分布調整；各區 p95 僅保留作為診斷資訊。比例尺文字與箭頭同步縮小，
以避免 D 區因局地流速較低而出現特別長的比例尺箭頭。

本版輸出目錄為：

```text
SERVER /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_continuous_30d_30s_temporal_smoothed_v1/
LOCAL  /Users/mustlab/Workspace/OCM-NetCDF-Visualizer/outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_continuous_30d_30s_temporal_smoothed_v1/
```

畫面仍採四區一致的 864×500 px、固定 0.0–0.8 m/s 色階與 0.2 m/s 刻度，並保留
精確 vector coastline、無音訊 H.264／`yuv420p`、poster、起中末 QA 幀、contact
sheet、manifest 與輸出 README。SERVER 端若沒有 `ffprobe`，需將成果同步回本機後
使用 `scripts/validate_ocm_raw_surface_only.py` 重新完成編碼與檔案 QA。

### 四海域純原始表層流場動畫—30 秒無內嵌主標題版

為便於在 PPTX 內後製海域名稱，另以同一個 30 秒、120 個實際 6 小時觀測時段與
三點展示平滑設定產生無內嵌主標題版本。此變體只移除 figure-level 主標題，保留
主圖、色條、座標軸、比例尺、岸線、時間窗與四區共用 quiver scale；上方安全區仍
保留約 30 px，供簡報端放置可編輯文字；主圖／色條上緣已向上延伸以回收多餘白底。
含標題版本不會被覆寫。

```text
SERVER /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_continuous_30d_30s_temporal_smoothed_no_title_v1/
LOCAL  /Users/mustlab/Workspace/OCM-NetCDF-Visualizer/outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_continuous_30d_30s_temporal_smoothed_no_title_v1/
```

renderer 以 `--no-show-title` 控制此變體，manifest 會記錄
`title_visible=false` 與 `title_removed_for_editable_ppt_overlay=true`。本版仍採
864×500 px、4 fps、120 幀、精確 30 秒、H.264／`yuv420p`、無音訊；固定流速色階
仍為 0.0–0.8 m/s、每 0.2 m/s 一格。無標題版與含標題版可直接逐幀對照，差異僅限
主標題是否寫入畫面像素。

### 四海域純原始表層流場動畫—60 秒雙版本

依同一版面標準再製作 60 秒版本：每支影片使用 240 個實際 6 小時觀測時段，
4 fps、精確 60 秒；中間展示位置仍採三點時間平滑，未增加虛擬影格或修改原始資料。
含標題版與無標題版各自存放於獨立目錄，兩者使用相同時間窗、色階、岸線、主圖框及
固定跨區 quiver scale。無標題版保留約 30 px 上方安全區，供 PPTX 放置可編輯標題。

```text
SERVER /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_continuous_60d_60s_temporal_smoothed_v1/
LOCAL  /Users/mustlab/Workspace/OCM-NetCDF-Visualizer/outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_continuous_60d_60s_temporal_smoothed_v1/
SERVER /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_continuous_60d_60s_temporal_smoothed_no_title_v1/
LOCAL  /Users/mustlab/Workspace/OCM-NetCDF-Visualizer/outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_continuous_60d_60s_temporal_smoothed_no_title_v1/
```

兩組 manifest 均記錄 `source_frame_count=240`、`expected_duration_seconds=60.0`；含
標題版記錄 `title_visible=true`，無標題版記錄 `title_visible=false`。

### 四海域純原始表層流場動畫—2024–2025 全部共同實測時段無標題版

依使用者要求，另以同一套無標題 raw-only 版面完整播放 2024–2025 兩年資料。全臺
6 小時產品共有 2,924 個理論時間格；其中 40 格為 imputed，且與四區正式 SVD、同源
surface cache 精確交集後，四區共同可追溯的 source-valid、非 imputed 實測位置為
2,848 格。影片保留這些實測時間位置與原始時間缺口，不以補值影格填洞；4 fps 下每區
輸出 2,848 幀、精確 712 秒（約 11 分 52 秒）。

為降低每 6 小時直接切換的跳動感，中間位置採相鄰實測場的三點展示平滑；缺口前後的
邊界影格不跨缺口平滑。平滑僅存在 renderer 記憶體與 MP4，不回寫 OCM cache、不改寫
正式 SVD，也不代表新增觀測。完整資料版的缺口、排除數量、起訖 UTC、輸出雜湊與
QA 均記錄於該目錄 `animation_manifest.json`。

```text
SERVER /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_full_2024_2025_temporal_smoothed_no_title_v1/
LOCAL  /Users/mustlab/Workspace/OCM-NetCDF-Visualizer/outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_full_2024_2025_temporal_smoothed_no_title_v1/
```

檔案名稱使用 `region_<A-D>_<name>_raw_surface_only_full_2024_2025_712s_temporal_smoothed_no_title.mp4`，
以免與 30／60 秒長時窗版本混淆。版面、固定 0.0–0.8 m/s 色階、0.2 m/s 刻度、共用
quiver scale、exact coastline 展示遮罩及無標題上方安全區均沿用 60 秒無標題版。

### 四海域純原始表層流場動畫—2024–2025 全期間 3 分鐘重採樣無標題版

為在簡報中保留兩年起訖趨勢、同時避免約 12 分鐘完整版播放時間過長，使用
`scripts/render_ocm_raw_surface_only_continuous.py` 將四區共同的 2,848 個實測
source-valid／非 imputed 觀測作為時間錨點，均勻重採樣為 720 個展示影格。輸出為
4 fps、精確 180 秒；正常相鄰 6 小時觀測間以 u/v 線性時間內插，已知資料缺口不
跨越內插而採最近有效觀測保持。這不是新增觀測、不是七日內插，也不改寫原始
surface cache 或正式 SVD。

```text
SERVER /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_full_2024_2025_3min_temporal_resampled_no_title_v1/
LOCAL  /Users/mustlab/Workspace/OCM-NetCDF-Visualizer/outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_full_2024_2025_3min_temporal_resampled_no_title_v1/
```

本版保留無內嵌主標題的 30 px 上方安全區，固定 0.0–0.8 m/s 色階、0.2 m/s 刻度、
四區共用 quiver scale、exact coastline 向量陸地與 864×500 px 版面。每區 MP4
均為 H.264／`yuv420p`、720 幀、無音訊；輸出 README、manifest、poster、起／中／
末影格與 contact sheet 同時記錄 source/display frame count、實際日曆時間步階、
內插／缺口保持數量、固定比例尺與 QA。同步回本機後，`qa/local_validation.json`
已以本機 ffprobe 完成編碼與檔案稽核。

### 四海域純原始表層流場動畫—三日每小時實測版

本版先以靜態圖供影片渲染前審核四區共同版面與動態 UTC 標示，後續已完成四區影片。
時窗由 2024–2025 的既有
6 小時產品以四區流場變化指標自動挑選，但實際畫面資料重新讀取原始 SCHISM
NetCDF 的 24 小時日檔；每小時一幀，共 72 個原始觀測時段，完全不使用時間內插、
三點平滑、淡化或預測值。中間時刻預覽為 `2024-11-01 13:00 UTC`。

```text
SERVER hourly product  /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/intermediate_hourly_three_day_actual_v1/
SERVER selection       /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/hourly_window_selection_v1/selection_three_day_hourly.json
SERVER preview         /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_3day_hourly_actual_2fps_v1/preview/
SERVER formal output   /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_3day_hourly_actual_2fps_v1/
LOCAL preview          /Users/mustlab/Workspace/OCM-NetCDF-Visualizer/outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_3day_hourly_actual_2fps_v1/preview/
LOCAL formal output    /Users/mustlab/Workspace/OCM-NetCDF-Visualizer/outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/formal_abcd_raw_surface_only_3day_hourly_actual_2fps_v1/
```

預覽畫布為四區 2×2 審核圖及 C 區放大圖；正式影片使用相同 renderer、
864×500 px、2 fps，72 幀對應精確 36 秒。畫面上方只置中顯示隨幀更新的 UTC
日期時間，海域名稱與「原始流場」標籤留給簡報端另行製作；色階固定 0.0–0.8 m/s、每 0.2 m/s 一格，四區共用比例尺與
exact coastline 展示遮罩。這裡的潮汐變化仍是原始表層流場的觀測序列，不等同於
已完成潮汐調和分離。

## Smoke Test 是什麼

Smoke test 是軟體開發裡的「冒煙測試」：用最小資料量快速確認整條流程是否能跑通。此專案的 smoke test 通常只取 1 個日檔、較疏時間抽樣或較粗解析度，用來檢查 NetCDF 讀取、缺值處理、水平插值、輸出 `.npy` 與繪圖是否會失敗。

Smoke test 的目的不是產生研究用結論，而是及早發現環境、路徑、變數名稱、維度順序或繪圖流程問題。當 smoke test 成功後，才適合改用完整月份、較細解析度與較密時間抽樣產生正式 demo。

## 專案資料夾與檔案說明

### 根目錄

- `README.md`：專案主要說明文件，記錄本機資料假設、共通執行流程、輸出格式與限制。後續若修改 bbox、深度層、時間抽樣或輸出策略，應同步更新此文件。
- `README_SERVER.md`：server 專用操作文件，集中記錄 VS Code Remote SSH、server 資料路徑、月份批次、背景執行、監看與畫圖指令。
- `pyproject.toml`：Python 專案與相依套件設定。此檔定義需要的 Python 版本與 `numpy`、`scipy`、`netCDF4`、`matplotlib`、`imageio` 等套件，讓環境可用 `uv sync` 重建。
- `uv.lock`：由 `uv` 產生的鎖定檔，用於固定相依套件版本。移到伺服器或其他電腦時，保留此檔可提高環境重建的一致性。
- `.venv/`：本機 Python 虛擬環境。這是可重建資料夾，不建議納入版本控制；若搬移後不能執行，可重新跑 `UV_CACHE_DIR=work/uv-cache uv sync`。

### `scripts/`

- `scripts/inspect_ocm_netcdf.py`：檢查單一 OCM/SCHISM NetCDF 檔案結構，輸出維度、變數、屬性、時間軸與主要欄位範圍。用途是在正式前處理前確認 `hvel`、`zcor`、`depth`、`time` 等資料是否符合腳本假設；其 JSON 輸出目前只作為人工檢查紀錄，不會被月前處理腳本自動讀取。
- `scripts/preprocess_ocm_month.py`：月資料前處理主程式。它會直接讀取單月所有 `*_schout.nc` 日檔，選取台灣鄰近 bbox，優先使用原始 `SCHISM_hgrid_face_nodes` 元素連結把非結構網格節點資料插值到規則經緯度格點；若來源檔缺少 face connectivity，才退回 Delaunay 重心權重。選用 `--include-elev` 時會輸出 `elev.npy`，選用 `--land-geojson` 時會在靜態 mesh mask 之外再扣除 GeoJSON 陸域 polygon，輸出包含 `u/v/speed/elev/zcor_mean/bathymetry/mask` 等中間檔；目前不接受 inspect JSON 作為輸入。
- `scripts/visualize_ocm_month.py`：月資料視覺化主程式。它讀取前處理輸出的 `.npy` 與 JSON metadata，產生可選中性底圖或 η/elev 水位底圖的表層流場 GIF、指定垂向層 GIF，以及含海底面參照的 3D 稀疏箭頭示意圖。
- `scripts/plot_ocm_clean_region_maps.py`：投影片後製用乾淨區域圖腳本。它讀取既有月資料 `.npy`，輸出只含經緯度刻度與 `Longitude`/`Latitude` 的 PNG：四個等物理尺寸 flow-domain bbox 主圖，以及龜山島、貢寮、南北竿三張獨立放大圖。此腳本不取代一般動畫流程，也不修改 `visualize_ocm_month.py` 的標題、圖例或比例尺設計。
- `scripts/preprocess_ocm_surface_year.py`：Server 完整年度表層前處理程式。它以原始檔名日期修復部分 NetCDF `time` units 的偏移，建立固定 6 小時時間軸，讀取單一表層 `layer 047`，以 UGRID face connectivity 優先插值到完整台灣周邊約 1 km 規則格點，套用逐時 wet/dry 與指定 GeoJSON 岸線遮罩，並對缺日時間步作可追溯的相鄰時間線性補值。輸出與既有月資料格式隔離，不能直接覆寫非空資料夾。
- `scripts/visualize_ocm_surface_year.py`：完整年度表層 GIF 渲染程式。它讀取年度前處理 `.npy`，以 1 km 報告圖相同的四個研究區域 bbox、較稀疏且縮短的箭頭與固定圖面產生簡報趨勢版、固定流速色階備查版及完整 6 小時年度版。預設輸出不含標題、區域名稱、時間、狀態或其它說明文字；`--show-region-labels` 僅供人工診斷，不應用於正式簡報檔。
- `scripts/visualize_ocm_svd_modal_context.py`：四海域六層聯合 SVD 模態—表層分量關聯動畫 renderer。它只讀取既有正式 SVD 與 metadata 追溯出的同源 `preprocessed/ocm_surface` cache，再以精確 UTC epoch-ns 交集選取 source-valid、非 imputed 的 6 小時資料；上半部繪製原始表層流場，下半部依 `mean + Σ(mode_per_raw_pc × pc)` 產生前 n 個模態重建流場，並以 exact coastline 只做展示遮罩。輸出 864×1080、4 fps、H.264/yuv420p MP4、poster、相位 QA 幀與 manifest。程式與文件保留「六層聯合 SVD 模態之表層分量」的科學語意，不可解讀成 surface-only SVD；畫面名詞遵循簡報原文，內部仍可追溯 `pc.npy`、`pc_standardized.npy` 與 K90。
- `scripts/render_ocm_raw_surface_only_continuous.py`：四海域純原始表層流場長時窗 renderer。它只讀取正式 SVD 所追溯的同源 surface u/v cache；可逐一播放全部共同 6 小時實測時段，或以 `--display-frame-count` 將完整日曆時間範圍重採樣為固定展示影格。重採樣模式在正常相鄰觀測間對 u/v 做 display-only 線性時間內插，資料缺口不跨越內插；輸出 864×500、固定 0.0–0.8 m/s 色階、共用 quiver scale、H.264/yuv420p MP4、poster、contact sheet、manifest 與 QA。它不產生 SVD 重建、不改寫原始 cache，也不把展示內插值視為新增觀測。
- `scripts/select_ocm_raw_hourly_window.py`：從既有 2024–2025 全臺 6 小時產品挑選四區共同三日代表時窗。它只計算候選評分與日檔存在性，不建立 hourly 資料、不修改來源；實際影格由原始 NetCDF 前處理重新產生。
- `scripts/render_ocm_raw_surface_hourly.py`：四區三日每小時原始表層流場 renderer。它驗證 72 個連續 hourly source-observed 時段，使用 exact coastline vector 只做展示階段陸地覆蓋，輸出動態 UTC 靜態預覽或 2 fps H.264 影片；不包含 SVD 重建，也不做時間內插或平滑。
- `scripts/coastline_utils.py`：v2 共用精確岸線工具。它驗證 GeoJSON SHA-256、統計 feature/polygon 數，並以 cell center、四角或 ring vertex 接觸 polygon 的保守 1 km cell-overlap 規則建立 `(lat, lon)` `coastline_land_mask`；洞環會扣除，輸入網格與速度值不被修改，向量外環則供最高 z-order 陸地覆蓋使用。
- `scripts/audit_ocm_svd_coastline.py`：A–D exact-land 科學稽核工具。它把真實陸地、分析幾何域外、模型靜態域外、surface feature 未納入與逐時 invalid 分開統計，並跨 2024–2025 全部同源 surface cache 計算 exact-land 有限 u/v、valid pair 與速度統計；已完成的污染診斷只作方法敏感度記錄，不會改寫正式 SVD 或正式動畫來源。
- `scripts/build_coastline_corrected_surface_inputs.py`：建立不覆寫原始 cache 的 corrected surface SVD 診斷輸入。它只複製 grid、將 `mask_static.npy` 改為 `original & ~coastline_land_mask`，兩年 monthly arrays 以唯讀 symlink 保留原始來源；此輸入僅供污染／方法敏感度檢查，不是正式動畫輸入。
- `scripts/create_coastline_corrected_svd_configs.py`：把既有 water-column v1 config 複製到版本化診斷目錄，新增 coastline correction metadata 與 exact GeoJSON 雜湊；不改寫 v1 config，也不啟動正式重算。
- `scripts/compare_ocm_svd_coastline_versions.py`：以同一精確 source-valid/non-imputed 6 小時交集比較診斷版與既有版 K90、前四模態流場變異百分比、raw-PC 重建與 PC1 正/負相位視窗；結果只供方法敏感度追溯，不納入正式動畫 manifest。
- `scripts/make_coastline_svd_qa_overlays.py`：輸出各區 exact coastline mask＋1 km 網格＋正/負相位 raw/K90 代表影格疊圖，並自動統計 exact-land finite render 與箭頭數。
- `scripts/validate_ocm_svd_modal_context.py`：四海域 modal-context MP4 QA 工具。它不修改來源資料，使用 `ffprobe` 確認單一 H.264 video stream、無音訊、`yuv420p`、尺寸、fps、時長與 poster，再以 `imageio` 抽取首／中／末幀建立 contact sheet；v2 另驗證 coastline SHA-256、polygon count、land-mask cell count、exact-land finite render=0、land-arrow=0 與分析域外未被標作陸地。人工仍需確認投影片縮放後的可讀性。
- `scripts/concat_ocm_year_gifs.py`：年度 GIF 串接工具。它讀取已完成的每月 GIF，依月份順序輸出年度 GIF 與 manifest JSON；此工具不重畫影格，也不改變每月原本的色階或水位異常基準。
- `scripts/run_ocm_2025_year.sh`：月份批次入口，實際 server 執行方式與環境變數範例請見 `README_SERVER.md`。
- `scripts/summarize_ocm_year.py`：月份/年度摘要檢查工具。它讀取每個月的 `monthly_summary.json` 與 `.npy` header，輸出 JSON/CSV 摘要，檢查缺檔、shape、月份格點一致性與日檔缺日。
- `scripts/__pycache__/`：Python 自動產生的 bytecode cache。這是可刪除、可重建資料夾，不影響專案邏輯。

### `outputs/`

- `outputs/inspect_20250101.json`：`inspect_ocm_netcdf.py` 對 `20250101_schout.nc` 的檢查摘要。它用來記錄原始資料的維度、變數屬性、時間單位與抽樣後的數值範圍，方便人工確認前處理假設；目前不會被 `preprocess_ocm_month.py` 讀取或合併到月資料輸出。
- `outputs/ocm_2025_01_smoke/`：小型 smoke test 輸出。通常只處理少量日檔、較疏時間步或較粗解析度，用於快速確認讀檔、插值與繪圖流程是否能跑通。
- `outputs/ocm_2025_01_daily/`：一月份每日抽樣的主要 demo 輸出。此資料夾可作為後續其它月份處理的月資料格式範本。
- `outputs/ocm_2025_01_taiwan_10km_3h/`：台灣鄰近海域經度 `[119, 123]`、緯度 `[20, 27]` 的 10 km / 3 小時抽樣月資料輸出。這是目前較細解析度與較密時間抽樣的主要 demo 設定。
- `outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_v1/`：歷史 v1 四海域簡報嵌入用動畫成果，保留作為比較與備查，不覆寫、不以新版文字規格取代。
- `outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/`：正式 v2 display-only coastline 成果目錄。它沿用 2026-08-13 既有正式 SVD，只在繪圖階段套用 exact coastline 遮罩；包含四支 864×1080、4 fps、H.264/yuv420p MP4、poster、正／負相位幀、首／中／末 contact sheet、exact coastline 疊圖、land audit、manifest 與輸出專用 README。早期 corrected-SVD C pilot／comparison 若存在，均與正式 manifest 隔離。

### 月資料輸出檔案

以下檔案會出現在 `outputs/ocm_2025_01_smoke/`、`outputs/ocm_2025_01_daily/`、`outputs/ocm_2025_01_taiwan_10km_3h/` 等月資料資料夾中：

- `lon.npy`：規則格點的經度一維陣列，單位為度。對應輸出陣列最後一個維度。
- `lat.npy`：規則格點的緯度一維陣列，單位為度。對應輸出陣列倒數第二個維度。
- `sigma.npy`：垂向層代表值。若原始 `sigma` 無法提供有效層座標，會退回層索引；實際深度判讀應優先參考 `zcor_mean.npy`。
- `time_iso.npy`：抽樣後的時間字串陣列，用於動畫標題與年度時間序列合併。
- `u.npy`：插值後東西向流速，形狀為 `time, layer, lat, lon`，單位通常為 m/s。
- `v.npy`：插值後南北向流速，形狀為 `time, layer, lat, lon`，單位通常為 m/s。
- `speed.npy`：水平流速大小，由 `sqrt(u^2 + v^2)` 計算，形狀為 `time, layer, lat, lon`。
- `elev.npy`：自由水面高度 η，由原始 `elev(time, node)` 插值而來，形狀為 `time, lat, lon`。此檔沒有 layer 維度，單位通常為公尺；視覺化時作為底圖色階，不能與 `speed` 的 m/s 色階混用。
- `zcor_mean.npy`：每個垂向層在每個格點的月平均實際 z 座標，單位通常為公尺。此檔主要用於 3D 示意，不代表固定深度重採樣。
- `zcor.npy`：逐時實際 z 座標，形狀為 `time, layer, lat, lon`，單位通常為公尺。此檔由 `--include-zcor-time` 產生，主要供 `--make-3d-animation` 呈現自由水面與模型層位隨時間變動。
- `bathymetry.npy`：插值後水深，單位通常為公尺，正值代表海床深度。
- `mask.npy`：規則格點靜態有效海域遮罩。`True` 代表該格點位於原始水體 mesh 且有可用海域資料，`False` 代表原始 mesh 外、插值無效、GeoJSON 陸域或不應用於統計。若來源 NetCDF 有 `wetdry_elem(time, elem)`，前處理會另外依逐時乾濕狀態把乾出元素的 `u/v/speed/zcor` 設為 NaN；視覺化腳本會同時依 `mask.npy` 與 NaN 排除中性海域底圖和 quiver 箭頭。
- `monthly_summary.json`：月資料 metadata 與統計摘要，包含年月、bbox、輸入檔案、時間抽樣、格點大小、層數、時間起訖與流速統計。

### 圖像輸出資料夾

以下檔案位於月資料資料夾下的 `figures/`：

- `surface_speed_elev_anomaly_quiver.gif`：表層 `η'` 水位異常底圖加箭頭動畫，是建議的主要研究圖，用於觀察台灣鄰近表層流向、流速變化與相對水位變化的關聯。底圖 colorbar 是 `η'` 公尺，箭頭長度搭配右下角 m/s 參考箭頭才代表流速大小。
- `surface_speed_elev_quiver.gif`：表層原始 `η/elev` 底圖加箭頭動畫，是原始模式輸出檢查圖，用於確認自由水面高度範圍與空間分布是否合理。
- `bottom_layer_000_horizontal_current_speed_quiver.gif`、`model_layer_016_horizontal_current_speed_quiver.gif`、`model_layer_032_horizontal_current_speed_quiver.gif`：指定垂向層的中性底圖加箭頭動畫，用於比較不同深度或模型層的流場差異。實際輸出層數由 `--layer-indices` 或 `--all-layers` 決定；圖中的淡灰色區域代表該 layer 沒有有效資料，不代表低流速。
- `flow_field_3d.png`：3D 稀疏箭頭示意圖，使用 `zcor_mean.npy` 放置不同垂向層，並加上半透明海底面作為深度參照。
- `flow_field_3d_time_layers_032_040_047.gif`：近表層 3D 時間動畫，使用 `zcor.npy` 放置每一幀的水面與模型層位；標題中的 `surface mean z` 是該幀表層平均水位，方便檢查水位逐時變化。
- `outputs/ocm_2025_year_taiwan_10km_3h/figures/*.gif`：由 `concat_ocm_year_gifs.py` 將每月 GIF 串接後的年度 GIF。旁邊的 `*.manifest.json` 記錄來源月份、每月幀數、輸出 fps 與圖面尺寸。

### `work/`

- `work/uv-cache/`：`uv` 的本專案快取位置，避免套件管理工具寫入使用者家目錄造成權限問題。此資料夾可重建。
- `work/matplotlib-cache/`：Matplotlib 字型與設定快取位置，避免繪圖時寫入不可用的家目錄 cache。此資料夾可重建。

## 限制

- 目前水平插值優先使用原始 UGRID/SCHISM face-node connectivity，因此 `mask.npy`
  會保留原始網格的陸地洞與海岸邊界；只有來源檔缺少 `SCHISM_hgrid_face_nodes`
  時才退回 Delaunay 線性插值，超出來源節點凸包的格點會保留為缺值。
- 若來源檔提供 `wetdry_elem`，前處理會採用 SCHISM 常見慣例 `0=wet、非 0=dry`
  逐時遮蔽乾出元素。`mask.npy` 不會變成三維逐時遮罩；乾濕變化已反映在
  `u/v/speed/zcor` 的 NaN 與 `monthly_summary.json` 的 `wetdry_elem` 區段。
- `--land-geojson` 是靜態陸域遮罩，適合修正本島、離島與行政區 polygon 內的
  假水體格點；它不是逐時潮汐乾濕遮罩，也不會取代 `wetdry_elem`。遮罩演算法
  假設 GeoJSON 使用 WGS84 `[lon, lat]` 座標，且 polygon 不跨日期變更線。為了
  在不降低解析度、不增加月資料檔案大小的情況下保留小島，GeoJSON rasterize
  會檢查格點 cell 的中心、四角與 GeoJSON ring 頂點是否互相接觸，而不是只檢查
  中心點是否落在 polygon 內。
- 垂向維度先沿用原檔層索引或 sigma 代表值，尚未重採樣到固定水深層。
- 三維示意圖使用靜態稀疏箭頭，適合先期判讀；若要高品質互動流線，建議後續輸出 VTK/XDMF 給 ParaView 或 pyParaOcean。
- 大範圍、高解析度、全時間步、全垂向層會產生大量中間檔，整年處理時應以月份為單位分批執行。
