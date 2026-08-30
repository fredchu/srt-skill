# Changelog

## 1.9.0 - 2026-08-30

> **VibeVoice 上雲了**，四條 ASR 路徑完成第二條。
> 但這一版真正的內容是**五個「跑一次真的雲端才會現形」的 bug**——
> 它們全部通過了靜態檢查、通過了模擬測試、也通過了我對著檔案的逐條確認。

### 新增

- **`cloud_asr.sh --vv`：VibeVoice 走雲端。** 60 秒 clip 實測 177 秒、US$0.036。
  熱詞經 `apply_transcription_request(prompt=)` 進模型；金絲雀測試確認沒有提示詞洩漏。

- **`gpu.minCudaVersion`（預設 12.8）** — 直連 SSH 埠「約一半機率不生成」
  這件事之前當成隨機的，實際上跟主機的 CUDA 驅動版本有關：

  | 主機 CUDA | 直連埠 |
  |---|---|
  | 12.4 ×3 | 各等 425-431 秒，**永遠沒出現** |
  | 12.8、13.0 | 26 秒、222 秒、20 秒 |

  三台失敗與成功那台**都在 US-NC-1**，所以不是資料中心是主機批次。
  ⚠️ 用 `minCudaVersion` 不用 `allowedCudaVersions`：後者是**精確比對**，
  列到沒有機器提供的版本會**回容量錯誤而不是自動退回**。

- **`scripts/runpod_timing_ledger.py`** — 累積「開機到直連就緒」的秒數。
  目前 7 成功 / 7 未就緒。⚠️ 未就緒那些是**右設限樣本**（「我們在那秒放棄了」
  不是「那台在那秒確定失敗」），不能拿最小值去推安全門檻。

### 修復（全部是雲端實跑才現形的）

- **缺 `-vn` 讓 ffmpeg 把整條影片轉成 PNG 塞進音檔當封面。**
  FLAC 容器接受附圖，於是 h264 影片軌被逐幀解碼。700% CPU 是在解影片不是壓聲音。
  **20 秒切片：58.3 秒 → 0.2 秒，291 倍。** 109 分鐘原本要 5.3 小時——
  不是慢，是**永遠跑不完**。另加 `-sample_fmt s16`（否則變 24-bit，檔案大一倍）。

  ⚠️ **這個真因是第三個假說。** 前兩個（壓縮等級太高、解碼加重採樣慢）都合理、都錯。
  **把 stream mapping 印出來才看到它實際在做什麼。**

- **正規化搬到開 pod 之前，而且兩個模式共用一次呼叫。**
  原本先開機器才在本機轉檔，GPU 全程空轉計費。
  ⚠️ 第一次修只修了 Breeze 主流程，**VV 有自己的一份舊 ffmpeg**——
  我對著檔案逐條確認過四項修法「都在」，**但確認的是檔案內容不是執行路徑**，
  而我跑的是 `--vv`。現在整份檔案只剩**一處** ffmpeg，並有靜態測試釘住。

- **DELETE 收到 404 是成功不是失敗。** 外部的 `runpod_reap.sh`（v1.8.0 才加的）
  砍掉機器後，主流程自己的 cleanup 撞 404 被判成失敗、保留暫存目錄、回傳非零——
  **機器明明已經清乾淨了**。這個交互是我們自己造出來的：
  任何人照 docs 設排程跑收屍工具都會撞到。

- **能力探針的失敗原因印錯。** `subtitle.sh` 取 `breeze_reason=`，
  探針只輸出合併的 `reason=`，抓不到就落預設字串「記憶體不足」——
  而在沒裝 mlx_whisper 的機器上真因是「套件沒裝」。
  **照那個訊息使用者會去買記憶體。** 已加兩支腳本之間的鍵名契約測試。

- **合併時就過濾 subagent 漏出的工具標籤。** 原本靠後處理兜底，
  代表任何不經後處理的路徑會帶著標籤出貨。

### 調整

- VibeVoice 的等價性數字改成**兩側都 `s2twp`** 的口徑：
  跨側 20.5% → **10.6%**、雲端自噪 3.15% → **1.8%**。
  原數字是**單邊轉換**造成的（只轉本地、以為雲端是繁體）。
  **字形不是這個模型的穩定屬性**：帶不帶 prompt 會改變字形方向。

- `cloud_to_srt.py` 的 VV 模式**無條件 `s2twp`**，不做「已經是繁體就跳過」的判斷——
  那種判斷正是這次出錯的原因。

