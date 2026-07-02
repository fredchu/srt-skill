# Changelog

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
