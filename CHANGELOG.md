# Changelog

## 1.5.1 - 2026-07-17

### 修復
- **`hallucination_fallback.sh` bash 引號 KeyError**。patch 段 Python 包在 `python3 -c "..."` 雙引號裡，f-string 的 `e["text"]` 內層引號被 bash 剝掉，Python 實收 `e[text]`——`text` 恰好殘留迴圈變數（最後一條字幕文字），炸出以字幕內容為 key 的詭異 `KeyError`。錯誤訊息指向資料、defect 在 shell quoting。修法：先取 `txt = e['text']` 再進 f-string。
- **Whisper fallback 補丁區時間戳漂移 2-4 秒**。`mlx_whisper` 不帶 `--word-timestamps` 時 segment 級時間戳在補丁窗口內整體提早（實測 34 秒窗口漂 2.5-4s），單調性／重疊／條數等自動驗證全過，只有人工看片抓得到（字幕提早出現）。改為預設帶 `--word-timestamps True`（word 對齊重定 segment 邊界），並與 VibeVoice 獨立時間軸對拍驗證。

### 變更
- **SKILL.md 立「ASR 補丁區人工抽查」制度**：Step 1.5 的 Breeze 自動修復區與 Whisper fallback 區（同為截音檔獨立重跑＋偏移縫合）必記起止時間，完成後回報逐段列出、明確提醒用戶播放確認——補丁區是全片時間軸風險最高處。
- preprocess 新增 10 條 Breeze 同音字規則（FOMC、權值股、事件交易、關鍵價、週假摔、逃頂、泡沫掉等，出自技術分析-6月-03/04 術語學習）。
- README（英／繁）新增 Patch-region disclosure 設計說明。

## 1.5.0 - 2026-07-16

### 新增
- **`.pptx` 抽術語補上圖片 OCR**。事故：pptx 路徑只讀 OOXML 文字層，**圖片像素層一個字都沒抽**。投影片的資訊有兩層（XML 文字 + 圖片像素），2026-04-12 補了 `has_table` 之後就默認「抽全了」——其實只抽全了 XML 側。實測 `67月.pptx`（21 頁、8 張 K 線／看盤截圖）：PLTR、MA300DIST、CME_MINI、NASDAQ、NQ、EURUSD **全部只存在於圖片裡**，純文字抽取 100% 漏掉，而這些正是 ASR 最容易聽錯、最需要 ground truth 的詞。
- `extract_pptx_text()` 走訪 shapes 時把圖片 `shape.image.blob` 寫進暫存檔，丟給腳本**已有的** `ocr_with_rapidocr()`（影片路徑在用的同一支），回傳改為 `(xml_lines, ocr_lines, image_count)`；輸出新增 `# 螢幕 OCR 文字（原始）` 區塊，OCR 行與 XML 行共用去重。實測 8 圖 152 行、約 1 秒/圖，XML 側 97 行**逐字不變**。
- 三個實作要點：圖片判斷用 `getattr(sh, "image", None)` 而非硬比 `shape_type == 13`（group 內的圖同樣要吃到）；**OCR 失敗只 warn 不 hard-fail**（pptx 路徑原本不依賴 rapidocr，加了 OCR 不得讓沒裝的環境連術語都抽不出來）；`.pptx` 分支是獨立寫檔，不走影片路徑的 `write_terms_file()`。
- 對拍結論：這不是 python-pptx 的缺陷，**換 OfficeCLI 也一樣**（v1.0.136 實測純文字抽取 269/271 token 交集，等價）——兩者都只讀 OOXML，都看不到像素。**缺的是 OCR 那一段，不是 pptx 解析庫。**

> 走 `/dispatch`（classifier → packet → codex worker → 主 session 自跑 VERIFICATION → review 清掉 function-attribute 側通道）。派工前驗前提抓到兩個會誤導 worker 的錨：skill 是 symlink → 真 repo 在 `~/dev/srt-skill`（WRITE SCOPE 得用該 repo 的 repo-relative 路徑）；教訓檔寫的「併進 `# 螢幕 OCR 文字（原始）` 區塊」其實是影片路徑的區塊，pptx 分支上並不存在。

## 1.4.2 - 2026-07-08