- 新增 `docs/RELEASE-CHECKLIST.md`：**最後一步是在目標機器上跑發布的那個標籤**。
  v1.8.0 就是漏了這步，發完才發現發布版沒在真機跑過。

### 全片 109 分鐘實測通過（2026-08-30 19:08）

| 項目 | 結果 |
|---|---|
| pod | **1 台**（37 秒就緒、主機 CUDA 13.0） |
| 切片 | 3 片，切點 [2197.44, 4392.83]，各約 2196 秒 |
| 段數 | 443 |
| 涵蓋 | 首段 0.00 → 末段 6589.21（音長 6589.22，**差 0.01 秒**） |
| 單調遞增 / 負時長 / 斷層 / 切點重疊 | ✅ / 0 / 0 / 0 |
| 下游 | `srt_prepare_segments` 13 段全部有 VV 參考，**0 個 NO_VV_REFERENCE** |
| 速度 | RTF 0.122 |
| 花費 | 1035 秒、**US$0.21**，pod 確認砍掉 |
| GPU 峰值 | 20780 MiB（事前預估 20.2 GiB，對得上） |

兩個一度標為異常的，追完都不是問題：段數 443 超出事前估的 350-420，
是**那個估算鬆了**（三片字數 10166/10296/10262、對 Breeze 同區間比都是 1.22，
內容完整；第三片段數少是切得粗不是掉內容）；殘留 3 個「簡體」字元是
几／台／占，**那是合法繁體**（茶几、平台、占卜）。

### Known issues

- ⚠️ **熱詞（terms）那條路徑尚未 live 驗證。**
  60 秒 clip 證明參數會進 prompt、金絲雀沒洩漏，但「熱詞是否真的改善辨識」
  需要對照組實測，尚未做。
- ⚠️ **Whisper large-v3 備援仍未上雲**，四條路完成二條。
- ⚠️ **熱詞只送術語表前 N 個且不排序**，術語表學越多送出比例越低，且無訊號。
  本版只加可觀測性（log 印「送出 N / 共 M」、`env/terms_sent.txt`），未修行為。
- ⚠️ 原生 Windows 與 Linux 仍未實測。

## 1.8.0 - 2026-08-30

> **這一版是「Breeze 上雲＋無 MLX 機器跑得完＋砍機安全網」，不是「全部 ASR 上雲」。**
> 四條 ASR 路徑只有 Breeze（Step 1）真的上雲；VibeVoice 完全沒有雲端實作，
> Whisper large-v3 備援在雲端模式明確拒絕，Step 1.5 的重跑理論上跟著上雲但沒實際觸發過。
> 剩下三條**是還沒做完，不是做不到**。
>
> 實測平台：Mac mini M1、8 GB 記憶體、macOS 15.4.1，**沒有 MLX、沒有 coreutils、
> 只有系統內建的 bash 3.2**。1.7.0 在這台機器上一步都跑不動。

### 修復（全部是「開發機看不到、目標機一定撞」的那類）

- **依賴檢查要跟著引擎走。** `--engine=runpod` 時仍無條件 `check_dependency mlx_whisper`，
  於是沒有 MLX 的機器在第 1 步就死，永遠到不了雲端分流。引擎分流加在執行點，
  檢查點沒跟上。改成 runpod 檢查 `curl`/`ssh`/`scp`/`python3`。

- **`[[ -v VAR ]]` 需要 bash 4.2，macOS 內建是 3.2.57。** shebang 是 `env bash`，
  在沒裝 homebrew bash 的機器上就指到它，`cloud_asr.sh` 整支報
  `conditional binary operator expected` 並 exit 2。改用 `${VAR+x}`。

- **沒有 `timeout` 時不可以裸跑命令。** `maybe_timeout` 原本在找不到
  `timeout`/`gtimeout` 時直接執行。四個呼叫點全是 ssh/scp——**卡住不會產生任何訊號**，
  於是 `trap cleanup EXIT INT TERM HUP QUIT` 永遠不觸發，pod 一直計費。
  補了純 bash 的替代，逾時一律回 124 與 coreutils 對齊。

  `<&0` 不可省：非互動 shell 的背景工作預設把 stdin 接到 /dev/null，
  不加會讓 `ssh 'bash -se' <"$script_file"` 讀到空輸入、遠端靜默不做事也不報錯。

