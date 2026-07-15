#!/usr/bin/env bash
#
# 2025 年 OCM/SCHISM 月資料批次處理入口。
#
# 使用情境：
#   1. 透過 VS Code Remote SSH 進入資料所在 server。
#   2. 在本專案根目錄執行此腳本。
#   3. 腳本讀取指定月份的每日 *_schout.nc，產生月資料中間檔、選用圖像輸出，
#      最後建立該次執行的摘要檢查檔。
#
# 設計重點：
#   - 月份彼此獨立，任一月份失敗時會停止，避免後續年度摘要誤把缺漏月份視為完成。
#   - 所有耗時命令都寫入 work/logs，方便在 VS Code terminal 或遠端 shell 回查。
#   - 預設參數對齊 README 的台灣周邊 10 km / 3 小時設定；若要改 bbox、解析度或圖像
#     產出，使用環境變數覆蓋，不需要修改腳本內容。
#   - GeoJSON 陸域遮罩預設不啟用，因為 data/geojson/ 是外部下載資料且不一定存在於
#     server clone；若 server 已放好同版圖資，可設定 APPLY_LAND_GEOJSON=1。

set -Eeuo pipefail

# 年份與來源資料根目錄。支援兩種來源結構：
#   1. 月資料夾：SOURCE_ROOT/01、SOURCE_ROOT/02 ... 內含 *_schout.nc。
#   2. 平放日檔：SOURCE_ROOT/20250101_schout.nc、SOURCE_ROOT/20250102_schout.nc ...
#      此時腳本會在 work/month_inputs/YEAR/MM 建 symlink staging 目錄，不複製大型 NetCDF。
YEAR="${YEAR:-2025}"
SOURCE_ROOT="${SOURCE_ROOT:-/CWA-OCM/2025}"
SOURCE_LAYOUT="${SOURCE_LAYOUT:-auto}"

# 輸出根目錄與月份輸出命名後綴。OUTPUT_SUFFIX 代表 bbox、解析度與時間抽樣語意，
# 讓不同實驗設定可在同一個 outputs/ 底下並存。
OUTPUT_ROOT="${OUTPUT_ROOT:-outputs}"
OUTPUT_SUFFIX="${OUTPUT_SUFFIX:-taiwan_10km_3h}"

# 預設只處理單一月份，符合「一個月接一個月」的正式工作模式。若要處理其它月份，
# 明確指定 MONTHS，例如：
#   MONTHS="02" bash scripts/run_ocm_2025_year.sh
# 若真的要一次跑多月，仍可用 MONTHS="02 03"，但不建議在未檢查前一月輸出前連跑。
MONTHS="${MONTHS:-01}"

# 研究區域與重採樣設定。BBOX 的四個值依序為 lon_min lon_max lat_min lat_max，
# 單位是 WGS84 經緯度；TARGET_RESOLUTION_KM 是目標規則格點約略水平間距。
DOMAIN_ID="${DOMAIN_ID:-taiwan-surrounding}"
BBOX="${BBOX:-119.0 123.0 20.0 27.0}"
TARGET_RESOLUTION_KM="${TARGET_RESOLUTION_KM:-10}"
SOURCE_MARGIN_DEG="${SOURCE_MARGIN_DEG:-0.25}"
TIME_STRIDE="${TIME_STRIDE:-3}"
MAX_FILES="${MAX_FILES:-}"

# 欄位輸出控制。INCLUDE_ELEV=1 會輸出 elev.npy，供水位與水位異常底圖使用；
# INCLUDE_ZCOR_TIME=1 會輸出逐時 zcor.npy，檔案較大，但可支援 3D 時間動畫。
INCLUDE_ELEV="${INCLUDE_ELEV:-1}"
INCLUDE_ZCOR_TIME="${INCLUDE_ZCOR_TIME:-1}"

