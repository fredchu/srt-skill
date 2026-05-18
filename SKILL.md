---
name: srt
description: >
  影片/音檔一鍵產出校正後的繁體中文字幕（YouTube 下載 → ASR → 預處理 → LLM 校正 → 後處理）。
  當用戶提到「做字幕」「跑字幕」「產字幕」「字幕 xxx」「幫我 transcribe」「srt」
  「這個影片要上字幕」，或給了 YouTube 連結、影片/音檔路徑並暗示需要字幕時使用。
  也適用於用戶要求「更新術語」「學習術語」「術語表」時（--learn 模式）。
  不要用於：已有 SRT 只想潤稿（用 subtitle-polisher）、翻譯（用 translator 類 skill）。
---

# SRT — 一鍵字幕 Pipeline

將影片或音檔轉成校正後的台灣繁體中文 SRT 字幕。全程自動化，用戶只需提供 YouTube 連結或本地檔案路徑。

## Pipeline 總覽

```
YouTube 連結 或 本地影片/音檔
  ↓ Step 0 (if YouTube): yt-dlp 下載影片
  ↓ Step 0.5 (if 投影片文字): 抽取本集術語補充表
本地影片檔
  ├─ Step 1:  subtitle.sh (Breeze ASR)        ─┐ 平行
  └─ Step 1': vibevoice_asr.py (VV ASR)       ─┘
原始 SRT (.srt) + VV JSON
  ↓ Step 1.5: srt_hallucination_fix.py (幻覺偵測+自動修復)
  ↓ Step 2a: srt_preprocess.py → _2a_preprocessed.srt
  ↓ Step 2b: Agent subagent (Sonnet) 逐段校正 + VV 交叉參考 → _2b_corrected.srt
  ↓ Step 2c: 複查 + srt_postprocess.py → _2c_reviewed.srt → _2c_final.srt
  ↓ Step 3: 術語學習
術語表自動成長
  ↓ Step 4 (if 有影片檔): ffmpeg 合併字幕進影片
字幕影片 (_sub.mkv)
```

## 檔案路徑

所有 pipeline 腳本位於：
```
SUBTITLE_DIR=/Users/fredchu/Documents/For_Claude/scripts/subtitle
CORRECT_DIR=${SUBTITLE_DIR}/srt_correct

${SUBTITLE_DIR}/
├── subtitle.sh                          # Step 1: ASR
├── srt_correct/
│   ├── srt_correct_prompt.txt           # LLM system prompt
│   ├── srt_preprocess.py                # Step 2a: 機械性預處理
│   ├── srt_postprocess.py               # Step 2c: 後處理（強制拆句等）
│   ├── srt_strip_commentary.py          # Step 2c: 清掉複查 subagent 殘留的判斷文字
│   └── terms_austin_v2.txt              # 講者術語表
```

## 執行步驟

### 解析用戶意圖

從用戶訊息中判斷輸入類型，三種路徑：

| 輸入 | 判斷方式 | 起始步驟 |
|------|---------|---------|
| YouTube 連結 | 包含 youtube.com 或 youtu.be | Step 0（下載）→ Step 1 → Step 2 |
| 本地影片 | .mp4 / .mkv / .mov / .webm 等 | Step 1（ASR）→ Step 2 |
| 本地音檔 | .mp3 / .wav / .m4a / .flac 等 | Step 1（ASR，subtitle.sh 自動處理音檔格式）→ Step 2 |

其他參數：
- **ASR 模式**：預設 Breeze（`--breeze`）。用戶提到 whisper / 英文內容 / 非中文 → 用 Whisper（不加 `--breeze`）
- **術語表**：預設 `terms_austin_v2.txt`。用戶指定其他講者 → 尋找對應術語表
- **投影片文字**：用戶提供投影片文字檔（.txt）→ 啟用 Step 0.5 抽取本集術語
- **特殊要求**：`--learn`（術語學習）、`--bilingual`（雙語輸出）
- **LLM 模式**：預設 Sonnet subagent（雲端）。用戶提到 `--local` / 「用本地」/ 「離線」→ 用 Ollama gemma4:26b。需要 Ollama 已啟動且 gemma4:26b 已拉取

### 工作目錄設定（每部影片必做）

所有產出檔案放進 `media/<簡短名稱>/`，不要散落在根目錄。

1. 從影片標題取一個簡短資料夾名：去掉 YouTube ID、去掉副檔名、截斷過長標題
   - 例：`20260311-驚不驚喜 [CkzcQfVr5ow].mkv` → `20260311-驚不驚喜`
   - 例：`投資組合-2月-03 [DZ5LgiWOPZ8].mkv` → `投資組合-2月-03`
   - 例：YouTube 標題 `用Claude Code自动做Skill的万能配方` → `用Claude-Code自动做Skill`
2. 建立目錄：

```bash
VIDEO_DIR="${SUBTITLE_DIR}/media/<簡短名稱>"
mkdir -p "${VIDEO_DIR}"
```

3. 後續所有步驟的 `<工作目錄>` 都用 `${VIDEO_DIR}`

### Step 0 (YouTube only): 下載影片

只有輸入是 YouTube 連結時才執行這步。本地檔案直接跳到 Step 1。

```bash
yt-dlp -f "bestvideo[height<=1080]+bestaudio/best" \
  --merge-output-format mkv \
  --output "${VIDEO_DIR}/%(title)s [%(id)s].%(ext)s" \
  "<YouTube URL>"
```

本地檔案的處理：把影片/音檔搬進 `${VIDEO_DIR}`（如果已經在裡面就不用搬）：
```bash
mv "<原始路徑>" "${VIDEO_DIR}/"
```

說明：
- 直接下載到 `${VIDEO_DIR}`，不使用暫存目錄（避免 cwd 被刪除導致背景任務崩潰）
- 檔名格式：`影片標題 [影片ID].mkv`
- 下載完成後，用 `${VIDEO_DIR}` 內的 mkv 檔案路徑繼續 Step 1
- 影片檔在 pipeline 結束後保留，不要刪除

