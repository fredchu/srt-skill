"""DELETE 收到 404 的處理：機器已經不在算成功，但假的 404 不可以被吞掉。

背景（2026-08-30 真實事故）：外部的 `runpod_reap.sh` 先砍掉機器，
`cloud_asr.sh` 自己的 cleanup 隨後 DELETE 收到 404，被「不是 2xx 就是錯誤」
判成失敗，記成 `terminate result=failure`，保留暫存目錄、回傳非零。
**機器明明已經清乾淨了，卻報清理失敗。**

而 `runpod_reap.sh` 是 v1.8.0 才進 repo 的，所以任何人照 docs 設排程跑它
都會撞到同一件事，然後以為砍機路徑壞了。

⚠️ **這兩個案例必須同時存在**：
- 只測「404 就算成功」，會讓「API 說 404 但機器其實還活著」被靜默吞掉
- 第二個案例才是雙向驗證的另一半

⚠️ 測試設計上的一個坑：mock 層原本在 `runpod_rest_request` 開頭就直接 return，
所以 404 的判定邏輯根本不會被 mock 路徑執行到——那樣測到的是 mock 自己。
現在 mock 回 44 代表「模擬 404」，兩條路走同一段判定，測的才是真的邏輯。
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
CLOUD_ASR = SCRIPTS / "cloud_asr.sh"


def run_mock(env_extra: dict, tmp_path: Path):
    wav = tmp_path / "in.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=16000:cl=mono", "-t", "1", str(wav)],
        check=True,
    )
    out = tmp_path / "out"
    out.mkdir()
    env = dict(os.environ)
    env.update({
        "CLOUD_ASR_TEST_HOOK": "delete_404",
        "RUNPOD_API_KEY": "mock-key",
        "POLL_INTERVAL_SECONDS": "1",
        "SSH_READY_TIMEOUT_SECONDS": "5",
        "SSH_PROBE_TIMEOUT_SECONDS": "2",
        **env_extra,
    })
    return subprocess.run(
        ["bash", str(CLOUD_ASR), str(wav), str(out), "demo", "zh", "--breeze"],
        capture_output=True, text=True, env=env, timeout=180,
    )


def test_404_with_pod_absent_counts_as_terminated(tmp_path: Path) -> None:
    """API 回 404 且清單裡也沒有 → 那台真的不在了，算砍機成功。"""
    r = run_mock({"MOCK_DELETE_404_ON_ATTEMPT": "1"}, tmp_path)
    combined = r.stdout + r.stderr
    assert "pod already absent" in combined, (
        "404 應該印出「已經不在」的說明，讓 log 看得出它不是『刪成功了』：\n"
        + combined[-1500:]
    )
    assert not re.search(r"action=terminate result=failure", combined), (
        "機器已經不在卻報砍機失敗——這正是這條測試要擋的事故：\n" + combined[-1500:]
    )


def test_fake_404_with_pod_still_listed_is_not_swallowed(tmp_path: Path) -> None:
    """⚠️ 反向：API 回 404 但清單裡還在 → 那是假的 404，不可以當成功。

    這條沒過的後果比第一條嚴重：機器還活著卻被當成已砍，一直計費而且沒人知道。
    下游的 `pod_present_in_list` 是攔它的那道，這條測試證明那道還在。
    """
    r = run_mock(
        {"MOCK_DELETE_404_ON_ATTEMPT": "1", "MOCK_404_KEEP_IN_LIST": "1"},
        tmp_path,
    )
    combined = r.stdout + r.stderr
    assert not re.search(r"action=terminate result=success", combined), (
        "清單裡還有那台，卻報砍機成功——假的 404 被吞掉了：\n" + combined[-1500:]
    )
