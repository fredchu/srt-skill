---
name: srt
version: 1.4.2
description: >
  影片/音檔一鍵產出校正後的繁體中文字幕（YouTube 下載 → ASR → 預處理 → LLM 校正 → 後處理）。
  當用戶提到「做字幕」「跑字幕」「產字幕」「字幕 xxx」「srt」「這個影片要上字幕」「上字幕」，
  或給了 YouTube 連結、影片/音檔路徑並暗示需要「帶時間軸的字幕」時使用。
  也適用於用戶要求「更新術語」「學習術語」「術語表」時（--learn 模式）。
  不要用於：要「忠於原話的整理短文/文字稿（非字幕、無時間軸）」（用 speech-to-prose）、
  已有 SRT 只想潤稿（用 subtitle-polisher）、翻譯（用 translator 類 skill）。
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
  ↓ Step 2b: Agent subagent (Sonnet) 逐段校正 + VV 交叉參考
  ↓         srt_merge_segments.py 合併（內建條數/時長 gate，fail 自動重派）→ _2b_corrected.srt
  ↓ Step 2c: 複查 + srt_postprocess.py → _2c_reviewed.srt → _2c_final.srt
  ↓ Step 3: 術語學習
術語表自動成長
  ↓ Step 4 (if 有影片檔): ffmpeg 合併字幕進影片
字幕影片 (_sub.mkv)
```

## 檔案路徑

所有 pipeline 腳本位於：
```
# 路徑可用環境變數覆寫；未設時 fallback 到 $HOME 下的慣例位置。
# 開源使用者：把 SRT_DATA_DIR / SRT_TERMS / SRT_VV_SCRIPT 指到自己的位置即可（見 README）。
SKILL_DIR="${SRT_SKILL_DIR:-$HOME/.claude/skills/srt}"
SUBTITLE_DIR="${SKILL_DIR}/scripts"
CORRECT_DIR="${SUBTITLE_DIR}/srt_correct"
# 私人術語表與媒體產物留在 user workspace（不在 skill repo）
DATA_DIR="${SRT_DATA_DIR:-$HOME/Documents/For_Claude/scripts/subtitle}"
TERMS="${SRT_TERMS:-${DATA_DIR}/srt_correct/terms_austin_v2.txt}"

