# OCM 海流資料三維流場示意與月資料動畫流程

本專案以中央氣象署 OCM NetCDF 月資料為起點，建立可從「單月資料」擴充到「整年時間序列」的處理與視覺化管線。現階段先針對本機一月份資料夾 `/Users/mustlab/Downloads/CWA-OCM/2025/01` 實作，後續只要替換輸入路徑與年月參數，即可批次處理伺服器上的全年資料。

## 目標

- 讀取每日 `*_schout.nc` NetCDF 檔案，盤點時間、水平網格、垂向層、海流與水位資料。
- 將非結構網格 OCM/SCHISM 節點資料，插值到研究區域的規則經緯度格點。
- 產出月資料中間檔，作為年度動畫、三維示意圖與後續研究區域分割的共同輸入。
- 產出表層流場動畫、固定垂向層動畫與三維流場示意圖。

## 後續實作規格

- `docs/NEXT_PHASE_ENHANCED_SPEC.md`：記錄下一階段加強版實作規格，包含 `dahv` 深度平均流場、`elev` 水位疊圖、`vertical_velocity` 垂直流速、溫鹽密度特徵、乾濕遮罩、年度批次處理與驗收標準。後續開發新欄位或新動畫前，應先依此 spec 拆分任務與更新 README。

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
- `elev`：自由水面高度，單位通常為公尺。

若全年資料的變數名稱或維度順序不同，應先用檢查腳本確認，再調整變數參數或程式中的維度解析邏輯。

## 安裝

請先切到專案根目錄；後續所有相對路徑都以此資料夾為基準：

```bash
cd /Users/mustlab/Workspace/OCM-NetCDF-Visualizer
```

```bash
UV_CACHE_DIR=work/uv-cache uv sync
```

## 1. 檢查一月份 NetCDF 結構

```bash
UV_CACHE_DIR=work/uv-cache uv run python3 scripts/inspect_ocm_netcdf.py \
  --input /Users/mustlab/Downloads/CWA-OCM/2025/01/20250101_schout.nc \
  --output outputs/inspect_20250101.json
```

此步驟會輸出維度、變數、屬性、座標範圍與主要流場變數資訊，用於人工確認後續前處理是否讀對資料。`outputs/inspect_20250101.json` 是檢查紀錄與資料假設參考，不是 `preprocess_ocm_month.py` 的程式輸入；目前前處理腳本不會自動讀取這個 JSON。

## 2. 前處理一月份資料

以下範例先取台灣鄰近海域，經度 `[119, 123]`、緯度 `[20, 27]`，並使用 10 km 解析度與 3 小時抽樣。這個設定比早期 smoke test 更細，適合作為一月份正式 demo 與後續全年批次處理的基準設定：

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
  --include-zcor-time
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
- `zcor_mean.npy`：每層在每個格點的月平均垂向座標。
- `zcor.npy`：逐時垂向座標，形狀為 `time, layer, lat, lon`，用於需要呈現水位與 sigma/z 層位隨時間變動的 3D 動畫。此檔約與單一流速分量同等大小，只有加上 `--include-zcor-time` 才會輸出。
- `bathymetry.npy`：插值後水深。
- `mask.npy`：有效海域遮罩。新版前處理會優先依原始 `SCHISM_hgrid_face_nodes`
  水平元素判斷靜態海域，避免 Delaunay 凸包把陸地洞補成流場；若原始資料提供
  `wetdry_elem`，逐時乾出元素會在輸出的 `u/v/speed/zcor` 中直接寫成 NaN。若提供
  `--land-geojson`，落在 GeoJSON 陸域 polygon 內的格點也會從 `mask.npy` 扣除，
  並在 `bathymetry/u/v/speed/zcor` 寫成 NaN。
- `monthly_summary.json`：流速統計、時間範圍、輸入檔案與參數。

## 3. 產生 2D 動畫

以下指令會重跑目前建議的主要 2D 成果圖，不會輸出 `flow_field_3d.png`。
`--surface-animation` 固定輸出表層，`--layer-animation --layer-indices 0,16,32,-1`
輸出多個代表模型垂向層。新版 2D 圖面使用固定淡藍色底圖，底圖不代表流速大小；
流速大小由深藍色箭頭長度表示。`--target-arrows 1000` 會讓箭頭比早期版本更密，
適合目前台灣 10 km / 3 小時 demo。

