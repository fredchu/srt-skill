"""所有 shell 腳本都必須能在 macOS 內建的 bash 3.2 底下解析。

背景：macOS 內建的 /bin/bash 是 3.2.57（蘋果因授權問題二十年沒更新）。
腳本的 shebang 是 `#!/usr/bin/env bash`，在沒裝 homebrew bash 的機器上
就會指到它。2026-08-30 在 Mini CC（M1 8GB、macOS 15.4.1）實測，
cloud_asr.sh 的 `[[ -v INITIAL_PROMPT ]]` 讓整支腳本 exit 2，
雲端 ASR 完全跑不起來，而在有 brew bash 的開發機上完全看不出來。
"""

import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SHELL_SCRIPTS = sorted(SCRIPTS_DIR.glob("*.sh"))

# bash 4.0 以後才有的語法。每條都附最低版本，方便看訊息就知道為什麼。
BASH4_PATTERNS = [
    (re.compile(r"\[\[\s*-v\s"), "[[ -v VAR ]] 需要 bash 4.2；請改用 [[ -n \"${VAR+x}\" ]]"),
    (re.compile(r"^\s*(declare|local)\s+-A\b", re.M), "關聯陣列需要 bash 4.0"),
    (re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(\^\^|,,)"), "${VAR^^} / ${VAR,,} 需要 bash 4.0"),
    (re.compile(r"\b(mapfile|readarray)\b"), "mapfile / readarray 需要 bash 4.0"),
    (re.compile(r"&>>"), "&>> 需要 bash 4.0；請改用 >>file 2>&1"),
]


def _bash32() -> str:
    """回傳 macOS 內建 bash 3.2 的路徑，沒有就跳過測試。"""
    out = subprocess.run(
        ["/bin/bash", "--version"], capture_output=True, text=True, check=False
    ).stdout
    if not out.startswith("GNU bash, version 3."):
        pytest.skip(f"/bin/bash 不是 3.x，跳過：{out.splitlines()[0] if out else '無輸出'}")
    return "/bin/bash"


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_parses_under_bash32(script: Path) -> None:
    """bash 3.2 要能解析（-n 只解析不執行）。"""
    bash = _bash32()
    r = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"{script.name} 在 bash 3.2 下解析失敗：\n{r.stderr}"


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_no_bash4_only_syntax(script: Path) -> None:
    """光靠 bash -n 抓不到全部——有些 bash 4 語法在 3.2 底下解析得過、
    執行才炸。所以再做一次靜態掃描。"""
    text = script.read_text(encoding="utf-8")
    # 去掉註解行，避免說明文字裡出現的範例被誤判
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    found = [msg for pat, msg in BASH4_PATTERNS if pat.search(body)]
    assert not found, f"{script.name} 用了 bash 4+ 語法：\n  " + "\n  ".join(found)