- **`cleanup` 要收掉孤兒子程序**（`pkill -TERM -P $$`）。背景跑的 ssh/scp 在
  SIGTERM 之後不會死，可能在父程序已宣告失敗之後才把半成品 SRT 寫進輸出目錄。
  而 `subtitle.sh` 找輸出的方式是「比標記檔新的 `${BASENAME}*.srt`」，
  於是使用者 Ctrl-C 後重跑，第二次會撿走第一次的半成品當自己的產物——
  不報錯，資料是錯的。**這是靜默失效，不是浪費資源。**

- **後處理不可以寫死 repo 內的 `.venv`。** 乾淨 clone 沒有那個目錄。
  時序最糟：ASR 已跑完、雲端的錢已花掉、SRT 也產出來了，才死在後處理。

- **`sed -i ''` 是 BSD 專屬語法**，GNU sed 會把 `''` 當檔名並 exit 2。改 `-i.bak`。

### 調整

- `SSH_COMMAND_TIMEOUT_SECONDS` 900 → **1800**。實測同一段遠端安裝指令，
  快的機器 19 秒、慢的機器 7 分 30 秒（差 20 倍，差在那台機器連套件庫的網速）。
- `SSH_READY_TIMEOUT_SECONDS` 180 → **420**。直連端口約一半機率生不出來是已知的，
  但生得出來的實測有等到 3.7 分鐘的；180 秒會把好機器誤判成壞的砍掉重開。
- `COST_CAP_USD` 註明是**步驟之間才檢查的軟上限**，單一步驟跑再久都不會被它中斷。

### 新增

- **`scripts/runpod_reap.sh`** — 獨立的收屍工具。`cloud_asr.sh` 的 trap 能處理正常結束、
  逾時、Ctrl-C、SIGTERM，但 **`kill -9` 攔不住**——那是作業系統的硬規定。
  關掉終端機視窗、當機、斷電都一樣，機器會留在雲端一直計費而且不會有人通知你。
  用法：`runpod_reap.sh --kill-older-than 30`。

- **`scripts/asr_capacity_check`** — 判斷這台機器跑不跑得動本地模型，
  分模型給答案並附兩條路的代價。權重大小**讀實際檔案不用常數**
  （優先本機 HF 快取，其次 HF API），輸出標明來源。
  8 GB 的 M1 實測：Metal 工作集上限 5.33 GiB、VibeVoice 權重 5.32 GiB——
  差 0.01，所以「8 GB 跑不動 VibeVoice」是結構性的，不是保守估計。

- **`scripts/asr_engine.sh`** — 逐步驟的引擎解析，優先序 CLI > 各步驟環境變數 >
  全域 > mlx。

- **`docs/CLOUD-ASR-SETUP.md`** — 沒有 Apple Silicon 也能跑的從零安裝文件。
  每一條都在真機執行過，含「沒有驗證過的部分」專節。

- **`scripts/tests/test_bash32_compat.py`** — bash 3.2 相容回歸測試，四類：
  靜默出錯（`$EPOCHSECONDS` 展開成空、算術當 0，計時器全錯且不報錯）、
  執行才炸、解析就炸、GNU 專屬指令。含規則自體檢（25 個該抓到 + 11 個不可誤判樣本）。

### 實測數字

| 機器 | Breeze 速度 | 109 分鐘影片 | 花費 |
|---|---|---|---|
| M1 Max 32GB（本地） | 17 倍 | 約 6.5 分鐘 | 電費 |
| M1 基本款 8GB（本地） | 3.5 倍 | 約 31 分鐘 | 電費 |
| RunPod RTX 4090（雲端） | 40 倍 | **13.4 分鐘** | **US$0.17** |

**8 GB 的機器上雲端比本地快 2.3 倍。**

無 MLX 機器跑完 109 分鐘影片**的 Breeze 那一段**：2510 條字幕、涵蓋 00:00:05 到
01:49:49（影片全長 1:49:49）、超過 30 秒的斷層 0 處、pod 砍乾淨。
同一台也跑完 Step 2a 預處理與 Step 2b 切段（2510→2525 條、13 段）。
Step 2b 的 LLM 校正與 Step 2c 在別台執行——那兩步是純雲端 LLM，與 ASR 主機無關。

與本地輸出比對：96.5% 逐字相同，671 處差異中位數 1 個字、82% 只差 1-2 字。

### Known issues