### Step 0.5: 畫面 Caption 擷取（ASR 完成後執行）

從影片畫面自動擷取帶時間戳的 caption，供 Step 2b 校正時作為視覺語境參考。

**執行時機**：Step 1（ASR）和 Step 1'（VV）都完成後、Step 2a 之前或之後。不可與 ASR 平行（都吃 MLX GPU）。

**如果用戶提供了投影片文字檔**（.txt），跳過自動擷取，直接用用戶的檔案作為全局術語表（舊行為）。

**自動擷取**（無投影片文字檔時）：

```bash
cd "${VIDEO_DIR}" && python3 "${SUBTITLE_DIR}/srt_extract_slides.py" \
    "${VIDEO_DIR}/<影片或音檔名>" \
    --caption --interval 60 \
    --output "${VIDEO_DIR}/<檔名>_slide_terms.txt"
```

產出：
- `<檔名>_slide_captions.json`：帶時間戳的 caption 陣列，格式 `[{time_s, caption, terms}, ...]`
- `<檔名>_slide_terms.txt`：全局術語表（向下相容）

腳本內部流程：ffmpeg 每 60 秒截一幀 → imagehash 去重 → VLM caption + 術語抽取 → JSON 輸出。
預設使用 Gemma4:26b（Ollama vision，品質較高）。Ollama 不可用時 fallback 到 Qwen3-VL-8B（mlx-vlm）：`--model lmstudio-community/Qwen3-VL-8B-Instruct-MLX-4bit`。

在 Step 2b 組裝 prompt 時：
- `_slide_terms.txt` 加在術語表後面（全局，與舊行為相同）
- `_slide_captions.json` 按時間戳分配給對應的 segment，寫入 `_caption_ref_<N>.txt`

### Step 1': VibeVoice 平行 ASR（與 Step 1 同時跑）

VibeVoice 做輔助 ASR，產出供 Step 2b 交叉參考。與 Step 1 用兩個平行 Bash 呼叫同時執行。

**短音檔（≤ 55 分鐘）— 直接跑：**

```bash
cd "${VIDEO_DIR}" && python3 /Users/fredchu/dev/vibevoice-poc/vibevoice_asr.py \
    "${VIDEO_DIR}/<影片或音檔名>" \
    --terms "${CORRECT_DIR}/terms_austin_v2.txt" \
    --terms-max 50 \
    --json \
    --output "${VIDEO_DIR}/<檔名>_vibevoice.srt"
```

**長音檔（> 55 分鐘）— 切段跑再合併 JSON：**

`mlx_audio` 套件硬限制 59 分鐘（`MAX_DURATION_SECONDS = 59 * 60`），超過會自動 trim 截斷。用 ffmpeg 在靜音點切段，每段 ≤ 50 分鐘（留安全餘量），各自跑 VV 再合併 JSON（時間戳偏移對齊）：

```bash
# 1. 偵測靜音點，在最接近 45 分鐘倍數的靜音處切段
ffmpeg -i "${VIDEO_DIR}/<影片或音檔名>" -af silencedetect=noise=-30dB:d=0.5 -f null - 2>&1 | grep silence_end

# 2. 每段各自跑 VV（序列，不可平行——會搶 GPU 記憶體）
cd "${VIDEO_DIR}" && python3 /Users/fredchu/dev/vibevoice-poc/vibevoice_asr.py \
    "${VIDEO_DIR}/<檔名>_part1.wav" \
    --terms "${CORRECT_DIR}/terms_austin_v2.txt" --terms-max 50 \
    --json --output "${VIDEO_DIR}/<檔名>_vv_part1.srt"
# ... 同理 part2, part3, ...

# 3. 合併 JSON：偏移每段的 Start/End 時間戳
python3 -c "
import json, sys
offset = 0.0
merged = []
for i in range(1, int(sys.argv[1]) + 1):
    segs = json.load(open(f'${VIDEO_DIR}/<檔名>_vv_part{i}_vibevoice.json'))
    for s in segs:
        for key in ['Start', 'start', 'start_time']:
            if key in s: s[key] = float(s[key]) + offset
        for key in ['End', 'end', 'end_time']:
            if key in s: s[key] = float(s[key]) + offset
    merged.extend(segs)
    # offset = 該段結束時間（從 ffmpeg 切段記錄取得）
    if segs:
        offset = max(float(s.get('End', s.get('end', s.get('end_time', 0)))) for s in segs)
json.dump(merged, open('${VIDEO_DIR}/<檔名>_vibevoice.json', 'w'), ensure_ascii=False, indent=2)
print(f'Merged {len(merged)} segments')
" <段數>
```

產出：
- `<檔名>_vibevoice.srt` — VV 的 SRT（備用）
- `<檔名>_vibevoice.json` — VV 的 segments JSON（Step 2b 用），欄位格式：`Start`/`End`/`Content`/`Speaker`

注意：
- 如果 VV 執行失敗（模型未安裝等），pipeline 繼續跑，Step 2b 跳過 VV 參考
- 用 `--breeze` 時才啟用 Step 1'（Whisper 模式不用 VV，因為 VV 底層也是 Whisper 架構）
- `mlx_audio` 套件硬限制 59 分鐘（`MAX_DURATION_SECONDS = 59 * 60`），超過會靜默 trim — 必須在 pipeline 端切段，不能依賴 VV 自己處理
- `vibevoice_asr.py` 的 `max_tokens` 已改為 32768（原 8192 對 > 30 分鐘音檔不夠，會導致 0 segments）
- `generate()` 其他可調參數：`repetition_penalty`（預設 1.2）、`prefill_step_size`（預設 2048，長音檔可提高以降低記憶體峰值）
- **不要同時跑兩個 VV instance** — 會搶 Apple Silicon GPU 記憶體互相 thrash，必須序列執行

### Step 1: ASR 語音辨識

```bash
cd /Users/fredchu/Documents/For_Claude/scripts/subtitle
./subtitle.sh "${VIDEO_DIR}/<影片或音檔名>" --breeze
```

