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


def _run(tmp_path: Path):
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
        ["bash", str(CLOUD_ASR), str(src), str(out), "demo", "zh", "--breeze"],
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