${SUBTITLE_DIR}/
├── subtitle.sh                          # Step 1: ASR
├── vv_longaudio.py                      # Step 1': VV 長音檔自動切段+合併（含 GPU flock）
├── srt_correct/
│   ├── srt_correct_prompt.txt           # LLM system prompt
│   ├── srt_preprocess.py                # Step 2a: 機械性預處理
│   ├── srt_prepare_segments.py          # Step 2b: 切分 + system prompt 組裝 + VV/caption ref
│   ├── srt_merge_segments.py            # Step 2b: 合併 + 結構性品質 gate + metrics
│   ├── srt_postprocess.py               # Step 2c: 後處理（強制拆句等，--terms 指向 DATA_DIR）
│   ├── srt_strip_commentary.py          # Step 2c: 清掉複查 subagent 殘留的判斷文字
│   ├── srt_learn_terms.py               # Step 3: 術語學習（diff 統計 + 已收錄判斷）
│   └── terms_austin_v2.txt              # 講者術語表實際位於 ${DATA_DIR}/srt_correct/
```

## GPU 資源互斥表（MLX / Apple Silicon）

| 組合 | 可否並行 |
|------|---------|
| Breeze ASR + VibeVoice | ✅ 可（skill 標準平行組合） |
| 兩個 VibeVoice instance | ❌ 不可（vv_longaudio.py 內建 flock 鎖強制序列） |
| 兩部影片同時跑 ASR（2×Breeze+2×VV） | ❌ 不可 — 多影片時 ASR 階段逐部排隊，前一部進入 Step 2 後下一部才開始 ASR |
| RapidOCR（Step 0.5，預設）+ 任一 ASR | ✅ 可（RapidOCR 純 CPU，不搶 MLX GPU） |
| VLM caption（Step 0.5，`--engine ollama/mlx`）+ 任一 ASR | ❌ 不可（VLM 走 GPU，須等 ASR 全部完成） |
| Step 2b/2c subagent（雲端）+ 任何本地 GPU 工作 | ✅ 可 |

## 背景啟動 + 等待契約

長時間背景 job 必須用工具層的 `run_in_background:true` 跑純命令，例如 `cd "${VIDEO_DIR}" && python3 ...`。被啟動的命令內任何地方都不得 inner-backgrounding / daemonize：不得 `nohup`、結尾 `&`、`setsid`、spawn-and-exit wrapper。否則 harness 只會追蹤到啟動器，完成通知會提早假觸發。

每個背景 job 啟動前先 `touch "${VIDEO_DIR}/.<stage>.launch"`（或記錄同等啟動時刻）。正常路徑是收到 harness 對真進程的完成通知後，立刻跑 `scripts/check_stage_artifacts.py --marker <marker> <type>:<path> ...` 做 strict 產物驗證，checker 回 `ready` 才往下。通知只是正常觸發訊號，不是硬性 gate；通知可能漏/丟，且「檔存在」不等於有效。本 repo 已有 mlx_whisper 0-byte SRT fail-loud 前例（README fail-loud、`subtitle.sh strict_srt_count`）。沉默或沒通知不得阻塞：主動查進程/log 並跑 checker 回查；checker 已判 `ready` 時即可據此收尾（必需階段）或記 warning 跳過（選用階段），絕不因等不到通知而無限等。status-checker 也會要求預期產物 newer than marker，避免撿到上一輪舊產物。

必需/選用語義：
- Breeze ASR 是必需階段。產物無效，或進程消失且沒有有效 SRT，必須 hard-fail。
- VibeVoice 是選用參考。進程消失但沒有有效 VV SRT/JSON 時，擷取 log/exit code 記 warning，跳過 VV 交叉參考繼續；絕不無限等。
- OCR/caption 依 slide-ref 是否為本次必要輸入比照處理；選用 caption 失敗時記 warning 並略過 caption ref。

並行邊界按引擎判斷：Breeze + VibeVoice 可並行；RapidOCR（純 CPU）可與 ASR 並行；VLM caption（`--engine ollama/mlx` 或顯式 VLM model）必須序列在 GPU ASR 之後，不能籠統宣稱 Step 0.5 可與 ASR 並行。

`ScheduleWakeup` 是 Pro CC（主 session）專屬，用於 quota-wait 或顯式 resume。Codex / 本地 worker 不能呼叫；背景等待只能留下可續產物或 retry marker，然後用手動/status 回查恢復。

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
- **投影片文字**：用戶提供投影片檔（.txt 純文字，或 .pptx/.ppt PowerPoint）→ 啟用 Step 0.5 抽取本集術語
- **特殊要求**：`--learn`（術語學習）、`--bilingual`（雙語輸出）
- **LLM 模式**：預設 Sonnet subagent（雲端）。用戶提到 `--local` / 「用本地」/ 「離線」→ 用 Ollama gemma4:26b。需要 Ollama 已啟動且 gemma4:26b 已拉取

### quota gate（雲端 LLM 自動前置）

用戶不需要另外說 `agent-orch`。只要走預設 Sonnet subagent 路徑，在 Step 2b/2c 發起 Claude subagent batch 前先執行：

```bash
/Users/fredchu/bin/agent-orch quota check --provider claude --threshold 85 --on-error fail-open --json
```

- exit 0 / `decision=="allow"`：照原 pipeline 發起 subagent。
- exit 2 / `decision=="wait"`：Pro CC（主 session）呼叫 `ScheduleWakeup` 到 JSON 的 `resume_at`，wake prompt 要寫明從 `${VIDEO_DIR}` 既有 `_seg_*`、`_seg_*_corrected.srt`、`_2b_corrected.srt` 等產物 resume；醒來先重跑 quota check，再只補未完成段。Codex / 本地 worker 不呼叫 `ScheduleWakeup`，只留下可續產物或 retry marker。
- `decision=="probe_failed"`：按 `retry_at` 或 `retry_after_seconds` 短延遲重試一次，不把它當成 5h reset。
- `extra_usage.state` 為 `disabled`/`exhausted`：降低雲端 batch concurrency，或改用 `--local` 路徑；不要等不存在的 reset。
- `--local` / Ollama 路徑不需要 Claude quota gate。

### 工作目錄設定（每部影片必做）

所有產出檔案放進 `media/<簡短名稱>/`，不要散落在根目錄。

1. 從影片標題取一個簡短資料夾名：去掉 YouTube ID、去掉副檔名、截斷過長標題
   - 例：`20260311-驚不驚喜 [CkzcQfVr5ow].mkv` → `20260311-驚不驚喜`
   - 例：`投資組合-2月-03 [DZ5LgiWOPZ8].mkv` → `投資組合-2月-03`
   - 例：YouTube 標題 `用Claude Code自动做Skill的万能配方` → `用Claude-Code自动做Skill`
2. 建立目錄：

```bash
VIDEO_DIR="${DATA_DIR}/media/<簡短名稱>"
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

