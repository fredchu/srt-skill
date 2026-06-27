# Changelog

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
