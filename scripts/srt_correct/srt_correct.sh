#!/usr/bin/env bash
# srt_correct.sh — 用 Claude Code CLI (claude -p) 批次校正中文字幕
# 用法: ./srt_correct.sh input.srt [chunk_size] [terminology.txt]
#
# 需求:
#   - Claude Code CLI (npm install -g @anthropic-ai/claude-code)
#   - 已登入 Claude Pro/Max 帳號
#   - srt_correct_prompt.txt (system prompt，放在同目錄)
#
# 功能:
#   - 自動分段 → 逐段校正 → 合併
#   - 跨段上下文（前一段最後 5 條作為參考）
#   - 講者專用術語表支援
#   - 失敗自動重試（最多 2 次）
#   - 斷點續跑（已完成的段落不會重跑）
#   - 每段成本與耗時記錄

set -euo pipefail

# ========== 參數 ==========
INPUT_SRT="${1:?用法: ./srt_correct.sh input.srt [chunk_size] [terminology.txt]}"
CHUNK_SIZE="${2:-300}"
TERMINOLOGY_FILE="${3:-}"
MODEL="${CLAUDE_MODEL:-sonnet}"  # Sonnet 4.6 是 Pro 預設，可用環境變數覆蓋
CONTEXT_OVERLAP=5          # 帶入前一段最後 N 條作為上下文
MAX_RETRIES=2              # 失敗重試次數
SLEEP_BETWEEN=3            # 段間等待秒數
USE_BREEZE="${USE_BREEZE:-false}"  # 是否使用 Breeze ASR（啟用 Breeze 專屬規則）

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROMPT_FILE="${SCRIPT_DIR}/srt_correct_prompt.txt"

# 工作目錄：固定在輸入檔旁邊，支援斷點續跑
BASENAME="$(basename "${INPUT_SRT%.srt}")"
WORK_DIR="$(dirname "$INPUT_SRT")/.srt_correct_${BASENAME}"
SEGMENTS_DIR="${WORK_DIR}/segments"
CORRECTED_DIR="${WORK_DIR}/corrected"
LOG_DIR="${WORK_DIR}/logs"
OUTPUT_SRT="${INPUT_SRT%.srt}_corrected.srt"

# ========== 全域匯出環境變數（確保 claude 子程序繼承） ==========
export MAX_THINKING_TOKENS=0   # 關閉 extended thinking
# 注意：claude -p 的 max output 固定 32K，無法透過環境變數調整

# ========== 前置檢查 ==========
if ! command -v claude &>/dev/null; then
    echo "❌ 找不到 claude CLI。請先安裝："
    echo "   npm install -g @anthropic-ai/claude-code"
    exit 1
fi

if [ ! -f "$PROMPT_FILE" ]; then
    echo "❌ 找不到 system prompt: ${PROMPT_FILE}"
    exit 1
fi

if [ ! -f "$INPUT_SRT" ]; then
    echo "❌ 找不到輸入檔案: ${INPUT_SRT}"
    exit 1
fi

mkdir -p "$SEGMENTS_DIR" "$CORRECTED_DIR" "$LOG_DIR"

# ========== 預處理：移除 \r ==========
# 用 tr + cmp 檢測，避免 macOS 沒有 grep -P 的問題
if ! tr -d '\r' < "$INPUT_SRT" | cmp -s - "$INPUT_SRT"; then
    echo "⚠️  偵測到 Windows 換行，自動轉換..."
    tr -d '\r' < "$INPUT_SRT" > "${INPUT_SRT}.tmp" && mv "${INPUT_SRT}.tmp" "$INPUT_SRT"
fi

# ========== 載入 system prompt 並注入術語表 ==========
SYSTEM_PROMPT=$(cat "$PROMPT_FILE")

if [ -n "$TERMINOLOGY_FILE" ] && [ -f "$TERMINOLOGY_FILE" ]; then
    TERMS=$(grep -v '^#' "$TERMINOLOGY_FILE" | grep -v '^[[:space:]]*$' | sed 's/^/- /')
    TERMINOLOGY_SECTION="## 講者專用術語