如果用戶要求 Whisper：
```bash
./subtitle.sh "${VIDEO_DIR}/<影片或音檔名>"
```

產出：`${VIDEO_DIR}/<檔名>.srt`（Breeze）或 `${VIDEO_DIR}/<檔名>_zh-TW.srt`（Whisper + OpenCC）

`subtitle.sh` 內部還會自動執行 Step 4.5（`postprocess_srt.py --no-punctuation`）：只做 `terminology_rules.py` 術語校正，不做標點恢復。ASR 輸出是無標點的純文字，標點由 Step 2b 的 LLM 在校正時一併加上（LLM 有完整語境，標點品質優於 sherpa-onnx 的局部標點模型）。

這一步耗時最長（約影片長度的 0.05-0.1x），跑完後告知用戶 ASR 完成並繼續。

### Step 1.5: ASR 幻覺偵測 + 自動修復

ASR 完成後，自動掃描 SRT 找兩種異常：
1. **重複型幻覺**：同一字/詞在單條字幕中重複 ≥ 10 次（如 Whisper 把「漲漲漲」幻覺成 100 個「漲」）
2. **時間軸空白**：相鄰字幕間隔 > 10 秒（ASR 跳過整段語音）

偵測到就自動截取該段音檔、用 Breeze ASR 重跑、patch 回 SRT。

```bash
CORRECT_DIR=/Users/fredchu/Documents/For_Claude/scripts/subtitle/srt_correct
python3 "${CORRECT_DIR}/srt_hallucination_fix.py" "<ASR 產出的 SRT>" "${VIDEO_DIR}/<影片或音檔名>" --breeze
```

如果是 Whisper ASR，不加 `--breeze`。

腳本會自動處理，無需人工介入。如果二次 ASR 仍是幻覺，會印 WARNING 跳過。

**Fallback：換 Whisper large-v3 重跑**。檢查 srt_hallucination_fix.py 的輸出，如果有「多次重試仍失敗」的 WARNING，不要跳過 — 用 `scripts/hallucination_fallback.sh` 自動改用 Whisper large-v3 重跑。Breeze 對特定段落會反覆產生相同幻覺，換模型通常能解決。

對每個 WARNING 跳過的幻覺段：
1. 從 SRT 定位幻覺條目的起止時間軸（前後各留 2 秒 buffer）
2. 取幻覺前 2-3 條字幕文字作為 initial prompt
3. 執行 fallback 腳本：

```bash
SKILL_DIR="$(dirname "$(readlink -f "$0")")"  # 或直接用 skill 路徑
"${SKILL_DIR}/scripts/hallucination_fallback.sh" \
  "<SRT檔路徑>" "<影片或音檔路徑>" "<起始時間>" "<結束時間>" "<initial prompt>"
```

腳本會自動完成：截取音檔 → Whisper large-v3 重跑 → 時間偏移 → patch 回 SRT → 清理暫存。如果 Whisper 結果仍為幻覺，會印 WARNING 跳過（該段可能真的是靜音/音樂）。

完成後如果已有字幕影片（`_sub.mkv`），刪掉舊的並用 ffmpeg 重新合併。

### Step 2: LLM 校正（三階段）

**重要：不要跑 `srt_correct.sh`！** 它內部用 `claude -p`，在 Claude Code 裡會被環境變數阻擋。改用以下三階段流程。

#### Step 2a: 預處理

```bash
CORRECT_DIR=/Users/fredchu/Documents/For_Claude/scripts/subtitle/srt_correct
python3 "${CORRECT_DIR}/srt_preprocess.py" "<ASR 產出的 SRT>" "<輸出路徑>_2a_preprocessed.srt" --stats --breeze
```

（如果是 Whisper ASR，不加 `--breeze`）

#### Step 2b: 分段 + Agent 校正

> **Context 節約原則**：主 agent 不讀 SRT 內容到自己的 context。切分、prompt 組裝全部在 disk 上用腳本完成，subagent 自己讀檔案。