```bash
UV_CACHE_DIR=work/uv-cache MPLCONFIGDIR=work/matplotlib-cache \
  uv run python3 scripts/visualize_ocm_month.py \
  --input-dir outputs/ocm_2025_01_taiwan_10km_3h \
  --output-dir outputs/ocm_2025_01_taiwan_10km_3h/figures \
  --surface-animation \
  --layer-animation \
  --layer-indices 0,16,32,-1 \
  --frame-stride 1 \
  --fps 8 \
  --target-arrows 1000
```

範例. 使用這組指令即可跑整月、10 km、台灣周邊 bbox，並套用 GeoJSON 陸地遮罩，輸出到指定位置：

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
  --include-zcor-time
```

產生 2D 表層動畫：

```bash
uv run python3 scripts/visualize_ocm_month.py \
  --input-dir outputs/ocm_2025_01_taiwan_10km_geojson_qc \
  --output-dir outputs/ocm_2025_01_taiwan_10km_geojson_qc/figures \
  --surface-animation \
  --layer-animation \
  --layer-indices 0,16,32,-1 \
  --frame-stride 1 \
  --fps 2 \
  --target-arrows 1000
```

## 4. 選用：產生三維示意圖

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

近表層 3D 時間動畫範例：

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

- `surface_layer_047_horizontal_current_speed_quiver.gif`：由 `--surface-animation` 產生，固定使用最後一個模型垂向層，通常代表最上層或表層。檔名中的 `047` 是解析後的 layer index；不同資料若 layer 數改變，此數字也會跟著改變。
- `bottom_layer_000_horizontal_current_speed_quiver.gif`、`model_layer_016_horizontal_current_speed_quiver.gif`、`model_layer_032_horizontal_current_speed_quiver.gif`：由 `--layer-animation --layer-indices 0,16,32,-1` 產生，代表多個指定模型垂向層的水平流場。這些 index 是 Python 陣列索引，不是固定水深，也不是公尺；實際深度需參考 `zcor_mean.npy`。
- `flow_field_3d.png`：由選用的 `--make-3d --three-d-layers 0,16,32,-1` 指令產生，代表同一時間點的多個模型層三維示意。`0` 是底部附近模型層，`-1` 通常是表層，`16` 與 `32` 是中間指定層。圖中的 z 軸使用 `zcor_mean.npy` 並套用垂向縮放，因此是結構示意，不是真實比例的三維場景。
- `flow_field_3d_time_layers_032_040_047.gif`：由 `--make-3d-animation --three-d-layers 32,40,-1` 產生，使用 `zcor.npy` 的逐時垂向座標，因此水面與所選 layer 會隨時間上下變動。此動畫刻意選近表層 layer，避免深海底層把 z 軸尺度拉大而看不出 1 公尺等級的水位起伏。

動畫圖面判讀：

- 固定淡藍色底圖只代表該 layer 在該水平位置有有效海域資料，不代表流速大小。
- 深藍色箭頭代表有效格點的水平流向與流速大小；方向表示流向，長度表示流速相對強弱。若 `speed/u/v` 任一分量缺值，該格點不畫箭頭。
- 淡灰色區域代表該 layer 在該水平位置沒有有效資料，常見原因是該模型層位於局部海底以下或插值後為 NaN；淡灰色不是低流速，也不應解讀為靜水。

重要參數意義：

- `--input-dir`：讀取 `preprocess_ocm_month.py` 產生的月資料中間檔。
- `--output-dir`：輸出 GIF 與 PNG 的資料夾。
- `--layer-index`：指定單一 layer 動畫使用哪一個模型垂向層。未提供 `--layer-indices` 時，`--layer-animation` 會使用此參數；預設值是 `-1`，通常代表表層。
- `--layer-indices`：指定多個 2D layer 動畫要輸出的模型層，逗號分隔，可混用正索引與負索引。例如 `0,16,32,-1` 會輸出底層、中間層與表層，檔名會分別包含 `bottom_layer_000`、`model_layer_016`、`model_layer_032`、`surface_layer_047`。
- `--all-layers`：輸出每一個模型垂向層的 2D GIF。此選項會讓工作量約等於單層動畫乘上 layer 數；以本專案 48 層、248 幀資料為例，會繪製 11,904 張暫存 PNG 並合成 48 個 GIF，因此只建議在需要完整垂向檢查時使用。
- `--target-arrows`：控制 2D 動畫每幀目標箭頭數，預設為 `1000`。數值越大，抽樣間距越小、箭頭越密；新版圖面以箭頭長度代表流速大小，因此預設比早期版本更密。
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

一月份流程穩定後，可用相同腳本逐月處理伺服器資料：

```bash
for m in 01 02 03 04 05 06 07 08 09 10 11 12; do
  UV_CACHE_DIR=work/uv-cache uv run python3 scripts/preprocess_ocm_month.py \
    --input-dir /server/path/CWA-OCM/2025/$m \
    --output-dir outputs/ocm_2025_$m \
    --year 2025 \
    --month $m \
    --domain-id taiwan-surrounding \
    --bbox 119.0 123.0 20.0 27.0 \
    --target-resolution-km 10 \
    --time-stride 3