# 選用外部 GeoJSON 陸域遮罩。此遮罩是靜態岸線/行政區 polygon，只會扣除靜態
# mask.npy 內的陸域格點，不會取代 SCHISM wetdry_elem 的逐時乾濕遮罩。
APPLY_LAND_GEOJSON="${APPLY_LAND_GEOJSON:-0}"
LAND_GEOJSON="${LAND_GEOJSON:-data/geojson/twCounty2010.geo.json}"

# 圖像輸出控制。RUN_VISUALIZE=1 會為每個月輸出主要 2D GIF；3D 圖與 3D 時間動畫
# 較耗時，預設關閉，可依需求另外開啟。
RUN_VISUALIZE="${RUN_VISUALIZE:-1}"
RUN_3D="${RUN_3D:-0}"
RUN_3D_ANIMATION="${RUN_3D_ANIMATION:-0}"
LAYER_INDICES="${LAYER_INDICES:-0,16,32,-1}"
FRAME_STRIDE="${FRAME_STRIDE:-1}"
FPS="${FPS:-2}"
TARGET_ARROWS="${TARGET_ARROWS:-1000}"
THREE_D_LAYERS="${THREE_D_LAYERS:-0,16,32,-1}"
THREE_D_TIME_LAYERS="${THREE_D_TIME_LAYERS:-32,40,-1}"
THREE_D_FRAME_STRIDE="${THREE_D_FRAME_STRIDE:-4}"
THREE_D_XY_STEP="${THREE_D_XY_STEP:-4}"
VERTICAL_EXAGGERATION="${VERTICAL_EXAGGERATION:-0.02}"

# REPROCESS=0 時，若某月份必要輸出已存在，會跳過前處理；REPROCESS=1 則重跑。
# 圖像輸出同樣遵守 REPROCESS，避免不小心覆蓋已完成 GIF。
REPROCESS="${REPROCESS:-0}"

# uv 與 Matplotlib cache 固定在專案 work/ 底下，避免 server 上的家目錄 cache 權限、
# NFS latency 或 quota 影響批次處理。
export UV_CACHE_DIR="${UV_CACHE_DIR:-work/uv-cache}"
export MPLCONFIGDIR="${MPLCONFIGDIR:-work/matplotlib-cache}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

LOG_ROOT="${LOG_ROOT:-work/logs/ocm_${YEAR}_year}"
YEAR_SUMMARY="${YEAR_SUMMARY:-}"
MONTH_INPUT_ROOT="${MONTH_INPUT_ROOT:-work/month_inputs/${YEAR}}"
SUMMARY_STRICT="${SUMMARY_STRICT:-auto}"
ALLOW_PARTIAL_YEAR="${ALLOW_PARTIAL_YEAR:-auto}"

mkdir -p "$OUTPUT_ROOT" "$UV_CACHE_DIR" "$MPLCONFIGDIR" "$LOG_ROOT" "$MONTH_INPUT_ROOT"

log_info() {
  # 統一 log 前綴，讓長時間遠端批次執行時可從 VS Code terminal 快速辨識階段。
  printf '[ocm-year] %s\n' "$*"
}

require_binary() {
  # 在正式跑資料前確認必要命令存在；若 server 尚未安裝 uv 或 Python 環境未建好，
  # 早點失敗比跑到第一個月份才中斷更容易定位問題。
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    printf '缺少必要命令：%s\n' "$name" >&2
    return 1
  fi
}

month_number() {
  # Bash 對前導 0 的數字可能以八進位解讀；10# 強制用十進位，確保 08、09 合法。
  local month="$1"
  printf '%d' "$((10#$month))"
}

month_output_dir() {
  # 月輸出資料夾名稱包含年份、月份與設定後綴，便於同一年度保留多組實驗結果。
  local month="$1"
  printf '%s/ocm_%s_%s_%s' "$OUTPUT_ROOT" "$YEAR" "$month" "$OUTPUT_SUFFIX"
}