本地檔案的處理：把影片/音檔複製進 `${VIDEO_DIR}`（如果已經在裡面就不用；用 cp 不用 mv，不動用戶原檔）：
```bash
cp "<原始路徑>" "${VIDEO_DIR}/"
```

說明：
- 直接下載到 `${VIDEO_DIR}`，不使用暫存目錄（避免 cwd 被刪除導致背景任務崩潰）
- 檔名格式：`影片標題 [影片ID].mkv`
- 下載完成後，用 `${VIDEO_DIR}` 內的 mkv 檔案路徑繼續 Step 1
- 影片檔在 pipeline 結束後保留，不要刪除

### Step 0.5: 畫面 Caption 擷取（依引擎決定並行）

從影片畫面自動擷取帶時間戳的 caption，供 Step 2b 校正時作為視覺語境參考。

**執行時機**：RapidOCR / Apple Vision 是 CPU 或平台 OCR，可與 Step 1（ASR）和 Step 1'（VV）並行。`--engine ollama` / `--engine mlx` 或顯式 VLM model 會吃 GPU，必須等 Step 1 和 Step 1' 完成後、Step 2a 之前或之後再跑。

**如果用戶提供了投影片檔**，跳過自動擷取，直接用該檔作為全局術語表（舊行為）：
- `.txt`（純文字）→ 直接當術語表用
- `.pptx` / `.ppt`（PowerPoint）→ 用 `srt_extract_slides.py` 直接抽文字（遞迴 group/table/text_frame + 備註，去重），跳過 ffmpeg/dedup/VLM：
  ```bash
  python3 "${SUBTITLE_DIR}/srt_extract_slides.py" "<投影片.pptx>" -o "${VIDEO_DIR}/<檔名>_slide_terms.txt"
  ```

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

腳本內部流程：ffmpeg 每 60 秒截一幀 → imagehash 去重 → OCR/VLM 抽術語 → 輸出。

**引擎（`--engine`，預設 `auto`）**：
- `auto`（推薦）：全平台預設 **RapidOCR v3**（純 CPU、跨 macOS/Windows/Linux 含 VM/Docker；用 default PP-OCRv5 `ch` 模型，繁中+英文混合一起讀——實測勝專用 `chinese_cht` v3 模型）。安裝 `pip install "rapidocr>=3.9,<4" onnxruntime`。RapidOCR 不可用且在 macOS → 退回 Apple Vision（零安裝）。
- `--engine apple-vision`：macOS 原生 OCR（零安裝，僅 macOS）。
- `--engine ollama` / `--engine mlx` 或顯式 `--model`：走 VLM caption（Gemma4:26b Ollama / Qwen3-VL-8B mlx-vlm）—— 會多產散文式畫面描述，但較慢（~10s/幀 vs OCR ~0.5s/幀）。
> Migration（2026-06-28）：`auto` 從「VLM caption 預設」改為「RapidOCR 預設」。實證 OCR 字面文字對字幕校正品質不輸甚至更好且更省資源（見 wiki `SRT Slide OCR Extraction`）。要舊 VLM 行為請顯式 `--engine ollama` 或給 `--model`。

在 Step 2b 組裝 prompt 時：
- `_slide_terms.txt` 加在術語表後面（全局，與舊行為相同）
- `_slide_captions.json` 按時間戳分配給對應的 segment，寫入 `_caption_ref_<N>.txt`

### Step 1': VibeVoice 平行 ASR（與 Step 1 同時跑）

VibeVoice 做輔助 ASR，產出供 Step 2b 交叉參考。與 Step 1 用兩個平行 Bash 呼叫同時執行。