### 修復
- **背景長任務「等待踩空」防呆**。事故：跑 2h13m 影片時 VibeVoice 早已跑完，但等待它的背景輪詢等待器 Bash timeout 設太短（10 分，誤抄前景 subtitle.sh 慣例）被靜默殺、輸出空白、完成通知永不來；主 session 把「沒通知」當「還在跑」空等約 6 小時。更深根因：啟動 Breeze/VV/OCR 時把 `nohup <cmd> &` 塞進工具層 `run_in_background`（double-background），`&` 把真 job detach，harness 只追蹤到啟動器 → 完成通知提早假觸發，才逼出脆弱等待器。
- **SKILL.md 新增「背景啟動 + 等待契約」**：長 job 用 `run_in_background` 跑純命令、命令內任何地方不得 inner-backgrounding/daemonize（`nohup`／結尾 `&`／`setsid`／spawn-and-exit）；完成以「harness 通知（正常觸發、非硬 gate）＋ strict 產物檢查」確認，**沉默/沒通知一律主動回查、絕不無限等**；必需/選用語義（Breeze hard-fail、VibeVoice warn-and-skip）、引擎並行邊界（Breeze+VV+RapidOCR 可並行、VLM caption 必序列在 GPU ASR 後）、`ScheduleWakeup` 為 Pro CC 主 session 專屬（Codex/本地 worker 不呼叫）。修掉 line 730「600000ms」誤導錨（只適用前景 subtitle.sh，附 bad/good 範例）。
- **新增 `scripts/check_stage_artifacts.py`**（+ 11 case 測試）：單次、冪等、JSON 輸出的 strict 階段就緒檢查器。逐 cue 結構化驗證 SRT（完整時軸 + MM/SS<60 + ms<1000 + end>start + ≥1 非空文字）、VV JSON（≥1 usable segment、容忍 metadata 列但擋畸形 speech 列）、caption JSON 形狀，且要求產物 newer than launch marker。把「檔存在≠有效」碼化——**v1.4.1 那個 0-byte SRT 會被它判 invalid 而非 ready**。

> 修法走 `/dispatch` loop mode（design → codex review 1 blocker+6 major → worker → 真實 e2e 含 0-byte → codex verify 1 major+3 minor → worker polish），每步主 session 獨立重跑驗證；配合 memory `feedback_background_wait_watchdog_not_silence` 通則（跨 skill 情境）。

## 1.4.1 - 2026-07-08

### 修復
- **消滅 ASR 靜默 0-byte SRT 假完成**。`mlx_whisper` 在 `word_timestamps=False` 下，SRT writer 會存取不存在的 `segment['words']` 丟 `KeyError: 'words'` 並略過寫檔——辨識其實完整成功、但 SRT 從沒落地（0 條），`subtitle.sh` 卻仍 `exit 0` 印「✅ 全部完成」。現在 `subtitle.sh` 用 `mktemp + tee` 捕獲 mlx_whisper 的 verbose stdout（`set +e`／`PIPESTATUS`，不靠其 exit code），ASR 後加嚴格 SRT 時間軸 gate（`strict_srt_count`，避免把雜檔誤收成有效輸出）；SRT 缺失或 0 條時從 stdout 重建，重建仍 0 條 → `exit 1`（不再假完成）。fallback 的檔名探索也收緊為 `${BASENAME}*.srt` 且限定 marker 之後。
- 新增 `scripts/reconstruct_srt_from_log.py`：從 mlx_whisper verbose stdout 重建 SRT。strict「每個時間戳行＝一個完整 cue」解析——對**交錯出現在串流中段的 traceback**（實測 mlx 的 KeyError 交錯在中段、辨識仍續到結尾）與尾隨的 `KeyError:`／`Skipping` 錯誤噴發行天然免疫，不截斷後續 cue、不讓錯誤行滲入字幕。支援 `MM:SS.mmm` 與 `HH:MM:SS.mmm` 兩式、`.`／`,` 毫秒分隔、UTF-8 BOM／CRLF；`.`→`,` 只作用於時間欄（`3.5%`、URL 等文字逐字保留）；整行音樂符號（`♪`）與空段丟棄，僅在 `Traceback (most recent call last):`（marker 緊接冒號）才剝尾以免誤截講者字面用詞。
- 新增 `scripts/tests/test_reconstruct_srt_from_log.py`：鎖死上述迴歸的 regression 測試（交錯 traceback 不截斷／錯誤行不滲入、字面 marker 保留、雙時間格式、comma 毫秒、BOM+CRLF、return code 契約）。經 codex mutation 驗證確能擋回退。