1. **用 Python 腳本一次完成切分 + prompt 組裝**：

   在 Bash 中執行以下 Python 腳本（不要用 Read 工具讀 SRT 檔案）：

   ```python
   python3 -c "
   import re, os, json

   CORRECT_DIR = '${CORRECT_DIR}'
   WORK_DIR = '<工作目錄>'
   SRT_FILE = '<preprocessed SRT 路徑>'
   SLIDE_TERMS = '<投影片術語路徑或空字串>'  # 沒有就留空
   VV_JSON = '<VV JSON 路徑或空字串>'  # Step 1' 產出的 _vibevoice.json，沒有就留空
   CAPTIONS_JSON = '<caption JSON 路徑或空字串>'  # Step 0.5 產出的 _slide_captions.json，沒有就留空

   # 讀取 prompt 模板和術語表
   prompt = open(f'{CORRECT_DIR}/srt_correct_prompt.txt').read()
   terms = open(f'{CORRECT_DIR}/terms_austin_v2.txt').read()

   # 組裝術語區塊
   term_section = terms
   if SLIDE_TERMS and os.path.exists(SLIDE_TERMS):
       slide = open(SLIDE_TERMS).read()
       term_section += '\n\n## 本集投影片術語\n' + slide

   system_prompt = prompt.replace('{{TERMINOLOGY_SECTION}}', term_section)

   # 如果有 VV JSON，在 system prompt 末尾加 VV 交叉參考 section
   vv_segments = []
   if VV_JSON and os.path.exists(VV_JSON):
       vv_segments = json.load(open(VV_JSON))
       system_prompt += '''

## 交叉參考：VibeVoice ASR

以下每段字幕會附帶另一個 ASR 引擎（VibeVoice，有 hotwords 注入）對同一段音檔的辨識結果。
VibeVoice 的英文專有名詞和部分中文財經術語辨識較準確，但語氣詞過多。

使用規則：
1. 英文 ticker / 專有名詞（如 ETF 代碼）→ 以 VibeVoice 版本為準
2. 中文財經術語 → 如果 VibeVoice 的詞彙更合理且語境正確，採用之
3. 語氣詞（哦、呃、嗯）→ 忽略 VibeVoice 多出的部分
4. 已知 VibeVoice 錯誤（不要採用）：
   - 「值信」「指信」應為「質性」
   - 「MOT」應為「MOAT」
   - 「SHD」應為「SPHD」
   - 「KVW」應為「KWEB」
   - 「BOZ」應為「BOTZ」
   - 「QILD」應為「QYLD」
   - 「XILP」應為「XLP」
   - 「SkyYY」應為「SKYY」
5. 不確定時 → 保留 Breeze（主 ASR）的版本

VibeVoice 參考文字會寫在每段的 _vv_ref_<N>.txt 檔案中。
'''
       print(f'VV JSON loaded: {len(vv_segments)} segments')

   # 如果有 Caption JSON，在 system prompt 末尾加畫面描述 section
   captions = []
   if CAPTIONS_JSON and os.path.exists(CAPTIONS_JSON):
       captions = json.load(open(CAPTIONS_JSON))
       system_prompt += '''

## 畫面截圖描述（帶時間戳）

以下每段字幕會附帶影片畫面的 VLM 描述，標示了該時間點畫面顯示的內容（投影片、圖表、人物等）。

使用規則：
1. 畫面描述中的英文術語/ticker/人名 → 以畫面為準（這是 ground truth）
2. 如果 ASR 文字跟畫面描述的術語不一致 → 優先信任畫面
3. 畫面描述提供語境，幫助判斷同音字校正方向

畫面描述會寫在每段的 _caption_ref_<N>.txt 檔案中。
'''
       print(f'Captions JSON loaded: {len(captions)} entries')

   # 寫出 system prompt
   with open(f'{WORK_DIR}/_system_prompt.txt', 'w') as f:
       f.write(system_prompt)

   # 切分 SRT
   content = open(SRT_FILE).read()
   blocks = re.split(r'\n\n+', content.strip())
   SEG_SIZE = 300
   segments = []
   for i in range(0, len(blocks), SEG_SIZE):
       segments.append(blocks[i:i+SEG_SIZE])

   # 輔助函數：從 SRT block 提取時間戳（毫秒）
   def parse_srt_time_ms(block):
       lines = block.strip().split('\n')
       if len(lines) >= 2 and '-->' in lines[1]:
           tc = lines[1].strip()
           parts = tc.split(' --> ')
           def to_ms(t):
               h, m, rest = t.split(':')
               s, ms = rest.split(',')
               return int(h)*3600000 + int(m)*60000 + int(s)*1000 + int(ms)
           return to_ms(parts[0]), to_ms(parts[1])
       return None, None

   # 輔助函數：提取 VV 參考文字
   def extract_vv_reference(seg_start_ms, seg_end_ms):
       parts = []
       for seg in vv_segments:
           vv_start = float(seg.get('Start', seg.get('start', seg.get('start_time', 0)))) * 1000
           vv_end = float(seg.get('End', seg.get('end', seg.get('end_time', 0)))) * 1000
           if vv_end > seg_start_ms and vv_start < seg_end_ms:
               text = seg.get('Content', seg.get('text', '')).strip()
               if text and text != '[Silence]':
                   parts.append(text)
       return '\n'.join(parts)

   # 寫出每段 + 上文參考 + VV 參考
   for idx, seg in enumerate(segments):
       with open(f'{WORK_DIR}/_seg_{idx}.srt', 'w') as f:
           f.write('\n\n'.join(seg) + '\n')
       # 上文參考：前一段最後 5 條的純文字
       if idx > 0:
           prev_blocks = segments[idx-1][-5:]
           ctx_lines = []
           for b in prev_blocks:
               lines = b.strip().split('\n')
               text_lines = [l for l in lines[2:] if not re.match(r'\d+:\d+:\d+', l)]
               ctx_lines.extend(text_lines)
           with open(f'{WORK_DIR}/_ctx_{idx}.txt', 'w') as f:
               f.write('\n'.join(ctx_lines))
       # VV 參考：從 VV segments 提取時間重疊的文字
       if vv_segments:
           first_start, _ = parse_srt_time_ms(seg[0])
           _, last_end = parse_srt_time_ms(seg[-1])
           if first_start is not None and last_end is not None:
               vv_ref = extract_vv_reference(first_start, last_end)
               with open(f'{WORK_DIR}/_vv_ref_{idx}.txt', 'w') as f:
                   f.write(vv_ref if vv_ref else 'NO_VV_REFERENCE')
           else:
               with open(f'{WORK_DIR}/_vv_ref_{idx}.txt', 'w') as f:
                   f.write('NO_VV_REFERENCE')
       # Caption 參考：從 captions 提取時間重疊的畫面描述
       if captions:
           first_start, _ = parse_srt_time_ms(seg[0])
           _, last_end = parse_srt_time_ms(seg[-1])
           if first_start is not None and last_end is not None:
               seg_caps = []
               for c in captions:
                   cap_ms = c['time_s'] * 1000
                   if first_start - 30000 <= cap_ms <= last_end + 30000:
                       m, s = divmod(int(c['time_s']), 60)
                       seg_caps.append(f'[{m:02d}:{s:02d}] {c["caption"]}')
                       if c.get('terms'):
                           seg_caps.append(f'        術語: {", ".join(c["terms"])}')
               with open(f'{WORK_DIR}/_caption_ref_{idx}.txt', 'w') as f:
                   f.write('\n'.join(seg_caps) if seg_caps else 'NO_CAPTIONS')
           else:
               with open(f'{WORK_DIR}/_caption_ref_{idx}.txt', 'w') as f:
                   f.write('NO_CAPTIONS')

   print(f'Total blocks: {len(blocks)}')
   print(f'Number of segments: {len(segments)}')
   for i, s in enumerate(segments):
       print(f'  Segment {i}: {len(s)} blocks')
   if vv_segments:
       print(f'VV reference files written for {len(segments)} segments')
   "
   ```

   這個腳本產出：
   - `_system_prompt.txt`：組裝好的完整 system prompt（含 VV 交叉參考規則 + 畫面描述規則，如有）
   - `_seg_0.srt` ~ `_seg_N.srt`：每段 SRT
   - `_ctx_1.txt` ~ `_ctx_N.txt`：每段的上文參考（第一段沒有）
   - `_vv_ref_0.txt` ~ `_vv_ref_N.txt`：每段的 VV 參考文字（如有 VV JSON）
   - `_caption_ref_0.txt` ~ `_caption_ref_N.txt`：每段的畫面描述（如有 Caption JSON）