**短音檔（≤ 55 分鐘）— 直接跑：**

```bash
cd "${VIDEO_DIR}" && python3 "${SRT_VV_SCRIPT:-$HOME/dev/vibevoice-poc/vibevoice_asr.py}" \
    "${VIDEO_DIR}/<影片或音檔名>" \
    --terms "${TERMS}" \
    --terms-max 50 \
    --json \
    --output "${VIDEO_DIR}/<檔名>_vibevoice.srt"
```

**長音檔（> 55 分鐘）— 用 `vv_longaudio.py` 自動切段：**

`mlx_audio` 套件硬限制 59 分鐘（`MAX_DURATION_SECONDS = 59 * 60`），超過會自動 trim 截斷。`vv_longaudio.py` 自動完成：ffprobe 時長偵測 → silencedetect 選切點（每段 ≤ 50 分鐘）→ 切 wav → 序列跑 VV（內建 flock GPU 鎖，防兩個 VV 同跑）→ JSON 時間戳偏移合併 → 清理 part 暫存檔：

```bash
cd "${VIDEO_DIR}" && python3 "${SUBTITLE_DIR}/vv_longaudio.py" \
    "${VIDEO_DIR}/<影片或音檔名>" \
    --terms "${TERMS}" --terms-max 50 \
    --output-json "${VIDEO_DIR}/<檔名>_vibevoice.json" \
    --output-srt "${VIDEO_DIR}/<檔名>_vibevoice.srt"
```

先加 `--dry-run` 可只看切段計畫（JSON 印出 parts 與切點）不執行推理。

產出：
- `<檔名>_vibevoice.srt` — VV 的 SRT（備用）
- `<檔名>_vibevoice.json` — VV 的 segments JSON（Step 2b 用），欄位可能是 `Start`/`End`/`Content` 或小寫 `start`/`end`/`text`（兩種都要支援，下游腳本已處理）

注意：
- 如果 VV 執行失敗（模型未安裝等），pipeline 繼續跑，Step 2b 跳過 VV 參考
- 用 `--breeze` 時才啟用 Step 1'（Whisper 模式不用 VV，因為 VV 底層也是 Whisper 架構）
- `mlx_audio` 套件硬限制 59 分鐘（`MAX_DURATION_SECONDS = 59 * 60`），超過會靜默 trim — 必須在 pipeline 端切段，不能依賴 VV 自己處理
- `vibevoice_asr.py` 的 `max_tokens` 已改為 32768（原 8192 對 > 30 分鐘音檔不夠，會導致 0 segments）
- `generate()` 其他可調參數：`repetition_penalty`（預設 1.2）、`prefill_step_size`（預設 2048，長音檔可提高以降低記憶體峰值）
- **不要同時跑兩個 VV instance** — 會搶 Apple Silicon GPU 記憶體互相 thrash，必須序列執行

### Step 1: ASR 語音辨識

```bash
cd ${SUBTITLE_DIR}
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
CORRECT_DIR="${SRT_SKILL_DIR:-$HOME/.claude/skills/srt}/scripts/srt_correct"
TERMS="${SRT_TERMS:-$HOME/Documents/For_Claude/scripts/subtitle/srt_correct/terms_austin_v2.txt}"
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
CORRECT_DIR="${SRT_SKILL_DIR:-$HOME/.claude/skills/srt}/scripts/srt_correct"
python3 "${CORRECT_DIR}/srt_preprocess.py" "<ASR 產出的 SRT>" "<輸出路徑>_2a_preprocessed.srt" --stats --breeze
```

（如果是 Whisper ASR，不加 `--breeze`）

#### Step 2b: 分段 + Agent 校正

> **Context 節約原則**：主 agent 不讀 SRT 內容到自己的 context。切分、prompt 組裝全部在 disk 上用腳本完成，subagent 自己讀檔案。

