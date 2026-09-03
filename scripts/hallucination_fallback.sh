#!/bin/bash
# ASR 幻覺 fallback：用 Whisper large-v3 重跑 Breeze 無法修復的幻覺段
# 用法：hallucination_fallback.sh <SRT檔> <影片或音檔> <起始時間> <結束時間> [可選提示詞]
#
# ASR 引擎：預設本地 mlx；設 SRT_ASR_ENGINE=runpod 走雲端 RTX 4090，SRT_ASR_ENGINE=vast 走 Vast.ai。
#
# 已知前置證據（2026-08-30 live 實測更新，n=1 clip）：
# 正常語音段：雲端 Systran/faster-whisper-large-v3 與本地 MLX 帶同一個 prompt，
#   六條輸出文字 0 字元差、時間戳 0.00 秒差（位元組級相同）。
# 前一個模型已幻覺的音訊段：本地 MLX 會丟掉約 30 秒的窗口，雲端 ct2 不丟。
#   這是「不丟窗口」不是「辨識更準」——同一段音訊本地補回 4 條、雲端補回 11 條，
#   差的是本地整段沒吐出來，不是兩邊認得的字不同。
# 範例：hallucination_fallback.sh final.srt video.mp4 00:55:33 00:56:07
#
# 流程：截取音檔 → Whisper large-v3 重跑 → 時間偏移 → patch 回 SRT
# 注意：一定用 whisper-large-v3-mlx，不要用 turbo

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

SRT_FILE="$1"
MEDIA_FILE="$2"
HALL_START="$3"
HALL_END="$4"
INITIAL_PROMPT="${5:-}"
ASR_ENGINE="${SRT_ASR_ENGINE:-mlx}"
ASR_ENGINE_IS_CLOUD=false
case "${ASR_ENGINE}" in
  mlx) ;;
  runpod) ASR_ENGINE_IS_CLOUD=true; ENGINE_LABEL="RunPod" ;;
  vast)   ASR_ENGINE_IS_CLOUD=true; ENGINE_LABEL="Vast.ai" ;;
  *) echo "ERROR: 未知引擎：${ASR_ENGINE}（可用 mlx、runpod 或 vast）" >&2; exit 1 ;;
esac

WORK_DIR="$(dirname "$SRT_FILE")"
FIX_WAV="${WORK_DIR}/_fix_segment.wav"
FIX_SRT="${WORK_DIR}/_fix_segment.srt"

# 1. 截取幻覺段音檔（前後各留 2 秒 buffer）
echo "截取音檔：${HALL_START} ~ ${HALL_END}"
ffmpeg -y -i "$MEDIA_FILE" \
  -ss "$HALL_START" -to "$HALL_END" \
  -ar 16000 -ac 1 \
  "$FIX_WAV" 2>/dev/null

# 2. Whisper large-v3 重跑
echo "Whisper large-v3 重跑..."
WHISPER_ARGS=(
  --model mlx-community/whisper-large-v3-mlx
  --language zh
  --task transcribe
  --temperature 0
  --condition-on-previous-text False
  # word-level 對齊重定 segment 邊界；缺了它 segment 時間戳會漂移 2-4 秒
  # （2026-07-17 技術分析-6月-03 27:19-27:53 補丁區實測，字幕提早出現）
  --word-timestamps True
  --output-format srt
  --output-name "_fix_segment"
  --output-dir "$WORK_DIR"
)

if [ -n "$INITIAL_PROMPT" ]; then
  WHISPER_ARGS+=(--initial-prompt "$INITIAL_PROMPT")
fi