2. **平行發起所有 subagent**：用單一訊息同時發起所有 Agent tool 呼叫：

   ```
   Agent tool 參數：
   - model: "sonnet"
   - mode: "bypassPermissions"
   - prompt: 見下方
   ```

   每段 prompt 格式（subagent 自己讀檔案，主 agent 不 inline 內容）：
   ```
   你是字幕校正 subagent。請完成以下步驟：

   1. 用 Read 工具讀取 system prompt：<工作目錄>/_system_prompt.txt
   2. 用 Read 工具讀取待校正字幕：<工作目錄>/_seg_<N>.srt
   3. [如果 N > 0] 用 Read 工具讀取上文參考：<工作目錄>/_ctx_<N>.txt
   4. [如果檔案存在] 用 Read 工具讀取 VibeVoice 參考：<工作目錄>/_vv_ref_<N>.txt
      - 如果內容是 NO_VV_REFERENCE 則跳過
      - 否則交叉參考 VibeVoice 結果，特別注意英文名詞和財經術語
   5. [如果檔案存在] 用 Read 工具讀取畫面描述：<工作目錄>/_caption_ref_<N>.txt
      - 如果內容是 NO_CAPTIONS 則跳過
      - 否則參考畫面描述中的術語和語境進行校正，畫面上的英文拼法是 ground truth
   6. 依照 system prompt 的規則校正字幕
   7. 用 Write 工具把校正結果寫入：<工作目錄>/_seg_<N>_corrected.srt

   重要：
   - 上文參考僅供理解語境，不要輸出這些內容
   - VibeVoice 參考僅供交叉比對，不要直接複製其語氣詞
   - 畫面描述中的英文術語是 ground truth，優先信任
   - 輸出必須是完整的 SRT 格式（含序號、時間軸、字幕文字）
   - 完成後回報修改了多少條
   ```

   **重要**：所有 Agent 呼叫必須在**同一個訊息**中發出，才能真正平行執行。

   **驗證**：subagent 完成後，主 agent 檢查 `_seg_<N>_corrected.srt` 檔案是否存在且包含 SRT 時間軸格式。如果沒有，重試。

#### Step 2b 替代路徑：本地 LLM（--local 模式）

用戶指定 `--local` 時，用 `ollama_llm.py` 取代 Sonnet subagent。逐段序列執行（Ollama 單 GPU 不支援平行）。

```bash
OLLAMA_LLM="${CORRECT_DIR}/ollama_llm.py"

for seg_file in "${WORK_DIR}"/_seg_*.srt; do
    N=$(echo "$seg_file" | grep -oP '_seg_\K\d+')

    # 組裝 user input：上文參考 + VV 參考 + SRT 段落
    USER_INPUT=""
    if [ -f "${WORK_DIR}/_ctx_${N}.txt" ]; then
        USER_INPUT="【上文參考】\n$(cat "${WORK_DIR}/_ctx_${N}.txt")\n\n"
    fi
    if [ -f "${WORK_DIR}/_vv_ref_${N}.txt" ] && ! grep -q "NO_VV_REFERENCE" "${WORK_DIR}/_vv_ref_${N}.txt"; then
        USER_INPUT="${USER_INPUT}【VibeVoice 參考】\n$(cat "${WORK_DIR}/_vv_ref_${N}.txt")\n\n"
    fi
    USER_INPUT="${USER_INPUT}$(cat "$seg_file")"
    echo -e "$USER_INPUT" > "${WORK_DIR}/_user_${N}.txt"

    python3 "$OLLAMA_LLM" \
        --system "${WORK_DIR}/_system_prompt.txt" \
        --user "${WORK_DIR}/_user_${N}.txt" \
        --output "${WORK_DIR}/_seg_${N}_corrected.srt" \
        --max-tokens 16384

    echo "Segment $N corrected"
done
```

**注意事項**：
- 本地模式不做 SRT 拆句（26B 測試顯示不具備此能力），依賴 Step 2c 的 `srt_postprocess.py` 自動拆句
- 速度：每 300 blocks 約 30-60 秒（vs Sonnet 平行 ~10 秒），總時間較長但免費
- 如果 Ollama 回傳 error，印 WARNING 並 fallback 到 Sonnet subagent（該段）

