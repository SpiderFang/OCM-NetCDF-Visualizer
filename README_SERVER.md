# Server / VS Code Remote SSH 操作說明

本文件集中記錄 server 端操作流程。主 `README.md` 保留本機與共通流程；凡是需要在
`mustlab@140.117.88.206` 上執行的前處理、批次月份、畫圖與監看指令，都以本文件為準。

## Server 資料結構

2025 年完整資料集位於 server：

```text
/CWA-OCM/2025
```

實際盤點結果顯示，2025 日檔平放在該根目錄下，例如：

```text
/CWA-OCM/2025/20250101_schout.nc
/CWA-OCM/2025/20250102_schout.nc
...
```

server 上不是 `/CWA-OCM/2025/01/`、`/CWA-OCM/2025/02/` 這種月份資料夾。
`scripts/run_ocm_2025_year.sh` 會在 `work/month_inputs/2025/MM/` 建立每月 symlink
staging 目錄，再把單月前處理流程套用上去。這個 staging 目錄只建立符號連結，不複製
大型 NetCDF 原始資料。

## 遠端環境準備

用 VS Code Remote SSH 進入 server 後，在遠端 terminal 執行：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer
UV_CACHE_DIR=work/uv-cache uv sync
```

## 長時間工作一律使用 tmux

Server 上的 NetCDF 前處理、年度 metadata 整理、GIF 渲染與大型驗收都必須在
`tmux` 工作階段執行，避免 SSH 或 VS Code Remote SSH 斷線後終止工作。新建工作階段、
離開而不停止工作、重新接回與監看範例如下：

```bash
tmux new -s ocm_surface_2024_2025
# 在 tmux 內執行長時間指令；按 Ctrl-b，再按 d，可安全離開工作階段
tmux attach -t ocm_surface_2024_2025
tmux ls
```

長時間指令應將 stdout/stderr 導向專屬 log，並以完成 marker 判斷是否完整結束；不能
只依 SSH 視窗是否仍開啟來判定工作狀態。若工作已在背景 tmux 中執行，可使用：

```bash
tail -f /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/logs/formal_preprocess_v2.log
tail -f /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/logs/formal_render_v8.log
test -f /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/intermediate_v2/PREPROCESS_COMPLETE && echo preprocess_complete
test -f /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_v8/RENDER_COMPLETE && echo render_complete
```

## 完整台灣周邊 1 km 表層動畫（2024–2025）

完整年度產品直接使用 `/CWA-OCM/2024` 與 `/CWA-OCM/2025` 的平放原始日 NetCDF，
不使用或修改既有 `/data/OCM-Preprocessed-Data/preprocessed`。本次專用輸出根目錄為：

```text
/data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/
```

前處理使用檔名日期修復 NetCDF `time units` 偏移，建立 `01/07/13/19 UTC` 的固定
6 小時時間軸；2025 年 10 個缺日建立位置後，以相鄰來源幀線性補值並標記。輸出網格
為 `780×409`、固定表層 `layer 047`、UGRID 優先插值、台灣周邊 `[119,123]E ×
[20,27]N`，完整說明與可重跑指令位於主文件
[`README.md`](README.md) 的「Server 完整台灣周邊 1 km 表層流場產品」段落。

正式成果的本次版本先獨立輸出至新建的版本資料夾：

```text
/data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_v8/
```

為避免重新渲染時覆蓋既有成果，本次版本重新配置固定流速版主圖與獨立 colorbar 軸，
以較寬畫布保留主圖高度與經緯度比例；同時將中性趨勢與年度備查版箭頭改為較明亮的
中深藍青 `#1f5f83`，並沿用箭頭目標約 2600 支、抽樣步距約 11 格、箭頭縮放倍數 16
與固定流速版白色箭頭，並採用中度灰米色陸地；四個研究區域框均
取消內部填色，中性趨勢版維持深藍框線，固定流速版改用低飽和磚紅框線。上述版本
完成驗證後同步至本機主專案的
`outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations/`；Server 的原
`animations/`、`animations_v2/`、`animations_v3/`、`animations_v4/`、`animations_v5/`、`animations_v6/` 與 `animations_v7/` 目錄均保留，不刪除、不覆寫。

