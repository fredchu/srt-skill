"""srt_postprocess.normalize_fullwidth_punct 全形標點正規化 regression 測試。

背景：ASR 無標點，標點由 LLM 校正時加上；LLM 在英文 token 後（如 "AWS, Google"）
常留半形逗號/問號。Step 2c 加一道確定性全形收尾，不依賴 LLM 記得（2026-07-03
美股展望 7hr 字幕出現 849 個半形逗號後定案）。
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "srt_correct"))
import srt_postprocess as m  # noqa: E402


@pytest.mark.parametrize("inp,expected", [
    # 英文詞後半形逗號 → 全形，並吃掉英文慣例尾隨空格
    ("我們看 AWS, Google 跟 Meta", "我們看 AWS，Google 跟 Meta"),
    ("YOY, RPO, CAPEX 都看", "YOY，RPO，CAPEX 都看"),
    ("AWS,Google 沒空格", "AWS，Google 沒空格"),
    # 數字千分位保護（不動）
    ("全球供給大概 2,200 的 EB", "全球供給大概 2,200 的 EB"),
    # 小數點、冒號時間不動
    ("大概 3.5 倍", "大概 3.5 倍"),
    ("time 10:00 開盤", "time 10:00 開盤"),
    # CJK 後的問號/驚嘆號 → 全形
    ("他到底在講什麼?", "他到底在講什麼？"),
    ("真的假的!", "真的假的！"),
    # 純英文的問號/驚嘆號不動（避免誤傷 URL / 英文語氣）
    ("WTF! 這什麼", "WTF! 這什麼"),
])
def test_normalize_fullwidth_punct(inp, expected):
    assert m.normalize_fullwidth_punct(inp) == expected


def test_idempotent():
    """已是全形的文字再跑一次不變。"""
    s = "我們看 AWS，Google，然後呢？真的！"
    assert m.normalize_fullwidth_punct(s) == s
