"""asr_capacity_check 與 subtitle.sh 之間的鍵名契約。

背景：subtitle.sh 用 `sed -n 's/^breeze_reason=//p'` 取原因，但探針一度只輸出
合併的 `reason=`。抓不到就落到預設字串「記憶體不足」——而在一台沒裝 mlx_whisper
的機器上，真因是「套件沒裝」。**那個錯誤訊息會讓使用者去買記憶體而不是裝套件。**

這類 bug 兩邊各自的測試都抓不到，因為兩邊各自都「正確」。
只有把契約本身測起來才擋得住。
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
PROBE = SCRIPTS / "asr_capacity_check"
SUBTITLE = SCRIPTS / "subtitle.sh"


def probe_output() -> str:
    r = subprocess.run(
        [sys.executable, str(PROBE), "--no-cache"],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        pytest.skip(f"探針跑不起來（可能沒有 MLX 環境）：{r.stderr[:200]}")
    return r.stdout


def keys_subtitle_reads() -> set:
    """從 subtitle.sh 掃出它實際會去取的鍵名。"""
    text = SUBTITLE.read_text(encoding="utf-8")
    return set(re.findall(r"sed -n 's/\^([a-z_]+)=//p'", text))


def test_subtitle_reads_only_keys_the_probe_emits() -> None:
    """subtitle.sh 取的每一個鍵，探針都必須真的輸出。"""
    emitted = {line.split("=", 1)[0] for line in probe_output().splitlines() if "=" in line}
    wanted = keys_subtitle_reads()
    assert wanted, "沒有從 subtitle.sh 掃到任何鍵名，掃描的正規表示式可能過時了"
    missing = wanted - emitted
    assert not missing, (
        f"subtitle.sh 會去取這些鍵但探針不輸出：{sorted(missing)}\n"
        f"抓不到的後果是落到預設字串，印出錯誤的原因給使用者。\n"
        f"探針實際輸出：{sorted(emitted)}"
    )


def test_every_key_has_nonempty_value() -> None:
    """鍵存在還不夠，值不能是空的——空值一樣會落到預設字串。"""
    out = probe_output()
    for key in keys_subtitle_reads():
        m = re.search(rf"^{key}=(.*)$", out, re.M)
        assert m and m.group(1).strip(), f"{key} 的值是空的，subtitle.sh 會印預設字串"
