# 區域 bbox 決策紀錄

本文紀錄各研究區域使用的 OCM/SCHISM 前處理 bbox。此檔放在 `docs/`，
原因是 bbox 屬於研究設定與資料產品決策，應和一次性 `outputs/` 產物分開保存，
也不應埋在 Python 腳本預設值中，避免後續各區域比較時失去決策脈絡。

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

## 研究區參考圖

五張研究區參考圖統一存放於專案內的 `data/reference/`。文件一律使用以下專案相對路徑，
避免搬移工作目錄或在其他主機重現分析時失去來源圖。參考圖用於確認近岸作業範圍與等深線脈絡，
不直接作為 OCM 前處理輸入，也不能取代 bbox smoke test 的流場檢查。

- 屏東縣國立海洋生物博物館周邊海域：`data/reference/屏東縣國立海洋生物博物館周邊海域.png`
- 宜蘭縣龜山島海域：`data/reference/宜蘭縣龜山島海域.png`
- 新北市貢寮海域：`data/reference/新北市貢寮海域.png`
- 新竹縣外海：`data/reference/新竹縣外海.png`
- 連江縣海域：`data/reference/連江縣海域.png`

## 判定原則與報告使用限制

- bbox 使用 WGS84 經緯度，目的在從既有 OCM/SCHISM 模式結果切出「足以判讀外海流場脈絡」的分析範圍；
  它不是重新執行水動力模式時的開放邊界，也不能解讀成 bbox 外的海流對目標區域完全沒有影響。
- 四向邊界優先涵蓋可能接近目標海域的主流、支流、陸棚交換、渦旋及上游流入路徑，
  再排除與研究問題關聯較低、會明顯增加資料量或干擾圖面判讀的遠端海域與陸地。
- smoke test 統一使用 `2025-01-01T01:00:00` 單一時間點及 `1 km` 規則格點，
  用來確認來源網格覆蓋、陸域遮罩、插值結果及 bbox 是否裁切主要流場；單一時間點不能代表季節、潮週期或極端事件。
- `1 km` 產品適合區域尺度流場判讀，但無法解析港灣、岬角尾流、島嶼背風側、近岸 `5–10 m`
  等深線內的小尺度環流。後續若要評估實際作業區，應另做局部高解析度或巢狀模式分析。
- `--source-margin-deg 0.25` 只用於取得 bbox 外圍的來源節點以穩定插值，並不會擴張輸出 bbox。
- 已確認的 smoke test 成果應視為決策證據保留；若日後改 bbox、解析度、遮罩或抽樣時間，
  必須使用新的輸出資料夾，避免覆寫本紀錄對應的結果。

## 已確認區域

### 區域 1：宜蘭縣龜山島海域

- 狀態：smoke test 與圖面檢視已完成，正式保留目前 bbox 與成果
- 區域名稱：宜蘭縣龜山島、台灣東北外海與東海南部陸棚交換區
- 主要目標：涵蓋龜山島西側近岸作業區，以及可能影響該區的東岸黑潮上游、
  黑潮東側渦旋、台灣東北角陸棚坡折入侵、北台灣沿岸與台灣海峽出口流場。
- 近岸參考圖：`data/reference/宜蘭縣龜山島海域.png`
- 正式 bbox（專案 CLI 順序）：

```text
120.00 125.00 22.50 27.00
```

- GIS / GeoJSON bbox：

```text
[120.00, 22.50, 125.00, 27.00]
```

- 對應 `DOMAIN_ID`：

```text
guishan-northeast-taiwan-outer-current-12000-12500-2250-2700
```

- 實際 1 km 規則格點：`501 x 506`，共 `253,506` 格
- 需要納入的主要影響路徑：
  - 黑潮沿台灣東岸向北流經宜蘭外海，抵達台灣東北角與東海陸棚坡折後轉向東北；
    部分黑潮水會跨越坡折進入陸棚，形成與龜山島外海直接相關的強流、鋒面及湧升背景。
  - 台灣東側向西傳播的氣旋或反氣旋中尺度渦旋可推移黑潮位置並改變跨陸棚入侵強度，
    因此東界不能只框到龜山島或黑潮近岸側。
  - 宜蘭灣水團除近岸黑潮水外，也受到北台灣陸棚水與沿北岸、東北岸流動的沿岸反流影響；
    季風與東北冷渦／冷穹頂會使此混合具有季節差異。
  - 龜山島西側 `5–10 m` 作業海域另受島體遮蔽、尾流、潮流及局部地形控制，但必須放在上述區域背景流下解讀。

- 四向決策邊界：
  - 西界 `120.00E`：涵蓋北台灣及台灣海峽北口，保留陸棚水與沿北岸進入宜蘭灣的可能路徑；
    代價是納入較多台灣陸地與中國東南沿岸邊緣，但 smoke test 的外海判讀仍清楚。
  - 東界 `125.00E`：較龜山島向東約 3 度，越過黑潮近岸側與主軸，保留主軸外側及中尺度渦旋調制空間；
    smoke test 顯示主要向北流並未貼著東界，故具有足夠緩衝。
  - 南界 `22.50N`：向南延伸到台灣東南部外海，保留黑潮抵達宜蘭前約數百公里的上游路徑；
    若只截到 `23.5–24.0N`，只能看到龜山島附近瞬時流向，無法判斷黑潮主軸是如何進入研究區。
  - 北界 `27.00N`：越過台灣東北方主要陸棚坡折、冬季黑潮入侵及回轉區，能追蹤流體向東海陸棚延伸或重新接回主流；
    再往北將逐漸轉為東海更大尺度環流問題，對龜山島近場判讀的增益下降。

- 範圍取捨與排除理由：
  - 本區第一次 smoke test 即採大範圍 `120.00 125.00 22.50 27.00`，結果已完整呈現上游、主軸、陸棚入侵與外側渦旋，未再另外測試縮小 bbox。
  - 西界雖造成有效海域比例只有約 `0.576`，但保留北台灣與台灣海峽北口是研究目的的一部分，不能只用海域比例判定為浪費。
  - 南界未延伸至巴士海峽，因為龜山島的直接上游已由台灣東岸段提供；若研究問題改為黑潮源區或呂宋海峽遙相關，應另建更大分析域，而不是改寫本 bbox。
- 已產生 QC 輸出：