1. **跑 `srt_prepare_segments.py` 完成切分 + prompt 組裝**（不要用 Read 工具讀 SRT 檔案）：

   ```bash
   python3 "${CORRECT_DIR}/srt_prepare_segments.py" "<preprocessed SRT 路徑>" \
       --workdir "${VIDEO_DIR}" \
       --prompt-template "${CORRECT_DIR}/srt_correct_prompt.txt" \
       --terms "${TERMS}" \
       --captions-json "<caption JSON 路徑，沒有就省略此參數>"
   ```

   stdout 會印 JSON summary：`{"strategy": "dynamic", "tokenizer": "tiktoken", "total_blocks": N, "segments": [...], "segment_tokens": [...], "vv_segments": N, "captions": N}`。

   > **切分策略：動態 token 預估（預設，2026-06-26 上線）**。不帶 `--seg-size` 時走**雙約束**動態切分：每塊估算 output token，當「累積 token > `--max-tokens`（預設 8000）」**或**「條數 > `--max-entries`（預設 200）」任一觸發就切。預設值不用帶。
   >
   > **為什麼要切分**：校正 subagent 逐條保留輸出整段 SRT，段太大時 Write 會報 `response exceeded the 32000 output token maximum`，該段靜默失敗（合併 gate 會 fail 並自動重派，但已浪費一輪）。
   >
   > **失敗率曲線（2026-06-27 實測，跨 6 部影片真實 Sonnet subagent）**：
   > | 每段條數 | 32K 失敗率 | 樣本 |
   > |---|---|---|
   > | 150 | 14% | 1/7 |
   > | 200 | **18%** | 2/11 |
   > | 250 | 44% | 4/9 |
   > | 275 | 80% | 4/5 |
   > | 300 | 71% | 10/14 |
   >
   > **拐點在 200→250 之間**：200 是平原末端，250 起內容敏感性引爆（難影片——美股展望/英文密——在 250 條系統性全爆；200 條時連難影片都還 PASS，18% 是隨機底噪）。所以 `--max-entries 200` 是**「內容敏感性引爆前的最大安全 cap」**，有曲線佐證，非拍腦袋。失敗對內容高度敏感，cap 須按 worst-case 難影片定。18% 殘餘失敗由合併 gate 自動重派兜底。
   >
   > **為什麼是雙約束而非單一 token 閾值**：用 cl100k_base 實測，校正後 SRT 文字只有 **~35 token/條**，300 條 raw 才 ~10.5K，遠不到 32K。真正撞上限的是**看不到的** Sonnet reasoning/thinking token + Write JSON 序列化開銷（cl100k 量不到，且 raw token 完全不能預測失敗——實測 PASS 樣本 token 範圍涵蓋並高於 FAIL）。所以**不能**只用 raw token 閾值切（文字短時 12K token 會對應到 ~340 條，比已崩的 300 還大）。`--max-entries 200` 是 thinking-token 的代理硬上限，`--max-tokens 8000` 負責密集段提前切。實測 718 條 → **4 段 [200,200,200,118]**（比固定 150 的 5 段少 1 段，省一次 ~10.5K 的 system prompt 重發）。
   >
   > **逃生艙**：顯式帶 `--seg-size N` 會切回舊的固定條數模式（向後相容）。heuristic fallback（tiktoken 不可用時用 `len(text)`）只對「含時間軸的完整 SRT block」保守安全，非通用中文 tokenizer，勿挪作他用。
   >
   > 教訓與實證來源：2026-06-26-27 投資組合-5月-03（codex 實證 review + 多輪對抗改善循環 + 跨 6 影片失敗率曲線實測收斂）。

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
   8. 校正過程中若發現「專有名詞（公司/ticker/人名/術語）與上下文明顯矛盾、但你不確定正解」：
      字幕照 ASR 原樣輸出（不要瞎改），另外把可疑名詞寫進 sidecar：
      - 先用 Bash 對剛寫完的檔案取 hash：`shasum -a 256 <工作目錄>/_seg_<N>_corrected.srt`
      - 再用 Write 寫 <工作目錄>/_seg_<N>_uncertain.json，格式（單一 JSON 物件）：
        {"seg": <N>, "overflow": false, "corrected_sha256": "<上面的 hash>",
         "items": [{"term": "<原詞>", "ts": "<該條字幕起始時間 HH:MM:SS>",
                    "context": "<該條字幕文字>", "guess": "<你的猜測，可省略>"}]}
      - items 上限 10 條，超過取矛盾最明顯前 10 並把 overflow 設 true
      - 沒有可疑名詞就**不要**寫這個檔案

   重要：
   - **保持逐條對應：輸出條數必須 ≥ 原始條數的 90%。禁止把多條字幕合併成一條長字幕**。只有「整條只有單一語氣詞」（如「然後」「所以」「這個」）的條目才可刪除或併入相鄰條目
   - 上文參考僅供理解語境，不要輸出這些內容
   - VibeVoice 參考僅供交叉比對，不要直接複製其語氣詞
   - 畫面描述中的英文術語是 ground truth，優先信任
   - 輸出必須是完整的 SRT 格式（含序號、時間軸、字幕文字）；**sidecar 內容絕不寫進 corrected.srt**
   - 完成後回報修改了多少條
   ```

   **重要**：所有 Agent 呼叫必須在**同一個訊息**中發出，才能真正平行執行。

   **驗證**：subagent 完成後不需逐檔人工抽查 — 第 3 步的合併腳本內建結構性品質 gate（條數比例 + 超長時長），會自動擋下過度合併。檔案不存在或無時間軸格式時 gate 也會以 ratio=0 觸發。

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

3. **跑 `srt_merge_segments.py` 合併（內建結構性品質 gate）**：

   ```bash
   python3 "${CORRECT_DIR}/srt_merge_segments.py" \
       --workdir "${VIDEO_DIR}" \
       --preprocessed "<preprocessed SRT 路徑>" \
       --output "<最終合併路徑>_2b_corrected.srt"
   ```

   腳本行為：
   - **品質 gate**（合併前逐段檢查）：`ratio = 校正後條數 / 原始條數`
     - `ratio < 0.55` → FAIL（過度合併，如 300→110 事故）
     - `ratio < 0.80` 且該段有條目時長 > 15 秒 → FAIL（合併症狀）
     - `0.55 ≤ ratio < 0.80` 且無時長症狀 → 合併照常，記入 `warn_segments`（破碎句密集區的合法合併）
   - gate 全過 → 嚴格 block 驗證、雙行重複修復、時間排序、end-time clamp、coverage check（>15s gap 用 preprocessed 補洞）、重新編號
   - 成功：stdout 印 JSON metrics（entries / patched / per_segment ratio / max_dur_sec / over_12s_count），exit 0
   - gate FAIL：不寫輸出，stdout 印 `{"gate": "fail", "failed_segments": [...]}`，**exit 2**

   **exit 2 時的處理**：對每個 failed segment 重派校正 subagent（同一段、同樣的 prompt），並在 prompt「重要」清單最前面追加一行：「**上一輪輸出只有 <output> 條（原始 <input> 條），嚴重過度合併。這次必須逐條校正，輸出條數 ≥ <input×0.9> 條**」。**重派前先刪該段舊 sidecar：`rm -f <工作目錄>/_seg_<N>_uncertain.json`**（防上一輪殘留的可疑名詞清單被 Step 2d 誤採；hash 綁定是第二道保險）。重派完成後重跑合併腳本。

#### Step 2c: 複查 pass + 後處理

> **同樣遵守 Context 節約原則**：提取未變動條目、組裝複查 prompt 全部用 Python 腳本在 disk 上完成。

1. **用 Python 腳本提取未變動條目 + 組裝複查 prompt**：

   ```python
   python3 -c "
   import re, os, difflib

   WORK_DIR = '<工作目錄>'
   CORRECT_DIR = '${CORRECT_DIR}'
   TERMS = '${TERMS}'
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
   terms = open(TERMS).read()
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
           lines = [l for l in b.strip().split('\n') if l.strip()]
           # 容忍前置序號行：subagent 偶爾在時間軸前多寫一行序號（如 \"833\"）。
           # 找含 '-->' 的那行當時間軸，不要假設它是 lines[0]，否則該修正會被靜默漏套。
           tc_idx = next((i for i, l in enumerate(lines) if '-->' in l), None)
           if tc_idx is None:
               continue
           tc = lines[tc_idx].strip()
           text = '\n'.join(lines[tc_idx + 1:]).strip()
           if text:
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
       python3 "${CORRECT_DIR}/srt_postprocess.py" "<_2c_reviewed.srt>" "<最終輸出路徑>_2c_final.srt" --stats --terms "${TERMS}"
   else
       python3 "${CORRECT_DIR}/srt_postprocess.py" "<_2c_reviewed.srt>" "<最終輸出路徑>_2c_final.srt" --stats --ref "<preprocessed SRT 路徑>" --terms "${TERMS}"
   fi && \
   # 第二層保險：postprocess 後再掃一次（含 Type B/C 啟發法，處理 split 殘留）
   python3 "${CORRECT_DIR}/srt_strip_commentary.py" "<最終輸出路徑>_2c_final.srt"
   ```

### Step 2d: 名詞查證 pass（主 session；sidecar 全缺時自動 no-op）

校正 subagent 只標記不查證（它們刻意離線）。本步在 `_2c_final.srt` 產出後、Step 4 壓字幕**之前**執行——聚合 sidecar、查證、修 final srt：

1. **聚合＋新鮮度檢查（deterministic）**：收集 `_seg_*_uncertain.json`；逐檔重算對應 `_seg_N_corrected.srt` 的 SHA-256 與 envelope 的 `corrected_sha256` 比對——**corrected 檔不存在、缺 hash 欄位、或 hash 不符 → 丟棄該 sidecar 並警示**（stale 殘留）。通過者以 normalized term 去重聚合。零 sidecar → 本步結束（no-op）。
2. **四層查證**（同 speech-to-prose Step 3.5，證據不足寧可不改）：
   - **L0 全文內部交叉比對（最優先）**：`python3 "$SP_DIR/scripts/noun_xref.py" --term "<詞>" ... <_2c_final.srt> <ASR 原始 srt> <_vibevoice.srt> --json`（`SP_DIR=~/.claude/skills/speech-to-prose`），LLM 判讀候選段落語境是否指向同一實體（實例：KISS 在他處被聽對成 keys）
   - **L1 本地**：講者 terms 檔、`_slide_terms.txt`（畫面 OCR 是 ground truth）、wiki
   - **L2 中性 WebSearch**：query 只准用上下文關鍵詞、**禁止放入猜測答案**（防確認偏誤）；得候選清單後才驗音近
   - **L3 未收斂**：字幕原樣不改、記入報告（**與 speech-to-prose 的〔註〕標記刻意不同**：字幕是螢幕顯示格式、校正契約嚴禁 commentary 入字幕，未收斂項的候選與理由一律進報告讓用戶決定）
3. **套用門檻**（其一，且候選必須不靠 guess 發現）：L0 內部證據／L1 權威對應（全同音）／L2 雙重收斂（換 query 措辭結果不變）。**不可驗證類**（會員 ID、暱稱、私人人名）永不查。
4. **定位替換**（cue 編號經 merge 已重編，**不得**用 cue 定位）：對每個 mapping，在 final srt 找「起始時間 ∈ sidecar `ts` ± 2s 且文字含 `term`」的條目——命中恰 1 條才改；0 或多條 → 列報告人工確認。L0 發現的額外變體出現處逐處確認語境後才改。**絕不全檔字串替換**。
5. **量上限**：聚合後 ≤30 個獨特名詞進查證（超額按出現段數×重要性取前 30）；WebSearch 短片（≤55min）≤10 次、長片 ≤20 次；溢出必回報統計。
6. **回寫 terms 檔**：確認 mapping 寫入講者 terms 檔——provenance 獨立註解行在前、mapping 行純 `wrong→correct`（行內 `#` 會被 parser 吃進 term，禁止）；寫前 grep 防重複。
7. **報告**：逐 mapping 一行 `原詞 → 新詞 @ HH:MM:SS（證據層級）`＋考慮過的候選與淘汰理由＋溢出統計。

