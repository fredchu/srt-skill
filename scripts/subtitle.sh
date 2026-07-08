#!/bin/bash
# ============================================================
# 中文演講影片 → 台灣繁體字幕 一鍵流程
# 適用環境：macOS Apple Silicon (M1/M2/M3/M4)
# 依賴：ffmpeg, mlx-whisper, opencc
# ============================================================

set -euo pipefail

# ---------- 顏色輸出 ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ---------- 設定區（可自行修改）----------
MODEL="mlx-community/whisper-large-v3-mlx"   # Whisper 模型
LANGUAGE="zh"                                  # 語言：中文
OUTPUT_FORMAT="srt"                            # 輸出格式
OPENCC_CONFIG="s2twp"                          # 簡體→台灣繁體+慣用詞
SAMPLE_RATE=16000                              # WAV 取樣率
# ------------------------------------------

# ---------- 函數 ----------
print_step() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}[$1/5]${NC} $2"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

print_error() {
    echo -e "${RED}❌ 錯誤：$1${NC}" >&2
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

check_dependency() {
    if ! command -v "$1" &> /dev/null; then
        print_error "找不到 $1，請先安裝"
        echo ""
        echo "安裝方式："
        case "$1" in
            ffmpeg)
                echo "  brew install ffmpeg"
                ;;
            mlx_whisper)
                echo "  pip install mlx-whisper"
                ;;
            opencc)
                echo "  brew install opencc"
                ;;
        esac
        exit 1
    fi
}

show_usage() {
    echo ""
    echo "用法："
    echo "  ./subtitle.sh <影片或音檔路徑> [選項]"
    echo ""
    echo "選項："
    echo "  --breeze      使用 Breeze ASR（原生繁中，跳過 OpenCC）"
    echo "  --turbo       使用 large-v3-turbo（較快但中文品質略差）"
    echo "  --keep-wav    保留中繼 WAV 檔案"
    echo "  --skip-opencc 跳過繁體轉換（輸出簡體）"
    echo "  --bilingual   同時產出簡體和繁體版本"
    echo "  --help        顯示此說明"
    echo ""
    echo "範例："
    echo "  ./subtitle.sh 演講.mp4"
    echo "  ./subtitle.sh 錄音.m4a --turbo"
    echo "  ./subtitle.sh interview.mov --keep-wav --bilingual"
    echo ""
    echo "輸出檔案："
    echo "  <檔名>_zh-TW.srt    繁體中文字幕（主要輸出）"
    echo "  <檔名>_zh-CN.srt    簡體中文字幕（--bilingual 時產出）"
    echo ""
}

format_duration() {
    local seconds=$1
    local minutes=$((seconds / 60))
    local remaining=$((seconds % 60))
    if [ "$minutes" -gt 0 ]; then
        echo "${minutes} 分 ${remaining} 秒"
    else
        echo "${remaining} 秒"
    fi
}

strict_srt_count() {
    local f=$1
    if [ ! -f "$f" ]; then
        echo 0
        return
    fi
    grep -cE '^[0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} --> [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3}' "$f" 2>/dev/null || true
}

# ---------- 參數解析 ----------
INPUT_FILE=""
USE_BREEZE=false
USE_TURBO=false
KEEP_WAV=false
SKIP_OPENCC=false
BILINGUAL=false

for arg in "$@"; do
    case "$arg" in
        --breeze)
            USE_BREEZE=true
            MODEL="eoleedi/Breeze-ASR-25-mlx"
            SKIP_OPENCC=true
            ;;
        --turbo)
            USE_TURBO=true
            MODEL="mlx-community/whisper-large-v3-turbo"
            ;;
        --keep-wav)
            KEEP_WAV=true
            ;;
        --skip-opencc)
            SKIP_OPENCC=true
            ;;
        --bilingual)
            BILINGUAL=true
            ;;
        --help|-h)
            show_usage
            exit 0
            ;;
        -*)
            print_error "未知選項：$arg"
            show_usage
            exit 1
            ;;
        *)
            INPUT_FILE="$arg"
            ;;
    esac
done

if [ -z "$INPUT_FILE" ]; then
    print_error "請指定輸入檔案"
    show_usage
    exit 1
fi

if [ ! -f "$INPUT_FILE" ]; then
    print_error "找不到檔案：$INPUT_FILE"
    exit 1
fi

# ---------- 檔案路徑設定 ----------
DIR="$(dirname "$INPUT_FILE")"
BASENAME="$(basename "$INPUT_FILE" | sed 's/\.[^.]*$//')"
WAV_FILE="${DIR}/${BASENAME}.wav"
SRT_CN="${DIR}/${BASENAME}.srt"              # mlx-whisper 預設輸出
SRT_TW="${DIR}/${BASENAME}_zh-TW.srt"
SRT_CN_FINAL="${DIR}/${BASENAME}_zh-CN.srt"

TOTAL_START=$SECONDS

# ============================================================
# Step 1: 檢查依賴
# ============================================================
print_step 1 "檢查依賴工具"

