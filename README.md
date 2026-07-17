# srt-skill

> One-command video/audio → corrected Traditional Chinese (Taiwan) subtitles, as a [Claude Code](https://claude.com/claude-code) skill.
> 影片／音檔一鍵產出校正後的台灣繁體中文字幕，以 [Claude Code](https://claude.com/claude-code) skill 形式提供。

**[English](#english) · [繁體中文](#繁體中文)**

---

## English

### What it does

Give it a YouTube link or a local video/audio file. It runs a fully-automated pipeline and produces a polished Traditional Chinese (Taiwan) `.srt` — and, for video inputs, a subtitle-muxed `.mkv`.

```
YouTube link OR local video/audio
  ↓ Step 0  (YouTube only)  yt-dlp download
  ↓ Step 0.5 (optional)     slide/frame caption extraction for visual context
  ├─ Step 1                 ASR  (Breeze / Whisper)        ┐ run in parallel
  └─ Step 1'                VibeVoice ASR (optional)        ┘ cross-reference
  ↓ Step 1.5                ASR hallucination detect + auto-fix
  ↓ Step 2a                 mechanical preprocessing
  ↓ Step 2b                 segmented LLM correction (Claude subagents, or local Ollama)
  ↓ Step 2c                 review pass + post-processing
  ↓ Step 2d                 noun verification pass (flag → cross-reference → bounded verify → scoped fix)
  ↓ Step 3                  terminology auto-learning (the glossary grows over time)
  ↓ Step 4  (video only)    ffmpeg mux subtitles into the video
```

Design highlights:
- **Two-ASR cross-reference** — a primary ASR plus an optional VibeVoice pass; the LLM uses both to fix English terms and homophones.
- **Two-layer slide extraction** — a `.pptx` input is read on both layers: the OOXML text *and* the pixels of its embedded images. Chart screenshots routinely carry tickers and indicator names that appear nowhere in the XML, so text-only extraction misses them silently. OCR failure degrades to a warning — term extraction never hard-fails on it.
- **Structural quality gate** — the merge step rejects over-merged segments and auto-retries.
- **Fail-loud ASR** — if the ASR step yields an empty/0-byte SRT (a known `mlx_whisper` `KeyError: 'words'` writer bug that discards output despite a successful transcription), the pipeline reconstructs the SRT from the captured verbose stdout, or hard-fails — it never silently reports success on an empty subtitle.
- **Patch-region disclosure** — spans repaired by hallucination auto-fix or the Whisper fallback (clip-extract, re-transcribe, offset-and-stitch) are the highest timestamp-risk parts of the output, and whole-region drift is invisible to structural validation; the completion report lists each patched span and explicitly asks for a manual playback check. The Whisper fallback also passes `--word-timestamps` by default (word-level alignment re-times segment boundaries, eliminating a measured 2-4 s drift).
- **Strict stage-readiness** — background stages (ASR, VibeVoice, OCR) are launched so the harness tracks the real process, and completion is confirmed by a strict artifact check (`check_stage_artifacts.py`: valid SRT cues / VV segments / caption shape, newer than a launch marker) rather than by file existence or a possibly-lost notification. A silent or missing signal is re-checked, never waited on indefinitely.
- **Adaptive segment splitting** — dual-constraint splitting (estimated tokens + entry count) keeps each subagent's Write under the 32K output ceiling. The 200-entry cap is calibrated from a cross-video failure-rate curve, not guessed.
- **Self-growing glossary** — each run diffs corrections and proposes new terminology rules.
- **Noun verification pass** — correction subagents flag proper nouns that contradict their context (sidecar JSON, hash-bound to the corrected output); a post-merge pass verifies them through four evidence layers (whole-transcript phonetic cross-reference first, local sources, neutral web search with the guess barred from queries, else report-only) and applies fixes with timestamp-scoped replacement — never whole-file substitution. Confirmed mappings feed back into the glossary.
- **Fullwidth punctuation normalization** — a deterministic Step 2c pass converts half-width commas/question/exclamation marks in CJK context to fullwidth (protecting thousands separators, decimals, and times) instead of relying on the LLM to remember.
- **Context-frugal** — segmentation/prompt assembly happen on disk; subagents read their own files so the main agent's context stays small.

### Platform & requirements

> ⚠️ **The ASR backends are MLX-based and run on Apple Silicon (M-series) macOS.** The orchestration and LLM-correction layers are cross-platform Python; only the local ASR/MLX steps are Apple-Silicon-specific. For Windows, see **[docs/WINDOWS.md](docs/WINDOWS.md)**.

| Component | Requirement |
|-----------|-------------|
| Primary ASR (`subtitle.sh`) | `ffmpeg`, `mlx-whisper`, `opencc` (Apple Silicon) |
| LLM correction | Claude Code (Sonnet subagents) **or** local Ollama (`--local`) |
| Slide OCR (Step 0.5, default) | RapidOCR v3 (cross-platform CPU): `pip install "rapidocr>=3.9,<4" onnxruntime`. VLM caption optional via `--engine ollama/mlx` |
| VibeVoice ASR (Step 1', optional) | external repo — see below |
| Download (YouTube) | `yt-dlp` |
| Mux (video) | `ffmpeg` |

### Install

This is a Claude Code skill. Clone into your skills directory:

```bash
git clone <repo-url> ~/.claude/skills/srt
```

Then in Claude Code, invoke it with `/srt <youtube-url-or-file>`.

### Configuration

All user-specific paths default to `$HOME` conventions and can be overridden with environment variables — no code edits needed:

| Env var | Default | Purpose |
|---------|---------|---------|
| `SRT_SKILL_DIR` | `$HOME/.claude/skills/srt` | where the skill (scripts) live |
| `SRT_DATA_DIR` | `$HOME/Documents/For_Claude/scripts/subtitle` | private glossary + media output (kept out of this repo) |
| `SRT_TERMS` | `$SRT_DATA_DIR/srt_correct/terms_austin_v2.txt` | your terminology file |
| `SRT_VV_SCRIPT` | `$HOME/dev/vibevoice-poc/vibevoice_asr.py` | the optional VibeVoice ASR script |

### Bring your own glossary

The terminology file (`SRT_TERMS`) is **user-specific and not shipped in this repo** — the default name (`terms_austin_v2.txt`) is just the original author's example. Point `SRT_TERMS` at a plain-text file of your own correction terms (one per line / domain-specific names, English terms, etc.); the pipeline grows it automatically via Step 3.

### VibeVoice (optional external dependency)

Step 1' uses [VibeVoice](https://github.com/microsoft/VibeVoice) as a **secondary** ASR for cross-referencing. It is **optional** and lives in a separate repo (set `SRT_VV_SCRIPT`). If it is absent or fails, the pipeline continues with the primary ASR only and simply skips the cross-reference. You do **not** need it to use this skill.

### Windows

Native MLX ASR does not run on Windows. The Python orchestration/correction pipeline does. See **[docs/WINDOWS.md](docs/WINDOWS.md)** for how to adapt (WSL2 + an alternative ASR backend). The macOS flow is unchanged.

### License

[MIT](LICENSE).

---

## 繁體中文

### 這是什麼

丟一個 YouTube 連結或本地影片／音檔給它，它會跑一條全自動 pipeline，產出校正後的台灣繁體中文 `.srt`；若輸入是影片，還會輸出內嵌字幕的 `.mkv`。

```
YouTube 連結 或 本地影片／音檔
  ↓ Step 0   (僅 YouTube)   yt-dlp 下載
  ↓ Step 0.5 (選用)         投影片／畫面 caption 擷取，提供視覺語境
  ├─ Step 1                 ASR（Breeze／Whisper）          ┐ 平行
  └─ Step 1'                VibeVoice ASR（選用）           ┘ 交叉參考
  ↓ Step 1.5                ASR 幻覺偵測 + 自動修復
  ↓ Step 2a                 機械性預處理
  ↓ Step 2b                 分段 LLM 校正（Claude subagent，或本地 Ollama）
  ↓ Step 2c                 複查 pass + 後處理
  ↓ Step 2d                 名詞查證 pass（標記 → 全文交叉比對 → 有界查證 → 逐處修正）
  ↓ Step 3                  術語自動學習（術語表會隨使用成長）
  ↓ Step 4   (僅影片)       ffmpeg 把字幕內嵌進影片
```

設計重點：
- **雙路 ASR 交叉參考** — 主 ASR 加上選用的 VibeVoice；LLM 用兩者一起修正英文術語與同音字。
- **投影片雙層抽取** — `.pptx` 輸入會同時讀兩層：OOXML 文字層**與**內嵌圖片的像素層。K 線截圖裡常有 XML 完全沒有的 ticker 與指標名，只抽文字會靜默漏掉。OCR 失敗降級為 warning——不會讓術語抽取整個掛掉。
- **結構性品質 gate** — 合併步驟會擋下過度合併的段落並自動重派。
- **ASR 失敗會出聲** — 若 ASR 步驟產出空／0-byte SRT（mlx_whisper 已知的 `KeyError: 'words'` 寫檔 bug：辨識其實成功卻丟棄輸出），pipeline 會從捕獲的 verbose stdout 重建 SRT，否則直接 hard-fail——絕不對空字幕靜默回報成功。
- **ASR 補丁區揭露** — 幻覺自動修復與 Whisper fallback 補過的區段（截音檔獨立重跑＋偏移縫合）是全片時間軸風險最高處，整體漂移自動驗證抓不到；完成回報會逐段列出起止時間，明確提醒人工播放抽查。Whisper fallback 並預設帶 `--word-timestamps`（word 對齊重定 segment 邊界，實測消除 2-4 秒漂移）。
- **嚴格階段就緒檢查** — 背景階段（ASR、VibeVoice、OCR）以「讓 harness 追蹤真進程」的方式啟動，完成以 strict 產物檢查（`check_stage_artifacts.py`：有效 SRT 時軸／VV segment／caption 形狀、且 newer than launch marker）確認，而非靠檔案存在或可能遺失的通知。沉默或缺席的訊號一律回查，絕不無限等待。
- **自適應切分** — 雙約束（估算 token + 條數）切分讓每段 subagent 的 Write 不撞 32K output 上限。200 條上限由跨影片失敗率曲線校準，非拍腦袋。
- **會自我成長的術語表** — 每次跑完 diff 校正結果，提出新術語規則。
- **名詞查證 pass** — 校正 subagent 把「與上下文矛盾的專有名詞」寫進 sidecar（以 SHA-256 綁定該段校正產物防殘留）；合併後主流程走四層查證（全文音近變體交叉比對優先、本地資源、中性網路搜尋且禁止把猜測放進 query、查不動就只進報告），修正以時間戳定位逐處套用、絕不全檔取代，確認的對應會回寫術語表。
- **全形標點正規化** — Step 2c 後處理確定性把中文語境的半形逗號／問號／驚嘆號轉全形（保護數字千分位、小數點、時間），不依賴 LLM 每次記得用全形。
- **節約 context** — 切分／prompt 組裝都在 disk 上完成，subagent 自己讀檔，主 agent context 維持精簡。

### 平台與需求

> ⚠️ **ASR 後端基於 MLX，跑在 Apple Silicon（M 系列）macOS。** 編排與 LLM 校正層是跨平台 Python，只有本地 ASR／MLX 步驟限 Apple Silicon。Windows 請見 **[docs/WINDOWS.md](docs/WINDOWS.md)**。

| 元件 | 需求 |
|------|------|
| 主 ASR（`subtitle.sh`） | `ffmpeg`、`mlx-whisper`、`opencc`（Apple Silicon） |
| LLM 校正 | Claude Code（Sonnet subagent）**或** 本地 Ollama（`--local`） |
| 投影片 OCR（Step 0.5，預設） | RapidOCR v3（跨平台純 CPU）：`pip install "rapidocr>=3.9,<4" onnxruntime`。VLM caption 選用 `--engine ollama/mlx` |
| VibeVoice ASR（Step 1'，選用） | 外部 repo — 見下 |
| 下載（YouTube） | `yt-dlp` |
| 內嵌字幕（影片） | `ffmpeg` |

### 安裝

這是一個 Claude Code skill。clone 到你的 skills 目錄：

```bash
git clone <repo-url> ~/.claude/skills/srt
```

接著在 Claude Code 用 `/srt <youtube-連結或檔案路徑>` 呼叫。

### 設定

所有與使用者相關的路徑都預設為 `$HOME` 慣例，並可用環境變數覆寫，**不需改 code**：

| 環境變數 | 預設值 | 用途 |
|----------|--------|------|
| `SRT_SKILL_DIR` | `$HOME/.claude/skills/srt` | skill（腳本）位置 |
| `SRT_DATA_DIR` | `$HOME/Documents/For_Claude/scripts/subtitle` | 私人術語表 + media 產物（不進此 repo） |
| `SRT_TERMS` | `$SRT_DATA_DIR/srt_correct/terms_austin_v2.txt` | 你的術語檔 |
| `SRT_VV_SCRIPT` | `$HOME/dev/vibevoice-poc/vibevoice_asr.py` | 選用的 VibeVoice ASR 腳本 |

### 自備術語表

術語檔（`SRT_TERMS`）是**因人而異、不隨此 repo 發佈**的——預設檔名（`terms_austin_v2.txt`）只是原作者的範例。把 `SRT_TERMS` 指到你自己的純文字術語檔（每行一條／領域名詞、英文術語等），pipeline 會透過 Step 3 自動讓它成長。

### VibeVoice（選用的外部依賴）

Step 1' 用 [VibeVoice](https://github.com/microsoft/VibeVoice) 當**輔助** ASR 做交叉參考。它是**選用的**，放在另一個 repo（用 `SRT_VV_SCRIPT` 指定）。若它不存在或執行失敗，pipeline 會只用主 ASR 繼續，單純跳過交叉參考。你**不需要**它也能用這個 skill。

### Windows

原生 MLX ASR 在 Windows 跑不起來，但 Python 編排／校正 pipeline 可以。如何調整（WSL2 + 替代 ASR 後端）請見 **[docs/WINDOWS.md](docs/WINDOWS.md)**。macOS 流程維持不變。

### 授權

[MIT](LICENSE)。