if [ "${ASR_ENGINE_IS_CLOUD}" = true ]; then
  CLOUD_RUNS_DIR="${WORK_DIR}/.cloud_asr_runs"
  mkdir -p "$CLOUD_RUNS_DIR"
  CLOUD_RUN_DIR="$(mktemp -d "${CLOUD_RUNS_DIR}/run.XXXXXX")"
  CLOUD_EVIDENCE_DIR="${CLOUD_RUN_DIR}/env"
  echo "${ENGINE_LABEL} cloud ASR run dir: ${CLOUD_RUN_DIR}" >&2
  echo "${ENGINE_LABEL} evidence dir: ${CLOUD_EVIDENCE_DIR}" >&2

  CLOUD_ARGS=(
    "$SCRIPT_DIR/cloud_asr.sh"
    "$FIX_WAV"
    "$CLOUD_RUN_DIR"
    "_fix_segment"
    "zh"
    "--largev3"
  )
  if [ -n "$INITIAL_PROMPT" ]; then
    CLOUD_ARGS+=(--initial-prompt "$INITIAL_PROMPT")
  fi
  if ! CLOUD_ASR_PROVIDER="${ASR_ENGINE}" bash "${CLOUD_ARGS[@]}"; then
    rm -f "$FIX_WAV"
    exit 1
  fi

  CLOUD_FIX_SRT="${CLOUD_RUN_DIR}/_fix_segment.srt"
  CLOUD_FIX_JSON="${CLOUD_RUN_DIR}/_fix_segment.cloud.json"
  if ! [ -f "$CLOUD_FIX_SRT" ]; then
    echo "ERROR: cloud_asr.sh 未產出修復 SRT：$CLOUD_FIX_SRT" >&2
    rm -f "$FIX_WAV"
    exit 1
  fi
  if [ -f "$FIX_SRT" ]; then
    cp "$FIX_SRT" "${CLOUD_RUN_DIR}/previous__fix_segment.srt"
  fi
  if [ -f "${WORK_DIR}/_fix_segment.cloud.json" ]; then
    cp "${WORK_DIR}/_fix_segment.cloud.json" "${CLOUD_RUN_DIR}/previous__fix_segment.cloud.json"
  fi

  cp "$CLOUD_FIX_SRT" "$FIX_SRT"
  if [ -f "$CLOUD_FIX_JSON" ]; then
    cp "$CLOUD_FIX_JSON" "${WORK_DIR}/_fix_segment.cloud.json"
  fi
else
  mlx_whisper "${WHISPER_ARGS[@]}" "$FIX_WAV"
fi

# 3. 檢查結果是否仍為幻覺
if ! [ -f "$FIX_SRT" ]; then
  echo "ERROR: Whisper 未產出 SRT"
  rm -f "$FIX_WAV"
  exit 1
fi

# 4. 用 Python patch 回 SRT
python3 -c "
import re, sys

SRT_FILE = '$SRT_FILE'
FIX_SRT = '$FIX_SRT'
HALL_START = '$HALL_START'

# 解析起始時間為秒數
parts = HALL_START.split(':')
OFFSET_S = int(parts[0])*3600 + int(parts[1])*60 + float(parts[2]) if len(parts) == 3 else int(parts[0])*60 + float(parts[1])

