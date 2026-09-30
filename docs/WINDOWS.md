# Running srt-skill on Windows · 在 Windows 上使用 srt-skill

> **This is documentation only.** It does not change the macOS execution flow in any way — it explains how a Windows user (or their agent) can adapt the skill. The scripts and `SKILL.md` are unchanged for macOS.
> **這是純文件。** 它完全不改動 macOS 的執行流程，只說明 Windows 使用者（或其 agent）如何調整。腳本與 `SKILL.md` 對 macOS 維持不變。

**[English](#english) · [繁體中文](#繁體中文)**

---

## English

### TL;DR

**There is a supported path: `--engine=cloud` runs the ASR on a rented cloud GPU — Vast.ai first, RunPod only when Vast has no usable machine — so no local Apple Silicon is needed** (`--engine=vast` / `--engine=runpod` pin one provider). See [CLOUD-ASR-SETUP.md](CLOUD-ASR-SETUP.md). This is the recommended route for Windows; the rest of this document describes the older substitute-your-own-engine approach, which is still valid if you prefer to keep everything local.

The skill's **orchestration + LLM-correction layers are cross-platform Python and work on Windows**. The **local ASR backends are MLX (Apple Silicon only)** and do **not** run natively on Windows. Two ways around that: use `--engine=cloud` (~US$0.05-0.15 per 50-minute video), or **swap the ASR step for a Windows-friendly engine** (e.g. `faster-whisper`) and feed its `.srt` into the rest of the pipeline unchanged.

**Recommended environment: WSL2 (Ubuntu).** The pipeline includes bash scripts (`subtitle.sh`, `hallucination_fallback.sh`) and one Unix-only module (`vv_longaudio.py` uses `fcntl`), so a Linux userland avoids the most friction. Native PowerShell works for the pure-Python steps but not the bash/`fcntl` parts.

### What works / what doesn't

| Step | Native Windows | In WSL2 | Notes |
|------|:--:|:--:|-------|
| Step 0 — `yt-dlp` download | ✅ | ✅ | cross-platform |
| Step 0.5 — OCR terms (RapidOCR, default) | ✅ | ✅ | `auto` default; pure-CPU cross-platform, `pip install "rapidocr>=3.9,<4" onnxruntime`. VLM caption still available via `--engine ollama` (Ollama runs on Windows; `mlx-vlm` fallback does **not**) |
| **Step 1 — Breeze ASR via `--engine=cloud`** | ⚠️ | ✅ | **cloud GPU, no MLX needed**; RunPod path verified end-to-end on a 109-minute video from a machine with no MLX, no coreutils and bash 3.2 (2026-08-30); Vast-first path verified on macOS (2026-09-30). Native Windows untested — the scripts are bash |
| Step 1 — Breeze/Whisper ASR (local MLX) | ❌ | ❌ | MLX is Apple-Silicon-only → use `--engine=cloud` or **substitute** (see below) |
| Step 1' — VibeVoice ASR | ⚠️ | ✅ | local MLX path does not run; **cloud path since v1.9.0**: `cloud_asr.sh <media> <out> <basename> zh --vv --json` (Vast first, same fallback) |
| Step 1.5 — hallucination fix | ⚠️ | ✅ | re-runs ASR internally → honors `SRT_ASR_ENGINE=cloud`, so it works on the cloud too |
| Step 2a — preprocess | ✅ | ✅ | pure Python |
| Step 2b — LLM correction (Claude subagents) | ✅ | ✅ | Claude Code runs on Windows |
| Step 2b/2c — `--local` (Ollama) | ✅ | ✅ | Ollama runs on Windows |
| Step 2c — review + postprocess | ✅ | ✅ | pure Python |
| Step 3 — terminology learning | ✅ | ✅ | pure Python |
| Step 4 — `ffmpeg` mux | ✅ | ✅ | cross-platform |
| `subtitle.sh`, `hallucination_fallback.sh` | ❌ | ✅ | bash → use WSL or Git Bash |

### Setup (recommended: WSL2)

1. Install WSL2 + Ubuntu: `wsl --install` (PowerShell, admin), reboot.
2. In Ubuntu: install `ffmpeg`, `python3`, `pip`, `git`, `yt-dlp`.
3. Clone the skill: `git clone <repo-url> ~/.claude/skills/srt`.
4. Cloud route: `pipx install vastai`, then follow [CLOUD-ASR-SETUP.md](CLOUD-ASR-SETUP.md) (keys for Vast and RunPod). Local route: install a Windows-friendly ASR engine (see next section).
5. Optional: install Ollama on Windows (for caption / `--local` correction); it's reachable from WSL at `http://localhost:11434`.

### Substituting the ASR backend (the one real change)

The rest of the pipeline only needs **an `.srt` file from Step 1**. Any ASR that emits SRT will do. Recommended: **[`faster-whisper`](https://github.com/SYSTRAN/faster-whisper)** (CUDA or CPU).

```bash
pip install faster-whisper
# Produce zh SRT (then run OpenCC s2twp for Traditional Chinese if your engine emits Simplified)
faster-whisper-xxl "<media>" --language zh --output_format srt --model large-v3
```

Then start the pipeline at **Step 1.5 / Step 2a**, pointing the scripts at the SRT you just produced. Because the primary ASR is no longer Breeze, **omit the `--breeze` flag** in `srt_preprocess.py` / `srt_hallucination_fix.py`, and run OpenCC `s2twp` if the output is Simplified Chinese.

> Note: Step 1.5 (`srt_hallucination_fix.py`) re-runs ASR on suspect spans. On Windows it will try to call the MLX ASR; either wire it to your substitute engine or skip Step 1.5 and rely on Step 2b/2c LLM correction.

### Paths & environment variables

The skill resolves user paths from `$HOME` with env-var overrides — the same `SRT_*` variables documented in the [README](../README.md) work on Windows:

- **WSL2:** `$HOME` = `/home/<you>`. Set `SRT_DATA_DIR`, `SRT_TERMS` to WSL paths.
- **Native PowerShell:** use `%USERPROFILE%` / `$env:USERPROFILE`; set the same `SRT_*` vars (`setx SRT_DATA_DIR "C:\Users\you\srt-data"`). Bash-only steps still need WSL/Git Bash.

### Guidance for an agent driving this on Windows

1. Detect platform; if Windows, prefer WSL2.
2. Prefer `SRT_ASR_ENGINE=cloud` (Breeze on Vast, RunPod fallback) and run VibeVoice with `cloud_asr.sh --vv`. Only if cloud is not an option, replace Step 1 with `faster-whisper` (or equivalent) emitting a zh `.srt`; apply OpenCC `s2twp` if Simplified.
3. On the substitute-engine route only: drop `--breeze` from preprocess/hallucination-fix invocations and skip Step 1'.
4. Skip the `mlx-vlm` caption fallback; RapidOCR (default) or Ollama-based caption is fine.
5. Run Steps 2a → 4 exactly as in `SKILL.md` (pure Python + Claude subagents + ffmpeg all work).

---

## 繁體中文

### 一句話總結

這個 skill 的**編排層 + LLM 校正層是跨平台 Python，可在 Windows 運作**；但**本地 ASR 後端是 MLX（僅 Apple Silicon）**，在 Windows **無法**原生執行。解法有兩條：**建議走雲端 `--engine=cloud`**（先用 Vast.ai，Vast 沒有可用機器才改 RunPod；一支 50 分鐘影片約 US$0.05–0.15，設定見 [CLOUD-ASR-SETUP.md](CLOUD-ASR-SETUP.md)）；或**把 ASR 那一步換成 Windows 友善的引擎**（例如 `faster-whisper`），把它產生的 `.srt` 餵進後面不變的 pipeline。

**建議環境：WSL2（Ubuntu）。** pipeline 含 bash 腳本（`subtitle.sh`、`hallucination_fallback.sh`）與一個 Unix-only 模組（`vv_longaudio.py` 用到 `fcntl`），用 Linux userland 摩擦最小。原生 PowerShell 可跑純 Python 步驟，但跑不了 bash／`fcntl` 部分。

### 哪些能跑、哪些不行

| 步驟 | 原生 Windows | WSL2 內 | 說明 |
|------|:--:|:--:|------|
| Step 0 — `yt-dlp` 下載 | ✅ | ✅ | 跨平台 |
| Step 0.5 — OCR 術語（RapidOCR，預設） | ✅ | ✅ | `auto` 預設；純 CPU 跨平台，`pip install "rapidocr>=3.9,<4" onnxruntime`。VLM caption 仍可用 `--engine ollama`（Ollama 在 Windows 可跑；`mlx-vlm` fallback **不行**） |
| **Step 1 — Breeze ASR 走 `--engine=cloud`** | ⚠️ | ✅ | **雲端 GPU，不需要 MLX**；RunPod 路徑 2026-08-30 在無 MLX、bash 3.2 的機器實跑 109 分鐘；Vast 優先路徑 2026-09-30 在 macOS 實跑。原生 Windows 沒測（腳本是 bash） |
| Step 1 — Breeze/Whisper ASR（本地 MLX） | ❌ | ❌ | MLX 僅 Apple Silicon → 走 `--engine=cloud` 或**替換引擎**（見下） |
| Step 1' — VibeVoice ASR | ⚠️ | ✅ | 本地 MLX 跑不了；**v1.9.0 起有雲端路徑**：`cloud_asr.sh <影片> <輸出> <basename> zh --vv --json`（同樣 Vast 優先） |
| Step 1.5 — 幻覺修復 | ⚠️ | ✅ | 內部會重跑 ASR → 認 `SRT_ASR_ENGINE=cloud`，雲端也能跑；走替代引擎時需自行接上 |
| Step 2a — 預處理 | ✅ | ✅ | 純 Python |
| Step 2b — LLM 校正（Claude subagent） | ✅ | ✅ | Claude Code 在 Windows 可跑 |
| Step 2b/2c — `--local`（Ollama） | ✅ | ✅ | Ollama 在 Windows 可跑 |
| Step 2c — 複查 + 後處理 | ✅ | ✅ | 純 Python |
| Step 3 — 術語學習 | ✅ | ✅ | 純 Python |
| Step 4 — `ffmpeg` 內嵌字幕 | ✅ | ✅ | 跨平台 |
| `subtitle.sh`、`hallucination_fallback.sh` | ❌ | ✅ | bash → 用 WSL 或 Git Bash |

### 環境設定（建議 WSL2）

1. 安裝 WSL2 + Ubuntu：PowerShell（管理員）跑 `wsl --install`，重開機。
2. 在 Ubuntu 內：安裝 `ffmpeg`、`python3`、`pip`、`git`、`yt-dlp`。
3. clone skill：`git clone <repo-url> ~/.claude/skills/srt`。
4. 雲端路線：`pipx install vastai`，再照 [CLOUD-ASR-SETUP.md](CLOUD-ASR-SETUP.md) 設好 Vast 與 RunPod 的金鑰。本地路線：安裝 Windows 友善的 ASR 引擎（見下節）。
5. 選用：在 Windows 裝 Ollama（caption／`--local` 校正用），WSL 內可透過 `http://localhost:11434` 連到。

### 替換 ASR 後端（唯一真正要改的地方）

後面整條 pipeline 只需要 **Step 1 產出的 `.srt`**，任何能輸出 SRT 的 ASR 都行。建議用 **[`faster-whisper`](https://github.com/SYSTRAN/faster-whisper)**（CUDA 或 CPU）。

```bash
pip install faster-whisper
# 產生中文 SRT（若引擎輸出簡體，再跑 OpenCC s2twp 轉台灣繁體）
faster-whisper-xxl "<media>" --language zh --output_format srt --model large-v3
```

接著從 **Step 1.5 / Step 2a** 開始，把腳本指向你剛產生的 SRT。因為主 ASR 不再是 Breeze，`srt_preprocess.py` / `srt_hallucination_fix.py` 要**拿掉 `--breeze`**；若輸出是簡體就跑 OpenCC `s2twp`。

> 注意：Step 1.5（`srt_hallucination_fix.py`）會對可疑段落重跑 ASR。在 Windows 它會嘗試呼叫 MLX ASR；請改接你的替代引擎，或乾脆跳過 Step 1.5、靠 Step 2b/2c 的 LLM 校正。

### 路徑與環境變數

skill 從 `$HOME` 解析使用者路徑並可用環境變數覆寫——[README](../README.md) 列的那組 `SRT_*` 變數在 Windows 一樣有效：

- **WSL2：** `$HOME` = `/home/<你>`。把 `SRT_DATA_DIR`、`SRT_TERMS` 設成 WSL 路徑。
- **原生 PowerShell：** 用 `%USERPROFILE%` / `$env:USERPROFILE`；設同一組 `SRT_*` 變數（`setx SRT_DATA_DIR "C:\Users\you\srt-data"`）。bash-only 步驟仍需 WSL／Git Bash。

### 給在 Windows 上驅動此 skill 的 agent 的指引

1. 偵測平台；若為 Windows，優先用 WSL2。
2. 優先設 `SRT_ASR_ENGINE=cloud`（Breeze 走 Vast、RunPod 備援），VibeVoice 用 `cloud_asr.sh --vv`。雲端不可行時才把 Step 1 換成 `faster-whisper`（或等效）輸出中文 `.srt`；簡體就套 OpenCC `s2twp`。
3. 只有走替代引擎時：preprocess／hallucination-fix 拿掉 `--breeze`，並跳過 Step 1'。
4. 跳過 `mlx-vlm` caption fallback；用 RapidOCR（預設）或 Ollama caption 即可。
5. Step 2a → Step 4 完全照 `SKILL.md` 跑（純 Python + Claude subagent + ffmpeg 都可用）。