其中趨勢版與固定流速備查版各 366 幀、`fps=4`、約 91.5 秒；完整年度備查版 2924
幀、`fps=4`、約 731 秒。新版每幀目標約 2600 支箭頭、抽樣步距約 11 格，並將箭頭
縮放倍數設為 16，以改善完整台灣周邊範圍的流場連續性；固定流速版箭頭改為白色，
中性趨勢版採用深藍系中深藍青 `#1f5f83`，以和深灰海岸線區分；固定流速版使用
`7.2×11.0` 英吋畫布，主圖高度與中性版一致，右側色條及 `流速(公尺/秒)` 標籤
保留獨立留白。三個版本的 `top` 邊界由 `0.935` 調整為 `0.9675`，使上方空白約縮減
一半，固定流速版 colorbar 也同步延伸至相同上界。三項 GIF 均使用整部動畫共用的固定 256 色盤且不使用
抖動量化，以避免固定色階版 colorbar 在時間幀之間跳動。三個版本的 XY 軸標籤為中文
「經度」與「緯度」，數值刻度仍代表實際地理座標。
固定色階版為 `0.0–2.0 m/s`，刻度固定為一位小數的 `0.0、0.5、1.0、1.5、2.0`，
標籤為 `流速(公尺/秒)`；這個色階不由資料最大值或百分位自動決定。陸地使用中度
灰米色；四個區域框皆為透明內部，僅保留外框，避免遮蔽局部流場。正式動畫預設
不含標題、區域名稱、日期、時間或狀態文字。

三個對應的 MP4 已在本機由上述 v8 GIF 轉製，使用 H.264、`yuv420p`、`fps=4` 且不含音訊；
影片保留 GIF 的影格順序、解析度、中文經緯度軸與無標題版面，可直接用於簡報播放。

## 四海域六層聯合 SVD 表層分量動畫：display-only coastline v2

本節記錄四海域簡報右側輔助動畫的正式流程。既有 2026-08-13 正式 SVD 的分析遮罩
不能直接視為真實海岸線，因此 renderer 在展示階段另套用 exact coastline；這是
`coastline_correction_scope: visualization_only`，不是 SVD 方法或結果重算。正式
資料旗標為 `svd_source_unchanged: true`，既有模態、時間係數、流場變異百分比、
達累積 90% 所需模態數與代表視窗均維持不變。既有 v1 SVD、v1 動畫、簡報 PPTX 與
任何正式分析結果均不修改。

正式 SVD 根目錄為：

```text
/home/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/2026-08-13_water_column_four_regions/water_column_svd/
```

四區 run 均已核對具備 `metadata.json`、`explained_variance.npy`、
`cumulative_explained_variance.npy`、`pc.npy`、`pc_standardized.npy`、
`time_utc_ns.npy`、6 層 `mode_u/v_mps_per_raw_pc.npy` 與經緯度／遮罩。正式原始表層
流場使用 metadata 追溯出的同源 cache：

```text
/data/OCM-Preprocessed-Data/preprocessed/ocm_surface/
```

使用的岸線檔案為：

```text
/data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/config/taiwan_exact_coastline.geojson
```

其 SHA-256 應為
`9e2e0ac9bc527aca87d89332cd428fdcb776eefbf94a85dd70f887f729b95fdd`。目前記錄為
FeatureCollection、1,905 個原始 features、1,912 個可 rasterize polygon groups。每個
1 km 規則網格 cell 的中心、任一角點或 GeoJSON ring vertex 接觸 land polygon 即標記
`coastline_land_mask=True`，洞環扣除；這是保守的 cell-overlap 近似，不代表潮汐乾濕線。

### 稽核與 display-only 渲染

稽核結果寫入新的動畫版本資料夾，絕不回寫既有成果：

```text
/data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/coastline_svd_land_audit.json
/data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/coastline_svd_land_audit.csv
```

exact coastline 檔案為：

```text
/data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/config/taiwan_exact_coastline.geojson
```

其 SHA-256 為
`9e2e0ac9bc527aca87d89332cd428fdcb776eefbf94a85dd70f887f729b95fdd`。目前記錄為
FeatureCollection、1,905 個原始 features、1,912 個可 rasterize polygon groups。
每個 1 km cell 的中心、任一角點或 ring vertex 接觸 polygon 即標記
`coastline_land_mask=True`，洞環扣除；這是保守的 cell-overlap 近似，不代表潮汐乾濕線。