### Step 3: 術語自動學習（每次必跑）

每次 pipeline 完成後自動執行，不需要用戶觸發。用 `srt_learn_terms.py`（可一次帶多部影片的 pairs）：

```bash
python3 "${CORRECT_DIR}/srt_learn_terms.py" \
    --pairs "<2a_preprocessed.srt>:<2c_final.srt>" \
    [--pairs "<另一部 2a>:<另一部 2c_final>" ...] \
    --terms "${TERMS}" \
    --preprocess "${CORRECT_DIR}/srt_preprocess.py" \
    --min-count 2
```

腳本完成：difflib 比對 replace 操作、噪音過濾（標點/大小寫/人稱代詞/超長片段）、出現次數統計、
已收錄判斷（詞級解析 terms 與 preprocess AST，非子字串粗查）、分類建議（preprocess vs terms），
輸出 markdown 候選表（含 already 標記）。

主 agent 接手：
1. 從候選表挑出值得收錄的項目（already 標記非 `-` 的跳過；單一英文字母對、過於語境特定的修正不收）
2. **向用戶展示篩選後清單**（含次數和範例），請用戶確認後寫入對應檔案
3. 沒有候選 → 告知用戶「本次無新術語」即可

### Step 4 (有影片檔時): 合併字幕進影片