has_month_source_files() {
  # 使用 compgen 檢查 glob 是否有匹配檔案，避免未匹配的萬用字元原樣傳進 Python。
  local input_dir="$1"
  compgen -G "${input_dir}"'/*_schout.nc' >/dev/null
}

prepare_flat_month_input() {
  # SOURCE_ROOT 若是平放日檔，preprocess_ocm_month.py 仍需要「單月資料夾」作為輸入。
  # 這裡以 symlink 建立 staging 目錄，只引用原始 NetCDF，不複製數十 GB 原始資料；
  # 若同一月份缺日，仍會保留現有檔案並讓年度摘要與 log 揭露缺漏，因為實務上可能需要
  # 先處理可用日期，再回頭補資料。
  local month="$1"
  local staging_dir="${MONTH_INPUT_ROOT}/${month}"
  mkdir -p "$staging_dir"

  # 清掉舊 symlink，避免 SOURCE_ROOT 改變後 staging 還指向上一批資料。只刪除 symlink，
  # 不刪除一般檔案，避免誤動使用者手動放進 staging 的大型資料。
  find "$staging_dir" -maxdepth 1 -type l -name '*_schout.nc' -delete

  local matched=0
  local source_file
  for source_file in "${SOURCE_ROOT}/${YEAR}${month}"??_schout.nc; do
    [[ -e "$source_file" ]] || continue
    ln -s "$source_file" "${staging_dir}/$(basename "$source_file")"
    matched=$((matched + 1))
  done

  if [[ "$matched" -eq 0 ]]; then
    printf '平放來源找不到月份 %s 的日檔：%s/%s%s??_schout.nc\n' "$month" "$SOURCE_ROOT" "$YEAR" "$month" >&2
    return 1
  fi
  printf '%s\n' "$staging_dir"
}

resolve_month_input_dir() {
  # 依 SOURCE_LAYOUT 選擇實際傳給前處理的月份資料夾。auto 模式會先採用既有月份資料夾，
  # 若不存在或無檔案，再退回平放日檔 staging。這支援本機 README 的 01/02 月資料夾慣例，
  # 也支援 server 上 /CWA-OCM/2025 根目錄平放每日檔案的實際結構。
  local month="$1"
  local month_dir="${SOURCE_ROOT}/${month}"

  if [[ "$SOURCE_LAYOUT" == "month_dirs" || "$SOURCE_LAYOUT" == "auto" ]]; then
    if [[ -d "$month_dir" ]] && has_month_source_files "$month_dir"; then
      printf '%s\n' "$month_dir"
      return 0
    fi
    if [[ "$SOURCE_LAYOUT" == "month_dirs" ]]; then
      printf '找不到月份資料夾或日檔：%s\n' "$month_dir" >&2
      return 1
    fi
  fi

  if [[ "$SOURCE_LAYOUT" == "flat" || "$SOURCE_LAYOUT" == "auto" ]]; then
    prepare_flat_month_input "$month"
    return $?
  fi

  printf '不支援的 SOURCE_LAYOUT：%s；可用值為 auto、month_dirs、flat\n' "$SOURCE_LAYOUT" >&2
  return 1
}

month_preprocess_done() {
  # 完成條件聚焦在後續分析必需的中間檔。若啟用 elev 或 zcor time，也會要求對應檔案；
  # 這讓中斷後重跑時可安全跳過已完整的月份。
  local output_dir="$1"
  local required_files=(
    monthly_summary.json
    lon.npy
    lat.npy
    time_iso.npy
    sigma.npy
    u.npy
    v.npy
    speed.npy
    zcor_mean.npy
    bathymetry.npy
    mask.npy
  )

  if [[ "$INCLUDE_ELEV" == "1" ]]; then
    required_files+=(elev.npy)
  fi
  if [[ "$INCLUDE_ZCOR_TIME" == "1" ]]; then
    required_files+=(zcor.npy)
  fi

  local file
  for file in "${required_files[@]}"; do
    [[ -s "${output_dir}/${file}" ]] || return 1
  done
}