以下是此講者自創或常用的術語，Whisper 很可能聽錯。遇到發音相近但不通順的詞，優先考慮是否為以下術語：
${TERMS}

"
    SYSTEM_PROMPT="${SYSTEM_PROMPT/\{\{TERMINOLOGY_SECTION\}\}/${TERMINOLOGY_SECTION}}"
else
    SYSTEM_PROMPT="${SYSTEM_PROMPT/\{\{TERMINOLOGY_SECTION\}\}/}"
    if [ -n "$TERMINOLOGY_FILE" ]; then
        echo "⚠️  找不到術語表: ${TERMINOLOGY_FILE}，將不使用術語表"
    fi
fi

echo "========================================"
echo "📄 輸入: ${INPUT_SRT}"
echo "🤖 模型: ${MODEL}（thinking: off, output 上限: 32K）"
echo "🔢 每段: ${CHUNK_SIZE} 條（含 ${CONTEXT_OVERLAP} 條上下文）"
echo "📂 工作目錄: ${WORK_DIR}"
if [ -n "$TERMINOLOGY_FILE" ] && [ -f "$TERMINOLOGY_FILE" ]; then
    echo "📖 術語表: ${TERMINOLOGY_FILE}"
fi
echo "========================================"
echo ""

echo "🔧 步驟 0: 預處理…"
ORIGINAL_RAW_ENTRIES=$(grep -cE '^[0-9]+$' "$INPUT_SRT" 2>/dev/null || echo "?")
PREPROCESS_ARGS=("$INPUT_SRT" "${INPUT_SRT%.srt}_preprocessed.srt" "--stats")
if [ "$USE_BREEZE" = true ]; then
    PREPROCESS_ARGS+=("--breeze")
fi
python3 "${SCRIPT_DIR}/srt_preprocess.py" "${PREPROCESS_ARGS[@]}"
INPUT_SRT="${INPUT_SRT%.srt}_preprocessed.srt"

# ========== 步驟 1: 分段 ==========
# 如果 segments 已存在且非空，跳過（斷點續跑）
if ls "$SEGMENTS_DIR"/segment_*.srt &>/dev/null; then
    TOTAL_SEGMENTS=$(ls "$SEGMENTS_DIR"/segment_*.srt | wc -l | tr -d ' ')
    echo "✂️  步驟 1: 已有 ${TOTAL_SEGMENTS} 個分段，跳過分段步驟"
else
    echo "✂️  步驟 1: 分段中..."

    awk -v chunk="$CHUNK_SIZE" -v outdir="$SEGMENTS_DIR" '
    BEGIN {
        entry_num = 0
        current_chunk = 1
        chunk_count = 0
        outfile = outdir "/segment_" sprintf("%03d", current_chunk) ".srt"
    }
    /^[0-9]+$/ && (prev == "" || prev ~ /^[[:space:]]*$/) {
        entry_num++
        chunk_count++
        if (chunk_count > chunk) {
            current_chunk++
            chunk_count = 1
            outfile = outdir "/segment_" sprintf("%03d", current_chunk) ".srt"
        }
    }
    {
        print >> outfile
        prev = $0
    }
    ' "$INPUT_SRT"

    TOTAL_SEGMENTS=$(ls "$SEGMENTS_DIR"/segment_*.srt 2>/dev/null | wc -l | tr -d ' ')
    echo "   分成 ${TOTAL_SEGMENTS} 段"
fi

if [ "$TOTAL_SEGMENTS" -eq 0 ]; then
    echo "❌ 分段失敗，請檢查 SRT 格式"
    exit 1
fi

echo ""

# ========== 步驟 2: 逐段校正 ==========
echo "🔧 步驟 2: 逐段校正中..."
echo ""

FAILED_SEGMENTS=()
SKIPPED=0
PROCESSED=0
TOTAL_COST=0
START_TIME=$(date +%s)