只有輸入是影片檔（YouTube 下載的 mkv 或本地影片）時才執行。音檔輸入跳過此步。

```bash
ffmpeg -y -i "${VIDEO_DIR}/<影片檔名>" -i "${VIDEO_DIR}/<_2c_final.srt>" \
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
find "${VIDEO_DIR}" -maxdepth 1 \( \
    -name "_seg_*.srt" -o -name "_seg_*_uncertain.json" -o -name "_ctx_*.txt" \
    -o -name "_vv_ref_*.txt" -o -name "_caption_ref_*.txt" \
    -o -name "_review_seg_*" -o -name "_system_prompt.txt" -o -name "_review_prompt.txt" \
    -o -name "*_vvpart*" \
    -o -name "*_vv_part*" -o -name "*_part*_vibevoice.json" \
    -o -name "*_part[0-9].wav" -o -name "*_benchmark.txt" \) -delete
# subtitle.sh 產生的全長 wav（如有）
rm -f "${VIDEO_DIR}/<影片檔名同名>.wav"
```

（用 find 而非裸 glob：zsh 預設 NOMATCH，任一 pattern 沒匹配整批會 abort。
`*_vv_part*` 與 `*_part*_vibevoice.json` 是舊版手動切段流程的檔名慣例 — 刻意保留以清理舊 run 殘檔，非失效引用。）