早期建立的 `/home/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/2026-08-27_coastline_corrected_v2/`
與 `/data/OCM-Preprocessed-Data/preprocessed/ocm_surface_coastline_corrected_v2/`
只作 diagnostic／方法敏感度檢查；既有 C pilot comparison 也與正式 manifest 隔離，
不可作為正式動畫資料來源。正式流程不再啟動 A–D coastline-corrected SVD 重算。

renderer 的展示有效遮罩為
`analysis_geometry & static_ocean & surface_feature & ~coastline_land` 且逐時 `u/v`
有限；exact-land 只阻止底色與箭頭顯示，再以灰米色／深灰岸線向量 polygon 置於最高
z-order。分析域外、模型靜態域外、特徵未納入與逐時缺值不共用真實陸地語意。

### 由 tmux 執行正式 renderer 與地理 QA

正式動畫輸出至新的 Server 目錄：

```text
/data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/
```

所有長時間工作必須在 tmux 內執行；正式 renderer 只指定 2026-08-13 既有 SVD，
不帶任何 corrected-SVD suffix：

```bash
cd /home/mustlab/Workspace/OCM-SVD-Analysis
tmux new -s ocm_render_formal_display_only_v2
MPLCONFIGDIR=work/matplotlib-cache PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/python scripts/visualize_ocm_svd_modal_context.py \
  --svd-base /home/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/2026-08-13_water_column_four_regions/water_column_svd \
  --surface-cache-base /data/OCM-Preprocessed-Data/preprocessed/ocm_surface \
  --full-product-dir /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/intermediate_v2 \
  --coastline-geojson /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/config/taiwan_exact_coastline.geojson \
  --output-dir /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2 \
  --regions A,B,C,D --fps 4 --width 864 --height 1080 --target-arrows 420
```

重建公式固定為 `mean + Σ(mode_u/v_mps_per_raw_pc × pc.npy)`；`pc_standardized.npy`
只在內部用於模態 1 相位案例選取。兩段視窗各 28 個 6 小時資料影格，片頭片尾各約
1 秒，正式影片共 64 幀、4 fps、約 16 秒；四區共用固定 0–2.2 m/s 色階與真正代表
1 m/s 的箭頭圖例。

畫面文字完全遵守簡報原文：標題為 `海域 A（東北角）`、`海域 B（新竹外海）`、
`海域 C（後灣海域）`、`海域 D（連江海域）`；相位列為「模態 1 時間係數：正／負相位
案例」；caption 為「原始流場」及「前 n 個模態重建流場／累積流場變異百分比達
90%」。色條使用單一完整標籤「流速（公尺／秒）」並整行旋轉 90 度置於色階最外側，
箭頭圖例為 `1（公尺／秒）`，座標軸為「經度／緯度」。觀眾可見文字不得出現 `PC`、`K`、`K90`、`解釋變異`；內部 manifest
與 README 可保留 `pc.npy`、`pc_standardized.npy`、K90 等資料欄位名稱。

渲染後在同一 tmux 或另一個 tmux session 執行地理疊圖：

```bash
MPLCONFIGDIR=work/matplotlib-cache PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/python scripts/make_coastline_svd_qa_overlays.py \
  --svd-base /home/mustlab/Workspace/OCM-SVD-Analysis/work/server_results/2026-08-13_water_column_four_regions/water_column_svd \
  --surface-cache-base /data/OCM-Preprocessed-Data/preprocessed/ocm_surface \
  --full-product-dir /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/intermediate_v2 \
  --coastline-geojson /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/config/taiwan_exact_coastline.geojson \
  --output-dir /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2 \
  --regions A,B,C,D
```

ffprobe 驗證可在本機執行：

```bash
.venv/bin/python scripts/validate_ocm_svd_modal_context.py \
  --manifest /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/animation_manifest.json \
  --coastline-geojson /data/OCM-Preprocessed-Data/ocm_2024_2025_taiwan_1km_surface_6h_v1/config/taiwan_exact_coastline.geojson \
  --expected-width 864 --expected-height 1080 --expected-fps 4 --expected-duration 16
```

