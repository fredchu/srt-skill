"""srt_strip_commentary tool-call/XML tag 洩漏清理的 regression 測試。

背景：校正 subagent 偶爾把工具呼叫閉合 tag（</content></invoke> 等）寫進校正輸出的
字幕尾，force-split 又會把它連同真字幕拆句、散成整行 tag、行首碎片、行尾殘缺開頭。
本測試鎖住 strip_tool_tag_residue() 與 clean() 的行為（2026-07-02 事故 + 2 輪
codex reviewer 對抗審查後定案）。
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "srt_correct"))
import srt_strip_commentary as m  # noqa: E402

# tag 字面用串接組出，避免任何工具解析器誤讀
C = "</" + "content>"
CO = "<" + "content>"
I = "</" + "invoke>"
OPEN_I = "<in" + "voke name=\"x\">"


@pytest.mark.parametrize("raw, expected", [
    # 分行完整 tag（真實資料形態）
    (C, ""),
    (I, ""),
    (CO, ""),
    (OPEN_I, ""),
    # 合併一行多 tag（reviewer BLOCKER）
    (C + I, ""),
    # inline 接在中文後（reviewer BLOCKER：有 CJK 仍須剝尾綴 tag）
    ("那這種時候這種狀況下" + C + I, "那這種時候這種狀況下"),
    # force-split 拆出的碎片
    ("/con" + "tent>" + I, ""),        # 左半右碎片
    ("con" + "tent>", ""),             # 左< 被切掉
    ("</", ""),                          # 裸 </
    ("某句尾巴<", "某句尾巴"),            # 行尾裸 <
    # 屬性/斜線續行碎片（reviewer MAJOR）
    ("name=\"x\">", ""),
    ("/param" + "eters>", ""),
    ("</" + "parameter>", ""),
    # split 切在 > 之前的殘缺開頭（reviewer verify 殘餘 MAJOR）
    ("尾巴<in" + "voke name=\"x\"", "尾巴"),
    ("caption <con" + "tent", "caption"),
    ("caption </con" + "tent", "caption"),
    ("<in" + "voke", ""),
])
def test_strip_removes_residue(raw, expected):
    assert m.strip_tool_tag_residue(raw) == expected


@pytest.mark.parametrize("raw", [
    # 純英文 ticker / 代號（不在 allowlist,不可誤刪）
    "SPHD", "AAPL", "REITs", "FFO", "VRT", "QQQ 加上這個",
    "AAPL>", "OK>", "<BRK.B>", "<ETF>",
    # 含 < > = 的合法內容
    "他的 P/E > 20 倍", "x>5", "beta 是 2.22", "size=\"large\" 很重要",
    # allowlist 詞出現在字中（非 tag，reviewer MINOR：不可誤吃）
    "mycon" + "tent> 應保留", "前綴 con" + "tent> 後綴", "value name=\"x\"> 應保留",
    # 正常中文
    "那這種時候這種狀況下", "毋庸置疑它就是在 AI 的前段班",
])
def test_strip_preserves_legit(raw):
    assert m.strip_tool_tag_residue(raw) == raw


def test_clean_e2e(tmp_path):
    """clean() 對含各種污染形態的 SRT：剝殘留、整行-tag 條目 Type B 刪、真字幕保留。"""
    srt = "\n\n".join([
        "1\n00:00:01,000 --> 00:00:02,000\n正常字幕一",
        "2\n00:00:02,000 --> 00:00:03,000\n那這種時候這種狀況下" + C + I,   # inline
        "3\n00:00:03,000 --> 00:00:04,000\n" + C + I,                          # 整行 tag → Type B
        "4\n00:00:04,000 --> 00:00:05,000\n綜合前兩堂\n" + C + "\n" + I,        # 分行
        "5\n00:00:05,000 --> 00:00:06,000\n某句尾巴<\n/con" + "tent>" + I,     # split 碎片
        "6\n00:00:06,000 --> 00:00:07,000\nAAPL 加到 QQQ",                     # 合法英文
        "7\n00:00:07,000 --> 00:00:08,000\n<BRK.B> 是波克夏",                  # 合法 <ticker>
    ]) + "\n"
    p = tmp_path / "poll.srt"
    p.write_text(srt, encoding="utf-8")
    m.clean(p)
    out = p.read_text(encoding="utf-8")

    assert "content>" not in out and "invoke>" not in out
    assert "那這種時候這種狀況下" in out          # inline 真字幕保住
    assert "綜合前兩堂" in out and "某句尾巴" in out
    assert "AAPL 加到 QQQ" in out and "<BRK.B> 是波克夏" in out  # 合法行不誤刪
    nums = [ln for ln in out.splitlines() if ln.isdigit()]
    assert len(nums) == 6                          # 整行-tag 條目 3 被 Type B 刪 → 7→6
    assert nums == [str(i) for i in range(1, 7)]   # 重編號連續


def test_clean_idempotent(tmp_path):
    """乾淨 SRT 再跑 clean() 零改動（無誤傷）。"""
    srt = ("1\n00:00:01,000 --> 00:00:02,000\nAAPL 加到 QQQ\n\n"
           "2\n00:00:02,000 --> 00:00:03,000\n<BRK.B> 是波克夏\n")
    p = tmp_path / "clean.srt"
    p.write_text(srt, encoding="utf-8")
    a, b, c = m.clean(p)
    assert (a, b, c) == (0, 0, 0)
    assert p.read_text(encoding="utf-8") == srt
