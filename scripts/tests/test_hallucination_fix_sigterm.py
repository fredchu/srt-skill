"""超時要先送 SIGTERM 讓子程序自己砍雲端機器，不可以直接 SIGKILL。

**2026-08-30 實測**：`subprocess.run(..., timeout=N)` 超時時送的是 **SIGKILL**，
子程序的 `trap` 完全不會跑。而 `srt_hallucination_fix.py` 呼叫的是
`subtitle.sh` → `cloud_asr.sh`，後者的 `trap cleanup` 就是砍雲端機器那段。

**所以原本的寫法會在超時時遺棄一台一直計費的機器，而且沒有任何訊息。**

更糟的是這條路有三段 buffer 重試（10/20/30 秒），每段各開一台，
最壞情況三台同時被遺棄。

原本的上限是 600 秒。今天實測光是遠端裝套件在慢機器上就要 450 秒，
加上開機、上傳、推論，600 秒**經常不夠**——所以這不是罕見路徑。
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
FIXER = SCRIPTS / "srt_correct" / "srt_hallucination_fix.py"


def test_subprocess_run_timeout_uses_sigkill() -> None:
    """先證明「為什麼不能用 subprocess.run(timeout=)」。

    這條測的是 Python 的行為不是我們的程式碼——它存在的意義是
    **讓下一個想改回 subprocess.run 的人看到後果**。
    """
    with tempfile.TemporaryDirectory() as t:
        mark = Path(t) / "trap.txt"
        sh = Path(t) / "c.sh"
        sh.write_text(
            f'#!/bin/bash\ncleanup() {{ echo ran > "{mark}"; }}\n'
            f'trap cleanup EXIT INT TERM HUP QUIT\nsleep 30\n')
        sh.chmod(0o755)
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run([str(sh)], timeout=2, capture_output=True)
        time.sleep(1)
        assert not mark.exists(), (
            "subprocess.run 超時居然讓 trap 跑了——"
            "如果 Python 改了行為，這條測試要更新，但別忘了檢查我們的替代寫法還需不需要")


def test_terminate_first_lets_trap_run() -> None:
    """我們的替代寫法：先 SIGTERM、寬限、才 SIGKILL。"""
    with tempfile.TemporaryDirectory() as t:
        mark = Path(t) / "trap.txt"
        sh = Path(t) / "c.sh"
        sh.write_text(
            f'#!/bin/bash\ncleanup() {{ echo ran > "{mark}"; }}\n'
            f'trap cleanup EXIT INT TERM HUP QUIT\nsleep 30\n')
        sh.chmod(0o755)
        proc = subprocess.Popen([str(sh)], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
        time.sleep(1)
        assert mark.exists(), "先 SIGTERM 之後 trap 仍然沒跑——雲端機器會被遺棄"


def test_fixer_does_not_use_subprocess_run_timeout() -> None:
    """靜態釘住：`srt_hallucination_fix.py` 不可以再出現 `subprocess.run(... timeout=`。

    **這條才是防止改回去的東西。** 註解擋不住，註解不會執行。
    """
    body = "\n".join(
        line for line in FIXER.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#"))
    joined = " ".join(body.split())
    assert "subprocess.run(" not in joined or "timeout=" not in joined.split("subprocess.run(")[-1][:200], (
        "又用回 subprocess.run(timeout=) 了——超時會 SIGKILL，"
        "雲端機器的 trap 不會跑，會留下一台一直計費的機器")
