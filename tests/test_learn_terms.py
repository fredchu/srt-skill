import json
import subprocess
import sys
import textwrap
from pathlib import Path

from srt_correct.srt_learn_terms import (
    enrich_candidates,
    learn_candidates,
    parse_preprocess_rules,
    parse_terms,
    replacements_between,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "srt_correct" / "srt_learn_terms.py"


def block(n, seconds, text):
    return textwrap.dedent(f"""\
        {n}
        00:00:{seconds:02d},000 --> 00:00:{seconds + 1:02d},000
        {text}
    """).strip()


def test_replacements_filter_noise():
    assert replacements_between("Alpha", "alpha") == []
    assert replacements_between("他要買", "她要買") == []
    assert replacements_between("你好，", "你好。") == []
    assert replacements_between("這是很長很長很長的錯字", "短") == []


def test_learn_terms_counts_and_already_flags(tmp_path):
    before = tmp_path / "2a.srt"
    after = tmp_path / "2c.srt"
    before.write_text(
        "\n\n".join(
            [
                block(1, 1, "我們看道球走勢"),
                block(2, 3, "道球今天很強"),
                block(3, 5, "這是升級型藥題材"),
                block(4, 7, "升級型藥很重要"),
                block(5, 9, "Alpha"),
                block(6, 11, "他要買"),
                block(7, 13, "你好，"),
                block(8, 15, "空投是訊號"),
            ]
        ),
        encoding="utf-8",
    )
    after.write_text(
        "\n\n".join(
            [
                block(1, 1, "我們看道瓊走勢"),
                block(2, 3, "道瓊今天很強"),
                block(3, 5, "這是生技新藥題材"),
                block(4, 7, "生技新藥很重要"),
                block(5, 9, "alpha"),
                block(6, 11, "她要買"),
                block(7, 13, "你好。"),
                block(8, 15, "空頭是訊號"),
            ]
        ),
        encoding="utf-8",
    )
    terms = tmp_path / "terms.txt"
    terms.write_text("道瓊（Dow Jones）\n錯詞→正詞\n", encoding="utf-8")
    preprocess = tmp_path / "srt_preprocess.py"
    preprocess.write_text(
        textwrap.dedent("""\
            AUTO_REPLACE_COMMON = [
                ("升級型藥", "生技新藥", [], []),
            ]
            AUTO_REPLACE_BREEZE = []
            PREFIX_REPLACE = [
                ("四指", "市值"),
            ]
        """),
        encoding="utf-8",
    )

    term_words, term_arrows = parse_terms(terms)
    preprocess_rules = parse_preprocess_rules(preprocess)
    candidates = learn_candidates([(before, after)], min_count=2)
    rows = enrich_candidates(candidates, term_words, term_arrows, preprocess_rules)
    by_pair = {(row["wrong"], row["correct"]): row for row in rows}

    assert set(by_pair) == {("道球", "道瓊"), ("升級型藥", "生技新藥")}
    assert by_pair[("道球", "道瓊")]["already_in_terms"] is True
    assert by_pair[("道球", "道瓊")]["already_in_preprocess"] is False
    assert by_pair[("升級型藥", "生技新藥")]["already_in_terms"] is False
    assert by_pair[("升級型藥", "生技新藥")]["already_in_preprocess"] is True
    assert by_pair[("升級型藥", "生技新藥")]["suggestion"] == "preprocess"


def test_learn_terms_cli_writes_json(tmp_path):
    before = tmp_path / "2a.srt"
    after = tmp_path / "2c.srt"
    before.write_text("\n\n".join([block(1, 1, "道球"), block(2, 3, "道球")]), encoding="utf-8")
    after.write_text("\n\n".join([block(1, 1, "道瓊"), block(2, 3, "道瓊")]), encoding="utf-8")
    terms = tmp_path / "terms.txt"
    terms.write_text("", encoding="utf-8")
    preprocess = tmp_path / "srt_preprocess.py"
    preprocess.write_text("AUTO_REPLACE_COMMON = []\nAUTO_REPLACE_BREEZE = []\nPREFIX_REPLACE = []\n", encoding="utf-8")
    out_json = tmp_path / "learned.json"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--pairs",
            f"{before}:{after}",
            "--terms",
            str(terms),
            "--preprocess",
            str(preprocess),
            "--json",
            str(out_json),
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    assert "道球→道瓊" in proc.stdout
    rows = json.loads(out_json.read_text(encoding="utf-8"))
    assert rows[0]["wrong"] == "道球"
    assert rows[0]["correct"] == "道瓊"
    assert rows[0]["count"] == 2
