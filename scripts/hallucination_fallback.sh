#!/bin/bash
# ASR 幻覺 fallback：用 Whisper large-v3 重跑 Breeze 無法修復的幻覺段
# 用法：hallucination_fallback.sh <SRT檔> <影片或音檔> <起始時間> <結束時間> [initial_prompt]
# 範例：hallucination_fallback.sh final.srt video.mp4 00:55:33 00:56:07 "在Seeking Alpha上面"
#
# 流程：截取音檔 → Whisper large-v3 重跑 → 時間偏移 → patch 回 SRT
# 注意：一定用 whisper-large-v3-mlx，不要用 turbo

set -euo pipefail

SRT_FILE="$1"
MEDIA_FILE="$2"
HALL_START="$3"
HALL_END="$4"
INITIAL_PROMPT="${5:-}"

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
  --output-format srt
  --output-name "_fix_segment"
  --output-dir "$WORK_DIR"
)

if [ -n "$INITIAL_PROMPT" ]; then
  WHISPER_ARGS+=(--initial-prompt "$INITIAL_PROMPT")
fi

mlx_whisper "${WHISPER_ARGS[@]}" "$FIX_WAV"

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

# 重新編號
final_blocks = []
seq = 1
for b in result:
    lines = b.strip().split('\n')
    if len(lines) >= 2 and '-->' in lines[1]:
        lines[0] = str(seq)
        final_blocks.append('\n'.join(lines))
        seq += 1

with open(SRT_FILE, 'w') as f:
    f.write('\n\n'.join(final_blocks) + '\n')
print(f'Patched: 刪除 {removed} 條幻覺，插入 {len(new_entries)} 條，總計 {seq-1} 條')
"

# 5. 清理暫存
rm -f "$FIX_WAV" "$FIX_SRT"
echo "完成，暫存已清理"
