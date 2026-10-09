import json
from types import SimpleNamespace

import srt_translate_ko as ko


def test_assemble_writes_bilingual_txt(tmp_path):
    srt = tmp_path / "ep.srt"
    srt.write_text(
        "1\n00:00:09,840 --> 00:00:13,660\n今天是 10 月 3 號\n\n"
        "2\n00:01:05,000 --> 00:01:08,000\n我們開始\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "ko"
    out_dir.mkdir()
    (out_dir / "batch_000.json").write_text(
        json.dumps([{"i": 1, "ko": "오늘은 2026년\\n10월 3일이에요"}, {"i": 2, "ko": "시작할게요"}]),
        encoding="utf-8",
    )
    out = tmp_path / "ep_ko.srt"

    rc = ko.cmd_assemble(SimpleNamespace(srt=str(srt), out_dir=str(out_dir), out=str(out)))

    assert rc == 0
    assert (tmp_path / "ep_中韓對照.txt").read_text(encoding="utf-8") == (
        "00:00:09\n今天是 10 月 3 號\n오늘은 2026년 10월 3일이에요\n\n"
        "00:01:05\n我們開始\n시작할게요\n"
    )