`qa.all_passed=true` 必須同時滿足 H.264、`yuv420p`、864×1080、4 fps、無音訊、首／中／末
幀、coastline hash、polygon／land cell count、exact-land finite render=0、land-arrow=0、
分析域外未被標作陸地，以及四區文字 allowlist／denylist。完成檔案同步至本機：

```text
/Users/mustlab/Workspace/OCM-NetCDF-Visualizer/outputs/ocm_2024_2025_taiwan_1km_surface_6h_v1/animations_svd_modal_context_coastline_corrected_v2/
```

## Smoke Test

先跑單日 smoke test，確認遠端 Python 環境、NetCDF 讀取、平放來源 staging 與輸出
shape 都正常。此範例只取 2025 年 2 月第一個日檔，並把結果寫到 `outputs/server_smoke/`：

```bash
MONTHS=02 \
SOURCE_LAYOUT=flat \
OUTPUT_ROOT=outputs/server_smoke \
OUTPUT_SUFFIX=taiwan_10km_3h_smoke \
RUN_VISUALIZE=0 \
INCLUDE_ZCOR_TIME=0 \
INCLUDE_ELEV=1 \
MAX_FILES=1 \
TIME_STRIDE=999 \
REPROCESS=1 \
bash scripts/run_ocm_2025_year.sh
```

## 單月前處理

正式處理建議維持「一次一個月」。以下範例只處理 2025 年 1 月，輸出到
`outputs/ocm_2025_01_taiwan_10km_3h/`；確認該月輸出與摘要後，再把 `MONTHS=01`
改成 `MONTHS=02` 處理下一個月：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer

MONTHS=01 \
SOURCE_LAYOUT=flat \
RUN_VISUALIZE=0 \
RUN_3D=0 \
RUN_3D_ANIMATION=0 \
INCLUDE_ELEV=1 \
INCLUDE_ZCOR_TIME=1 \
REPROCESS=0 \
bash scripts/run_ocm_2025_year.sh
```

## 單月前處理：GeoJSON 陸地遮罩 QC

此範例對應主 `README.md`「GeoJSON 陸地遮罩 QC 範例」中的前處理指令。差異是
server 原始資料平放在 `/CWA-OCM/2025`，因此改用 `SOURCE_LAYOUT=flat`，並由
`scripts/run_ocm_2025_year.sh` 建立該月 symlink staging 目錄。輸出會寫到：

```text
outputs/ocm_2025_01_taiwan_10km_geojson_qc
```

第一次使用前，先確認 server 專案內有 GeoJSON 圖資：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer
mkdir -p data/geojson
curl -L https://raw.githubusercontent.com/g0v/twgeojson/master/json/twCounty2010.geo.json \
  -o data/geojson/twCounty2010.geo.json
```

一月份 GeoJSON 陸地遮罩 QC 前處理指令：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer

MONTHS=01 \
SOURCE_LAYOUT=flat \
OUTPUT_SUFFIX=taiwan_10km_geojson_qc \
DOMAIN_ID=taiwan-surrounding-geojson-qc \
BBOX="119.0 123.0 20.0 27.0" \
TARGET_RESOLUTION_KM=10 \
TIME_STRIDE=3 \
APPLY_LAND_GEOJSON=1 \
LAND_GEOJSON=data/geojson/twCounty2010.geo.json \
INCLUDE_ZCOR_TIME=1 \
INCLUDE_ELEV=1 \
RUN_VISUALIZE=0 \
RUN_3D=0 \
RUN_3D_ANIMATION=0 \
REPROCESS=1 \
bash scripts/run_ocm_2025_year.sh
```

若要處理二月份，只要把 `MONTHS=01` 改成 `MONTHS=02`；輸出會變成：

```text
outputs/ocm_2025_02_taiwan_10km_geojson_qc
```

## 選用：一次補跑 2 月到 5 月

若已確認前面月份設定正確，也可以一次補跑連續幾個月份。以下範例會依序處理
2025 年 2 月、3 月、4 月與 5 月，輸出仍會分成獨立月份資料夾：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer

MONTHS="02 03 04 05" \
SOURCE_LAYOUT=flat \
RUN_VISUALIZE=0 \
INCLUDE_ELEV=1 \
INCLUDE_ZCOR_TIME=1 \
REPROCESS=0 \
bash scripts/run_ocm_2025_year.sh
```

