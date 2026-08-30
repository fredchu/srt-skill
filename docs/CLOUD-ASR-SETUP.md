# 在沒有 Apple Silicon 的機器上跑 srt-skill · Cloud ASR Setup

> 這份文件講的是「**這台機器跑不動 ASR**」的情況：沒有 Apple Silicon、
> 或有但記憶體不夠。做法是把語音辨識丟到雲端的 GPU，本地只留輕量的步驟。
>
> 全文的每一條都在真機上跑過。實測機器：Mac mini M1、8 GB 記憶體、
> macOS 15.4.1，**沒有裝 MLX、沒有裝 coreutils、只有系統內建的 bash 3.2**。
> 日期 2026-08-30。

**[繁體中文](#繁體中文) · [English](#english)**

---

## 繁體中文

### 這份文件適合誰

| 你的情況 | 適用 |
|---|---|
| Windows 電腦 | ✅ 走 WSL2，再照這份做 |
| Intel Mac | ✅ |
| Apple Silicon 但記憶體 8 GB | ✅ 大模型跑不動，走雲端 |
| Linux 沒有 NVIDIA 顯卡 | ✅ |
| Apple Silicon 16 GB 以上 | 不需要，用預設的本地路徑就好 |

### 本地還要跑什麼

**只有四樣**，都不吃顯卡：

1. `ffmpeg` — 把影片的聲音抽出來
2. Python 3.10 以上 — 跑後處理與字幕組裝
3. `ssh` / `scp` / `curl` — 跟雲端機器溝通
4. Claude Code — 做字幕校正那一步

**不需要安裝任何語音辨識模型**，一個都不用。

### 你需要先準備

- 一個 RunPod 帳號（<https://runpod.io>），裡面要有餘額。
  一支 50 分鐘的影片大約花 **US$0.05 到 0.15**。
- 該帳號的 API 金鑰（在 Settings → API Keys）

### 安裝步驟

#### 1. 裝基本工具

macOS：

```bash
brew install ffmpeg python@3.13
```

Ubuntu / WSL2：

```bash
sudo apt update && sudo apt install -y ffmpeg python3 python3-venv git curl openssh-client
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

#### 4. 產一把專用的 SSH 金鑰

**每台機器產自己的一把，不要複製別台的私鑰過來。**

```bash
ssh-keygen -t ed25519 -N "" -C "$(whoami)@$(hostname)-runpod" -f ~/.ssh/id_ed25519_runpod
cat ~/.ssh/id_ed25519_runpod.pub
```

把印出來的那一行貼到 RunPod 網站的 **Settings → SSH Public Keys**。

> ⚠️ **那個欄位是整份覆寫的。** 如果你已經有別台機器的金鑰在裡面，
> 要**保留舊的、在後面加一行新的**，不要整份換掉——換掉那台就失去存取權了。
> 貼完重新整理頁面，確認舊的還在。

#### 5. 放 API 金鑰

```bash
mkdir -p ~/.config/runpod
printf '%s' '你的_API_金鑰' > ~/.config/runpod/api_key
chmod 600 ~/.config/runpod/api_key
```

#### 6. 寫一個環境設定檔

存成 `~/srt-work/srt.env`：

```bash
export SRT_SKILL_DIR="$HOME/dev/srt-skill"
export SRT_DATA_DIR="$HOME/srt-work"
export SRT_TERMS="$HOME/srt-work/terms/terms.txt"   # 講者術語表，沒有可先留空檔
export SRT_ASR_ENGINE=runpod                        # 關鍵：語音辨識走雲端
export SSH_PRIVATE_KEY="$HOME/.ssh/id_ed25519_runpod"
export SSH_PUBLIC_KEY_PATH="$HOME/.ssh/id_ed25519_runpod.pub"
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
bash ~/dev/srt-skill/scripts/subtitle.sh "$PWD/test60.mp4" --breeze --engine=runpod
```

正常的話大約 **100 秒**跑完，花費約 **US$0.02**，產出 `test60.srt`。

**跑完一定要確認機器有砍掉。** 到 RunPod 網站的 Pods 頁面看，應該是空的。
沒砍掉就是一直在計費。

### 常見問題

| 症狀 | 原因 | 解法 |
|---|---|---|
| `找不到 mlx_whisper` | 忘了加 `--engine=runpod` | 加上旗標，或設 `SRT_ASR_ENGINE=runpod` |
| `conditional binary operator expected` | 你的 bash 太舊（3.2） | 升級到 v1.8.0 以後的版本 |
| `missing RunPod credential` | 金鑰檔沒放好 | 檢查 `~/.config/runpod/api_key` 存在且可讀 |
| SSH 一直連不上、最後失敗 | RunPod 的直連端口**大約一半機率生不出來** | 這是正常的，腳本會等 180 秒後換一台重來，最多三次 |
| 跑完機器還在 | 腳本被強制中斷 | 到 RunPod 網站手動刪除。之後改用 `Ctrl-C` 而不是關掉整個視窗 |

### 沒有驗證過的部分

誠實列出來，不要當成已知可用：

- **Windows 原生**（不透過 WSL2）沒測過。腳本是 bash 寫的，理論上要 WSL2 或 Git Bash。
- **Linux** 沒測過。用到的工具都是跨平台的，但沒有實跑數據。
- **VibeVoice 輔助辨識**目前還不能上雲，走 `--engine=runpod` 時這一步會跳過。
- **雲端與本地的辨識結果不完全一樣**（實測有 2.07% 的字元差異），
  而且**目前不知道哪一邊比較準**。要求字對字一致的場合請先自行比對。

---

## English

### Who this is for

Machines that cannot run the local ASR models: Windows PCs, Intel Macs,
8 GB Apple Silicon machines, and Linux boxes without an NVIDIA GPU.
Speech recognition runs on a rented cloud GPU; everything else stays local.

Every step below was executed on a real machine: Mac mini M1, 8 GB RAM,
macOS 15.4.1, with **no MLX, no coreutils, and only the system bash 3.2**
(2026-08-30).

### What still runs locally

Only four things, none of which need a GPU: `ffmpeg`, Python 3.10+,
`ssh`/`scp`/`curl`, and Claude Code. **No ASR model is installed at all.**

### Prerequisites

A RunPod account with credit. A 50-minute video costs roughly **US$0.05–0.15**.

### Install

```bash
# 1. Base tools
brew install ffmpeg python@3.13                  # macOS
# sudo apt install -y ffmpeg python3 python3-venv git curl openssh-client   # Ubuntu/WSL2

# 2. The skill
git clone https://github.com/fredchu/srt-skill.git ~/dev/srt-skill

# 3. Python environment
python3 -m venv ~/srt-work/venv
~/srt-work/venv/bin/pip install jieba opencc requests tiktoken
# optional, for on-screen text extraction:
# ~/srt-work/venv/bin/pip install "rapidocr>=3.9,<4" onnxruntime

# 4. A dedicated SSH key (do NOT copy a private key from another machine)
ssh-keygen -t ed25519 -N "" -C "$(whoami)@$(hostname)-runpod" -f ~/.ssh/id_ed25519_runpod
cat ~/.ssh/id_ed25519_runpod.pub    # paste into RunPod Settings -> SSH Public Keys

# 5. API key
mkdir -p ~/.config/runpod
printf '%s' 'YOUR_API_KEY' > ~/.config/runpod/api_key
chmod 600 ~/.config/runpod/api_key
```

> `coreutils` is **not** required. On machines without `timeout` the scripts
> fall back to a built-in timer; that path has test coverage.

> ⚠️ RunPod's SSH public key field is **overwritten wholesale**. If you already
> have another machine's key registered, **append** rather than replace, then
> reload the page and confirm the old key survived.

Then create `~/srt-work/srt.env` with the exports shown in the Chinese section
(the key one is `export SRT_ASR_ENGINE=runpod`) and `source` it before use.

### Verify

Start with a 60-second clip, not a full video:

```bash
source ~/srt-work/srt.env
ffmpeg -y -ss 600 -t 60 -i your-video.mkv -c:v libx264 -crf 30 -c:a aac test60.mp4
bash ~/dev/srt-skill/scripts/subtitle.sh "$PWD/test60.mp4" --breeze --engine=runpod
```

Expect ~100 seconds and ~US$0.02. **Then check the RunPod Pods page is empty** —
a surviving pod keeps billing.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `找不到 mlx_whisper` | `--engine=runpod` missing | add the flag, or set `SRT_ASR_ENGINE=runpod` |
| `conditional binary operator expected` | bash 3.2 | upgrade to v1.8.0 or later |
| `missing RunPod credential` | key file not found | check `~/.config/runpod/api_key` |
| SSH never connects, then fails | RunPod's direct port fails to appear roughly half the time | expected; the script waits 180 s, then retries on a new pod, up to 3 times |
| Pod still running after exit | script was hard-killed | delete it in the RunPod console; prefer `Ctrl-C` over closing the window |

### Not verified

Stated plainly rather than assumed working:

- **Native Windows** (without WSL2) is untested; the scripts are bash.
- **Linux** is untested, though all tools used are cross-platform.
- **VibeVoice cross-reference** does not have a cloud path yet and is skipped
  under `--engine=runpod`.
- Cloud and local transcripts are **not byte-identical** (2.07% character
  difference measured), and **which one is more accurate is not yet known**.
