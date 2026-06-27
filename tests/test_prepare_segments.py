import json
import subprocess
import sys
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "srt_correct" / "srt_prepare_segments.py"


def srt_block(n, start_s, end_s, text):
    start_m, start_sec = divmod(start_s, 60)
    end_m, end_sec = divmod(end_s, 60)
    return textwrap.dedent(f"""\
        {n}
        00:{start_m:02d}:{start_sec:02d},000 --> 00:{end_m:02d}:{end_sec:02d},000
        {text}
    """).strip()


def test_prepare_segments_writes_segments_context_vv_and_prompt(tmp_path):
    srt = tmp_path / "preprocessed.srt"
    srt.write_text(
        "\n\n".join(
            srt_block(i, (i - 1) * 10, (i - 1) * 10 + 2, f"字幕{i}")
            for i in range(1, 9)
        ),
        encoding="utf-8",
    )
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("PROMPT\n{{TERMINOLOGY_SECTION}}\nEND", encoding="utf-8")
    terms = tmp_path / "terms.txt"
    terms.write_text("MOAT\n", encoding="utf-8")
    vv_json = tmp_path / "vv.json"
    vv_json.write_text(
        json.dumps([
            {"Start": 1, "End": 1.5, "Content": "Alpha reference"},
            {"Start": 35, "End": 36, "Content": "[Silence]"},
            {"Start": 45, "End": 46, "Content": "Beta reference"},
        ]),
        encoding="utf-8",
    )
    workdir = tmp_path / "work"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(srt),
            "--workdir",
            str(workdir),
            "--prompt-template",
            str(prompt),
            "--terms",
            str(terms),
            "--vv-json",
            str(vv_json),
            "--seg-size",
            "3",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    metrics = json.loads(proc.stdout)
    assert metrics["total_blocks"] == 8
    assert metrics["segments"] == [3, 3, 2]
    assert metrics["vv_segments"] == 3
    assert metrics["captions"] == 0
    assert metrics["strategy"] == "fixed"
    assert "tokenizer" in metrics
    assert len(metrics["segment_tokens"]) == len(metrics["segments"])
    assert (workdir / "_seg_0.srt").read_text(encoding="utf-8").count("-->") == 3
    assert (workdir / "_seg_1.srt").read_text(encoding="utf-8").count("-->") == 3
    assert (workdir / "_seg_2.srt").read_text(encoding="utf-8").count("-->") == 2
    assert (workdir / "_ctx_1.txt").read_text(encoding="utf-8").splitlines() == ["字幕1", "字幕2", "字幕3"]
    assert "Alpha reference" in (workdir / "_vv_ref_0.txt").read_text(encoding="utf-8")
    assert "Beta reference" in (workdir / "_vv_ref_1.txt").read_text(encoding="utf-8")
    assert (workdir / "_vv_ref_2.txt").read_text(encoding="utf-8") == "NO_VV_REFERENCE"
    system_prompt = (workdir / "_system_prompt.txt").read_text(encoding="utf-8")
    assert "MOAT" in system_prompt
    assert "## 交叉參考：VibeVoice ASR" in system_prompt


def test_prepare_segments_writes_caption_reference(tmp_path):
    srt = tmp_path / "preprocessed.srt"
    srt.write_text(
        "\n\n".join([
            srt_block(1, 0, 2, "字幕1"),
            srt_block(2, 80, 82, "字幕2"),
        ]),
        encoding="utf-8",
    )
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("{{TERMINOLOGY_SECTION}}", encoding="utf-8")
    terms = tmp_path / "terms.txt"
    terms.write_text("NVDA\n", encoding="utf-8")
    captions = tmp_path / "captions.json"
    captions.write_text(
        json.dumps([
            {"time_s": 12, "caption": "投影片顯示 NVDA", "terms": ["NVDA"]},
        ]),
        encoding="utf-8",
    )
    workdir = tmp_path / "work"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(srt),
            "--workdir",
            str(workdir),
            "--prompt-template",
            str(prompt),
            "--terms",
            str(terms),
            "--captions-json",
            str(captions),
            "--seg-size",
            "1",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    assert json.loads(proc.stdout)["captions"] == 1
    cap_ref = (workdir / "_caption_ref_0.txt").read_text(encoding="utf-8")
    assert "[00:12] 投影片顯示 NVDA" in cap_ref
    assert "術語: NVDA" in cap_ref
    assert (workdir / "_caption_ref_1.txt").read_text(encoding="utf-8") == "NO_CAPTIONS"
    assert "## 畫面截圖描述（帶時間戳）" in (workdir / "_system_prompt.txt").read_text(encoding="utf-8")