month_figures_done() {
  # 主要 2D 圖像完成條件。這裡只檢查正式研究與檢查常用 GIF，不要求選用 3D 產物。
  local figure_dir="$1"
  local required_figures=(
    surface_speed_elev_anomaly_quiver.gif
    surface_speed_elev_quiver.gif
    surface_layer_047_horizontal_current_speed_quiver.gif
    bottom_layer_000_horizontal_current_speed_quiver.gif
    model_layer_016_horizontal_current_speed_quiver.gif
    model_layer_032_horizontal_current_speed_quiver.gif
  )

  local figure
  for figure in "${required_figures[@]}"; do
    [[ -s "${figure_dir}/${figure}" ]] || return 1
  done
}

run_logged() {
  # 執行耗時命令並同步寫入 log 檔。輸出同時留在 terminal，是為了遠端處理時能即時看到
  # 目前月份進度；log 檔則供失敗後回查完整 traceback 或 Matplotlib 訊息。
  local log_file="$1"
  shift
  mkdir -p "$(dirname "$log_file")"
  log_info "寫入 log: ${log_file}"
  "$@" 2>&1 | tee "$log_file"
}

build_preprocess_command() {
  # 組出月前處理命令。使用 Bash array 可避免 bbox、路徑或參數中有空白時被 shell
  # 重新切字；BBOX 會刻意展開成四個參數，符合 preprocess_ocm_month.py 的 CLI。
  local input_dir="$1"
  local output_dir="$2"
  local month="$3"
  local -n command_ref="$4"

  read -r -a bbox_values <<<"$BBOX"
  if [[ "${#bbox_values[@]}" -ne 4 ]]; then
    printf 'BBOX 必須剛好包含四個值，目前為：%s\n' "$BBOX" >&2
    return 1
  fi

  command_ref=(
    uv run python3 scripts/preprocess_ocm_month.py
    --input-dir "$input_dir"
    --output-dir "$output_dir"
    --year "$YEAR"
    --month "$(month_number "$month")"
    --domain-id "$DOMAIN_ID"
    --bbox "${bbox_values[@]}"
    --target-resolution-km "$TARGET_RESOLUTION_KM"
    --source-margin-deg "$SOURCE_MARGIN_DEG"
    --time-stride "$TIME_STRIDE"
  )

  if [[ -n "$MAX_FILES" ]]; then
    # MAX_FILES 只用於 smoke test 或分段除錯；正式全年處理應留空，讓每個月份讀取所有
    # 可用日檔。此限制由 preprocess_ocm_month.py 套用在排序後的月日檔清單上。
    command_ref+=(--max-files "$MAX_FILES")
  fi

  if [[ "$INCLUDE_ELEV" == "1" ]]; then
    command_ref+=(--include-elev)
  fi
  if [[ "$INCLUDE_ZCOR_TIME" == "1" ]]; then
    command_ref+=(--include-zcor-time)
  fi
  if [[ "$APPLY_LAND_GEOJSON" == "1" ]]; then
    if [[ ! -s "$LAND_GEOJSON" ]]; then
      printf 'APPLY_LAND_GEOJSON=1 但找不到 GeoJSON：%s\n' "$LAND_GEOJSON" >&2
      return 1
    fi
    command_ref+=(--land-geojson "$LAND_GEOJSON")
  fi
}

build_visualize_command() {
  # 組出每月主要 2D 圖像命令。表層水位異常、原始水位、表層中性底圖與指定垂向層
  # 會分開輸出，避免把不同物理量混在同一張圖造成判讀混淆。
  local output_dir="$1"
  local -n command_ref="$2"

  command_ref=(
    uv run python3 scripts/visualize_ocm_month.py
    --input-dir "$output_dir"
    --output-dir "${output_dir}/figures"
    --surface-elev-anomaly-animation
    --surface-elev-animation
    --surface-animation
    --layer-animation
    --layer-indices "$LAYER_INDICES"
    --background neutral
    --frame-stride "$FRAME_STRIDE"
    --fps "$FPS"
    --target-arrows "$TARGET_ARROWS"
  )
}

