import json
import subprocess
import sys
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "srt_correct" / "srt_merge_segments.py"


def ts(seconds):
    minutes, sec = divmod(seconds, 60)
    return f"00:{minutes:02d}:{sec:02d},000"


def block(n, start_s, end_s, *lines):
    text = "\n".join(lines)
    return textwrap.dedent(f"""\
        {n}
        {ts(start_s)} --> {ts(end_s)}
        {text}
    """).strip()


def write_srt(path, blocks):
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def run_merge(workdir, preprocessed, output, *extra_args):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--workdir",
            str(workdir),
            "--preprocessed",
            str(preprocessed),
            "--output",
            str(output),
            *extra_args,
        ],
        text=True,
        capture_output=True,
    )


def test_merge_round_trip_sorts_dedups_clamps_and_patches_gap(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    preprocessed = tmp_path / "pre.srt"
    write_srt(preprocessed, [
        block(1, 0, 2, "one"),
        block(2, 4, 6, "two"),
        block(3, 22, 24, "three"),
        block(4, 50, 52, "four"),
    ])
    write_srt(workdir / "_seg_0.srt", [
        block(1, 0, 2, "one"),
        block(2, 4, 6, "two"),
        block(3, 22, 24, "three"),
        block(4, 50, 52, "four"),
    ])
    write_srt(workdir / "_seg_0_corrected.srt", [
        block(4, 50, 52, "four corrected"),
        block(2, 4, 6, "two", "two!"),
        block(1, 0, 5, "one corrected"),
    ])
    output = tmp_path / "out.srt"

    proc = run_merge(workdir, preprocessed, output)

    assert proc.returncode == 0
    metrics = json.loads(proc.stdout)
    assert metrics["gate"] == "pass"
    assert metrics["patched"] == 1
    assert metrics["entries"] == 4
    assert metrics["warn_segments"][0]["n"] == 0

    result = output.read_text(encoding="utf-8")
    assert "1\n00:00:00,000 --> 00:00:04,000\none corrected" in result
    assert "2\n00:00:04,000 --> 00:00:06,000\ntwo!" in result
    assert "3\n00:00:22,000 --> 00:00:24,000\nthree" in result
    assert "4\n00:00:50,000 --> 00:00:52,000\nfour corrected" in result


def test_merge_gate_fails_on_low_ratio(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    preprocessed = tmp_path / "pre.srt"
    input_blocks = [block(i, i * 2, i * 2 + 1, f"in {i}") for i in range(30)]
    output_blocks = [block(i, i * 2, i * 2 + 1, f"out {i}") for i in range(10)]
    write_srt(preprocessed, input_blocks)
    write_srt(workdir / "_seg_0.srt", input_blocks)
    write_srt(workdir / "_seg_0_corrected.srt", output_blocks)
    output = tmp_path / "out.srt"

    proc = run_merge(workdir, preprocessed, output)

    assert proc.returncode == 2
    metrics = json.loads(proc.stdout)
    assert metrics["gate"] == "fail"
    assert metrics["failed_segments"][0]["ratio"] == 0.333333
    assert not output.exists()


def test_merge_gate_fails_on_warn_ratio_with_long_duration(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    preprocessed = tmp_path / "pre.srt"
    input_blocks = [block(i, i * 3, i * 3 + 1, f"in {i}") for i in range(10)]
    output_blocks = [block(i, i * 3, i * 3 + (20 if i == 0 else 1), f"out {i}") for i in range(7)]
    write_srt(preprocessed, input_blocks)
    write_srt(workdir / "_seg_0.srt", input_blocks)
    write_srt(workdir / "_seg_0_corrected.srt", output_blocks)
    output = tmp_path / "out.srt"

    proc = run_merge(workdir, preprocessed, output)

    assert proc.returncode == 2
    metrics = json.loads(proc.stdout)
    assert metrics["gate"] == "fail"
    assert metrics["failed_segments"][0]["ratio"] == 0.7
    assert metrics["failed_segments"][0]["max_dur_sec"] == 20.0
    assert not output.exists()


def test_merge_gate_warn_ratio_passes_without_long_duration(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    preprocessed = tmp_path / "pre.srt"
    input_blocks = [block(i, i * 11, i * 11 + 10, f"in {i}") for i in range(10)]
    output_blocks = [block(i, i * 11, i * 11 + 10, f"out {i}") for i in range(7)]
    write_srt(preprocessed, input_blocks)
    write_srt(workdir / "_seg_0.srt", input_blocks)
    write_srt(workdir / "_seg_0_corrected.srt", output_blocks)
    output = tmp_path / "out.srt"

    proc = run_merge(workdir, preprocessed, output)

    assert proc.returncode == 0
    metrics = json.loads(proc.stdout)
    assert metrics["gate"] == "pass"
    assert metrics["warn_segments"][0]["ratio"] == 0.7
    assert metrics["warn_segments"][0]["max_dur_sec"] == 10.0
    assert output.exists()