清理完 `${VIDEO_DIR}` 後，也要檢查 pipeline 過程中是否在其他位置留下暫存檔（如 /tmp 下的 WAV 等），有的話一併刪除。

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
- 名詞查證摘要（Step 2d：查證 N／修正 M／未收斂 L3 數／溢出 O／丟棄 stale sidecar 數；有修正時附逐條 mapping＋證據層級、未收斂項附候選與理由；零 sidecar 則「無可疑名詞」）
- 術語學習結果：新增了哪些術語/preprocess 規則，或「無新術語」
- 總耗時

## 注意事項

- `subtitle.sh` 是前景長時間命令，用 Bash 工具執行時可設定 timeout 600000ms；這只適用前景 `subtitle.sh`，不得套到背景等待器。背景啟動 bad：把 `nohup python3 ... &` 塞進 background Bash。背景啟動 good：`cd "${VIDEO_DIR}" && python3 ...` 搭配工具層 `run_in_background:true`，且命令內無 inner backgrounding。
- **絕對不要用 Bash 跑 `srt_correct.sh`** — 它內部的 `claude -p` 在 Claude Code session 內會被 CLAUDECODE 環境變數阻擋，導致卡住或失敗。必須用 Agent tool + model: "sonnet" 替代
- Agent subagent 自己讀取 system prompt 和段落檔案，主 agent 不要把 SRT 內容 inline 到 Agent prompt 裡（避免撐爆主 context）
- **切分預設走動態 token 預估（雙約束 `--max-tokens 8000` + `--max-entries 200`），不要帶 `--seg-size`**：段太大時 Sonnet subagent 的 Write 會撞 32K output token 上限失敗。真正歸因不是 SRT 文字量（實測 ~35 token/條），而是看不到的 thinking + Write 序列化開銷——所以用「條數硬上限 200」當代理保護，不能只憑 raw token 放大段長。跨 6 影片失敗率曲線實測：150→14%、**200→18%**、250→44%、275→80%、300→71%，拐點在 200→250 間，200 是引爆前的最大安全 cap（殘餘 18% 由合併 gate 自動重派兜底）。`--seg-size N` 仍可切回舊固定模式（逃生艙）。詳見 Step 2b 切分指令下的說明
- 術語表 `terms_austin_v2.txt` 是 Austin 專用。未來有其他講者，建立新的 `terms_<講者>.txt`