done
```

年度研究區域分割建議在月資料中間檔上計算特徵，例如月平均流速、主流向、季節變化、渦度、散度、垂直剪切與粒子停留時間。這些特徵比單純影片更適合後續分群與區域邊界判讀。

## Smoke Test 是什麼

Smoke test 是軟體開發裡的「冒煙測試」：用最小資料量快速確認整條流程是否能跑通。此專案的 smoke test 通常只取 1 個日檔、較疏時間抽樣或較粗解析度，用來檢查 NetCDF 讀取、缺值處理、水平插值、輸出 `.npy` 與繪圖是否會失敗。

Smoke test 的目的不是產生研究用結論，而是及早發現環境、路徑、變數名稱、維度順序或繪圖流程問題。當 smoke test 成功後，才適合改用完整月份、較細解析度與較密時間抽樣產生正式 demo。

## 專案資料夾與檔案說明

### 根目錄

- `README.md`：專案主要說明文件，記錄資料假設、執行流程、輸出格式、整年擴充方式與限制。後續若修改 bbox、深度層、時間抽樣或輸出策略，應同步更新此文件。
- `pyproject.toml`：Python 專案與相依套件設定。此檔定義需要的 Python 版本與 `numpy`、`scipy`、`netCDF4`、`matplotlib`、`imageio` 等套件，讓環境可用 `uv sync` 重建。
- `uv.lock`：由 `uv` 產生的鎖定檔，用於固定相依套件版本。移到伺服器或其他電腦時，保留此檔可提高環境重建的一致性。
- `.venv/`：本機 Python 虛擬環境。這是可重建資料夾，不建議納入版本控制；若搬移後不能執行，可重新跑 `UV_CACHE_DIR=work/uv-cache uv sync`。

### `scripts/`

- `scripts/inspect_ocm_netcdf.py`：檢查單一 OCM/SCHISM NetCDF 檔案結構，輸出維度、變數、屬性、時間軸與主要欄位範圍。用途是在正式前處理前確認 `hvel`、`zcor`、`depth`、`time` 等資料是否符合腳本假設；其 JSON 輸出目前只作為人工檢查紀錄，不會被月前處理腳本自動讀取。
- `scripts/preprocess_ocm_month.py`：月資料前處理主程式。它會直接讀取單月所有 `*_schout.nc` 日檔，選取台灣鄰近 bbox，優先使用原始 `SCHISM_hgrid_face_nodes` 元素連結把非結構網格節點資料插值到規則經緯度格點；若來源檔缺少 face connectivity，才退回 Delaunay 重心權重。選用 `--land-geojson` 時，會在靜態 mesh mask 之外再扣除 GeoJSON 陸域 polygon，輸出包含 `u/v/speed/zcor_mean/bathymetry/mask` 等中間檔；目前不接受 inspect JSON 作為輸入。
- `scripts/visualize_ocm_month.py`：月資料視覺化主程式。它讀取前處理輸出的 `.npy` 與 JSON metadata，產生表層流場 GIF、指定垂向層 GIF，以及含海底面參照的 3D 稀疏箭頭示意圖。
- `scripts/__pycache__/`：Python 自動產生的 bytecode cache。這是可刪除、可重建資料夾，不影響專案邏輯。

### `outputs/`

- `outputs/inspect_20250101.json`：`inspect_ocm_netcdf.py` 對 `20250101_schout.nc` 的檢查摘要。它用來記錄原始資料的維度、變數屬性、時間單位與抽樣後的數值範圍，方便人工確認前處理假設；目前不會被 `preprocess_ocm_month.py` 讀取或合併到月資料輸出。
- `outputs/ocm_2025_01_smoke/`：小型 smoke test 輸出。通常只處理少量日檔、較疏時間步或較粗解析度，用於快速確認讀檔、插值與繪圖流程是否能跑通。
- `outputs/ocm_2025_01_daily/`：一月份每日抽樣的主要 demo 輸出。此資料夾可作為後續整年批次處理的月資料格式範本。
- `outputs/ocm_2025_01_taiwan_10km_3h/`：台灣鄰近海域經度 `[119, 123]`、緯度 `[20, 27]` 的 10 km / 3 小時抽樣月資料輸出。這是目前較細解析度與較密時間抽樣的主要 demo 設定。

### 月資料輸出檔案

以下檔案會出現在 `outputs/ocm_2025_01_smoke/`、`outputs/ocm_2025_01_daily/`、`outputs/ocm_2025_01_taiwan_10km_3h/` 等月資料資料夾中：

- `lon.npy`：規則格點的經度一維陣列，單位為度。對應輸出陣列最後一個維度。
- `lat.npy`：規則格點的緯度一維陣列，單位為度。對應輸出陣列倒數第二個維度。
- `sigma.npy`：垂向層代表值。若原始 `sigma` 無法提供有效層座標，會退回層索引；實際深度判讀應優先參考 `zcor_mean.npy`。
- `time_iso.npy`：抽樣後的時間字串陣列，用於動畫標題與年度時間序列合併。
- `u.npy`：插值後東西向流速，形狀為 `time, layer, lat, lon`，單位通常為 m/s。
- `v.npy`：插值後南北向流速，形狀為 `time, layer, lat, lon`，單位通常為 m/s。
- `speed.npy`：水平流速大小，由 `sqrt(u^2 + v^2)` 計算，形狀為 `time, layer, lat, lon`。
- `zcor_mean.npy`：每個垂向層在每個格點的月平均實際 z 座標，單位通常為公尺。此檔主要用於 3D 示意，不代表固定深度重採樣。
- `zcor.npy`：逐時實際 z 座標，形狀為 `time, layer, lat, lon`，單位通常為公尺。此檔由 `--include-zcor-time` 產生，主要供 `--make-3d-animation` 呈現自由水面與模型層位隨時間變動。
- `bathymetry.npy`：插值後水深，單位通常為公尺，正值代表海床深度。
- `mask.npy`：規則格點靜態有效海域遮罩。`True` 代表該格點位於原始水體 mesh 且有可用海域資料，`False` 代表原始 mesh 外、插值無效、GeoJSON 陸域或不應用於統計。若來源 NetCDF 有 `wetdry_elem(time, elem)`，前處理會另外依逐時乾濕狀態把乾出元素的 `u/v/speed/zcor` 設為 NaN；視覺化腳本會同時依 `mask.npy` 與 NaN 排除中性海域底圖和 quiver 箭頭。
- `monthly_summary.json`：月資料 metadata 與統計摘要，包含年月、bbox、輸入檔案、時間抽樣、格點大小、層數、時間起訖與流速統計。

### 圖像輸出資料夾

以下檔案位於月資料資料夾下的 `figures/`：

- `surface_layer_047_horizontal_current_speed_quiver.gif`：表層中性底圖加箭頭動畫，用於快速觀察台灣鄰近表層流向與流速變化；底圖色彩不代表流速，箭頭長度才代表流速相對強弱；`047` 代表目前 48 層資料中的最後一層。
- `bottom_layer_000_horizontal_current_speed_quiver.gif`、`model_layer_016_horizontal_current_speed_quiver.gif`、`model_layer_032_horizontal_current_speed_quiver.gif`：指定垂向層的中性底圖加箭頭動畫，用於比較不同深度或模型層的流場差異。實際輸出層數由 `--layer-indices` 或 `--all-layers` 決定；圖中的淡灰色區域代表該 layer 沒有有效資料，不代表低流速。
- `flow_field_3d.png`：3D 稀疏箭頭示意圖，使用 `zcor_mean.npy` 放置不同垂向層，並加上半透明海底面作為深度參照。
- `flow_field_3d_time_layers_032_040_047.gif`：近表層 3D 時間動畫，使用 `zcor.npy` 放置每一幀的水面與模型層位；標題中的 `surface mean z` 是該幀表層平均水位，方便檢查水位逐時變化。

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
  假設 GeoJSON 使用 WGS84 `[lon, lat]` 座標，且 polygon 不跨日期變更線。
- 垂向維度先沿用原檔層索引或 sigma 代表值，尚未重採樣到固定水深層。
- 三維示意圖使用靜態稀疏箭頭，適合先期判讀；若要高品質互動流線，建議後續輸出 VTK/XDMF 給 ParaView 或 pyParaOcean。
- 大範圍、高解析度、全時間步、全垂向層會產生大量中間檔，整年處理時應以月份為單位分批執行。
