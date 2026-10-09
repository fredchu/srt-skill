import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "srt_correct" / "srt_prepare_review.py"


def srt(*entries):
    return "\n\n".join(f"{i}\n00:00:0{i},000 --> 00:00:0{i},900\n{t}" for i, t in enumerate(entries, 1)) + "\n"


def test_prepare_review_keeps_rules_in_main_and_terms_in_files(tmp_path):
    (tmp_path / "pre.srt").write_text(srt("機點", "不變一", "不變二"), encoding="utf-8")
    (tmp_path / "cor.srt").write_text(srt("基點", "不變一", "不變二"), encoding="utf-8")
    (tmp_path / "terms.txt").write_text("講者詞\n", encoding="utf-8")
    (tmp_path / "slide.txt").write_text("MA300DIST\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--workdir", str(work), "--preprocessed", str(tmp_path / "pre.srt"),
         "--corrected", str(tmp_path / "cor.srt"), "--terms", str(tmp_path / "terms.txt"),
         "--slide-terms", str(tmp_path / "slide.txt")],
        check=True, text=True, capture_output=True,
    )

    out = json.loads(proc.stdout)
    assert (out["unchanged"], out["review_segments"], out["examples"]) == (2, 1, 1)
    assert out["prompt_files"] == ["_review_prompt.txt", "_review_terms_1.txt"]
    main = (work / "_review_prompt.txt").read_text(encoding="utf-8")
    assert "機點 → 基點" in main and "## 輸出格式（嚴格）" in main and "講者詞" not in main
    terms = (work / "_review_terms_1.txt").read_text(encoding="utf-8")
    assert terms.index("講者詞") < terms.index("## 本集投影片術語") < terms.index("MA300DIST")
    assert (work / "_review_seg_0.srt").read_text(encoding="utf-8").count("-->") == 2
