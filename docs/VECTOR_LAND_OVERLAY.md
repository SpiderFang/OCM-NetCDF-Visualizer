# 高解析向量陸地疊圖紀錄

本文紀錄「1 km OCM 規則格點資料 + GeoJSON 向量陸地疊圖」的視覺化做法。這個方法已用於
`surface_layer_047_first_frame_coverage_vector_land_layout_checked.png` 與
`geojson_mask_qc_1km_five_regions_gongliao_inset.png`，可作為後續成果報告製圖與 QC 圖面的共通設計。

## 使用目的

OCM/SCHISM 流場資料在本專案中會被重採樣到規則經緯度格點，例如 `1 km` 或 `10 km`。若直接用
`mask.npy` 畫陸地，陸地邊界會受格點大小限制；北竿、南竿、龜山島、蘭嶼、小琉球或狹窄岬角等小尺度陸地，
在全域圖上可能只剩少量像素，甚至因格點中心或 cell-overlap 判定而不容易辨識。

向量陸地疊圖的目的，是讓「陸地視覺呈現」比流場格點更細緻，方便成果報告讀者辨識島嶼、海岸線與區域位置。
這個方法只改變圖面上的地理參照，不改變 OCM 流場資料、有效海域遮罩、格點解析度或任何定量統計。

## 資料來源

目前使用的陸域圖資：

```text
data/geojson/twCounty2010.geo.json
```

此檔是台灣縣市 GeoJSON，幾何型態為 `Polygon` 或 `MultiPolygon`，座標系統為 WGS84 經緯度。因為 OCM 圖面同樣使用
經度與緯度作為座標軸，所以可直接用 Matplotlib 疊加到既有流場圖或 mask QC 圖上，不需額外投影轉換。

若後續改用更正式或更高解析的岸線資料，也應維持以下條件：

- 幾何必須是可代表陸域面的 `Polygon` 或 `MultiPolygon`。
- 座標應為 WGS84 lon/lat，或在繪圖前明確轉換到 WGS84。
- 來源、版本、授權與本機路徑必須寫入圖面附註或對應 JSON metadata。
- 若用於正式報告，需確認圖資是否足以代表目標尺度的岸線與離島輪廓。

## 圖層分工

此方法刻意把「資料判讀」和「視覺地理參照」分開：

- `lon.npy`、`lat.npy`：OCM 規則格點座標，決定流場與 mask 的資料解析度。
- `u.npy`、`v.npy`、`speed.npy`：流場資料來源，仍使用 `time, layer, lat, lon` 的 1 km 或 10 km 格點。
- `mask.npy`：有效海域遮罩，True 代表可判讀海域，False 代表陸地、mesh 外或已遮蔽區域。
- `data/geojson/twCounty2010.geo.json`：只用於畫出更細緻的陸地輪廓與填色。

因此報告說明應使用類似措辭：

```text
流場資料解析度為 1 km；陸地輪廓另以 GeoJSON 向量多邊形疊加作為地理參照。
```

不應寫成：

```text
流場解析度已提升到高解析岸線尺度。
```

## 實作流程

核心流程如下：

1. 讀取 OCM 中間檔，包括 `lon.npy`、`lat.npy`、`mask.npy`、`u.npy`、`v.npy` 與 `speed.npy`。
2. 以 `mask.npy` 或有效流速格點繪製底圖，維持原始資料解析度與缺值語意。
3. 讀取 GeoJSON 陸域多邊形。
4. 對每個 `Polygon` 或 `MultiPolygon` 取外環 ring。
5. 只選取與目前圖面經緯度範圍相交的 ring，避免不必要的繪圖負擔。
6. 用 Matplotlib `Polygon` 將陸域向量多邊形疊在底圖上。
7. 若目標島嶼在全域圖上仍太小，另外建立 inset axes 重新繪製局部範圍。

概念性程式片段如下：

```python
from matplotlib.patches import Polygon

for ring in land_outer_rings:
    if ring_overlaps_extent(ring, map_extent):
        ax.add_patch(
            Polygon(
                ring,
                closed=True,
                facecolor=LAND_COLOR,
                edgecolor="#4f4f4f",
                linewidth=0.42,
                zorder=4,
            )
        )
```

其中 `ring` 是形狀 `(n, 2)` 的經緯度座標陣列，第一欄是經度，第二欄是緯度。`zorder` 需高於海域底圖，
但低於區域框、標籤、座標點與流場箭頭，避免陸地填色遮住重要標註。

## 和 `--land-geojson` 的差異

`--land-geojson` 是前處理階段的資料遮罩功能；向量陸地疊圖是繪圖階段的視覺功能。兩者用途不同：

- `--land-geojson`：把與陸域 polygon 接觸的規則格點從 `mask.npy` 扣除，並將對應資料寫成 NaN。
- 向量陸地疊圖：在圖面上額外畫出 GeoJSON 多邊形，讓海岸與小島輪廓更清楚。

兩者可以同時使用。前處理遮罩確保資料不在陸地上被判讀；向量疊圖確保報告圖面上的陸地看起來不像粗格點方塊。

## Inset 放大框

即使用向量多邊形，北竿、南竿、龜山島這類小島在 `[119, 123] x [20, 27]` 全域圖上仍可能太小。
此時建議加入局部放大框：

- 南竿 / 北竿放大：用於確認連江縣小島輪廓。
- 龜山島放大：用於確認宜蘭外海小島輪廓。
- 貢寮海域放大：用於確認東北角岬角、沿岸與貢寮區域框位置。

放大框應放在不遮擋主要標籤、圖例、比例尺與研究區域的海面位置。若放大框無法避免遮擋，優先移動描述標籤；
若仍衝突，再調整放大框尺寸或位置。成果圖完成前應目視檢查所有文字框、圖例、比例尺、區域框與 inset 是否互相遮擋。

## 報告使用建議

成果報告可把此做法描述為：

```text
本圖的流場資料為 OCM 1 km 規則格點重採樣結果；為提高海岸線、離島與研究區域位置辨識度，
圖面另疊加台灣 GeoJSON 陸域向量多邊形作為地理參照。向量陸域不改變流場解析度或統計結果。
```

若圖面含區域框與 Google Maps 校正點，建議另加註：

```text
彩色框為依地圖座標核對後的近似展示框，非正式作業範圍或等深線 GIS 邊界。
```

## 限制與維護注意事項

- 向量陸域只改善視覺呈現，不提供更高解析度的流場資料。
- GeoJSON 邊界若年代、來源或精度不足，圖面陸地輪廓也會受限；正式報告應確認採用圖資來源。
- 若圖面 bbox 很大，直接繪製全部 GeoJSON polygon 可能較慢；應先用 ring bbox 篩選是否與圖面範圍相交。
- 若未來改用互動式地圖或非 lon/lat 投影，必須重新檢查投影轉換與座標軸語意。
- 若將此功能正式整合進 `visualize_ocm_month.py`，建議新增 CLI 參數，例如：

```bash
--land-overlay-geojson data/geojson/twCounty2010.geo.json
--land-overlay-insets gongliao,guishan,lienchiang
```

整合時需在 `monthly_summary.json` 或圖面 sidecar JSON 中記錄：

- 陸域向量資料路徑。
- 圖資來源與版本。
- 是否只作視覺疊圖。
- 每個 inset 的經緯度範圍與目的。