若要把 2 月到 5 月補跑工作留在 server 背景執行，使用 tmux：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer
tmux new -s ocm_2025_02_05
# 在 tmux 內執行：
MONTHS="02 03 04 05" SOURCE_LAYOUT=flat RUN_VISUALIZE=0 INCLUDE_ELEV=1 INCLUDE_ZCOR_TIME=1 REPROCESS=0 bash scripts/run_ocm_2025_year.sh \
  > work/logs/ocm_2025_months_02_05.log 2>&1
```

監看 2 月到 5 月補跑進度：

```bash
tmux attach -t ocm_2025_02_05
tail -f work/logs/ocm_2025_months_02_05.log
tail -f work/logs/ocm_2025_year/2025_02_preprocess.log
```

`REPROCESS=0` 代表若某月份必要輸出已存在且完整，就跳過該月份；若需要強制重跑
2 月到 5 月，才改成 `REPROCESS=1`。

## 其它年份資料集：2024 平放日檔

`scripts/run_ocm_2025_year.sh` 雖然檔名含有 `2025`，但年份、來源根目錄與來源結構都可
用環境變數覆蓋。若 2024 年資料集位於 `/CWA-OCM/2024`，且日檔同樣平放在該目錄下，
例如 `/CWA-OCM/2024/20240101_schout.nc`，不需要修改 shell 檔本身，只要在執行時指定：

- `YEAR=2024`：讓腳本尋找 `2024MMDD_schout.nc`，並把輸出命名成 `ocm_2024_MM_*`。
- `SOURCE_ROOT=/CWA-OCM/2024`：指定 2024 原始 NetCDF 所在根目錄。
- `SOURCE_LAYOUT=flat`：表示日檔平放在年份根目錄，腳本會建立每月 symlink staging。

單月處理 2024 年 1 月：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer

YEAR=2024 \
SOURCE_ROOT=/CWA-OCM/2024 \
SOURCE_LAYOUT=flat \
MONTHS=01 \
RUN_VISUALIZE=0 \
RUN_3D=0 \
RUN_3D_ANIMATION=0 \
INCLUDE_ELEV=1 \
INCLUDE_ZCOR_TIME=1 \
REPROCESS=0 \
bash scripts/run_ocm_2025_year.sh
```

處理下一個月份時只要改 `MONTHS`，例如 `MONTHS=02`。輸出與工作目錄會自動改用
2024 年份：

```text
outputs/ocm_2024_01_taiwan_10km_3h
work/month_inputs/2024/01
work/logs/ocm_2024_year
```

若要一次補跑 2024 年 2 月到 5 月：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer

YEAR=2024 \
SOURCE_ROOT=/CWA-OCM/2024 \
SOURCE_LAYOUT=flat \
MONTHS="02 03 04 05" \
RUN_VISUALIZE=0 \
INCLUDE_ELEV=1 \
INCLUDE_ZCOR_TIME=1 \
REPROCESS=0 \
bash scripts/run_ocm_2025_year.sh
```

## 單月背景執行與監看

若 VS Code terminal 可能中斷，單月工作也必須放在 `tmux`；不要以未受保護的遠端
terminal 直接執行：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer
tmux new -s ocm_2025_01
# 在 tmux 內執行：
MONTHS=01 SOURCE_LAYOUT=flat RUN_VISUALIZE=0 RUN_3D=0 RUN_3D_ANIMATION=0 INCLUDE_ELEV=1 INCLUDE_ZCOR_TIME=1 REPROCESS=0 bash scripts/run_ocm_2025_year.sh \
  > work/logs/ocm_2025_01.log 2>&1
```

監看進度：

```bash
tmux attach -t ocm_2025_01
tail -f work/logs/ocm_2025_01.log
tail -f work/logs/ocm_2025_year/2025_01_preprocess.log
```

## 前處理輸出

單月輸出範例：

- `outputs/ocm_2025_01_taiwan_10km_3h/`：該月份規則格點中間檔。
- `outputs/ocm_2025_01_run_summary.json`：單月完成檢查摘要。
- `outputs/ocm_2025_01_run_summary.csv`：方便人工掃描的單月表格。
- `work/logs/ocm_2025_year/YYYY_MM_preprocess.log`：每月前處理完整 log。

