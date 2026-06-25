# Running srt-skill on Windows · 在 Windows 上使用 srt-skill

> **This is documentation only.** It does not change the macOS execution flow in any way — it explains how a Windows user (or their agent) can adapt the skill. The scripts and `SKILL.md` are unchanged for macOS.
> **這是純文件。** 它完全不改動 macOS 的執行流程，只說明 Windows 使用者（或其 agent）如何調整。腳本與 `SKILL.md` 對 macOS 維持不變。

**[English](#english) · [繁體中文](#繁體中文)**

---

## English

### TL;DR

The skill's **orchestration + LLM-correction layers are cross-platform Python and work on Windows**. The **local ASR backends are MLX (Apple Silicon only)** and do **not** run natively on Windows. The fix is to **swap the ASR step for a Windows-friendly engine** (e.g. `faster-whisper`) and feed its `.srt` into the rest of the pipeline unchanged.

**Recommended environment: WSL2 (Ubuntu).** The pipeline includes bash scripts (`subtitle.sh`, `hallucination_fallback.sh`) and one Unix-only module (`vv_longaudio.py` uses `fcntl`), so a Linux userland avoids the most friction. Native PowerShell works for the pure-Python steps but not the bash/`fcntl` parts.

### What works / what doesn't

| Step | Native Windows | In WSL2 | Notes |
|------|:--:|:--:|-------|
| Step 0 — `yt-dlp` download | ✅ | ✅ | cross-platform |
| Step 0.5 — caption (Ollama vision) | ✅ | ✅ | Ollama runs on Windows; `mlx-vlm` fallback does **not** |
| **Step 1 — Breeze/Whisper ASR (MLX)** | ❌ | ❌ | MLX is Apple-Silicon-only → **substitute** (see below) |
| Step 1' — VibeVoice ASR (MLX) | ❌ | ❌ | optional; skip on Windows |
| Step 1.5 — hallucination fix | ⚠️ | ✅ | re-runs ASR internally → needs the substitute ASR wired in |
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
4. Install a Windows-friendly ASR engine (see next section).
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
2. Replace Step 1 with a `faster-whisper` (or equivalent) call that emits a zh `.srt`; apply OpenCC `s2twp` if Simplified.
3. Drop `--breeze` from preprocess/hallucination-fix invocations.
4. Skip Step 1' (VibeVoice) and the `mlx-vlm` caption fallback; Ollama-based caption is fine.
5. Run Steps 2a → 4 exactly as in `SKILL.md` (pure Python + Claude subagents + ffmpeg all work).

---

## 繁體中文

### 一句話總結

這個 skill 的**編排層 + LLM 校正層是跨平台 Python，可在 Windows 運作**；但**本地 ASR 後端是 MLX（僅 Apple Silicon）**，在 Windows **無法**原生執行。解法是**把 ASR 那一步換成 Windows 友善的引擎**（例如 `faster-whisper`），把它產生的 `.srt` 餵進後面不變的 pipeline。

**建議環境：WSL2（Ubuntu）。** pipeline 含 bash 腳本（`subtitle.sh`、`hallucination_fallback.sh`）與一個 Unix-only 模組（`vv_longaudio.py` 用到 `fcntl`），用 Linux userland 摩擦最小。原生 PowerShell 可跑純 Python 步驟，但跑不了 bash／`fcntl` 部分。

### 哪些能跑、哪些不行

| 步驟 | 原生 Windows | WSL2 內 | 說明 |
|------|:--:|:--:|------|
| Step 0 — `yt-dlp` 下載 | ✅ | ✅ | 跨平台 |
| Step 0.5 — caption（Ollama vision） | ✅ | ✅ | Ollama 在 Windows 可跑；`mlx-vlm` fallback **不行** |
| **Step 1 — Breeze/Whisper ASR（MLX）** | ❌ | ❌ | MLX 僅 Apple Silicon → **需替換**（見下） |
| Step 1' — VibeVoice ASR（MLX） | ❌ | ❌ | 選用；Windows 直接跳過 |
| Step 1.5 — 幻覺修復 | ⚠️ | ✅ | 內部會重跑 ASR → 需接上替代 ASR |
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
4. 安裝 Windows 友善的 ASR 引擎（見下節）。
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
2. 把 Step 1 換成 `faster-whisper`（或等效）呼叫，輸出中文 `.srt`；簡體就套 OpenCC `s2twp`。
3. preprocess／hallucination-fix 呼叫拿掉 `--breeze`。
4. 跳過 Step 1'（VibeVoice）與 `mlx-vlm` caption fallback；用 Ollama caption 即可。
5. Step 2a → Step 4 完全照 `SKILL.md` 跑（純 Python + Claude subagent + ffmpeg 都可用）。