- ⚠️ **VibeVoice 仍未上雲。** `cloud_asr.sh` 完全沒有 VibeVoice 的實作。
  走 `--engine=runpod` 時 Step 1' 會跳過，Step 2b 因此少了交叉參考（`vv_segments: 0`）。
- ⚠️ **逐步驟選引擎（`SRT_BREEZE_ENGINE` 等）本版沒有。** 曾寫過 `scripts/asr_engine.sh`
  並驗過五種優先序組合，但沒有接進任何腳本——設了不會生效。
  **發布一個設了不會生效的旋鈕，正是這一版在修的那類 bug**，所以本版把它拿掉了。
  程式碼留在 git 歷史：`git show 30a5bd2:scripts/asr_engine.sh`。
  等 VibeVoice 有雲端路徑、真的需要分步驟選引擎時再接回來。
- ⚠️ **`hallucination_fallback.sh`（Whisper large-v3 那條）仍綁 MLX**，
  雲端模式下會明確拒絕而不是默默跑。
- ⚠️ **雲端輸出與本地不等價，仍不知道哪邊比較對。** 需要對真人聽打比對。
- ⚠️ **原生 Windows 與 Linux 都沒實測過。** 腳本是 bash 寫的，Windows 建議走 WSL2。
- ⚠️ **`kill -9` 之後機器不會被砍**，這是硬極限，靠 `runpod_reap.sh` 兜底。
- ⚠️ **wav 檔名衝突未修**（沿自 1.7.0）。

## 1.7.0 - 2026-08-30

### 新增

- **可選的雲端 ASR 路徑**：`subtitle.sh --engine=runpod`（或 `SRT_ASR_ENGINE=runpod`）
  把 Step 1 的 ASR 送到 RunPod RTX 4090 跑。**預設維持 `mlx`（本地）不變。**

  目的是降低本地硬體需求——M1 Max 故障備援、8GB 的 MBA、非 Apple Silicon 的機器。
  實測（49 分鐘影片）：Breeze 走 faster-whisper RTF 0.0253，比本地 M1 Max 快 2.3 倍；
  一集約新台幣 5 元。完整實測見
  `company/_shared/collab/20260829-srt-cloud-asr-runpod/FINAL-srt-cloud-asr-plan.md`。

  分流只有一處（`subtitle.sh` 呼叫 ASR 那一行），輸出寫到同一個路徑，
  所以檔名 fallback、strict 驗證、後處理全部不變；
  `srt_hallucination_fix.py` 因為呼叫的就是 `subtitle.sh`，**自動獲得雲端能力**。

### Known issues

- ⚠️ **雲端輸出與本地不等價，而且不知道哪邊比較對。**
  跨側文字差 2.07%（Breeze）、**10.6%**（VibeVoice，2026-08-30 修正：兩側都 s2twp 的口徑；
  原本寫 20.5% 是單邊轉換造成的，一半是字形不是辨識）。
  Breeze 兩邊各自跑兩次都逐位元組相同；VibeVoice 雲端自噪 1.8%。
  差異是穩定的實質差異，不是雜訊。要判斷哪邊正確需要對真人聽打比對，尚未做。
- ⚠️ **雲端只支援 `--breeze`。** 不帶旗標（large-v3 全片）與 `--turbo` 都沒在雲端測過，
  `cloud_asr.sh` 對這兩種會直接報錯而不是默默跑。
- ⚠️ **VibeVoice 尚未上雲。** 本版只搬 Step 1 的 Breeze。
- ⚠️ **Step 1.5 的幻覺修復在雲端未驗證。** 主路徑呼叫 `subtitle.sh` 所以理論上跟著上雲，
  但沒實跑過；第二順位的 `hallucination_fallback.sh` 仍綁 MLX。
- ⚠️ **wav 檔名衝突未修**：`subtitle.sh` 與 `vibevoice_asr.py` 仍搶同一個
  `<影片檔名>.wav`，靠「先跑完 Breeze 再啟動 VV」的紀律緩解。本版未處理。

## 1.6.1 - 2026-08-29

### 修復