3. **合併**：用 Python 腳本合併所有段的校正結果（不要讀入主 context）：

   ```python
   python3 -c "
   import re, glob

   WORK_DIR = '<工作目錄>'
   files = sorted(glob.glob(f'{WORK_DIR}/_seg_*_corrected.srt'),
                  key=lambda f: int(re.search(r'_seg_(\d+)', f).group(1)))

   def ts_to_ms(ts):
       h, m, rest = ts.split(':')
       s, ms_part = rest.replace('.', ',').split(',')
       return int(h)*3600000 + int(m)*60000 + int(s)*1000 + int(ms_part)

   merged = []
   for fpath in files:
       content = open(fpath).read().strip()
       blocks = re.split(r'\n\n+', content)
       for block in blocks:
           lines = block.strip().split('\n')
           # 嚴格驗證 SRT 結構：至少 3 行、第二行必須有 -->
           if len(lines) < 3 or '-->' not in lines[1]:
               continue
           # 修復雙行重複：local LLM 可能輸出原文+校正兩行
           text_lines = lines[2:]
           if len(text_lines) > 1:
               # 如果兩行幾乎相同（只差標點），保留較長的那行
               from difflib import SequenceMatcher
               for i in range(len(text_lines) - 1, 0, -1):
                   ratio = SequenceMatcher(None, text_lines[i-1], text_lines[i]).ratio()
                   if ratio > 0.7:
                       # 保留較長的（通常是有標點的校正版）
                       keep = text_lines[i] if len(text_lines[i]) >= len(text_lines[i-1]) else text_lines[i-1]
                       text_lines = text_lines[:i-1] + [keep] + text_lines[i+1:]
           lines = lines[:2] + text_lines
           merged.append('\n'.join(lines))

   # 按時間軸排序（修復 local LLM 輸出亂序問題）
   def sort_key(block_str):
       lines = block_str.split('\n')
       ts_m = re.match(r'(\d{2}:\d{2}:\d{2}[,.]\d{3})', lines[1])
       return ts_to_ms(ts_m.group(1)) if ts_m else 0
   merged.sort(key=sort_key)

   def ms_to_ts(ms):
       h = ms // 3600000; ms %= 3600000
       m = ms // 60000; ms %= 60000
       s = ms // 1000; frac = ms % 1000
       return f'{h:02d}:{m:02d}:{s:02d},{frac:03d}'

   # Clamp end time：local LLM 常把 end time 寫錯（duration 暴增 60-240 秒）
   # 規則：如果 end > 下一條 start，clamp 到下一條 start
   for i in range(len(merged) - 1):
       lines_a = merged[i].split('\n')
       lines_b = merged[i+1].split('\n')
       ts_a = re.match(r'(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})', lines_a[1])
       ts_b = re.match(r'(\d{2}:\d{2}:\d{2}[,.]\d{3})', lines_b[1])
       if ts_a and ts_b:
           end_a = ts_to_ms(ts_a.group(2))
           start_b = ts_to_ms(ts_b.group(1))
           if end_a > start_b:
               lines_a[1] = f'{ts_a.group(1)} --> {ms_to_ts(start_b)}'
               merged[i] = '\n'.join(lines_a)

   # 重新編號
   for i, block in enumerate(merged):
       lines = block.split('\n')
       lines[0] = str(i + 1)
       merged[i] = '\n'.join(lines)

   # Coverage check：用 preprocessed SRT 補洞（local LLM 可能因 max_tokens 截斷丟失條目）
   PREPROCESSED = '<preprocessed SRT 路徑>'
   pre_blocks = re.split(r'\n\n+', open(PREPROCESSED).read().strip())
   merged_starts = set()
   for block in merged:
       lines = block.split('\n')
       ts_m = re.match(r'(\d{2}:\d{2}:\d{2}[,.]\d{3})', lines[1])
       if ts_m:
           merged_starts.add(ts_to_ms(ts_m.group(1)))

   patched = 0
   for b in pre_blocks:
       lines = b.strip().split('\n')
       if len(lines) < 3 or '-->' not in lines[1]:
           continue
       ts_m = re.match(r'(\d{2}:\d{2}:\d{2}[,.]\d{3})', lines[1])
       if ts_m and ts_to_ms(ts_m.group(1)) not in merged_starts:
           # 檢查這個條目是否落在 merged 的某個 gap 裡（>15s）
           start = ts_to_ms(ts_m.group(1))
           in_gap = False
           for j in range(len(merged) - 1):
               m_lines = merged[j].split('\n')
               m_next = merged[j+1].split('\n')
               m_end = re.match(r'\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})', m_lines[1])
               m_start = re.match(r'(\d{2}:\d{2}:\d{2}[,.]\d{3})', m_next[1])
               if m_end and m_start:
                   gap = ts_to_ms(m_start.group(1)) - ts_to_ms(m_end.group(1))
                   if gap > 15000 and ts_to_ms(m_end.group(1)) <= start <= ts_to_ms(m_start.group(1)):
                       in_gap = True
                       break
           if in_gap:
               merged.append('\n'.join(lines))
               patched += 1

   if patched > 0:
       merged.sort(key=sort_key)
       # 重新 clamp end times
       for i in range(len(merged) - 1):
           lines_a = merged[i].split('\n')
           lines_b = merged[i+1].split('\n')
           ts_a = re.match(r'(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})', lines_a[1])
           ts_b = re.match(r'(\d{2}:\d{2}:\d{2}[,.]\d{3})', lines_b[1])
           if ts_a and ts_b:
               end_a = ts_to_ms(ts_a.group(2))
               start_b = ts_to_ms(ts_b.group(1))
               if end_a > start_b:
                   lines_a[1] = f'{ts_a.group(1)} --> {ms_to_ts(start_b)}'
                   merged[i] = '\n'.join(lines_a)
       print(f'Coverage check: patched {patched} missing entries from preprocessed SRT')

   # 重新編號
   for i, block in enumerate(merged):
       lines = block.split('\n')
       lines[0] = str(i + 1)
       merged[i] = '\n'.join(lines)

   output = '<最終合併路徑>_2b_corrected.srt'
   with open(output, 'w') as f:
       f.write('\n\n'.join(merged) + '\n')
   print(f'Merged {len(merged)} entries to {output}')
   "
   ```

#### Step 2c: 複查 pass + 後處理

> **同樣遵守 Context 節約原則**：提取未變動條目、組裝複查 prompt 全部用 Python 腳本在 disk 上完成。

