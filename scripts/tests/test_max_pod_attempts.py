"""`MAX_POD_ATTEMPTS` 要真的限制住換機器的次數。

背景（2026-08-30）：直連埠約一半機率不生成，所以預設會換到第三台。
但**預算緊的時候需要「只試一次」**——當天剩 US$0.25 要跑最後一項驗收，
一次壞機器就吃掉 US$0.09，開到第三台會直接超支。

那時這個上限**寫死在四個地方、沒有旗標可關**，只能靠外部監看到 timeout
就 SIGTERM 整支腳本。那能work但很脆：要搶在第二台開起來之前。
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
CLOUD_ASR = SCRIPTS / "cloud_asr.sh"


def _run(tmp_path: Path, attempts: str):
    wav = tmp_path / "a.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=16000:cl=mono", "-t", "1", str(wav)], check=True)
    out = tmp_path / "out"
    out.mkdir()
    env = dict(os.environ)
    env.update({
        "CLOUD_ASR_TEST_HOOK": "provisioning_retry",
        "RUNPOD_API_KEY": "mock",
        "POLL_INTERVAL_SECONDS": "1",
        "SSH_READY_TIMEOUT_SECONDS": "3",
        "SSH_PROBE_TIMEOUT_SECONDS": "1",
        "MAX_POD_ATTEMPTS": attempts,
    })
    r = subprocess.run(
        ["bash", str(CLOUD_ASR), str(wav), str(out), "demo", "zh", "--breeze"],
        capture_output=True, text=True, env=env, timeout=180)
    return r.stdout + r.stderr


def test_one_attempt_opens_exactly_one_pod(tmp_path: Path) -> None:
    """`MAX_POD_ATTEMPTS=1` 就是「壞了就停，不換機器」。

    這條沒過的後果是超支：預算只夠一台時它會自己開到第三台。
    """
    log = _run(tmp_path, "1")
    n = log.count("action=create result=success")
    assert n == 1, f"MAX_POD_ATTEMPTS=1 卻開了 {n} 台機器：\n{log[-1200:]}"
    assert "after 1 pod(s); giving up" in log, (
        f"錯誤訊息應該說明只試了幾台：\n{log[-1200:]}")


def test_default_still_allows_three(tmp_path: Path) -> None:
    """⚠️ 反向：不設這個變數時，預設行為不可以被改掉。

    只驗「限制得住」不夠——限制寫太緊會讓正常情況一台就放棄，
    而直連埠約一半機率不生成，那樣幾乎每次都失敗。
    """
    log = _run(tmp_path, "3")
    n = log.count("action=create result=success")
    assert n == 3, f"預設應該換到第三台，實際只開了 {n} 台：\n{log[-1200:]}"


def test_invalid_value_is_rejected_not_silently_defaulted(tmp_path: Path) -> None:
    """給錯值要報錯，不可以靜默當成預設值。

    靜默降級成 3 的話，使用者以為設了「只試一次」，實際上開了三台，
    而且**沒有任何訊息告訴他**。那是靜默失效。
    """
    log = _run(tmp_path, "abc")
    assert "MAX_POD_ATTEMPTS 必須是正整數" in log, (
        f"無效值應該明確報錯：\n{log[-800:]}")
    assert log.count("action=create result=success") == 0, (
        "無效值時不可以開任何機器")