```text
outputs/ocm_2025_01_guishan_bbox_12000_12500_2250_2700_1km_smoke
```

- QC 設定與結果：
  - 原始檔：`/Users/mustlab/Downloads/CWA-OCM/2025/01/20250101_schout.nc`
  - 時間：只取第一個抽樣時間 `2025-01-01T01:00:00`
  - 解析度：`1 km`
  - 有效海域比例：約 `0.576`
  - 全層流速平均：`0.320 m/s`
  - 全層流速第 95 百分位：`0.818 m/s`
  - 全層最大流速：`2.662 m/s`
  - 陸域遮罩：`data/geojson/twCounty2010.geo.json`
  - 輸出資料量：約 `190 MB`（包含 GIF 與第一幀 PNG）

- 主要檢視圖：

```text
outputs/ocm_2025_01_guishan_bbox_12000_12500_2250_2700_1km_smoke/figures/surface_layer_047_horizontal_current_speed_quiver_first_frame.png
outputs/ocm_2025_01_guishan_bbox_12000_12500_2250_2700_1km_smoke/figures/surface_speed_elev_quiver_first_frame.png
outputs/ocm_2025_01_guishan_bbox_12000_12500_2250_2700_1km_smoke/figures/model_layer_032_horizontal_current_speed_quiver_first_frame.png
outputs/ocm_2025_01_guishan_bbox_12000_12500_2250_2700_1km_smoke/figures/model_layer_016_horizontal_current_speed_quiver_first_frame.png
outputs/ocm_2025_01_guishan_bbox_12000_12500_2250_2700_1km_smoke/figures/bottom_layer_000_horizontal_current_speed_quiver_first_frame.png
```

- 圖面判定：
  - 表層與第 32 層可辨識台灣東岸黑潮向北進入研究區，並在台灣東北外海向東海陸棚及東北方轉向。
  - 南界仍保有完整的上游流入剖面，東界未貼近黑潮主軸，北界已超過龜山島東北方的主要轉向區。
  - 西界包含北台灣與台灣海峽北口背景流；雖然陸域比例較高，但不妨礙外海流場判讀。
  - 底層圖在淺陸棚及地形區域出現大量無效格點，屬 sigma 最底層與水深限制，並非 bbox 或插值失敗。

- 報告引用限制：
  - smoke test 是 2025 年 1 月單一時刻，只能證明 bbox 與資料處理可行；黑潮入侵、冷穹頂及宜蘭灣反流具有季節與事件變化，正式結論必須使用較長時間序列。
  - sigma 第 0 層不是固定水深面，淺陸棚及陸地附近的大量無效格點不能解讀為「底層無流」。跨區或跨時間比較時應使用相同層定義，或另做固定深度重映射。
  - `1 km` 產品不能解析原參考圖中龜山島西側 `5–10 m` 等深線內作業區；報告應將本 bbox 稱為「台灣東北外海影響分析域」。

- 報告用決策摘要：本區以龜山島西側作業海域為核心，分析域向南保留台灣東岸黑潮上游、
  向東越過黑潮主軸並涵蓋中尺度渦旋調制區、向北越過東海陸棚坡折與黑潮入侵回轉區、
  向西保留北台灣陸棚水及台灣海峽北口交換。`120.00–125.00E、22.50–27.00N`
  在 smoke test 中完整呈現上游、主軸、陸棚入侵及外側流場，且計算量仍低於屏東分析域，因此正式保留。