截至 2026-07-14 在 server `/CWA-OCM/2025` 的實際盤點結果，以下日期缺少
`*_schout.nc`：

- 2025-03：缺 `03`, `14`, `19`
- 2025-05：缺 `23`
- 2025-07：缺 `21`
- 2025-11：缺 `02`, `05`, `19`, `20`, `27`

批次腳本會處理現有檔案；摘要工具會把缺日列為 warning。對應月份的月平均、年度時間
序列與季節統計只反映可用日期，不能在補齊原始檔前宣稱是逐日完整全年資料。

## 單月 2D 畫圖

前處理完成後，`visualize_ocm_month.py` 會讀取月份資料夾內的 `.npy` 中間檔，並把
GIF/PNG 寫到該月份的 `figures/` 子資料夾。以下以 2025 年 2 月為例：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer

UV_CACHE_DIR=work/uv-cache \
MPLCONFIGDIR=work/matplotlib-cache \
uv run python3 scripts/visualize_ocm_month.py \
  --input-dir outputs/ocm_2025_02_taiwan_10km_3h \
  --output-dir outputs/ocm_2025_02_taiwan_10km_3h/figures \
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

## 多月 2D 畫圖

若 2025 年 2 月到 5 月都已完成前處理，可用迴圈逐月畫圖。每個月份仍會輸出到
各自的 `outputs/ocm_2025_MM_taiwan_10km_3h/figures/`，不會混在同一個資料夾：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer

for m in 02 03 04 05; do
  UV_CACHE_DIR=work/uv-cache \
  MPLCONFIGDIR=work/matplotlib-cache \
  uv run python3 scripts/visualize_ocm_month.py \
    --input-dir outputs/ocm_2025_${m}_taiwan_10km_3h \
    --output-dir outputs/ocm_2025_${m}_taiwan_10km_3h/figures \
    --surface-elev-anomaly-animation \
    --surface-elev-animation \
    --surface-animation \
    --layer-animation \
    --layer-indices 0,16,32,-1 \
    --background neutral \
    --frame-stride 1 \
    --fps 2 \
    --target-arrows 1000