> 修法走 `/dispatch` loop mode：設計 → codex review → codex worker → 真實 log e2e 抓到「break-on-traceback 丟 554 條」迴歸 → 退回 worker 修＋補測試 → codex 對抗式 verify（mutation 證測試有效）→ codex worker 收 minor；每步主 session 獨立重跑驗證。實戰資料：財經M平方 2h13m 直播 → 重建 2384 條乾淨、0 錯誤滲入。

## 1.4.0 - 2026-07-06

### 新功能
- **Step 2d 名詞查證 pass**。校正 subagent 在校正時把「與上下文矛盾的專有名詞」（公司名／ticker／人名／術語）另寫獨立 sidecar（`_seg_N_uncertain.json`，單一 JSON envelope；以 `corrected_sha256` 綁定該段校正產物，重跑殘留的舊 sidecar 會被 hash 比對擋下）——**corrected srt 的 SRT-only 輸出契約完全不動**。合併完成後主流程走四層查證：L0 全文音近變體交叉比對優先（同一實體通常被提到多次、每次錯法不同，重用 speech-to-prose 的 `noun_xref.py`）、L1 本地資源（講者術語表／投影片 OCR）、L2 中性網路搜尋（**禁止把猜測放進 query**——帶假設搜尋只會自我證實）、L3 查不動的只進報告不改字幕。修正以「時間戳 ±2s ＋原詞比對」逐處定位（cue 編號經 merge 重編、不可作定位依據），0 或多重命中一律不自動改；確認的對應以獨立註解行＋純 `wrong→correct` 格式回寫術語表（行內註解會被 parser 吃進 term）。
- gate-fail 重派段落前先刪該段舊 sidecar；Step 5 清理清單納入 `_seg_*_uncertain.json`；完成回報新增名詞查證摘要（查證／修正／未收斂／溢出／stale 丟棄計數）。

### 文檔
- README（英/中）：pipeline 圖加 Step 2d、design highlights 加名詞查證段；補上 1.3.0 全形標點正規化的英文版說明（先前僅中文版有）。
- 實戰驗證：7 小時財經直播全量審計——138 個獨特可疑名詞、214+ 處修正（KISS→KEYS、one room→萬潤、asyna→Synaptics 等）；設計經 5 輪對抗式 review + 實作 2 輪 verify（codex）收斂。

## 1.3.0 - 2026-07-03

### 新功能
- **Step 2c 後處理新增全形標點正規化**（`normalize_fullwidth_punct`）。ASR 無標點、標點由 LLM 校正時加上，LLM 在英文 token 後（如 `AWS, Google`、`YOY, RPO`）常留半形逗號；pipeline 先前無任何 half→full 正規化步驟，屬潛在缺口（3Q2026 美股展望 7hr 英文密集片一次出現 849 個半形逗號才浮現）。新增確定性收尾，不依賴 LLM 記得用全形：半形逗號→全形（並吃掉英文慣例尾隨空格），保護數字千分位（`3,000`）；`?`/`!` 僅在緊鄰 CJK 時轉全形（避免誤傷純英文/URL）；句號、冒號、分號保守不動（小數點、`U.S.`、時間 `10:00` 誤傷風險高）。在 force-split 前執行，讓拆句依據的 `，` 也一致。
- Step 2b 校正 system prompt「標點」段補明確規則：**所有中文標點一律用全形**，即使緊接英文詞後（`AWS，Google` 非 `AWS, Google`）——作為 LLM 端第一道防線，程式正規化為確定性保底。

### 測試
- 新增 `tests/test_fullwidth_norm.py`（10 case：英文後逗號、千分位保護、小數點/時間不動、CJK 問號驚嘆號、純英不誤傷、冪等），全套 101 passed。

## 1.2.0 - 2026-07-03

### 新功能
- Step 3 術語學習新增 12 條 Austin 財經同音字規則（教育日→交易日、建商/健常/減長→建倉、長途→長投、日先→日線、波頓→波段、曲線盤整→區間盤整、識字管理→市值管理、均值回饋→均值回歸、健康週期→建倉週期、週三白→週三百），由 投資組合-6月-01/02 兩支影片術語學習產出。