- 科學依據：
  - [Hsueh, Wang, and Chern (1992), *The intrusion of the Kuroshio across the continental shelf northeast of Taiwan*](https://doi.org/10.1029/92JC01401)：說明黑潮在台灣東北方跨越陸棚坡折並形成向陸棚入侵支流。
  - [Liu et al. (2014), *The pattern and variability of winter Kuroshio intrusion northeast of Taiwan*](https://doi.org/10.1002/2014JC009879)：說明冬季黑潮入侵路徑、季節差異及陸棚上的次級流束。
  - [Yin et al. (2017), *Impact of mesoscale eddies on Kuroshio intrusion variability northeast of Taiwan*](https://doi.org/10.1002/2016JC012263)：指出東側氣旋與反氣旋渦旋會調制黑潮位置及跨陸棚輸送。
  - [*Interaction of coastal countercurrent in I-Lan Bay with the Kuroshio northeast of Taiwan* (2018)](https://doi.org/10.1016/j.csr.2018.10.012)：說明宜蘭灣同時受北台灣陸棚水、近岸黑潮水與沿岸反流混合影響。

- smoke test 重現命令：

```bash
UV_CACHE_DIR=work/uv-cache \
MPLCONFIGDIR=work/matplotlib-cache \
PYTHONDONTWRITEBYTECODE=1 \
uv run python3 scripts/preprocess_ocm_month.py \
  --input-dir /Users/mustlab/Downloads/CWA-OCM/2025/01 \
  --output-dir outputs/ocm_2025_01_guishan_bbox_12000_12500_2250_2700_1km_smoke \
  --year 2025 \
  --month 1 \
  --domain-id guishan-northeast-taiwan-outer-current-12000-12500-2250-2700 \
  --bbox 120.00 125.00 22.50 27.00 \
  --target-resolution-km 1 \
  --source-margin-deg 0.25 \
  --time-stride 999 \
  --max-files 1 \
  --include-elev \
  --land-geojson data/geojson/twCounty2010.geo.json
```

### 區域 2：貢寮與台灣東北角外海

- 狀態：smoke test 與圖面檢視已完成，正式保留目前 bbox 與成果
- 區域名稱：新北市貢寮海域、北台灣沿岸與東海南部陸棚交換區
- 主要目標：涵蓋貢寮東北角暨宜蘭海岸國家風景區海域，以及可能影響該區的台灣東岸黑潮上游、
  東北方黑潮陸棚入侵、北台灣沿岸流與東側中尺度渦旋，同時排除中國沿岸、台灣西部及過遠的東南外海。
- 近岸參考圖：`data/reference/新北市貢寮海域.png`
- Google Maps 區域中心參考：約 `25.017N, 121.946E`
- 正式 bbox（專案 CLI 順序）：

```text
121.00 124.50 23.50 27.00
```

- GIS / GeoJSON bbox：

```text
[121.00, 23.50, 124.50, 27.00]
```

- 對應 `DOMAIN_ID`：

```text
gongliao-northeast-taiwan-current-12100-12450-2350-2700
```

- 已產生 smoke test 輸出：

```text
outputs/ocm_2025_01_gongliao_bbox_12100_12450_2350_2700_1km_smoke
```

- 實際 1 km 規則格點：`390 x 353`，共 `137,670` 格。
- 圖面聚焦範圍建議：`121.40 123.50 24.85 26.20`；此範圍只用於報告插圖或作業區統計，
  不取代正式外海影響 bbox，亦不需要另做 OCM 前處理。

- 需要納入的主要影響路徑：
  - 貢寮位於台灣東北角，黑潮沿台灣東岸北上後在此接近東海陸棚坡折，主軸位置、跨陸棚入侵與冷穹頂會影響近岸鋒面及流向。
  - 南東海表層流場由黑潮、台灣海峽流及中國沿岸流共同作用，但貢寮正式圖面只需保留北台灣近岸與陸棚交換的東側結果，
    不必把中國沿岸源區完整納入同一 bbox。
  - 東側中尺度渦旋會改變黑潮主軸位置及陸棚入侵強度；北側季風與沿岸水南伸則可能改變貢寮近岸水團與鋒面。
  - 原參考圖聚焦 `10 m` 以上水深作業海域，局部灣澳、岬角與潮流仍需另以高解析度產品處理。

- 四向決策邊界：
  - 西界 `121.00E`：位於台灣北端以西，保留北台灣海岸與沿岸流進入貢寮的路徑，並排除中國沿岸及大部分台灣西部；
    smoke test 中貢寮附近流場未緊貼西界。
  - 東界 `124.50E`：越過黑潮近岸側與主軸，保留主軸外側與東側渦旋；相較 `125.00E`，減少和貢寮關聯較低的遠洋面積，
    但 smoke test 仍完整呈現 `123–124E` 的迴旋及主流外側結構。
  - 南界 `23.50N`：保留貢寮以南約 `1.5°`、約 `160 km` 的台灣東岸黑潮上游；
    若切到龜山島北端約 `24.85N`，貢寮距南界僅約 `19 km`，會失去判斷黑潮如何進入東北角的必要脈絡。
  - 北界 `27.00N`：保留貢寮以北約 `2°` 的南東海陸棚與黑潮入侵轉向區；更北將轉為較大尺度東海環流問題。

- 與龜山島 bbox 的關係：
  - 貢寮與龜山島相距約 `20 km`，核心黑潮與陸棚交換機制相同；正式外海影響 bbox 出現地理重疊是科學上合理的，不能為了區名分開而切斷上游。
  - 報告若需避免圖面混淆，可使用 `24.85N` 作為「聚焦插圖」南界，但不可把它當成主要 OCM 外海分析域南界。

- 已淘汰並移除的初版 smoke test：

```text
bbox: 119.50 125.00 22.50 27.50
原輸出路徑：outputs/ocm_2025_01_gongliao_bbox_11950_12500_2250_2750_1km_smoke
```

  初版涵蓋中國沿岸、龜山島分析域、台灣西部及較遠的東南外海，雖能完整呈現南東海大尺度流場，
  但對貢寮專屬圖面過度保守，因此不作為正式 bbox。初版的 `232 MB` 實體成果已於 `2026-07-17`
  依確認決定移除；本段只保留 bbox、原路徑及量化比較，供後續報告追溯決策過程。

- QC 設定與結果：
  - 原始檔：`/Users/mustlab/Downloads/CWA-OCM/2025/01/20250101_schout.nc`
  - 時間：只取第一個抽樣時間 `2025-01-01T01:00:00`
  - 解析度：`1 km`
  - 全層有效資料比例：約 `0.582`
  - 水平海域覆蓋比例：約 `0.892`
  - 全層流速平均：`0.369 m/s`
  - 全層流速第 95 百分位：`0.957 m/s`
  - 全層最大流速：`2.667 m/s`
  - 海表水位範圍：`0.141–1.554 m`
  - 陸域遮罩：`data/geojson/twCounty2010.geo.json`
  - 輸出資料量：約 `105 MB`（包含 GIF 與第一幀 PNG）
  - 相較初版，格點由 `309,135` 降至 `137,670`，減少約 `55%`；成果資料量由 `232 MB` 降至 `105 MB`。

- 主要檢視圖：

```text
outputs/ocm_2025_01_gongliao_bbox_12100_12450_2350_2700_1km_smoke/figures/surface_layer_047_horizontal_current_speed_quiver_first_frame.png
outputs/ocm_2025_01_gongliao_bbox_12100_12450_2350_2700_1km_smoke/figures/surface_speed_elev_quiver_first_frame.png
outputs/ocm_2025_01_gongliao_bbox_12100_12450_2350_2700_1km_smoke/figures/model_layer_032_horizontal_current_speed_quiver_first_frame.png
outputs/ocm_2025_01_gongliao_bbox_12100_12450_2350_2700_1km_smoke/figures/model_layer_016_horizontal_current_speed_quiver_first_frame.png
outputs/ocm_2025_01_gongliao_bbox_12100_12450_2350_2700_1km_smoke/figures/bottom_layer_000_horizontal_current_speed_quiver_first_frame.png
```

- smoke test 圖面判定：
  - 表層與第 32 層清楚呈現黑潮沿台灣東岸北上，在台灣東北角外海分流並向東海陸棚及東北方轉向；貢寮位於此轉向區的近岸側。
  - 西界已排除中國沿岸與台灣西部，圖面只保留北台灣近岸；相較初版，研究焦點明顯集中於貢寮及東北外海。
  - 南界 `23.50N` 仍能看見連續向北的黑潮上游，貢寮不再貼近南界；東界 `124.50E` 仍包含主軸外側與渦旋，沒有裁切關鍵結構。
  - 北界 `27.00N` 保留主要陸棚入侵與轉向區，貢寮距邊界約 `2°`，足以作為下游緩衝。
  - 表層水位疊圖色階與箭頭均正常，無空白、遮罩錯位或陸域流速殘留；第 16 層及底層的大片灰區來自 sigma 層在淺陸棚／地形上的有效水深限制，並非 bbox 或插值失敗。

- 報告引用限制：
  - smoke test 僅使用 2025 年 1 月單一時刻，證明的是 bbox、插值、遮罩及圖面可行性；季風、潮汐、颱風與黑潮季節位移仍須以完整時間序列分析。
  - `1 km` 網格無法解析原參考圖中 `10 m` 等深線附近的灣澳、岬角尾流與作業尺度環流，正式報告應將本 bbox 稱為「貢寮外海影響分析域」。
  - 龜山島出現在外海影響圖內是兩地相距近且共享黑潮／陸棚交換機制的結果；若報告需區分作業區，應使用聚焦插圖或 AOI polygon，而非再縮短外海 bbox。

- 報告用決策摘要：本區以貢寮東北角海域為核心，正式分析域 `121.00–124.50E、23.50–27.00N`
  向南保留約 `160 km` 的黑潮上游、向東越過黑潮主軸與外側渦旋、向北涵蓋南東海陸棚入侵與轉向，
  並以 `121.00E` 排除中國沿岸及大部分台灣西部。修正版 smoke test 保留全部關鍵流場，格點較初版減少約 `55%`，
  因此作為正式貢寮外海影響 bbox；龜山島僅在聚焦插圖中以 `24.85N` 南界區隔。

- 科學依據：
  - [Hsu et al. (2021), *Surface Current Variations and Oceanic Fronts in the Southern East China Sea*](https://doi.org/10.1029/2021JC017373)：
    說明南東海黑潮、台灣海峽流、中國沿岸流及北台灣沿岸流的季節性交換。
  - [Hsin et al. (2011), *Fluctuations of the thermal fronts off northeastern Taiwan*](https://doi.org/10.1029/2011JC007066)：
    說明黑潮、台灣暖流、中國沿岸流與東北外海冷穹頂共同控制熱鋒面。
  - [Liu et al. (2014), *The pattern and variability of winter Kuroshio intrusion northeast of Taiwan*](https://doi.org/10.1002/2014JC009879)：
    說明冬季黑潮跨陸棚入侵的主要路徑及其變異。
  - [Tsai et al. (2008), *Typhoon induced upper ocean cooling off northeastern Taiwan*](https://doi.org/10.1029/2008GL034368)：
    說明季風與颱風可快速改變黑潮陸棚入侵及東北外海湧升反應。

### 區域 3：新竹縣外海

- 狀態：smoke test 與圖面檢視已完成，正式保留目前 bbox 與成果
- 區域名稱：新竹縣外海、台灣海峽中北段近岸影響區
- 主要目標：涵蓋南寮漁港外海、香山外海人工魚礁區、頭前溪出海口及其可能受台灣海峽中北段背景流、
  季風驅動沿岸流、跨海峽流與河口近岸輸送影響的外海脈絡，同時避免把馬祖、澎湖、台灣東側及中國沿岸大量納入同一圖面。
- 近岸參考圖：`data/reference/新竹縣外海.png`
- Google Maps 區域中心參考：約 `24.82N, 120.93E`
- 正式 bbox（專案 CLI 順序）：

```text
119.70 121.30 23.90 25.30
```

- GIS / GeoJSON bbox：

```text
[119.70, 23.90, 121.30, 25.30]
```

- 對應 `DOMAIN_ID`：

```text
hsinchu-taiwan-strait-current-11970-12130-2390-2530
```

- 已產生 smoke test 輸出：

```text
outputs/ocm_2025_01_hsinchu_bbox_11970_12130_2390_2530_1km_smoke
```

- 已淘汰並移除的初版較大測試範圍：

```text
bbox: 119.50 121.30 23.90 25.50
輸出路徑：outputs/ocm_2025_01_hsinchu_bbox_11950_12130_2390_2550_1km_smoke
```

  初版能完整呈現新竹外海至海峽中軸的表層流場，但西北角碰到福建沿岸／島嶼陸域，且資料量由正式版的約 `19 MB`
  增加到約 `25 MB`。考量新竹外海作業區不需要把中國沿岸陸域放進主圖，正式版將西界由 `119.50E`
  收回到 `119.70E`、北界由 `25.50N` 收回到 `25.30N`，仍保留海峽中軸與南北向流場緩衝。初版實體成果已於
  `2026-07-17` 依確認決定移除；本段只保留 bbox、原路徑及量化比較，供後續報告追溯決策過程。

- QC 設定與結果：
  - 原始檔：`/Users/mustlab/Downloads/CWA-OCM/2025/01/20250101_schout.nc`
  - 時間：只取第一個抽樣時間 `2025-01-01T01:00:00`
  - 解析度：`1 km`
  - 實際 1 km 規則格點：`156 x 162`，共 `25,272` 格
  - 表層有效海域格點：`16,730` 格，約占水平格點 `0.662`
  - 全層有效資料比例：約 `0.207`
  - 全層流速平均：`0.351 m/s`
  - 全層流速第 95 百分位：`0.871 m/s`
  - 全層最大流速：`1.322 m/s`
  - 海表水位範圍：`0.081–0.545 m`
  - 陸域遮罩：`data/geojson/twCounty2010.geo.json`
  - 輸出資料量：約 `19 MB`（包含 GIF 與第一幀 PNG）

- 主要檢視圖：

```text
outputs/ocm_2025_01_hsinchu_bbox_11970_12130_2390_2530_1km_smoke/figures/surface_layer_047_horizontal_current_speed_quiver_first_frame.png
outputs/ocm_2025_01_hsinchu_bbox_11970_12130_2390_2530_1km_smoke/figures/surface_speed_elev_quiver_first_frame.png
outputs/ocm_2025_01_hsinchu_bbox_11970_12130_2390_2530_1km_smoke/figures/model_layer_040_horizontal_current_speed_quiver_first_frame.png
outputs/ocm_2025_01_hsinchu_bbox_11970_12130_2390_2530_1km_smoke/figures/model_layer_035_horizontal_current_speed_quiver_first_frame.png
```

- 需要納入的主要影響路徑：
  - 新竹外海位於台灣海峽東側淺陸棚，台灣海峽是南海與東海之間的主要通道；背景流具有明顯季風與地形控制，
    不能只用緊貼 20 m 等深線的小框判讀。
  - 冬季東北季風可使台灣海峽表層出現西側南下、東側或跨海峽分支等不同型態；新竹外海位於這些分支及近岸流交會的下游／側向交換區。
  - 原參考圖提到南寮漁港、香山人工魚礁及頭前溪出海口，代表局部河口淡水、泥沙、漁業活動與人工構造物會影響近岸污染物或廢棄物聚集；
    但這些局部過程必須在保留台灣海峽背景流後，另以更細尺度處理。

- 四向決策邊界：
  - 西界 `119.70E`：位於新竹外海以西、接近台灣海峽中軸，保留海峽背景流與跨海峽交換的視覺判讀空間；
    相較 `119.50E` 可避免西北角納入福建沿岸陸域，且 smoke test 未顯示新竹近岸主流需要更西側圖面才能判讀。
  - 東界 `121.30E`：越過新竹、苗栗、桃園近岸陸側，讓陸域遮罩與海岸線完整呈現；再往東會納入大量內陸，對外海流場判讀沒有助益。
  - 南界 `23.90N`：保留新竹以南約 `0.6–0.9°` 的苗栗至台中外海，使南側上游或反向沿岸流不貼邊界；
    未延伸到澎湖與台灣海峽南口，避免和南台灣 bbox 混淆。
  - 北界 `25.30N`：涵蓋新竹以北桃園至北台灣西側近岸與海峽中北段下游／上游緩衝；
    未延伸到馬祖或福建沿岸，避免把研究問題推成整個北台灣海峽或中國沿岸流源區。

- smoke test 圖面判定：
  - 表層圖清楚呈現海峽中北段由北側往西南及近岸彎折的流場，新竹外海位於圖面中央偏東，不貼近任何邊界。
  - 水位疊圖色階與箭頭正常，沒有空白、遮罩錯位或陸域流速殘留；收斂版沒有納入中國沿岸陸域。
  - 第 `35`、`40` 層仍有有效資料並顯示類似的海峽流向；第 `16` 層及底層在此淺陸棚幾乎全為無效值，
    屬 sigma 層與水深限制，不應作為新竹外海主要檢視層。

- 報告引用限制：
  - smoke test 僅使用 2025 年 1 月單一時刻，能證明 bbox、插值、遮罩與圖面可行性；季節性季風轉換、潮週期、
    河口逕流事件與颱風後輸送仍須用完整時間序列或事件資料檢查。
  - `1 km` OCM 產品無法解析南寮港區、人工魚礁、頭前溪口羽狀流、`10–30 m` 等深線內的細尺度渦流與岸線結構影響。
    報告應將本 bbox 稱為「新竹外海台灣海峽影響分析域」，而非精細作業區或污染物擴散模式邊界。

- 報告用決策摘要：本區以新竹西岸近海為核心，正式分析域 `119.70–121.30E、23.90–25.30N`
  向西保留至台灣海峽中軸、向東涵蓋新竹至桃竹苗岸線、向南保留苗栗／台中外海流場、向北保留桃園至北台灣西側緩衝。
  smoke test 顯示此範圍可判讀新竹外海可能受台灣海峽中北段背景流與跨海峽流影響的狀況，同時避免納入馬祖、澎湖、
  中國沿岸與台灣東側，因此作為正式新竹外海台灣海峽影響 bbox。

- 科學依據：
  - [Jan et al. (2002), *Seasonal variation of the circulation in the Taiwan Strait*](https://doi.org/10.1016/S0924-7963(02)00130-6)：
    說明台灣海峽東側與西側流系、季風及彰雲隆起地形共同控制季節性流場。
  - [Lin et al. (2005), *Taiwan strait current in winter*](https://doi.org/10.1016/j.csr.2004.12.008)：
    說明冬季東北季風下，台灣海峽次潮流與中國沿岸水南下及跨海峽分量的變化。
  - [Jan, Sheu, and Kuo (2006), *Water mass and throughflow transport variability in the Taiwan Strait*](https://doi.org/10.1029/2006JC003656)：
    說明東亞季風與地形是台灣海峽水團與通量變化的主要控制因子。
  - [Hu et al. (2019), *Characterizing surface circulation in the Taiwan Strait during NE monsoon from Geostationary Ocean Color Imager*](https://doi.org/10.1016/j.rse.2018.12.003)：
    說明東北季風期間台灣海峽表層可出現西南向、東北向與跨海峽流等不同流態。

### 區域 4：屏東縣國立海洋生物博物館周邊海域

- 狀態：smoke test 與圖面檢視已完成，正式保留目前 bbox 與成果
- 區域名稱：屏東縣國立海洋生物博物館、後灣與南台灣外海影響區
- 主要目標：涵蓋可能影響後灣與恆春半島西北側的外海流場，同時避免把菲律賓北岸與台灣中北部外海納入過多，降低圖面干擾與計算量。
- 近岸參考圖：`data/reference/屏東縣國立海洋生物博物館周邊海域.png`
- 正式 bbox（專案 CLI 順序）：

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

- 命名備註：成果資料夾使用 `houwan`，內部 `DOMAIN_ID` 使用 `nmmba`；兩者指向同一研究區域。
  為保留 smoke test 的可追溯性，不重新命名現有成果或改寫摘要檔。

- 已產生 QC 輸出：

```text
outputs/ocm_2025_01_houwan_bbox_11750_12400_1900_2400_1km_smoke
```

- QC 設定：
  - 原始檔：`/Users/mustlab/Downloads/CWA-OCM/2025/01/20250101_schout.nc`
  - 時間：只取第一個抽樣時間 `2025-01-01T01:00:00`
  - 解析度：`1 km`
  - 格點：`557 x 674`
  - 有效海域比例：約 `0.795`
  - 全層流速平均：`0.322 m/s`
  - 全層流速第 95 百分位：`0.918 m/s`
  - 全層最大流速：`2.410 m/s`
  - 陸域遮罩：`data/geojson/twCounty2010.geo.json`
  - 輸出資料量：約 `281 MB`（包含 GIF 與第一幀 PNG）

- 主要檢視圖：

```text
outputs/ocm_2025_01_houwan_bbox_11750_12400_1900_2400_1km_smoke/figures/surface_layer_047_horizontal_current_speed_quiver_first_frame.png
outputs/ocm_2025_01_houwan_bbox_11750_12400_1900_2400_1km_smoke/figures/surface_speed_elev_quiver_first_frame.png
outputs/ocm_2025_01_houwan_bbox_11750_12400_1900_2400_1km_smoke/figures/model_layer_032_horizontal_current_speed_quiver_first_frame.png
outputs/ocm_2025_01_houwan_bbox_11750_12400_1900_2400_1km_smoke/figures/model_layer_016_horizontal_current_speed_quiver_first_frame.png
outputs/ocm_2025_01_houwan_bbox_11750_12400_1900_2400_1km_smoke/figures/bottom_layer_000_horizontal_current_speed_quiver_first_frame.png
```

- 需要納入的主要影響路徑：
  - 台灣東南側黑潮向北流經恆春海脊與呂宋海峽北端，其路徑、分支與地形作用可能改變南台灣東西兩側的外海流場。
  - 呂宋海峽是北太平洋與南海的主要交換通道；黑潮入侵南海及其迴轉、渦旋活動可改變巴士海峽與南台灣西南側背景環流。
  - 南海暖流與台灣海峽向北輸送連接台灣西南外海及海峽南段，因此後灣西側不能只保留狹窄沿岸範圍。
  - 後灣本身受恆春半島、岬角、淺水地形與潮流控制，但這些局部過程必須在保留上述外海背景後另以較細尺度分析。

- 四向決策邊界：
  - 西界 `117.50E`：位於台灣西南岸以西約數百公里，保留南海北部陸棚坡折、台灣海峽南口及 smoke test 中約 `119–120E` 的顯著渦旋；
    若西界貼近台灣，會切掉可能輸送至西南沿岸的背景流與渦旋結構。
  - 東界 `124.00E`：超出台灣東岸及蘭嶼以東，保留黑潮主軸和其外側背景流，使強流核心不緊貼輸出邊界；
    再向東擴張主要增加西北太平洋遠洋面積，對後灣判讀的增益低於資料成本。
  - 南界 `19.00N`：涵蓋巴士海峽北部、巴丹群島周邊及呂宋海峽交換的北側脈絡，並在圖面上避免直接納入呂宋島北岸；
    這是保留南側上游／交換訊號與避免菲律賓陸地干擾之間的折衷。
  - 北界 `24.00N`：涵蓋高屏外海、澎湖及台灣海峽南至中段，讓向北輸送有足夠下游空間；
    `23.00N` 會過早切斷澎湖與海峽背景，`24.80N` 或 `25.50N` 則納入大量台灣中北部與東北外海，偏離後灣問題。

- 替代範圍與排除理由：
  - 曾完成較大範圍 `117.50 124.00 18.00 24.80` 的 smoke test。它能多涵蓋呂宋海峽與台灣中部，
    但圖面直接包含菲律賓北岸、目標海域比例下降且資料量增加，因此未採用。
  - 北界 `23.00N` 雖可縮小資料量，但無法完整保留澎湖及台灣海峽南段後續流向；正式 bbox 採 `24.00N`。
  - 北界 `25.50N` 對區域外海研究並非錯誤，但對「影響後灣的南台灣流場」屬過度延伸，會把東北台灣黑潮轉向區納入同一圖面。

- smoke test 圖面判定：
  - 表層圖同時呈現台灣東側向北強流、南端與巴士海峽交換、台灣西南外海渦旋及台灣海峽南段流場，四向均有上下游緩衝。
  - 南界沒有納入呂宋島北岸，北界則保留澎湖附近流場；圖面符合「大範圍但仍聚焦南台灣」的目標。
  - bbox 適合作為外海背景分析範圍；後灣與海生館周邊在此尺度上只占少量格點，不能直接取代局部近岸分析。

- 報告引用限制：
  - 目前 QC 僅證明 `2025-01-01T01:00:00` 的資料覆蓋與圖面完整，正式報告若討論季節性、潮汐或事件影響，仍須使用完整時間序列驗證。
  - 原參考圖標示 `5–10 m` 等深線附近作業需求；1 km OCM 重採樣無法忠實解析該尺度，報告應把本 bbox 稱為「外海影響分析域」，而非精細作業區模式。

- 報告用決策摘要：本區以後灣與恆春半島西北側為核心，分析域向西涵蓋南海北部與台灣海峽南口、
  向東越過台灣東側黑潮主軸、向南保留巴士海峽北部交換、向北保留澎湖及台灣海峽南至中段。
  `117.50–124.00E、19.00–24.00N` 能同時呈現南海暖流、呂宋海峽／巴士海峽交換、恆春海脊黑潮及西南外海渦旋，
  並避免把呂宋島北岸與台灣中北部大量納入，因此作為目前正式外海影響分析域。

- 科學依據：
  - [Liang et al. (2008), *Kuroshio in the Luzon Strait*](https://doi.org/10.1029/2007JC004609)：說明呂宋海峽是南海與北太平洋主要交換通道，以及黑潮入侵後的複雜流場。
  - [Yang et al. (2008), *On the dynamics of the South China Sea Warm Current*](https://doi.org/10.1029/2007JC004427)：說明南海暖流、台灣海峽輸送與黑潮入侵之間的動力關係。
  - [*Topographic–baroclinic instability and formation of Kuroshio current loop* (2018)](https://doi.org/10.1016/j.dynatmoce.2017.11.002)：指出恆春海脊附近黑潮與地形作用可形成迴流及渦旋。

- 建議前處理命令範例：

```bash
UV_CACHE_DIR=work/uv-cache \
MPLCONFIGDIR=work/matplotlib-cache \
PYTHONDONTWRITEBYTECODE=1 \
uv run python3 scripts/preprocess_ocm_month.py \
  --input-dir /Users/mustlab/Downloads/CWA-OCM/2025/01 \
  --output-dir outputs/ocm_2025_01_houwan_bbox_11750_12400_1900_2400_1km_smoke \
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

### 區域 5：連江縣海域（拆分為 7 個候選子區）

- 狀態：已依文字描述拆分為 7 個候選 `flow_bbox`，並新增 7 個低重疊 `focus_bbox`；
  已完成既有 OCM 1 km 流場疊圖檢查，尚未逐區執行獨立 smoke test。
- 區域名稱：連江縣北竿與南竿章魚籠／廢棄漁具熱點候選區
- 主要目標：依參考文字將北竿 `尼姑山`、`白廟／鐵尖島`、`芹壁龜島` 三區，以及南竿 `黃官嶼`、`翰林角`、
  `復興`、`機場下方` 四區各自獨立處理，避免再用單一北竿或南竿大 bbox 混合不同作業熱點。
- 近岸參考圖：`data/reference/連江縣海域.png`
- bbox 檢視輸出資料夾：

```text
outputs/ocm_2025_01_lienchiang_7subregions_bbox_review_1km
```

  此資料夾只保存連江縣 7 個候選子區的 bbox 檢視圖與 metadata，流場陣列來源仍為
  `outputs/ocm_2025_01_taiwan_1km_geojson_qc`。因目前尚未逐子區執行 OCM 前處理 smoke test，
  資料夾名稱使用 `bbox_review` 而非 `_smoke`，避免和前四個已完成獨立前處理的區域混淆。

- 保留 bbox 檢視圖：

```text
outputs/ocm_2025_01_lienchiang_7subregions_bbox_review_1km/bbox_review_summary.json
outputs/ocm_2025_01_lienchiang_7subregions_bbox_review_1km/figures/lienchiang_7_subregion_flow_focus_bboxes_land_neutral.png
outputs/ocm_2025_01_lienchiang_7subregions_bbox_review_1km/figures/lienchiang_7_subregion_flow_focus_bboxes_land_neutral.json
outputs/ocm_2025_01_lienchiang_7subregions_bbox_review_1km/figures/lienchiang_7_subregion_flow_focus_bboxes_land_neutral_zhlabels.png
outputs/ocm_2025_01_lienchiang_7subregions_bbox_review_1km/figures/lienchiang_7_subregion_flow_focus_bboxes_land_neutral_zhlabels.json
```

  上述 `*_land_neutral_zhlabels.png` 為目前建議報告檢視圖，使用繁體中文子區標籤並以短引線錯開位置；
  `白廟／鐵尖島` 採兩行標示，以降低和北竿北側島礁及相鄰 bbox 的遮掩。
  `*_land_neutral.png` 則為同版式的英數代碼標籤版本。兩者皆使用 `data/geojson/twCounty2010.geo.json` 中
  `COUNTYNAME=連江縣` 的陸地 polygon 疊加海岸線，並移除 `surface current speed` 色階底圖，只保留中性海面、
  表層流向箭頭、`flow_bbox` 與 `focus_bbox` 外框；相較只用 OCM mask 的粗格點圖，能辨識北竿、南竿、鐵尖島、
  黃官嶼及周邊小島礁輪廓，也比舊版速度底圖更適合判讀 bbox 顏色。
  早期候選疊圖、低重疊草稿疊圖與速度底圖版本已於 `2026-07-20` 移除實體輸出；上述兩版也已於同日從
  `outputs/ocm_2025_01_taiwan_1km_geojson_qc/figures` 搬移至連江專屬資料夾，作為後續報告與檢視依據。

- 候選 `flow_bbox` 表（供 OCM 前處理與外海流場檢視；專案 CLI 順序為 `lon_min lon_max lat_min lat_max`）：

| 編號 | 子區 | 定位中心參考 | 候選 bbox（CLI 順序） | GIS / GeoJSON bbox | 建議 `DOMAIN_ID` |
| --- | --- | --- | --- | --- | --- |
| B1 | 北竿尼姑山 | `119.9693E, 26.1976N` | `119.90 120.03 26.15 26.23` | `[119.90, 26.15, 120.03, 26.23]` | `lienchiang-beigan-nigushan-11990-12003-2615-2623` |
| B2 | 北竿白廟／鐵尖島 | `119.9768E, 26.2728N` | `119.92 120.04 26.23 26.32` | `[119.92, 26.23, 120.04, 26.32]` | `lienchiang-beigan-tiejian-baimiao-11992-12004-2623-2632` |
| B3 | 北竿芹壁龜島 | `119.9829E, 26.2266N` | `119.94 120.03 26.19 26.26` | `[119.94, 26.19, 120.03, 26.26]` | `lienchiang-beigan-qinbi-guidao-11994-12003-2619-2626` |
| N1 | 南竿黃官嶼 | `119.9719E, 26.1654N` | `119.93 120.01 26.13 26.20` | `[119.93, 26.13, 120.01, 26.20]` | `lienchiang-nangan-huangguanyu-11993-12001-2613-2620` |
| N2 | 南竿翰林角 | `119.9173E, 26.1746N` | `119.87 119.96 26.13 26.21` | `[119.87, 26.13, 119.96, 26.21]` | `lienchiang-nangan-hanlinjiao-11987-11996-2613-2621` |
| N3 | 南竿復興 | `119.9530E, 26.1630N` | `119.91 119.99 26.13 26.19` | `[119.91, 26.13, 119.99, 26.19]` | `lienchiang-nangan-fuxing-11991-11999-2613-2619` |
| N4 | 南竿機場下方 | `119.9586E, 26.1603N` | `119.93 120.00 26.10 26.17` | `[119.93, 26.10, 120.00, 26.17]` | `lienchiang-nangan-airport-south-11993-12000-2610-2617` |

- 低重疊 `focus_bbox` 表（供報告標示、熱點比較或格點統計；專案 CLI 順序為 `lon_min lon_max lat_min lat_max`）：

| 編號 | 子區 | focus bbox（CLI 順序） | GIS / GeoJSON focus bbox | 設計理由 |
| --- | --- | --- | --- | --- |
| B1 | 北竿尼姑山 | `119.910 119.985 26.185 26.215` | `[119.910, 26.185, 119.985, 26.215]` | 以 `26.185N` 作南北竿 focus 分界，保留尼姑山西南近岸但避免與南竿北岸重疊。 |
| B2 | 北竿白廟／鐵尖島 | `119.940 120.020 26.255 26.305` | `[119.940, 26.255, 120.020, 26.305]` | 鎖定鐵尖島／白廟外礁，和 B3 芹壁龜島留約 `0.005°` 緩衝。 |
| B3 | 北竿芹壁龜島 | `119.955 120.025 26.215 26.250` | `[119.955, 26.215, 120.025, 26.250]` | 鎖定芹壁龜島與北竿北岸中段，南界接 B1、北界低於 B2。 |
| N1 | 南竿黃官嶼 | `119.957 120.005 26.155 26.185` | `[119.957, 26.155, 120.005, 26.185]` | 鎖定南竿東北側黃官嶼外海，西界接 N3、北界接 B1 分界線。 |
| N2 | 南竿翰林角 | `119.870 119.930 26.160 26.185` | `[119.870, 26.160, 119.930, 26.185]` | 鎖定南竿西北端翰林角／四維外海，東界接 N3。 |
| N3 | 南竿復興 | `119.930 119.957 26.155 26.185` | `[119.930, 26.155, 119.957, 26.185]` | 鎖定復興／牛角北岸近海，西界接 N2、東界接 N1。 |
| N4 | 南竿機場下方 | `119.935 120.000 26.105 26.155` | `[119.935, 26.105, 120.000, 26.155]` | 鎖定機場跑道南側至東南側海域；使用海域範圍而非機場中心。 |

- 低重疊檢查結果：
  - `focus_bbox` 兩兩相交的面積重疊數量為 `0`；相鄰框最多只在邊界線相接。
  - 原 `flow_bbox` 仍保留必要流場緩衝，因此兩兩相交共 `14` 組；這些重疊不適合拿來做熱點統計，但適合 OCM 前處理與流場檢視。

- 子區邊界理由：
  - B1 `尼姑山`：以北竿西南側尼姑山為核心，bbox 向南保留南北竿水道、向西保留近岸外海流場；不向北包到高登外礁，
    避免和 B2 混淆。
  - B2 `白廟／鐵尖島`：依本案說明以鐵尖島定位，同時 bbox 北、東側仍覆蓋公開地圖上白廟島附近流場，避免地名差異造成漏切；
    此區位於北竿北方外礁，需保留高登南側及外海繞流。
  - B3 `芹壁龜島`：以芹壁北側龜島與北竿北岸為核心，bbox 南側保留北竿本島岸線，北側保留北岸外海來流。
  - N1 `黃官嶼`：以南竿東北側黃官嶼及 19 據點外海為核心，bbox 同時保留南竿機場東側與黃官嶼外側流場。
  - N2 `翰林角`：以南竿西北端四維／西尾鼻外海為核心，bbox 向西保留閩江口外側背景流，不向東包到機場與黃官嶼。
  - N3 `復興`：以復興村／牛角北岸近海為核心，bbox 介於翰林角與黃官嶼之間，保留北岸沿岸輸送但不下切到機場南側。
  - N4 `機場下方`：以南竿機場南側至東南側近岸為核心，bbox 南界比 N1/N3 更低，保留跑道下方外海與南側岸線流場。

- OCM 1 km 流場疊圖判定：
  - 北竿三區在既有 `2025-01-01T01:00:00` 表層流場中，均保留至少數公里外海緩衝；B2、B3 因北竿北岸與高登外礁距離近，
    bbox 有合理重疊，但定位中心與報告用途不同。
  - 南竿四區在既有表層流場中分布於南竿西北、北岸、東北與東南側；N1、N3、N4 因復興、機場與黃官嶼距離近而部分重疊，
    屬作業熱點相鄰造成，不應強行用過窄 bbox 切斷流場。
  - 7 個 bbox 均避開全連江大框，未納入東引、莒光或中國沿岸大範圍；相較原 `119.72–120.25E、25.82–26.45N`
    的全連江覆蓋框，圖面更適合逐熱點檢視。

- 已淘汰並移除的歷史合併測試：
  - 北竿合併 bbox `119.86 120.12 26.16 26.35` 曾產生
    `outputs/ocm_2025_01_lienchiang_beigan_bbox_11986_12012_2616_2635_1km_smoke`。
  - 南竿合併 bbox `119.84 120.04 26.03 26.21` 曾產生
    `outputs/ocm_2025_01_lienchiang_nangan_bbox_11984_12004_2603_2621_1km_smoke`。
  - 這兩個合併測試不符合目前「北竿 3 區、南竿 4 區各自獨立處理」的資料產品目標，
    且會和低重疊子區版本混淆；實體成果已於 `2026-07-20` 移除，本段只保留 bbox 與原路徑供追溯。

- 報告引用限制：
  - 本輪只完成候選 bbox 與既有流場疊圖檢查，尚未逐區輸出前處理資料夾；正式保留前仍需依 7 個 bbox 分別執行 smoke test。
  - `1 km` OCM 產品無法解析鐵尖島、白廟、芹壁龜島、黃官嶼、翰林角岸礁、港澳與機場下方小尺度尾流；
    報告應將這些 bbox 稱為「外海影響分析域」，不是打撈作業精準邊界。
  - 連江各子區距離中國沿岸與閩江口近，季風、潮汐、短期風浪、河口水團與漁業作業事件都可能改變漂移方向；
    本候選 bbox 僅證明空間切分合理，不能取代完整時間序列或現地清除點資料。

- 共同資料來源：
  - [國家文化記憶庫，北竿尼姑山觀測所](https://tcmb.culture.tw/zh-tw/detail?id=153516&indexCode=Culture_Place)：
    提供尼姑山座標與位置描述。
  - [連江縣戶外教育及海洋教育中心，燕鷗](https://www.sea.matsu.edu.tw/tern.html)：
    說明鐵尖、白廟等北竿外礁與保護區脈絡。
  - [馬祖國家風景區，龜島](https://www.matsu-nsa.gov.tw/zh-TW/attractions/1448)：
    提供芹壁村龜島地名與位置脈絡。
  - [國家文化記憶庫，南竿 19 據點](https://tcmb.culture.tw/zh-tw/detail?id=153275&indexCode=Culture_Place)：
    說明 19 據點正對黃官嶼，並提供復興村附近座標。
  - [南竿鄉公所，地理位置](https://www.nankan.gov.tw/Chhtml/content/2111)：
    提供南竿島極東、極西、極南、極北座標，用於避免南竿候選 bbox 過度擴張。
  - [南竿鄉公所，四維／翰林角描述](https://www.nankan.gov.tw/chhtml/Detail/2416?mcid=33034)：
    說明翰林角原名西尾鼻，突出於四維村落北方。
  - [馬祖航空站，地理位置](https://msa.gov.tw/airport-info/67)：
    提供南竿機場地址與馬祖列島地理背景；機場中心座標另以 Google Maps / OSM 交叉確認。

## 待確認區域

後續區域確認後，請依同一格式補上 bbox、GIS bbox、`DOMAIN_ID`、QC 輸出位置、
格點大小、有效海域比例、主要檢視圖、四向邊界、替代範圍、報告限制與科學依據。