- **1.6.0 的「不可並行」修復漏了 SKILL.md 自己兩處**，導致同一份文件自相矛盾四個月。
  1.6.0 已改互斥表、Step 1' 標題與內文、README 中英兩份 pipeline 圖，但漏掉：
  - SKILL.md 開頭的 Pipeline 總覽圖仍畫著 `─┐ 平行`
  - 「背景啟動 + 等待契約」段仍寫「並行邊界按引擎判斷：Breeze + VibeVoice 可並行」

  兩處都改為序列，並在後者註明「記憶體層面確實不會 OOM，但那不涵蓋搶檔 race」——
  這是當初會寫錯的原因：兩個判斷來自不同層面，只看記憶體會得到相反結論。

  1.6.0 的 CHANGELOG 自己寫過「只改互斥表無效，照 Step 1' 字面操作的人還是會並行」。
  同一個教訓在同一次修復裡沒做徹底：改了外部 README，漏了 agent 每次都會讀的 SKILL.md。
  wiki `subtitle-pipeline` 2026-08-01 條目就標了「SKILL.md 互斥表與緩解措施矛盾，待修」，
  拖到 2026-08-29 才由「VV 是否移到雲端」的評估連帶發現。

## 1.6.0 - 2026-08-13

### 修復
- **Breeze 與 VibeVoice 不可並行**（SKILL.md GPU 互斥表原本標為「標準平行組合」）。兩者都把音檔抽到 VIDEO_DIR 內同一個 `<影片檔名>.wav`：`subtitle.sh` 的清理步驟會刪掉它，而 `vibevoice_asr.py` 抽取的是同一路徑。並行時的兩種失敗都**靜默**——主 ASR 讀到被覆寫中的截斷 wav（2026-07-17 實例：51.8 分鐘影片只轉出 4.6 分鐘，條數檢查放行，只有拿尾端時間戳比對影片長度才抓得到），或 VibeVoice 因檔案被刪 `FileNotFoundError`。已出事三次（2026-07-17、2026-08-01、2026-08-13）。改為 ❌ 並寫明機制，同步改 Step 1' 標題與內文與 README 中英兩份 pipeline 圖——**只改互斥表無效**，照 Step 1' 字面操作的人還是會並行。
  - 刻意保留「記憶體層面 Breeze(1.5GB)+VV 不會 OOM」的既有結論並註明它不涵蓋這個搶檔 race：兩者講的是不同層面，把關係寫清楚才不會下次又被表面矛盾帶偏。
  - **根治是讓兩者各自使用獨立的 wav 檔名，尚未實作**；目前的序列執行是緩解措施。
- **Step 2b 範例漏 `--vv-json`**（2026-08-01 即記錄，本次確認仍漏）。漏了不報錯，症狀只有 summary 裡的 `vv_segments: 0`，六個校正 subagent 全讀到 `NO_VV_REFERENCE` 照常跑完，成品表面正常但完全失去雙路交叉參考。一併補 `--slide-terms`，並寫明 **VV JSON 檔名衍生自輸入檔 stem 而非 `--output`**（影片為 `X [id].mkv` 時 JSON 是 `X [id]_vibevoice.json`，SRT 卻可能是 `X_vibevoice.srt`，不可從 SRT 檔名推 JSON 路徑），以及跑完要檢查 `vv_segments`／`captions` 皆非 0。

### 新增
- **合併 gate 加入零時長與跨段重複文字的 deterministic 檢查**（`srt_merge_segments.py`，+347 行測試）。原本的 gate 只看條數比例與超長時長，擋不住 subagent 捏造時間軸或把其他段內容複製進本段；新檢查在 gate 失敗訊息中帶 `zero_dur_examples`／`dup_examples`，供重派時組裝針對性提示。
- preprocess 新增 15 條 Breeze 同音字規則（7月-03 七條、8月-01 八條）：頓畫→鈍化、的遁→的鈍、二向性→二象性、波利二／波力二→波粒二、正當指標→震盪指標、離 g 線→離均線等。8月-01 那批經回歸驗證：拿原片原始 ASR 重跑，替換 20→56 處，增量 36 正好等於六條規則的命中數，無誤傷。

### 變更
- **Step 2d 補兩條機械動作**：（1）名詞 xref **必須對錯詞與每一個候選詞都跑**——只搜錯詞會漏掉「候選在全片他處已被正確辨識」這種最強的 L0 內部證據；（2）subagent 聲稱「存疑但保留原樣」的條目，一律拿原文 grep 成品確認真的還在，防「回報說保留、實際已改」。
- **立「報告時間點必 grep 解出」hard rule**：subagent 自由文字回報的時間點不可信（本次實例：某段回報 00:03:37，實際 grep 出 00:04:20），報告中每一個時間點都要拿引用文字回成品 grep 後填入。

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