check_dependency ffmpeg
check_dependency mlx_whisper
if [ "$SKIP_OPENCC" = false ]; then
    check_dependency opencc
fi

print_success "所有依賴已就緒"

if [ "$USE_TURBO" = true ]; then
    print_warning "使用 turbo 模型（較快但中文品質略差）"
else
    echo "    模型：large-v3 完整版"
fi

# ============================================================
# Step 2: 轉換為 WAV
# ============================================================
print_step 2 "轉換音檔格式 → 16kHz WAV"

STEP_START=$SECONDS

# 如果已經是正確格式的 WAV，跳過
if [[ "$INPUT_FILE" == *.wav ]]; then
    # 檢查是否已是 16kHz mono
    CURRENT_RATE=$(ffprobe -v error -select_streams a:0 -show_entries stream=sample_rate -of default=noprint_wrappers=1:nokey=1 "$INPUT_FILE" 2>/dev/null || echo "0")
    CURRENT_CHANNELS=$(ffprobe -v error -select_streams a:0 -show_entries stream=channels -of default=noprint_wrappers=1:nokey=1 "$INPUT_FILE" 2>/dev/null || echo "0")
    
    if [ "$CURRENT_RATE" = "$SAMPLE_RATE" ] && [ "$CURRENT_CHANNELS" = "1" ]; then
        print_warning "輸入已是 16kHz mono WAV，跳過轉換"
        WAV_FILE="$INPUT_FILE"
        KEEP_WAV=true  # 不刪除原始輸入檔
    else
        ffmpeg -i "$INPUT_FILE" -ar "$SAMPLE_RATE" -ac 1 -y "$WAV_FILE" -loglevel warning
    fi
else
    ffmpeg -i "$INPUT_FILE" -ar "$SAMPLE_RATE" -ac 1 -y "$WAV_FILE" -loglevel warning
fi

# 取得音檔長度
DURATION=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$WAV_FILE" 2>/dev/null | cut -d. -f1)
DURATION=${DURATION:-0}

STEP_ELAPSED=$((SECONDS - STEP_START))
print_success "WAV 轉換完成（$(format_duration $STEP_ELAPSED)）"
echo "    音檔長度：$(format_duration $DURATION)"

# ============================================================
# Step 3: 語音辨識
# ============================================================
if [ "$USE_BREEZE" = true ]; then
    print_step 3 "Breeze ASR 語音辨識 → SRT 字幕（繁體中文）"
    INITIAL_PROMPT=""
else
    print_step 3 "Whisper 語音辨識 → SRT 字幕（簡體中文）"
    INITIAL_PROMPT="以下是台灣繁體中文演講逐字稿。今天，我們討論的主題是：人工智慧、數位轉型，以及資訊安全的發展趨勢。"
fi

STEP_START=$SECONDS

MLX_WHISPER_ARGS=(
    --model "$MODEL"
    --language "$LANGUAGE"
    --output-format "$OUTPUT_FORMAT"
    --condition-on-previous-text False
    --output-dir "$DIR"
    --output-name "$BASENAME"
)
if [ -n "$INITIAL_PROMPT" ]; then
    MLX_WHISPER_ARGS+=(--initial-prompt "$INITIAL_PROMPT")
fi

MLX_MARKER="$(mktemp -t mlx_marker.XXXXXX)"
MLX_STDOUT_LOG="$(mktemp -t mlx_stdout.XXXXXX)"
set +e
mlx_whisper "${MLX_WHISPER_ARGS[@]}" "$WAV_FILE" 2>&1 | tee "$MLX_STDOUT_LOG"
mlx_status=${PIPESTATUS[0]}
set -e

# mlx-whisper --output-name 仍使用 pathlib.with_suffix()，
# 檔名含 .數字 時會被截斷（如 Qwen3.5 → Qwen3.srt）。
# 因此用 find 搜尋實際產出的 SRT 檔案作為 fallback。
MLX_OUTPUT="${DIR}/${BASENAME}.srt"
if [ ! -f "$MLX_OUTPUT" ]; then
    MLX_FOUND=""
    while IFS= read -r candidate; do
        if [ -z "$MLX_FOUND" ] || [ "$candidate" -nt "$MLX_FOUND" ]; then
            MLX_FOUND="$candidate"
        fi
    done < <(find "$DIR" -maxdepth 1 -name "${BASENAME}*.srt" -newer "$MLX_MARKER" -print 2>/dev/null)
    if [ -n "$MLX_FOUND" ] && [ -f "$MLX_FOUND" ]; then
        MLX_OUTPUT="$MLX_FOUND"
        print_warning "Whisper 輸出檔名與預期不同：$(basename "$MLX_OUTPUT")"
    else
        MLX_OUTPUT="${DIR}/${BASENAME}.srt"
    fi
fi