build_3d_command() {
  # 組出選用的 3D 靜態圖命令。此圖使用 zcor_mean 作為垂向位置，適合快速檢查
  # 層化結構與不同 layer 的方向差異，不代表固定深度重採樣。
  local output_dir="$1"
  local -n command_ref="$2"

  command_ref=(
    uv run python3 scripts/visualize_ocm_month.py
    --input-dir "$output_dir"
    --output-dir "${output_dir}/figures"
    --make-3d
    --three-d-layers "$THREE_D_LAYERS"
    --three-d-time-index 0
    --three-d-xy-step "$THREE_D_XY_STEP"
  )
}

build_3d_animation_command() {
  # 組出選用的 3D 時間動畫命令。此動畫需要 zcor.npy，因此若 INCLUDE_ZCOR_TIME=0，
  # 這個階段會直接報錯，避免用月平均 zcor 假裝逐時水位變動。
  local output_dir="$1"
  local -n command_ref="$2"

  command_ref=(
    uv run python3 scripts/visualize_ocm_month.py
    --input-dir "$output_dir"
    --output-dir "${output_dir}/figures"
    --make-3d-animation
    --three-d-layers "$THREE_D_TIME_LAYERS"
    --three-d-frame-stride "$THREE_D_FRAME_STRIDE"
    --three-d-xy-step "$THREE_D_XY_STEP"
    --vertical-exaggeration "$VERTICAL_EXAGGERATION"
    --fps "$FPS"
  )
}

process_month() {
  # 單一月份完整流程：檢查來源、前處理、選用視覺化，並把輸出資料夾回傳給年度摘要。
  local month="$1"
  local input_dir
  local output_dir
  input_dir="$(resolve_month_input_dir "$month")"
  output_dir="$(month_output_dir "$month")"

  log_info "月份 ${YEAR}-${month}: 來源 ${input_dir}"
  if ! has_month_source_files "$input_dir"; then
    printf '月份資料夾沒有 *_schout.nc：%s\n' "$input_dir" >&2
    return 1
  fi

  mkdir -p "$output_dir"

  local preprocess_cmd=()
  if [[ "$REPROCESS" != "1" ]] && month_preprocess_done "$output_dir"; then
    log_info "月份 ${YEAR}-${month}: 前處理輸出已存在，跳過。"
  else
    build_preprocess_command "$input_dir" "$output_dir" "$month" preprocess_cmd
    run_logged "${LOG_ROOT}/${YEAR}_${month}_preprocess.log" "${preprocess_cmd[@]}"
  fi

  if [[ "$RUN_VISUALIZE" == "1" ]]; then
    local visualize_cmd=()
    if [[ "$REPROCESS" != "1" ]] && month_figures_done "${output_dir}/figures"; then
      log_info "月份 ${YEAR}-${month}: 主要 2D 圖像已存在，跳過。"
    else
      build_visualize_command "$output_dir" visualize_cmd
      run_logged "${LOG_ROOT}/${YEAR}_${month}_visualize.log" "${visualize_cmd[@]}"
    fi
  fi

  if [[ "$RUN_3D" == "1" ]]; then
    local three_d_cmd=()
    build_3d_command "$output_dir" three_d_cmd
    run_logged "${LOG_ROOT}/${YEAR}_${month}_3d.log" "${three_d_cmd[@]}"
  fi

  if [[ "$RUN_3D_ANIMATION" == "1" ]]; then
    local three_d_animation_cmd=()
    build_3d_animation_command "$output_dir" three_d_animation_cmd
    run_logged "${LOG_ROOT}/${YEAR}_${month}_3d_animation.log" "${three_d_animation_cmd[@]}"
  fi

  PROCESSED_MONTH_DIRS+=("$output_dir")
}