# 從前一段取最後 N 條字幕的純文字作為上下文（不含 SRT 格式，避免 LLM 複製輸出）
get_context_text() {
    local srt_file="$1"
    local n="$2"
    awk -v n="$n" '
    /^[0-9]+$/ && (prev == "" || prev ~ /^[[:space:]]*$/) {
        count++
        entry_num = count
    }
    # 跳過序號行和時間軸行，只收集文字
    !/^[0-9]+$/ && !/^[0-9][0-9]:[0-9][0-9]:[0-9][0-9]/ && !/^[[:space:]]*$/ {
        texts[entry_num] = texts[entry_num] ? texts[entry_num] " " $0 : $0
    }
    { prev = $0 }
    END {
        start_from = count - n + 1
        if (start_from < 1) start_from = 1
        for (i = start_from; i <= count; i++) {
            if (texts[i]) print texts[i]
        }
    }
    ' "$srt_file"
}

# 校正單個段落（含重試）
correct_segment() {
    local seg_file="$1"
    local out_file="$2"
    local log_file="$3"
    local context="$4"
    local attempt=0

    # 組裝 prompt 內容，寫入臨時檔避免超長 argument 或 pipe 問題
    local tmp_prompt="${LOG_DIR}/.tmp_prompt_$$"
    if [ -n "$context" ]; then
        {
            echo "【上文參考（純文字，僅供理解語境，不要輸出這些內容）】"
            echo "$context"
            echo ""
            echo "【以下是待校正字幕，只輸出這些的校正結果】"
            cat "$seg_file"
        } > "$tmp_prompt"
    else
        cat "$seg_file" > "$tmp_prompt"
    fi

    while [ "$attempt" -le "$MAX_RETRIES" ]; do
        attempt=$((attempt + 1))

        # 呼叫 claude -p
        # 環境變數已在腳本開頭 export，子程序自動繼承
        local result
        result=$(claude -p \
            --model "$MODEL" \
            --system-prompt "$SYSTEM_PROMPT" \
            --max-turns 1 \
            --output-format json \
            --disallowedTools "Bash,Read,Write,Edit,Grep,Glob,WebSearch,WebFetch" \
            < "$tmp_prompt" \
            2>"${log_file}.stderr") || true

        # 解析 JSON 結果
        local text cost duration
        text=$(echo "$result" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('result', ''))
except:
    pass
" 2>/dev/null) || text=""

        cost=$(echo "$result" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    c = d.get('total_cost_usd') or d.get('cost_usd') or 0
    if isinstance(c, dict):
        c = c.get('total_cost', 0)
    print(c)
except:
    print(0)
" 2>/dev/null) || cost="0"

        duration=$(echo "$result" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('duration_ms') or d.get('duration') or 0)
except:
    print(0)
" 2>/dev/null) || duration="0"

        # 驗證：輸出是否包含時間軸（基本 SRT 格式檢查）
        if echo "$text" | grep -qE '[0-9]{2}:[0-9]{2}:[0-9]{2}[,.][0-9]{3}.*-->'; then
            echo "$text" > "$out_file"
            echo "$cost" > "${out_file}.cost"
            echo "$duration" > "${out_file}.duration"
            echo "$result" > "$log_file"
            rm -f "$tmp_prompt"
            return 0
        fi

        # 失敗，等待後重試
        if [ "$attempt" -le "$MAX_RETRIES" ]; then
            echo -n "(重試 ${attempt}/${MAX_RETRIES})... "
            sleep 5
        fi
    done

    rm -f "$tmp_prompt"
    return 1
}

# 主迴圈
SEG_NUM=0
PREV_SEG_FILE=""

for seg_file in "$SEGMENTS_DIR"/segment_*.srt; do
    SEG_NUM=$((SEG_NUM + 1))
    seg_name=$(basename "$seg_file")
    out_file="${CORRECTED_DIR}/${seg_name}"
    log_file="${LOG_DIR}/${seg_name%.srt}.json"

    # 斷點續跑：已有校正結果就跳過
    if [ -f "$out_file" ] && grep -qE '[0-9]{2}:[0-9]{2}:[0-9]{2}' "$out_file" 2>/dev/null; then
        SKIPPED=$((SKIPPED + 1))
        corrected_entries=$(grep -cE '^[0-9]+$' "$out_file" 2>/dev/null || echo "?")
        echo "   [${SEG_NUM}/${TOTAL_SEGMENTS}] ${seg_name} ⏭️  已完成（${corrected_entries} 條）"
        PREV_SEG_FILE="$seg_file"
        continue
    fi

    seg_entries=$(grep -cE '^[0-9]+$' "$seg_file" 2>/dev/null || echo "?")
    echo -n "   [${SEG_NUM}/${TOTAL_SEGMENTS}] ${seg_name} (${seg_entries} 條)... "

    # 取前一段上下文（優先使用校正後版本，語境更準確）
    context=""
    if [ -n "$PREV_SEG_FILE" ] && [ -f "$PREV_SEG_FILE" ]; then
        prev_corrected="${CORRECTED_DIR}/$(basename "$PREV_SEG_FILE")"
        if [ -f "$prev_corrected" ]; then
            context=$(get_context_text "$prev_corrected" "$CONTEXT_OVERLAP")
        else
            context=$(get_context_text "$PREV_SEG_FILE" "$CONTEXT_OVERLAP")
        fi
    fi

    # 校正
    if correct_segment "$seg_file" "$out_file" "$log_file" "$context"; then
        corrected_entries=$(grep -cE '^[0-9]+$' "$out_file" 2>/dev/null || echo "?")
        seg_cost=$(cat "${out_file}.cost" 2>/dev/null || echo "0")
        seg_duration=$(cat "${out_file}.duration" 2>/dev/null || echo "0")
        seg_duration_s=$(python3 -c "
try:
    print(f'{float(${seg_duration:-0})/1000:.1f}')
except:
    print('?')
" 2>/dev/null || echo "?")
        TOTAL_COST=$(python3 -c "
try:
    print(round(float(${TOTAL_COST}) + float(${seg_cost:-0}), 4))
except:
    print(${TOTAL_COST})
" 2>/dev/null || echo "$TOTAL_COST")
        PROCESSED=$((PROCESSED + 1))
        echo "✅ ${seg_entries}→${corrected_entries} 條 | ${seg_duration_s}s | \$${seg_cost}"
    else
        echo "❌ 校正失敗（已重試 ${MAX_RETRIES} 次），保留原文"
        cp "$seg_file" "$out_file"
        FAILED_SEGMENTS+=("$seg_name")
    fi

    PREV_SEG_FILE="$seg_file"

    # 段間等待
    if [ "$SEG_NUM" -lt "$TOTAL_SEGMENTS" ]; then
        sleep "$SLEEP_BETWEEN"
    fi
done

echo ""

# ========== 步驟 3: 合併並重新編號 ==========
echo "📎 步驟 3: 合併中..."

python3 - "$CORRECTED_DIR" "$OUTPUT_SRT" "$SEGMENTS_DIR" << 'PYMERGE'
import sys, os, re, glob

corrected_dir = sys.argv[1]
output_path = sys.argv[2]
segments_dir = sys.argv[3]

TIMECODE_RE = re.compile(r'^\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}')

def ts_to_ms(ts):
    ts = ts.strip().replace(',', '.')
    h, m, rest = ts.split(':')
    parts = rest.split('.')
    s = int(parts[0])
    ms = int(parts[1]) if len(parts) > 1 else 0
    return int(h)*3600000 + int(m)*60000 + s*1000 + ms

def ms_to_ts(ms):
    h = ms // 3600000; ms %= 3600000
    m = ms // 60000; ms %= 60000
    s = ms // 1000; frac = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{frac:03d}"

def parse_entries(lines):
    entries = []
    i = 0
    while i < len(lines):
        if lines[i].strip().isdigit() and i + 1 < len(lines) and TIMECODE_RE.match(lines[i+1].strip()):
            timecode = lines[i+1].strip()
            text_lines = []
            i += 2
            while i < len(lines) and lines[i].strip() != '':
                if lines[i].strip().isdigit() and i + 1 < len(lines) and TIMECODE_RE.match(lines[i+1].strip()):
                    break
                text_lines.append(lines[i].strip())
                i += 1
            if text_lines:
                start_str, end_str = timecode.split('-->')
                entries.append({
                    'start_ms': ts_to_ms(start_str.strip()),
                    'end_ms': ts_to_ms(end_str.strip()),
                    'text': '\n'.join(text_lines),
                })
            while i < len(lines) and lines[i].strip() == '':
                i += 1
        else:
            i += 1
    return entries

def get_segment_first_start(seg_path):
    """取得原始 segment 的第一條字幕的 start_ms"""
    with open(seg_path, 'r', encoding='utf-8') as f:
        lines = f.read().strip().split('\n')
    entries = parse_entries(lines)
    return entries[0]['start_ms'] if entries else None

files = sorted(glob.glob(os.path.join(corrected_dir, 'segment_*.srt')))
seg_files = sorted(glob.glob(os.path.join(segments_dir, 'segment_*.srt')))

all_entries = []
dedup_count = 0

for fi, fpath in enumerate(files):
    with open(fpath, 'r', encoding='utf-8') as f:
        lines = f.read().strip().split('\n')
    entries = parse_entries(lines)

    if fi > 0 and fi < len(seg_files):
        # 取得此段原始字幕的第一條 start_ms
        # 校正後的 segment 中，start_ms 早於此時間的條目是 LLM 重複輸出的上文
        # 給 1.5 秒容差，因為碎片合併可能讓起始時間稍微提前
        cutoff_ms = get_segment_first_start(seg_files[fi])
        if cutoff_ms is not None:
            filtered = []
            for e in entries:
                if e['start_ms'] < cutoff_ms - 1500:
                    dedup_count += 1
                else:
                    filtered.append(e)
            entries = filtered

    all_entries.extend(entries)

# 按時間排序
all_entries.sort(key=lambda e: e['start_ms'])

# 最終安全網：相鄰條目時間軸幾乎相同的去重
final = []
for e in all_entries:
    if final and abs(e['start_ms'] - final[-1]['start_ms']) < 500 \
             and abs(e['end_ms'] - final[-1]['end_ms']) < 500:
        dedup_count += 1
        # 保留文字較長的版本
        if len(e['text']) > len(final[-1]['text']):
            final[-1] = e
        continue
    final.append(e)

with open(output_path, 'w', encoding='utf-8') as f:
    for idx, e in enumerate(final, 1):
        f.write(f'{idx}\n{ms_to_ts(e["start_ms"])} --> {ms_to_ts(e["end_ms"])}\n{e["text"]}\n\n')

print(f'   合併完成: {len(final)} 條字幕（去重 {dedup_count} 條）')
PYMERGE

echo "✅ 步驟 4: 後處理…"
python3 "${SCRIPT_DIR}/srt_postprocess.py" "$OUTPUT_SRT" "$OUTPUT_SRT" --stats

# ========== 統計 ==========
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
ELAPSED_MIN=$((ELAPSED / 60))
ELAPSED_SEC=$((ELAPSED % 60))

ORIGINAL_ENTRIES=$(grep -cE '^[0-9]+$' "$INPUT_SRT" 2>/dev/null || echo "?")
CORRECTED_ENTRIES=$(grep -cE '^[0-9]+$' "$OUTPUT_SRT" 2>/dev/null || echo "?")

echo ""
echo "========================================"
echo "✅ 校正完成！"
echo "   輸出: ${OUTPUT_SRT}"
echo "   原始: ${ORIGINAL_RAW_ENTRIES} 條 → 預處理後: ${ORIGINAL_ENTRIES} 條 → 校正後: ${CORRECTED_ENTRIES} 條"
echo "   段數: ${TOTAL_SEGMENTS}（校正: ${PROCESSED} / 跳過: ${SKIPPED}）"
echo "   耗時: ${ELAPSED_MIN}m ${ELAPSED_SEC}s"
echo "   總成本: \$${TOTAL_COST}"

# bash 3.2 安全寫法：用 2>/dev/null 防止空陣列 + set -u 報錯
if [ "${#FAILED_SEGMENTS[@]}" -gt 0 ] 2>/dev/null; then
    echo "   ⚠️  失敗段落: ${FAILED_SEGMENTS[*]}"
fi

echo "========================================"