MLX_FALLBACK_USED=false
if [ "$(strict_srt_count "$MLX_OUTPUT")" -eq 0 ]; then
    MLX_FALLBACK_USED=true
    print_warning "Whisper SRT 缺失或沒有有效時間軸，疑似 mlx_whisper KeyError:'words'；嘗試從 verbose stdout 重建。log: $MLX_STDOUT_LOG"
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    python3 "${SCRIPT_DIR}/reconstruct_srt_from_log.py" "$MLX_STDOUT_LOG" "$MLX_OUTPUT" || true
fi

if [ "$(strict_srt_count "$MLX_OUTPUT")" -eq 0 ]; then
    print_error "Whisper 未產生有效 SRT（mlx exit=$mlx_status）；已保留 stdout log: $MLX_STDOUT_LOG"
    rm -f "$MLX_MARKER"
    exit 1
fi

if [ "$MLX_FALLBACK_USED" = false ]; then
    rm -f "$MLX_STDOUT_LOG"
else
    print_warning "已使用 stdout fallback 重建 SRT；保留 log 供除錯：$MLX_STDOUT_LOG"
fi
rm -f "$MLX_MARKER"

# 搬移到預期的 SRT_CN 路徑
if [ "$MLX_OUTPUT" != "$SRT_CN" ]; then
    mv "$MLX_OUTPUT" "$SRT_CN"
fi

# 半形逗號 → 全形（跳過時間戳記行）
sed -i '' '/^[0-9][0-9]:[0-9][0-9]:[0-9][0-9],/! s/,/，/g' "$SRT_CN"

STEP_ELAPSED=$((SECONDS - STEP_START))
LINES=$(grep -cE '^[0-9]+$' "$SRT_CN" 2>/dev/null || echo "?")
print_success "語音辨識完成（$(format_duration $STEP_ELAPSED)）"
echo "    字幕段數：$LINES 段"

# ============================================================
# Step 4: 簡體 → 台灣繁體
# ============================================================
if [ "$SKIP_OPENCC" = false ]; then
    print_step 4 "OpenCC 簡繁轉換 → 台灣繁體中文"
    
    STEP_START=$SECONDS
    
    opencc -i "$SRT_CN" -o "$SRT_TW" -c "$OPENCC_CONFIG"
    
    STEP_ELAPSED=$((SECONDS - STEP_START))
    print_success "繁體轉換完成（$(format_duration $STEP_ELAPSED)）"
    
    # 處理簡體版
    if [ "$BILINGUAL" = true ]; then
        mv "$SRT_CN" "$SRT_CN_FINAL"
        echo "    簡體版保留為：$SRT_CN_FINAL"
    else
        rm -f "$SRT_CN"
    fi
else
    print_step 4 "跳過繁體轉換（--skip-opencc）"
    SRT_TW="$SRT_CN"
    # Breeze + bilingual: 反向 opencc 產出簡體版
    if [ "$BILINGUAL" = true ] && [ "$USE_BREEZE" = true ]; then
        check_dependency opencc
        opencc -i "$SRT_TW" -o "$SRT_CN_FINAL" -c "t2s"
        echo "    簡體版（反向轉換）：$SRT_CN_FINAL"
    fi
fi

# ============================================================
# Step 4.5: 標點恢復 + 術語校正
# ============================================================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "${SCRIPT_DIR}/postprocess_srt.py" ]; then
    print_step "4.5" "標點恢復 + 術語校正"
    STEP_START=$SECONDS
    "${SCRIPT_DIR}/.venv/bin/python3" "${SCRIPT_DIR}/postprocess_srt.py" "$SRT_TW" "$SRT_TW" --no-punctuation --stats
    STEP_ELAPSED=$((SECONDS - STEP_START))
    print_success "後處理完成（$(format_duration $STEP_ELAPSED)）"
fi

# ============================================================
# Step 5: 清理 & 完成
# ============================================================
print_step 5 "清理暫存檔案"

# 清理 WAV（除非用戶要求保留或輸入本身就是 WAV）
if [ "$KEEP_WAV" = false ] && [ "$WAV_FILE" != "$INPUT_FILE" ]; then
    rm -f "$WAV_FILE"
    echo "    已刪除暫存 WAV"
else
    echo "    WAV 已保留：$WAV_FILE"
fi

# ============================================================
# 完成摘要
# ============================================================
TOTAL_ELAPSED=$((SECONDS - TOTAL_START))

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  ✅ 全部完成！${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  📄 繁體字幕：$SRT_TW"
if [ "$BILINGUAL" = true ]; then
    echo "  📄 簡體字幕：$SRT_CN_FINAL"
fi
echo "  ⏱️  總耗時：$(format_duration $TOTAL_ELAPSED)"
echo "  📊 音檔長度：$(format_duration $DURATION) → 處理速度 $(( DURATION / (TOTAL_ELAPSED > 0 ? TOTAL_ELAPSED : 1) ))x 即時"
echo ""
echo -e "${YELLOW}  📋 下一步：把 SRT 丟進 Claude (Sonnet 4) 用字幕修正模板校對${NC}"
echo ""