write_year_summary() {
  # 年度摘要只讀取各月輸出的 .npy shape 與 monthly_summary.json 統計，不重新載入整個
  # 速度場到記憶體；目的在於完成檢查與後續合併前的品質控管。
  local processed_count="${#PROCESSED_MONTH_DIRS[@]}"
  local summary_cmd=(
    uv run python3 scripts/summarize_ocm_year.py
    --year "$YEAR"
    --output "$YEAR_SUMMARY"
    --csv-output "${YEAR_SUMMARY%.json}.csv"
  )

  # 單月或少數月份分批處理時，不應因為沒有提供其它月份就讓摘要工具回傳失敗；
  # 但若使用者明確一次提供 12 個月份，仍維持完整年度檢查語意。
  if [[ "$ALLOW_PARTIAL_YEAR" == "1" || ( "$ALLOW_PARTIAL_YEAR" == "auto" && "$processed_count" -ne 12 ) ]]; then
    summary_cmd+=(--allow-partial-year)
  fi
  if [[ "$SUMMARY_STRICT" == "1" || ( "$SUMMARY_STRICT" == "auto" && "$processed_count" -eq 12 ) ]]; then
    summary_cmd+=(--strict)
  fi

  if [[ "$INCLUDE_ELEV" == "1" ]]; then
    summary_cmd+=(--require-elev)
  fi
  if [[ "$INCLUDE_ZCOR_TIME" == "1" ]]; then
    summary_cmd+=(--require-zcor-time)
  fi

  summary_cmd+=("${PROCESSED_MONTH_DIRS[@]}")
  run_logged "${LOG_ROOT}/${YEAR}_summary.log" "${summary_cmd[@]}"
}

main() {
  # 主流程保持薄層：環境檢查、指定月份處理、摘要。資料科學邏輯留在 Python 腳本，
  # Bash 只負責批次 orchestration，降低遠端長時間工作時的維護風險。
  require_binary uv
  require_binary python3

  if [[ ! -d "$SOURCE_ROOT" ]]; then
    printf '找不到 SOURCE_ROOT：%s\n' "$SOURCE_ROOT" >&2
    return 1
  fi

  log_info "開始 ${YEAR} 年月份批次處理"
  log_info "SOURCE_ROOT=${SOURCE_ROOT}"
  log_info "SOURCE_LAYOUT=${SOURCE_LAYOUT}"
  log_info "OUTPUT_ROOT=${OUTPUT_ROOT}"
  log_info "MONTHS=${MONTHS}"
  if [[ -n "$MAX_FILES" ]]; then
    log_info "MAX_FILES=${MAX_FILES}（僅適合 smoke test，正式處理請留空）"
  fi
  log_info "RUN_VISUALIZE=${RUN_VISUALIZE}, RUN_3D=${RUN_3D}, RUN_3D_ANIMATION=${RUN_3D_ANIMATION}"

  read -r -a month_list <<<"$MONTHS"
  if [[ -z "$YEAR_SUMMARY" ]]; then
    if [[ "${#month_list[@]}" -eq 1 ]]; then
      YEAR_SUMMARY="${OUTPUT_ROOT}/ocm_${YEAR}_${month_list[0]}_run_summary.json"
    elif [[ "${#month_list[@]}" -eq 12 ]]; then
      YEAR_SUMMARY="${OUTPUT_ROOT}/ocm_${YEAR}_year_summary.json"
    else
      YEAR_SUMMARY="${OUTPUT_ROOT}/ocm_${YEAR}_selected_months_summary.json"
    fi
  fi
  log_info "SUMMARY=${YEAR_SUMMARY}"

  PROCESSED_MONTH_DIRS=()
  local month
  for month in "${month_list[@]}"; do
    process_month "$month"
  done

  write_year_summary
  log_info "完成 ${YEAR} 年月份批次處理，摘要：${YEAR_SUMMARY}"
}

main "$@"
