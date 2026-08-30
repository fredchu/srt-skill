"""輸入正規化必須發生在開 pod 之前。

背景（2026-08-30 真實事故）：全片那跑先開 pod、才在本機轉檔，
而輸入是 109 分鐘的 mkv（音軌 opus / 48 kHz / 立體聲）。
轉檔跑了 23 分鐘還沒完，那段期間 **GPU 用量 0%、機器一直計費**，白燒 US$0.29。

這條測試釘的是**順序**，不是轉檔本身會不會成功——
轉檔成功但順序錯，錢照樣白燒，而且沒有任何錯誤訊息。
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
CLOUD_ASR = SCRIPTS / "cloud_asr.sh"


def _make_mkv(path: Path) -> None:
    """做一個跟事故同型的輸入：Opus 音軌、48 kHz、立體聲、包在 mkv 裡。"""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3",
         "-c:a", "libopus", str(path)],
        check=True,
    )


def _run(tmp_path: Path, mode: str = "--breeze"):
    src = tmp_path / "in.mkv"
    _make_mkv(src)
    out = tmp_path / "out"
    out.mkdir()
    env = dict(os.environ)
    env.update({
        "CLOUD_ASR_TEST_HOOK": "provisioning_retry",
        "RUNPOD_API_KEY": "mock-key",
        "POLL_INTERVAL_SECONDS": "1",
        "SSH_READY_TIMEOUT_SECONDS": "5",
        "SSH_PROBE_TIMEOUT_SECONDS": "2",
    })
    return subprocess.run(
        ["bash", str(CLOUD_ASR), str(src), str(out), "demo", "zh", mode],
        capture_output=True, text=True, env=env, timeout=180,
    )


def test_normalize_happens_before_any_pod_is_created(tmp_path: Path) -> None:
    """log 裡「正規化」必須出現在「開 pod」之前。"""
    r = _run(tmp_path)
    log = r.stdout + r.stderr
    norm = log.find("normalizing input audio")
    pod = log.find("creating RunPod pod")
    assert norm != -1, f"沒有看到正規化的 log：\n{log[-1500:]}"
    assert pod != -1, f"沒有看到開 pod 的 log（mock 應該要開）：\n{log[-1500:]}"
    assert norm < pod, (
        "開 pod 發生在正規化之前——機器會在本機轉檔期間空轉計費。\n"
        "這正是 2026-08-30 白燒 US$0.29 的那個順序。\n" + log[-1500:]
    )


def test_mkv_with_opus_stereo_is_accepted(tmp_path: Path) -> None:
    """跟事故同型的輸入（Opus / 48 kHz / 立體聲 / mkv）要能被接受。

    契約從「只收 wav」改成「收 ffmpeg 讀得動的任何媒體」之後，
    這條確保那個擴大是真的，不是文件寫了但程式還在擋。
    """
    r = _run(tmp_path)
    log = r.stdout + r.stderr
    assert "failed to normalize input audio" not in log, (
        "Opus/48k/立體聲的 mkv 被正規化擋下來了：\n" + log[-1500:]
    )
    assert "normalized audio ready" in log, (
        "沒有看到正規化完成的訊息：\n" + log[-1500:]
    )


def test_vv_mode_also_normalizes_before_pod(tmp_path: Path) -> None:
    """⚠️ `--vv` 走的是另一條分支，它也必須正規化在開 pod 之前。

    2026-08-30 真實事故：第一版只把 normalize 接在 Breeze 主流程的 create_pod 之前，
    **VV 模式有自己的一份舊 ffmpeg（沒有 -vn）**，於是 `--vv` 那條路徑完全沒吃到修法。
    我對著檔案逐條確認過四項修法都在，但**確認的是檔案內容不是執行路徑**——
    那條路根本沒走到。白燒 US$0.22。

    只測 --breeze 的話，這個 bug 會完整地活下來。
    """
    r = _run(tmp_path, mode="--vv")
    log = r.stdout + r.stderr
    norm = log.find("normalizing input audio")
    pod = log.find("creating RunPod pod")
    assert norm != -1, f"--vv 路徑沒有跑正規化：\n{log[-1500:]}"
    # ⚠️ 這行不可以寫成 `if pod != -1:`。
    # 那樣寫的話，只要「creating RunPod pod」從來沒出現，測試就無聲通過——
    # 而「沒開 pod」本身就代表這條路徑沒走完，測試等於什麼都沒驗。
    # 守衛的失效方向必須是「擋下」不是「放行」。（pi 靜態審查指出）
    assert pod != -1, (
        f"--vv 路徑沒有走到開 pod，這條測試等於沒驗到順序：\n{log[-1500:]}"
    )
    assert norm < pod, (
        "--vv 的開 pod 發生在正規化之前：\n" + log[-1500:]
    )


def test_only_one_ffmpeg_flac_call_in_the_whole_file() -> None:
    """整份檔案只准有一處 `ffmpeg ... -c:a flac`。

    **這條才是防止「兩處只修一處」再發生的東西。**
    2026-08-30 有兩處（主流程一處、VV 分支一處），修了主流程那處、
    以為修完了，跑 --vv 才發現另一處還在。

    「一律正規化、不分支」那條規則寫在註解裡擋不住——註解不會執行。
    靜態斷言會。
    """
    # ⚠️ 指令會跨行（結尾反斜線），逐行比對會抓到 0 處。
    # 第一版就是這樣寫的，測試紅了才發現——先把接續行接起來。
    raw = CLOUD_ASR.read_text(encoding="utf-8")
    joined = re.sub(r"\\\n\s*", " ", raw)
    body = "\n".join(
        line for line in joined.splitlines()
        if not line.lstrip().startswith("#")
    )
    calls = [
        line.strip() for line in body.splitlines()
        if re.search(r"\bffmpeg\b", line) and "-c:a flac" in line
    ]
    assert len(calls) == 1, (
        f"應該只有一處 ffmpeg 轉 flac，實際有 {len(calls)} 處。\n"
        "多一處就多一條會被漏修的路徑。\n  " + "\n  ".join(calls)
    )