### 修正
- Step 2c commentary strip 新增 tool-call / XML tag 洩漏清理。校正 subagent 偶爾把工具呼叫閉合 tag（`</content></invoke>` 等）寫進校正輸出的字幕尾行，下游 force-split 又把它拆成整行 tag、行首碎片（`</` + `content>`）、行尾殘缺開頭（`<invoke name="x"`），導致成品字幕出現裸 tag。改用已知 tool-tag 名稱 allowlist（`invoke`/`parameter`/`content`/`function_calls`/`tool_use`/`tool_result`，含 `antml:` 前綴）+ 逐行剝除（非整行刪，保住 inline 接在中文後的真字幕）；allowlist 避免誤刪 `AAPL>` / `<BRK.B>` / `<ETF>` 等合法英文行。經兩輪 codex reviewer 對抗審查收斂，新增 `tests/test_strip_commentary.py`（38 case，全套 91 passed）。

## 1.1.0 - 2026-06-28

### 新功能
- Step 0.5 新增跨平台 OCR 引擎，取代原本只有 VLM caption 的做法。預設 `auto` 在所有平台走 **RapidOCR v3**（純 CPU，跨 macOS/Windows/Linux，含 VM/Docker），macOS 另可選原生 Apple Vision OCR。實證 OCR 字面文字對字幕校正品質不輸甚至優於 VLM caption，且速度約快 20×（OCR ~0.5s/幀 vs VLM ~10s/幀）。三平台（macOS ARM、Linux aarch64 Docker、Windows ARM64）同一畫面輸出一致。

### 變更
- Step 0.5 `auto` 預設 OCR 改為跨平台 RapidOCR v3。安裝：`pip install "rapidocr>=3.9,<4" onnxruntime`（RapidOCR 不會自動帶 ONNX Runtime backend）。Apple Vision 降為 macOS 可選引擎與 RapidOCR 不可用時的 macOS 保底。要回到舊 VLM caption 行為，顯式 `--engine ollama`（或 `mlx`）或給 `--model`。
- `--engine` 現支援 `{auto,rapidocr,apple-vision,ollama,mlx}`；顯式 `--model` 仍代表 VLM 意圖，`auto` 下含 `/` 走 mlx，否則走 ollama。
- Linux/Docker 部署需補 opencv 系統庫：`apt install libgl1 libglib2.0-0`。

### 修正
- 顯式 `--engine mlx/ollama --model ...` 現在原樣傳遞 model，不再用 slash heuristic 丟棄本機或 Windows 路徑。
- `ffmpeg` 缺失時改為輸出各平台安裝指引，避免裸 `FileNotFoundError`。

## 1.0.0 - 2026-06-27

首個正式 release。一鍵字幕 pipeline 已開源就緒（雙語 README + LICENSE + Windows 指南）。

### 新功能
- **自適應切分（雙約束動態）**：Step 2b 切分改為「估算 token > max-tokens(8000) 或 條數 > max-entries(200) 任一觸發即切」，取代固定 300/段。解決校正 subagent 逐條輸出整段 SRT 時撞 32K output token 上限的靜默失敗。200 條上限由跨 6 影片失敗率曲線校準（150→14% 200→18% 250→44% 275→80% 300→71%，拐點 200→250）。`--seg-size` 保留為向後相容逃生艙。
- **雙路 ASR 交叉參考**：主 ASR（Breeze）+ 選用 VibeVoice，LLM 用兩者修正英文術語與同音字。
- **畫面 caption 擷取**：VLM 自動擷取帶時間戳的畫面術語作為校正 ground truth。
- **結構性品質 gate**：合併步驟擋下過度合併段落並自動重派。
- **會自我成長的術語表**：每次跑完 diff 校正結果，提出新術語規則。
- **ASR 幻覺偵測**：重複型幻覺 + 時間軸空白自動偵測修復，Whisper large-v3 fallback。

### 修正
- 複查 fix 合併容忍前置序號行（不假設時間軸在 block 第一行）。
- VV 長音檔 `glob.escape` part-JSON 查找。

### 文件
- 雙語 README（英 + 繁中）+ LICENSE + Windows 指南 + 路徑消毒。
- 路由收斂：廣義 transcribe → speech-to-prose，srt 專注帶時間軸字幕。