done
```

`--fps 2` 適合保留較慢的播放速度，方便檢查潮汐水位與流向變化；如果只想快速預覽，
可以調高到 `--fps 4` 或 `--fps 8`。`--frame-stride 1` 代表使用前處理輸出的每個時間步；
若要快速檢查，可暫時改成 `--frame-stride 4` 降低繪圖時間。

## 全年 2D GIF 串接

若 2025 年 1 月到 12 月的同一種 2D GIF 都已畫好，可用
`scripts/concat_ocm_year_gifs.py` 把每月 GIF 依月份順序接成全年 GIF。此步驟只讀取
已完成的 `figures/*.gif`，不重跑 NetCDF 前處理，也不重畫每月影格。

串接主要研究圖 `surface_speed_elev_anomaly_quiver.gif`：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer

UV_CACHE_DIR=work/uv-cache \
uv run python3 scripts/concat_ocm_year_gifs.py \
  --year 2025 \
  --suffix taiwan_10km_3h \
  --figure-name surface_speed_elev_anomaly_quiver.gif \
  --fps 2
```

預設輸入為：

```text
outputs/ocm_2025_01_taiwan_10km_3h/figures/surface_speed_elev_anomaly_quiver.gif
...
outputs/ocm_2025_12_taiwan_10km_3h/figures/surface_speed_elev_anomaly_quiver.gif
```

輸出會寫到：

```text
outputs/ocm_2025_year_taiwan_10km_3h/figures/surface_speed_elev_anomaly_quiver.gif
outputs/ocm_2025_year_taiwan_10km_3h/figures/surface_speed_elev_anomaly_quiver.manifest.json
```

若要串接原始水位檢查圖，只改 `--figure-name`：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer

UV_CACHE_DIR=work/uv-cache \
uv run python3 scripts/concat_ocm_year_gifs.py \
  --year 2025 \
  --suffix taiwan_10km_3h \
  --figure-name surface_speed_elev_quiver.gif \
  --fps 2
```

若每月圖是 GeoJSON QC 版本，改用 `--suffix taiwan_10km_geojson_qc`。年度 GIF 的限制是
保留每月原本的色階、標題與 `elev_anomaly` 月平均基準；若要全年統一色階或全年平均
水位異常，需另外從 12 個月份的 `.npy` 中間檔重畫。
若某個月 GIF 讀取時同時出現 RGB 與 RGBA 影格，串接工具會先把影格合成為 RGB，
再檢查高寬是否一致；這可避免透明 channel 差異造成誤判，不需要為此重畫月份圖。

## 3D 靜態圖

靜態 3D 示意圖會輸出 `flow_field_3d.png`。以下以 2025 年 2 月為例：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer

UV_CACHE_DIR=work/uv-cache \
MPLCONFIGDIR=work/matplotlib-cache \
uv run python3 scripts/visualize_ocm_month.py \
  --input-dir outputs/ocm_2025_02_taiwan_10km_3h \
  --output-dir outputs/ocm_2025_02_taiwan_10km_3h/figures \
  --make-3d \
  --three-d-layers 0,16,32,-1 \
  --three-d-time-index 0 \
  --three-d-xy-step 3
```

## 3D 時間動畫

近表層 3D 時間動畫需要前處理時有輸出 `zcor.npy`，也就是前處理曾使用
`--include-zcor-time` 或 `INCLUDE_ZCOR_TIME=1`。此動畫會比 2D GIF 更耗時，建議在
主要 2D 圖完成後再產生：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer

UV_CACHE_DIR=work/uv-cache \
MPLCONFIGDIR=work/matplotlib-cache \
uv run python3 scripts/visualize_ocm_month.py \
  --input-dir outputs/ocm_2025_02_taiwan_10km_3h \
  --output-dir outputs/ocm_2025_02_taiwan_10km_3h/figures \
  --make-3d-animation \
  --three-d-layers 32,40,-1 \
  --three-d-frame-stride 4 \
  --three-d-xy-step 4 \
  --vertical-exaggeration 0.02 \
  --fps 2
```

## 常用輸出檔

- `surface_speed_elev_anomaly_quiver.gif`：表層流速搭配月平均水位異常，圖面含 m/s 流速箭頭比例尺。
- `surface_speed_elev_quiver.gif`：表層流速搭配原始 `η/elev` 水位，圖面含 m/s 流速箭頭比例尺。
- `surface_layer_047_horizontal_current_speed_quiver.gif`：表層中性底圖流場，圖面含 m/s 流速箭頭比例尺。
- `bottom_layer_000_horizontal_current_speed_quiver.gif`：底層中性底圖流場，圖面含 m/s 流速箭頭比例尺。
- `model_layer_016_horizontal_current_speed_quiver.gif`、`model_layer_032_horizontal_current_speed_quiver.gif`：指定中間層流場，圖面含 m/s 流速箭頭比例尺。
- `flow_field_3d.png`：靜態 3D 稀疏箭頭示意圖。
- `flow_field_3d_time_layers_032_040_047.gif`：近表層 3D 時間動畫。
- `outputs/ocm_2025_year_taiwan_10km_3h/figures/*.gif`：由每月 GIF 串接而成的全年 GIF。
- `outputs/ocm_2025_year_taiwan_10km_3h/figures/*.manifest.json`：全年 GIF 的來源月份、幀數、fps 與影格尺寸紀錄。

## 流速箭頭比例尺備註

2D quiver 圖的箭頭比例尺以同一段動畫、同一個 layer 的有效海域流速第 98 百分位作為
縮放基準 `vmax`。第 98 百分位代表約 98% 的有效流速不超過此值，可視為代表性高流速；
不用最大值是為了避免少數局部強流、邊界插值尖峰或資料雜訊把大多數箭頭壓得過短。
圖面右下角的 m/s 參考箭頭由同一個 `quiver` 物件的 `quiverkey` 產生，因此和主圖箭頭
使用相同縮放規則。參考箭頭標示值由 `0.5 * vmax` 轉成易讀的 `1/2/5 × 10^n` 刻度。
目前一月台灣 10 km / 3 小時表層檢查圖的例子為 `vmax_98pct=1.22541 m/s`，因此圖面
標示 `1 m/s` 參考箭頭。
