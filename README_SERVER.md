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

若要把 2 月到 5 月補跑工作留在 server 背景執行：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer
mkdir -p work/logs/ocm_2025_months

nohup bash -lc 'MONTHS="02 03 04 05" SOURCE_LAYOUT=flat RUN_VISUALIZE=0 INCLUDE_ELEV=1 INCLUDE_ZCOR_TIME=1 REPROCESS=0 bash scripts/run_ocm_2025_year.sh' \
  > work/logs/ocm_2025_months/2025_02_05_nohup.out 2>&1 &

echo $! > work/ocm_2025_02_05.pid
```

監看 2 月到 5 月補跑進度：

```bash
ps -p "$(cat work/ocm_2025_02_05.pid)" -o pid,etime,stat,cmd
tail -f work/logs/ocm_2025_months/2025_02_05_nohup.out
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

若 VS Code terminal 可能中斷，可用 `nohup` 讓單月工作留在 server 背景執行：

```bash
cd /home/mustlab/Workspace/OCM-NetCDF-Visualizer
mkdir -p work/logs/ocm_2025_month

nohup bash -lc 'MONTHS=01 SOURCE_LAYOUT=flat RUN_VISUALIZE=0 RUN_3D=0 RUN_3D_ANIMATION=0 INCLUDE_ELEV=1 INCLUDE_ZCOR_TIME=1 REPROCESS=0 bash scripts/run_ocm_2025_year.sh' \
  > work/logs/ocm_2025_month/2025_01_nohup.out 2>&1 &

echo $! > work/ocm_2025_01.pid
```

監看進度：

```bash
ps -p "$(cat work/ocm_2025_01.pid)" -o pid,etime,stat,cmd
tail -f work/logs/ocm_2025_month/2025_01_nohup.out
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

- `surface_speed_elev_anomaly_quiver.gif`：表層流速搭配月平均水位異常。
- `surface_speed_elev_quiver.gif`：表層流速搭配原始 `η/elev` 水位。
- `surface_layer_047_horizontal_current_speed_quiver.gif`：表層中性底圖流場。
- `bottom_layer_000_horizontal_current_speed_quiver.gif`：底層中性底圖流場。
- `model_layer_016_horizontal_current_speed_quiver.gif`、`model_layer_032_horizontal_current_speed_quiver.gif`：指定中間層流場。
- `flow_field_3d.png`：靜態 3D 稀疏箭頭示意圖。
- `flow_field_3d_time_layers_032_040_047.gif`：近表層 3D 時間動畫。
- `outputs/ocm_2025_year_taiwan_10km_3h/figures/*.gif`：由每月 GIF 串接而成的全年 GIF。
- `outputs/ocm_2025_year_taiwan_10km_3h/figures/*.manifest.json`：全年 GIF 的來源月份、幀數、fps 與影格尺寸紀錄。