def secs_to_tc(s):
    h = int(s // 3600); s %= 3600
    m = int(s // 60); s %= 60
    ms = int((s - int(s)) * 1000)
    return f'{h:02d}:{m:02d}:{int(s):02d},{ms:03d}'

def tc_to_secs(tc):
    h, m, rest = tc.replace(',', '.').split(':')
    return int(h)*3600 + int(m)*60 + float(rest)

def is_hallucination(text):
    \"\"\"檢查文字是否為重複型幻覺（同一字重複 >= 10 次）\"\"\"
    for c in set(text):
        if c.strip() and c * 10 in text:
            return True
    return False

# 讀取修復段 SRT，偏移時間軸
fix_blocks = re.split(r'\n\n+', open(FIX_SRT).read().strip())
new_entries = []
still_hallucinating = 0
for b in fix_blocks:
    lines = b.strip().split('\n')
    if len(lines) >= 3 and '-->' in lines[1]:
        start_s, end_s = lines[1].split(' --> ')
        abs_start = tc_to_secs(start_s.strip()) + OFFSET_S
        abs_end = tc_to_secs(end_s.strip()) + OFFSET_S
        text = '\n'.join(lines[2:])
        if is_hallucination(text):
            still_hallucinating += 1
            continue
        new_entries.append((secs_to_tc(abs_start), secs_to_tc(abs_end), text))

if not new_entries:
    print(f'WARNING: Whisper 結果仍全為幻覺（{still_hallucinating} 條），該段可能真的是靜音/音樂，跳過')
    sys.exit(0)

# 讀取最終 SRT，替換幻覺條目
content = open(SRT_FILE).read()
blocks = re.split(r'\n\n+', content.strip())

result = []
inserted = False
removed = 0
for b in blocks:
    lines = b.strip().split('\n')
    if len(lines) >= 2 and '-->' in lines[1]:
        text = '\n'.join(lines[2:])
        if is_hallucination(text):
            if not inserted:
                for s, e, t in new_entries:
                    result.append(f'0\n{s} --> {e}\n{t}')
                inserted = True
            removed += 1
            continue
    result.append(b)

# 排序 + 去除跟既有條目時間範圍重疊的 patch 條目 + 刪除 < 300ms 的短條目
# 原因：caller 通常傳含 ±2s buffer 的 HALL_START/HALL_END，Whisper 用 buffer 跑出
# 的條目時間戳會跟 ASR 既有條目重疊，造成「短暫重疊」字幕
def tc_to_ms(tc):
    h, m, rest = tc.split(':'); s, ms = rest.split(',')
    return int(h)*3600000 + int(m)*60000 + int(s)*1000 + int(ms)

parsed = []
for b in result:
    lines = b.strip().split('\n')
    if len(lines) < 3 or '-->' not in lines[1]:
        continue
    m = re.match(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', lines[1])
    if not m: continue
    parsed.append({
        'start': tc_to_ms(m.group(1)),
        'end': tc_to_ms(m.group(2)),
        'tc': lines[1],
        'text': '\n'.join(lines[2:]),
    })

# Sort by start time
parsed.sort(key=lambda x: x['start'])

# Drop short (<300ms) entries — likely buffer-edge artefacts
before_short = len(parsed)
parsed = [e for e in parsed if e['end'] - e['start'] >= 300]
dropped_short = before_short - len(parsed)

# Clamp end to next.start to prevent any residual overlap
clamped = 0
for i in range(len(parsed) - 1):
    if parsed[i]['end'] > parsed[i+1]['start']:
        parsed[i]['end'] = parsed[i+1]['start']
        clamped += 1

# Drop entries that became zero/negative duration after clamp
before_collapsed = len(parsed)
parsed = [e for e in parsed if e['end'] - e['start'] >= 300]
dropped_collapsed = before_collapsed - len(parsed)
dropped_short += dropped_collapsed

# Re-emit
final_blocks = []
for i, e in enumerate(parsed):
    s_ms = e['start']; e_ms = e['end']
    def ms_fmt(ms):
        h = ms // 3600000; ms %= 3600000
        m = ms // 60000; ms %= 60000
        s = ms // 1000; mss = ms % 1000
        return f'{h:02d}:{m:02d}:{s:02d},{mss:03d}'
    txt = e['text']
    final_blocks.append(f'{i+1}\n{ms_fmt(s_ms)} --> {ms_fmt(e_ms)}\n{txt}')
seq = len(parsed) + 1

with open(SRT_FILE, 'w') as f:
    f.write('\n\n'.join(final_blocks) + '\n')
if dropped_short or clamped:
    print(f'  Sort+cleanup: dropped {dropped_short} short(<300ms), clamped {clamped} overlap')
print(f'Patched: 刪除 {removed} 條幻覺，插入 {len(new_entries)} 條，總計 {seq-1} 條')
"

# 5. 清理暫存
rm -f "$FIX_WAV" "$FIX_SRT"
echo "完成，暫存已清理"
