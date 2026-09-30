# 在沒有 Apple Silicon 的機器上跑 srt-skill · Cloud ASR Setup

> 這份文件講的是「**這台機器跑不動 ASR**」的情況：沒有 Apple Silicon、
> 或有但記憶體不夠。做法是把語音辨識丟到雲端的 GPU，本地只留輕量的步驟。
>
> RunPod 的每一條都在真機上跑過：Mac mini M1、8 GB 記憶體、macOS 15.4.1，
> **沒有裝 MLX、沒有裝 coreutils、只有系統內建的 bash 3.2**（2026-08-30）。
> Vast.ai 與「Vast 優先、RunPod 備援」的部分在 Apple Silicon 32 GB 機器上實跑
> （2026-09-30），**沒有在 8 GB 那台重測**。

**[繁體中文](#繁體中文) · [English](#english)**

---

## 繁體中文

### 這份文件適合誰

| 你的情況 | 適用 |
|---|---|
| Windows 電腦 | ✅ 走 WSL2，再照這份做 |
| Intel Mac | ✅ |
| Apple Silicon 但記憶體 8 GB | ✅ 大模型跑不動，走雲端（VibeVoice 在 8 GB 上是結構性跑不動，不是慢） |
| Linux 沒有 NVIDIA 顯卡 | ✅ |
| Apple Silicon 16 GB 以上 | 不需要，用預設的本地路徑就好。長片（3 小時以上）可以只把 VibeVoice 丟雲端，見文末 |

### 雲端怎麼選

建議用 `--engine=cloud`：**先用 Vast.ai，Vast 沒有可用機器時自動改用 RunPod**。

| 引擎 | 行為 |
|---|---|
| `cloud`（建議） | 先 Vast。只有 Vast 以「沒有可用機器」結束（搜不到報價、開不起來、SSH 一直不通、CUDA 不能用，且開過的機器都已確認刪除）才換 RunPod |
| `vast` | 只用 Vast，失敗不換 |
| `runpod` | 只用 RunPod，失敗不換 |

**不會換家的情況**：辨識已經開始後出錯（換家多半也一樣錯，只是多花錢）、
或 Vast 的機器刪不掉（可能還在計費，這時候再開一台就兩邊一起燒錢）。

兩家都設好最穩。只設 Vast 的話，Vast 沒機器時就會直接失敗；只設 RunPod 也可以——`cloud` 發現 Vast 沒設定（沒金鑰、沒裝 `vastai`、帳號沒掛 SSH 公鑰）會直接改用 RunPod。明確指定 `vast` 時則照常報錯。

### 本地還要跑什麼

**只有這幾樣**，都不吃顯卡：

1. `ffmpeg` — 把影片的聲音抽出來
2. Python 3.10 以上 — 跑後處理與字幕組裝
3. `ssh` / `scp` / `curl` — 跟雲端機器溝通
4. `vastai` 命令列工具 — 開 Vast 的機器（只用 RunPod 的話不需要）
5. Claude Code — 做字幕校正那一步

**不需要安裝任何語音辨識模型**，一個都不用。

### 你需要先準備

- 一個 Vast.ai 帳號（<https://vast.ai>），裡面要有餘額。
  RTX 5090 每小時約 US$0.35–0.55。
- 一個 RunPod 帳號（<https://runpod.io>）當備援，裡面要有餘額。
  一支 50 分鐘的影片大約花 **US$0.05 到 0.15**。
- 兩邊的 API 金鑰（Vast：Account → API Keys；RunPod：Settings → API Keys）

### 安裝步驟

#### 1. 裝基本工具

macOS：

```bash
brew install ffmpeg python@3.13 pipx
pipx install vastai          # 也可以用 pip install vastai
```

Ubuntu / WSL2：

```bash
sudo apt update && sudo apt install -y ffmpeg python3 python3-venv pipx git curl openssh-client
pipx install vastai
```

> **不需要裝 `coreutils`。** 腳本在沒有 `timeout` 指令的機器上會自己用內建的
> 方式計時，這條路徑有測試涵蓋。

#### 2. 抓下 skill

```bash
git clone https://github.com/fredchu/srt-skill.git ~/dev/srt-skill
```

#### 3. 建一個獨立的 Python 環境

```bash
python3 -m venv ~/srt-work/venv
~/srt-work/venv/bin/pip install jieba opencc requests tiktoken
```

要用畫面文字擷取（Step 0.5）的話再加：

```bash
~/srt-work/venv/bin/pip install "rapidocr>=3.9,<4" onnxruntime
```

#### 4. 產一把專用的 SSH 金鑰，兩家都登記

**每台機器產自己的一把，不要複製別台的私鑰過來。** 同一把給兩家用。

```bash
ssh-keygen -t ed25519 -N "" -C "$(whoami)@$(hostname)-cloudasr" -f ~/.ssh/id_ed25519_cloudasr
cat ~/.ssh/id_ed25519_cloudasr.pub
```

- **Vast**：`vastai create ssh-key "$(cat ~/.ssh/id_ed25519_cloudasr.pub)"`，
  再用 `vastai show ssh-keys` 確認有列出來。
- **RunPod**：把印出來的那一行貼到網站的 **Settings → SSH Public Keys**。

> ⚠️ **RunPod 那個欄位是整份覆寫的。** 如果你已經有別台機器的金鑰在裡面，
> 要**保留舊的、在後面加一行新的**，不要整份換掉——換掉那台就失去存取權了。
> 貼完重新整理頁面，確認舊的還在。

#### 5. 放 API 金鑰

```bash
vastai set api-key '你的_VAST_金鑰'        # 存到 ~/.config/vastai/vast_api_key

mkdir -p ~/.config/runpod
printf '%s' '你的_RUNPOD_金鑰' > ~/.config/runpod/api_key
chmod 600 ~/.config/runpod/api_key
```

#### 6. 寫一個環境設定檔

存成 `~/srt-work/srt.env`：

```bash
export SRT_SKILL_DIR="$HOME/dev/srt-skill"
export SRT_DATA_DIR="$HOME/srt-work"
export SRT_TERMS="$HOME/srt-work/terms/terms.txt"   # 講者術語表，沒有可先留空檔
export SRT_ASR_ENGINE=cloud                         # 關鍵：語音辨識走雲端，Vast 優先
export SSH_PRIVATE_KEY="$HOME/.ssh/id_ed25519_cloudasr"
export SSH_PUBLIC_KEY_PATH="$HOME/.ssh/id_ed25519_cloudasr.pub"
export VIRTUAL_ENV="$HOME/srt-work/venv"
export PATH="$VIRTUAL_ENV/bin:$PATH"
```

用之前先 `source ~/srt-work/srt.env`。

### 驗證安裝

先用一分鐘的短片試，**不要一開始就跑整支**：

```bash
source ~/srt-work/srt.env
# 從任一影片切 60 秒出來
ffmpeg -y -ss 600 -t 60 -i 你的影片.mkv -c:v libx264 -crf 30 -c:a aac test60.mp4
bash ~/dev/srt-skill/scripts/subtitle.sh "$PWD/test60.mp4" --breeze --engine=cloud
```

log 裡應該看到 `searching Vast.ai offers`，成功時全程不會出現 RunPod。
要單獨驗證 RunPod 備援，改用 `--engine=runpod` 再跑一次。

**跑完一定要確認機器有砍掉**，沒砍掉就是一直在計費：

```bash
bash ~/dev/srt-skill/scripts/vast_reap.sh      # 只列出，不動手
bash ~/dev/srt-skill/scripts/runpod_reap.sh
```

兩支都應該回「目前沒有任何機器在跑」。

### VibeVoice 也能上雲（v1.9.0 起）

輔助辨識 VibeVoice 走另一支指令，直接吃影片、超過 60 分鐘自動切段：

```bash
bash ~/dev/srt-skill/scripts/cloud_asr.sh 你的影片.mkv 輸出資料夾 basename zh \
    --vv --json --terms "$SRT_TERMS" --terms-max 50
```

不設 `CLOUD_ASR_PROVIDER` 時一樣是 Vast 優先、RunPod 備援。
**長片（3 小時以上）建議本地跑 Breeze、同時把 VibeVoice 丟雲端**：兩者不搶同一個
音檔，可以並行，VibeVoice 整支都跑得到（2026-09-30 實測）。

### 常見問題

| 症狀 | 原因 | 解法 |
|---|---|---|
| `找不到 mlx_whisper` | 忘了指定雲端 | 加 `--engine=cloud`，或設 `SRT_ASR_ENGINE=cloud` |
| `conditional binary operator expected` | 你的 bash 太舊（3.2） | 升級到 v1.8.0 以後的版本 |
| `missing RunPod credential` / `missing Vast.ai credential` | 金鑰檔沒放好 | 檢查 `~/.config/runpod/api_key`、`~/.config/vastai/vast_api_key` |
| `Vast.ai has no usable machine; trying RunPod` | Vast 當下沒有合格的機器 | 正常，已自動換 RunPod |
| Vast 某台 `Permission denied (publickey)` | 單一主機的問題（帳號金鑰正確時也會發生） | 腳本會自己換下一台；若每台都被拒，用 `vastai show ssh-keys` 確認公鑰真的有登記 |
| Vast 連上後好幾分鐘才開始辨識 | 新機器要先裝 Python 套件，實測 1 到 7 分鐘不等 | 每次固定要付的開機成本；長片可忽略，幾分鐘的短片用 `--engine=runpod` 反而便宜 |
| RunPod SSH 一直連不上、最後失敗 | RunPod 的直連端口**大約一半機率生不出來** | 正常，腳本會等 180 秒後換一台重來，最多三次 |
| 跑完機器還在 | 腳本被強制中斷 | 用上面兩支收屍腳本確認，到網站手動刪除。之後改用 `Ctrl-C` 而不是關掉整個視窗 |

### 沒有驗證過的部分

誠實列出來，不要當成已知可用：

- **Windows 原生**（不透過 WSL2）沒測過。腳本是 bash 寫的，理論上要 WSL2 或 Git Bash。
- **Linux** 沒測過。用到的工具都是跨平台的，但沒有實跑數據。
- **Vast 路徑沒有在 8 GB、bash 3.2 的機器上測過**。RunPod 路徑有。
- **雲端與本地的辨識結果不完全一樣**：Breeze 實測 2.07% 字元差異、VibeVoice 10.6%，
  而且**目前不知道哪一邊比較準**。要求字對字一致的場合請先自行比對。

---

## English

### Who this is for

Machines that cannot run the local ASR models: Windows PCs, Intel Macs,
8 GB Apple Silicon machines, and Linux boxes without an NVIDIA GPU.
Speech recognition runs on a rented cloud GPU; everything else stays local.

The RunPod steps were executed on a Mac mini M1, 8 GB RAM, macOS 15.4.1, with
**no MLX, no coreutils, and only the system bash 3.2** (2026-08-30). The Vast.ai
steps and the Vast-first/RunPod-fallback behaviour were run on a 32 GB Apple
Silicon machine (2026-09-30) and **have not been re-tested on the 8 GB machine**.

### Choosing a provider

Use `--engine=cloud`: **Vast.ai first, RunPod only when Vast has no usable
machine** (no matching offer, instance fails to start, SSH never becomes ready,
or CUDA unavailable — and every machine it opened was confirmed deleted).
`--engine=vast` and `--engine=runpod` pin a single provider with no fallback. A RunPod-only setup also works: when Vast is not configured (no key, no `vastai` CLI, no SSH key on the account), `cloud` goes straight to RunPod.

It never falls back once transcription has started, or when a Vast machine
could not be deleted (it may still be billing; renting another would double
the cost).

### What still runs locally

`ffmpeg`, Python 3.10+, `ssh`/`scp`/`curl`, the `vastai` CLI (not needed for
RunPod-only), and Claude Code. None needs a GPU. **No ASR model is installed.**

### Prerequisites

A Vast.ai account with credit (RTX 5090 ≈ US$0.35–0.55/h), plus a RunPod account
with credit as the fallback (a 50-minute video costs roughly **US$0.05–0.15**).

### Install

```bash
# 1. Base tools
brew install ffmpeg python@3.13 pipx && pipx install vastai          # macOS
# sudo apt install -y ffmpeg python3 python3-venv pipx git curl openssh-client && pipx install vastai   # Ubuntu/WSL2

# 2. The skill
git clone https://github.com/fredchu/srt-skill.git ~/dev/srt-skill

# 3. Python environment
python3 -m venv ~/srt-work/venv
~/srt-work/venv/bin/pip install jieba opencc requests tiktoken
# optional, for on-screen text extraction:
# ~/srt-work/venv/bin/pip install "rapidocr>=3.9,<4" onnxruntime

# 4. One dedicated SSH key for both providers (do NOT copy a private key from another machine)
ssh-keygen -t ed25519 -N "" -C "$(whoami)@$(hostname)-cloudasr" -f ~/.ssh/id_ed25519_cloudasr
vastai create ssh-key "$(cat ~/.ssh/id_ed25519_cloudasr.pub)" && vastai show ssh-keys
cat ~/.ssh/id_ed25519_cloudasr.pub    # paste into RunPod Settings -> SSH Public Keys

# 5. API keys
vastai set api-key 'YOUR_VAST_KEY'    # stored in ~/.config/vastai/vast_api_key
mkdir -p ~/.config/runpod
printf '%s' 'YOUR_RUNPOD_KEY' > ~/.config/runpod/api_key
chmod 600 ~/.config/runpod/api_key
```

> `coreutils` is **not** required. On machines without `timeout` the scripts
> fall back to a built-in timer; that path has test coverage.

> ⚠️ RunPod's SSH public key field is **overwritten wholesale**. If you already
> have another machine's key registered, **append** rather than replace, then
> reload the page and confirm the old key survived.

Then create `~/srt-work/srt.env` with the exports shown in the Chinese section
(the key one is `export SRT_ASR_ENGINE=cloud`) and `source` it before use.

### Verify

Start with a 60-second clip, not a full video:

```bash
source ~/srt-work/srt.env
ffmpeg -y -ss 600 -t 60 -i your-video.mkv -c:v libx264 -crf 30 -c:a aac test60.mp4
bash ~/dev/srt-skill/scripts/subtitle.sh "$PWD/test60.mp4" --breeze --engine=cloud
bash ~/dev/srt-skill/scripts/vast_reap.sh && bash ~/dev/srt-skill/scripts/runpod_reap.sh
```

The log should show `searching Vast.ai offers`. Both reap scripts must report
no running machines — a surviving machine keeps billing. Run once more with
`--engine=runpod` to check the fallback provider on its own.

### VibeVoice in the cloud (since v1.9.0)

```bash
bash ~/dev/srt-skill/scripts/cloud_asr.sh your-video.mkv out-dir basename zh \
    --vv --json --terms "$SRT_TERMS" --terms-max 50
```

Same Vast-first default. For long videos (3 h+), run Breeze locally and
VibeVoice in the cloud **at the same time**: they do not share an audio file,
so the full video gets VibeVoice coverage (measured 2026-09-30).

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `找不到 mlx_whisper` | no cloud engine selected | add `--engine=cloud`, or set `SRT_ASR_ENGINE=cloud` |
| `conditional binary operator expected` | bash 3.2 | upgrade to v1.8.0 or later |
| `missing RunPod credential` / `missing Vast.ai credential` | key file not found | check `~/.config/runpod/api_key`, `~/.config/vastai/vast_api_key` |
| `Vast.ai has no usable machine; trying RunPod` | no qualifying Vast machine right now | expected; it switched to RunPod |
| `Permission denied (publickey)` on one Vast host | host-specific (seen with a correctly registered key) | the script moves to the next machine; if every host refuses, check `vastai show ssh-keys` |
| Several minutes between Vast SSH-ready and inference | a fresh machine installs Python packages (1–7 min measured) | fixed per-run cost; negligible on long videos, use `--engine=runpod` for short clips |
| RunPod SSH never connects, then fails | RunPod's direct port fails to appear roughly half the time | expected; the script waits 180 s, then retries on a new pod, up to 3 times |
| Machine still running after exit | script was hard-killed | check with both reap scripts and delete in the console; prefer `Ctrl-C` over closing the window |

### Not verified

Stated plainly rather than assumed working:

- **Native Windows** (without WSL2) is untested; the scripts are bash.
- **Linux** is untested, though all tools used are cross-platform.
- **The Vast path has not been run on the 8 GB / bash 3.2 machine**; the RunPod path has.
- Cloud and local transcripts are **not identical** (Breeze 2.07%, VibeVoice 10.6%
  character difference measured), and **which one is more accurate is not yet known**.