1. **用 Python 腳本提取未變動條目 + 組裝複查 prompt**：

   ```python
   python3 -c "
   import re, os, difflib

   WORK_DIR = '<工作目錄>'
   CORRECT_DIR = '${CORRECT_DIR}'
   PREPROCESSED = '<preprocessed SRT 路徑>'
   CORRECTED_RAW = '<_2b_corrected.srt 路徑>'

   # 解析 SRT 為 {timecode: text} 字典
   def parse_srt(path):
       blocks = re.split(r'\n\n+', open(path).read().strip())
       result = {}
       for b in blocks:
           lines = b.strip().split('\n')
           if len(lines) >= 2 and '-->' in lines[1]:
               tc = lines[1].strip()
               text = '\n'.join(lines[2:])
               result[tc] = text
       return result, blocks

   pre_dict, _ = parse_srt(PREPROCESSED)
   cor_dict, cor_blocks = parse_srt(CORRECTED_RAW)

   # 找未變動條目
   unchanged = []
   for b in cor_blocks:
       lines = b.strip().split('\n')
       if len(lines) >= 2 and '-->' in lines[1]:
           tc = lines[1].strip()
           text = '\n'.join(lines[2:])
           if tc in pre_dict and pre_dict[tc] == text:
               unchanged.append(b)

   # 找修正範例（前 10 個代表性修正）
   examples = []
   for tc, pre_text in pre_dict.items():
       if tc in cor_dict and pre_dict[tc] != cor_dict[tc]:
           examples.append(f'{pre_text} → {cor_dict[tc]}')
           if len(examples) >= 10:
               break

   # 組裝複查 prompt
   terms = open(f'{CORRECT_DIR}/terms_austin_v2.txt').read()
   review_prompt = f'''你是字幕複查員。以下字幕已經過一輪 ASR 校正但未被修改。
請逐條檢查是否有殘留的 ASR 錯誤。

## 術語表
{terms}

## 第一輪已發現的錯誤範例（供校準判斷標準）
''' + '\n'.join(examples) + '''

## 重點檢查項目
- 同音字/近音字錯誤（如「機點」應為「基點」、「教育日」應為「交易日」）
- 術語表中的詞被 ASR 聽成別的詞
- 英文辨識錯誤（大小寫、拼寫）
- 重複字詞未清理

## 不要改的
- 專有名詞、人名、地名、作品名、時事用語 — 即使你不認識也不要改，講者可能在引用你不知道的時事、作品、流行語
- 語意通順、在上下文中說得通的條目

## 輸出格式（嚴格）
只輸出需要修改的條目，格式：
原始時間軸
校正後文字

**絕對禁止**：
- 輸出判斷說明，例如 `[通順，不改]`、`[備註：...]`、`[確認：...]`、`→`、「原文通順」、「不改」、「請確認語境」、「應是...」、「若上條...」、「但後文...」
- 輸出多行判斷邏輯（一條目對應一行純字幕，禁止把推理過程當字幕第二行）
- 輸出未閉合的引號、括號或方括號（`「`、`[` 必須在同一行完成配對）
- 輸出「無修改」「OK」這類佔位文字

如果該條目沒問題，**完全不輸出**（連時間軸都不要列）。
「校正後文字」必須是純字幕內容，不含任何符號標記、推理文字或內部判斷。
'''

   with open(f'{WORK_DIR}/_review_prompt.txt', 'w') as f:
       f.write(review_prompt)

   # 切分未變動條目為段落
   SEG_SIZE = 300
   for i in range(0, len(unchanged), SEG_SIZE):
       seg = unchanged[i:i+SEG_SIZE]
       with open(f'{WORK_DIR}/_review_seg_{i//SEG_SIZE}.srt', 'w') as f:
           f.write('\n\n'.join(seg) + '\n')

   n_segs = (len(unchanged) + SEG_SIZE - 1) // SEG_SIZE
   print(f'Unchanged: {len(unchanged)} entries, split into {n_segs} review segments')
   print(f'Correction examples: {len(examples)}')
   "
   ```

2. **平行發起複查 subagent**：

   每段 prompt 格式：
   ```
   你是字幕複查 subagent。請完成以下步驟：

   1. 用 Read 工具讀取複查 prompt：<工作目錄>/_review_prompt.txt
   2. 用 Read 工具讀取待複查字幕：<工作目錄>/_review_seg_<N>.srt
   3. 依照 prompt 規則逐條檢查
   4. 用 Write 工具把需要修改的條目寫入：<工作目錄>/_review_seg_<N>_fixes.txt
      格式：每個修正一個 block，時間軸 + 校正後文字，block 間空行分隔
      如果全部沒問題，寫入 "NO_FIXES"

   完成後回報修改了多少條。
   ```

#### Step 2c 替代路徑：本地 LLM（--local 模式）

```bash
for review_file in "${WORK_DIR}"/_review_seg_*.srt; do
    N=$(echo "$review_file" | grep -oP '_review_seg_\K\d+')
    python3 "$OLLAMA_LLM" \
        --system "${WORK_DIR}/_review_prompt.txt" \
        --user "$review_file" \
        --output "${WORK_DIR}/_review_seg_${N}_fixes.txt" \
        --max-tokens 4096
done
```

3. **合併複查結果 + 後處理**：

   ```bash
   # 用 Python 腳本把複查修正覆蓋回 corrected_raw，然後跑後處理
   python3 -c "
   import re, glob

   WORK_DIR = '<工作目錄>'
   CORRECTED_RAW = '<_2b_corrected.srt 路徑>'

   # 收集所有複查修正
   fixes = {}
   for fpath in glob.glob(f'{WORK_DIR}/_review_seg_*_fixes.txt'):
       content = open(fpath).read().strip()
       if content == 'NO_FIXES':
           continue
       blocks = re.split(r'\n\n+', content)
       for b in blocks:
           lines = b.strip().split('\n')
           if len(lines) >= 2 and '-->' in lines[0]:
               tc = lines[0].strip()
               text = '\n'.join(lines[1:])
               fixes[tc] = text

   # 套用修正到 corrected_raw
   raw_blocks = re.split(r'\n\n+', open(CORRECTED_RAW).read().strip())
   result = []
   applied = 0
   for b in raw_blocks:
       lines = b.strip().split('\n')
       if len(lines) >= 2 and '-->' in lines[1]:
           tc = lines[1].strip()
           if tc in fixes:
               lines = [lines[0], lines[1]] + fixes[tc].split('\n')
               applied += 1
       result.append('\n'.join(lines))

   output = CORRECTED_RAW.replace('_2b_corrected.srt', '_2c_reviewed.srt')
   with open(output, 'w') as f:
       f.write('\n\n'.join(result) + '\n')
   print(f'Applied {applied} review fixes, saved to {output}')
   " && \
   # Strip commentary 殘留（複查 subagent 偶爾會把判斷文字當字幕寫入，必須在 postprocess force-split 之前清掉，
   # 否則 split 後 commentary 會被切成多塊 chunk 污染數倍 block）
   python3 "${CORRECT_DIR}/srt_strip_commentary.py" "<_2c_reviewed.srt>" && \
   # --ref 只在雲端模式使用（還原被 Sonnet 捏造的時間軸）
   # 本地模式不加 --ref（local LLM 保留原始時間軸，加 --ref 反而會破壞）
   if [ "$LOCAL_MODE" = "true" ]; then
       python3 "${CORRECT_DIR}/srt_postprocess.py" "<_2c_reviewed.srt>" "<最終輸出路徑>_2c_final.srt" --stats
   else
       python3 "${CORRECT_DIR}/srt_postprocess.py" "<_2c_reviewed.srt>" "<最終輸出路徑>_2c_final.srt" --stats --ref "<preprocessed SRT 路徑>"
   fi && \
   # 第二層保險：postprocess 後再掃一次（含 Type B/C 啟發法，處理 split 殘留）
   python3 "${CORRECT_DIR}/srt_strip_commentary.py" "<最終輸出路徑>_2c_final.srt"
   ```

### Step 3: 術語自動學習（每次必跑）

每次 pipeline 完成後自動執行，不需要用戶觸發。

1. **比對** `_2a_preprocessed.srt` 和 `_2c_final.srt`，用 difflib.SequenceMatcher 找出 `replace` 操作
2. **過濾雜訊**：跳過純標點變更、大小寫變更、`他→它` 人稱代詞、超長片段（>10 字）
3. **統計**：計算每組 `(錯, 對)` 出現次數
4. **篩選**：出現 >= 2 次、不在現有術語表中、不在 preprocess 規則中
5. **分類建議**：
   - 確定性高的機械替換（英文縮寫、固定同音字）→ 建議加入 `srt_preprocess.py` 的 `AUTO_REPLACE_COMMON` 或 `AUTO_REPLACE_BREEZE`
   - 需要語境判斷的術語 → 建議加入 `terms_austin_v2.txt`
6. **向用戶展示候選清單**（含出現次數和範例語境），請用戶確認後直接寫入對應檔案
7. 如果沒有候選（全部已在術語表/preprocess 中），告知用戶「本次無新術語」即可

### Step 4 (有影片檔時): 合併字幕進影片

只有輸入是影片檔（YouTube 下載的 mkv 或本地影片）時才執行。音檔輸入跳過此步。

```bash
ffmpeg -i "${VIDEO_DIR}/<影片檔名>" -i "${VIDEO_DIR}/<_2c_final.srt>" \
  -c copy -c:s srt \
  -metadata:s:s:0 language=chi -metadata:s:s:0 title="繁體中文" \
  "${VIDEO_DIR}/<影片檔名>_sub.mkv"
```

說明：
- `-c copy`：影音串流直接複製，不重新編碼，幾秒內完成
- `-c:s srt`：字幕軌使用 SRT 格式嵌入
- 產出檔名：原影片檔名加 `_sub` 後綴，如 `影片標題 [ID]_sub.mkv`
- 所有產出都在 `${VIDEO_DIR}/` 內
- 原始影片檔和 SRT 檔都保留，不刪除

### Step 5: 清理暫存檔

pipeline 完成後，刪除 `${VIDEO_DIR}` 內的中間產物，只保留成品：

```bash
rm -f "${VIDEO_DIR}"/_seg_*.srt "${VIDEO_DIR}"/_ctx_*.txt \
      "${VIDEO_DIR}"/_vv_ref_*.txt "${VIDEO_DIR}"/_caption_ref_*.txt \
      "${VIDEO_DIR}"/_review_seg_*.srt "${VIDEO_DIR}"/_review_seg_*_fixes.txt \
      "${VIDEO_DIR}"/_system_prompt.txt "${VIDEO_DIR}"/_review_prompt.txt \
      "${VIDEO_DIR}"/*_vv_part*.wav "${VIDEO_DIR}"/*_vv_part*_vibevoice.json
```

清理完 `${VIDEO_DIR}` 後，也要檢查 pipeline 過程中是否在其他位置留下暫存檔（如 WAV 暫存檔等），有的話一併刪除。

保留的成品（供除錯與階段比較）：
- 原始影片/音檔
- ASR 原始 SRT（`.srt`）
- VibeVoice SRT + JSON（`_vibevoice.srt`、`_vibevoice.json`，如有）
- 預處理後（`_2a_preprocessed.srt`）— 可比較 ASR→預處理的差異
- LLM 校正後（`_2b_corrected.srt`）— 可比較預處理→LLM 校正的差異
- 複查後（`_2c_reviewed.srt`）— 可比較校正→複查的差異
- 最終成品（`_2c_final.srt`）— 經後處理（時間軸還原+強制拆句）的最終版
- 字幕影片（`_sub.mkv`，如有）
- 投影片術語（`_slide_terms.txt`，如有）

## 完成後回報

執行完畢後，回報：
- 產出檔案路徑（含字幕影片路徑，如有）
- ASR 字幕段數 → 預處理後段數 → 校正後段數
- 術語學習結果：新增了哪些術語/preprocess 規則，或「無新術語」
- 總耗時

## 注意事項

- `subtitle.sh` 是長時間命令，用 Bash 工具執行時設定 timeout 600000ms
- **絕對不要用 Bash 跑 `srt_correct.sh`** — 它內部的 `claude -p` 在 Claude Code session 內會被 CLAUDECODE 環境變數阻擋，導致卡住或失敗。必須用 Agent tool + model: "sonnet" 替代
- Agent subagent 自己讀取 system prompt 和段落檔案，主 agent 不要把 SRT 內容 inline 到 Agent prompt 裡（避免撐爆主 context）
- 術語表 `terms_austin_v2.txt` 是 Austin 專用。未來有其他講者，建立新的 `terms_<講者>.txt`
