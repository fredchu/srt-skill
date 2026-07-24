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


def run_single_segment(base, input_blocks, output_blocks, *extra_args, pre_blocks=None):
    workdir = base / "work"
    workdir.mkdir()
    preprocessed = base / "pre.srt"
    write_srt(preprocessed, pre_blocks or input_blocks)
    write_srt(workdir / "_seg_0.srt", input_blocks)
    write_srt(workdir / "_seg_0_corrected.srt", output_blocks)
    output = base / "out.srt"
    return run_merge(workdir, preprocessed, output, *extra_args), output


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
    assert metrics["failed_segments"][0]["reasons"] == ["ratio"]
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


def test_gate_rejects_new_zero_duration_with_example(tmp_path):
    proc, output = run_single_segment(
        tmp_path,
        [block(1, 0, 2, "原始時間軸內容")],
        [block(1, 1, 1, "校正後內容")],
    )

    assert proc.returncode == 2
    item = json.loads(proc.stdout)["failed_segments"][0]
    assert item["reasons"] == ["zero_duration"]
    assert item["zero_dur_examples"] == ["00:00:01,000 --> 00:00:01,000"]
    assert not output.exists()


def test_gate_allows_preserved_input_zero_duration_and_reports_merged_metric(tmp_path):
    blocks = [block(1, 1, 1, "原始零時長條目")]
    proc, output = run_single_segment(tmp_path, blocks, blocks)

    assert proc.returncode == 0
    metrics = json.loads(proc.stdout)
    assert metrics["merged_zero_dur_count"] == 1
    assert "WARNING:" in proc.stderr
    assert output.exists()


def test_gate_rejects_two_new_nonadjacent_duplicate_keys(tmp_path):
    duplicate_a = "這是一段足夠長度的甲句內容"
    duplicate_b = "這是一段足夠長度的乙句內容"
    input_blocks = [block(i, i, i + 1, f"原始唯一內容第{i}句") for i in range(5)]
    output_blocks = [
        block(1, 0, 1, duplicate_a),
        block(2, 1, 2, duplicate_b),
        block(3, 2, 3, "中間填充的唯一內容"),
        block(4, 3, 4, duplicate_a),
        block(5, 4, 5, duplicate_b),
    ]

    proc, _ = run_single_segment(tmp_path, input_blocks, output_blocks)

    assert proc.returncode == 2
    item = json.loads(proc.stdout)["failed_segments"][0]
    assert item["reasons"] == ["dup_text"]
    assert item["dup_examples"] == [duplicate_a, duplicate_b]


def test_gate_warns_for_only_one_new_duplicate_key(tmp_path):
    duplicate = "只有一組新增重複文字不直接失敗"
    proc, output = run_single_segment(
        tmp_path,
        [block(i, i, i + 1, f"原始唯一內容第{i}句") for i in range(3)],
        [
            block(1, 0, 1, duplicate),
            block(2, 1, 2, "中間填充的唯一內容"),
            block(3, 2, 3, duplicate),
        ],
    )

    assert proc.returncode == 0
    item = json.loads(proc.stdout)["warn_segments"][0]
    assert item["dup_warn"] == [duplicate]
    assert output.exists()


def test_gate_allows_adjacent_same_segment_duplicate(tmp_path):
    duplicate = "相鄰的強調性重複應該被豁免"
    proc, _ = run_single_segment(
        tmp_path,
        [block(i, i, i + 1, f"原始唯一內容第{i}句") for i in range(3)],
        [
            block(1, 0, 1, duplicate),
            block(2, 1, 2, duplicate),
            block(3, 2, 3, "後續唯一內容足夠長"),
        ],
    )

    assert proc.returncode == 0
    metrics = json.loads(proc.stdout)
    assert metrics["warn_segments"] == []
    assert metrics["cross_dup_count"] == 0


def test_gate_allows_duplicate_already_present_in_input(tmp_path):
    duplicate = "講者原本就有重說的完整長句內容"
    blocks = [
        block(1, 0, 1, duplicate),
        block(2, 1, 2, "中間填充的唯一內容"),
        block(3, 2, 3, duplicate),
    ]

    proc, _ = run_single_segment(tmp_path, blocks, blocks)

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["warn_segments"] == []


def test_gate_ignores_short_duplicate_text(tmp_path):
    proc, _ = run_single_segment(
        tmp_path,
        [block(i, i, i + 1, f"原始唯一內容第{i}句") for i in range(3)],
        [
            block(1, 0, 1, "太短了"),
            block(2, 1, 2, "中間填充的唯一內容"),
            block(3, 2, 3, "太短了"),
        ],
    )

    assert proc.returncode == 0
    metrics = json.loads(proc.stdout)
    assert metrics["warn_segments"] == []
    assert metrics["cross_dup_count"] == 0


def test_zero_min_chars_disables_duplicate_gate_and_metric(tmp_path):
    duplicate_a = "停用時第一組重複長句內容"
    duplicate_b = "停用時第二組重複長句內容"
    proc, _ = run_single_segment(
        tmp_path,
        [block(i, i, i + 1, f"原始唯一內容第{i}句") for i in range(5)],
        [
            block(1, 0, 1, duplicate_a),
            block(2, 1, 2, duplicate_b),
            block(3, 2, 3, "中間填充的唯一內容"),
            block(4, 3, 4, duplicate_a),
            block(5, 4, 5, duplicate_b),
        ],
        "--gate-dup-min-chars",
        "0",
    )

    assert proc.returncode == 0
    metrics = json.loads(proc.stdout)
    assert metrics["warn_segments"] == []
    assert metrics["cross_dup_count"] == 0


def test_gate_uses_timeline_order_for_duplicate_distance(tmp_path):
    duplicate_a = "亂序測試第一組重複長句內容"
    duplicate_b = "亂序測試第二組重複長句內容"
    output_blocks = [
        block(1, 0, 1, duplicate_a),
        block(2, 3, 4, duplicate_a),
        block(3, 1, 2, duplicate_b),
        block(4, 4, 5, duplicate_b),
        block(5, 2, 3, "中間填充的唯一內容"),
    ]

    proc, _ = run_single_segment(
        tmp_path,
        [block(i, i, i + 1, f"原始唯一內容第{i}句") for i in range(5)],
        output_blocks,
    )

    assert proc.returncode == 2
    assert json.loads(proc.stdout)["failed_segments"][0]["reasons"] == ["dup_text"]


def test_gate_normalizes_multiline_entry_text(tmp_path):
    proc, _ = run_single_segment(
        tmp_path,
        [block(i, i, i + 1, f"原始唯一內容第{i}句") for i in range(3)],
        [
            block(1, 0, 1, "多行重複文字", "合併後足夠長度"),
            block(2, 1, 2, "中間填充的唯一內容"),
            block(3, 2, 3, "多行重複文字", "合併後足夠長度"),
        ],
        "--gate-dup-min-texts",
        "1",
    )

    assert proc.returncode == 2
    item = json.loads(proc.stdout)["failed_segments"][0]
    assert item["dup_examples"] == ["多行重複文字\n合併後足夠長度"]


def test_gate_aggregates_ratio_zero_and_duplicate_reasons_in_fixed_order(tmp_path):
    duplicate_a = "聚合測試第一組重複長句內容"
    duplicate_b = "聚合測試第二組重複長句內容"
    proc, _ = run_single_segment(
        tmp_path,
        [block(i, i, i + 1, f"原始唯一內容第{i}句") for i in range(10)],
        [
            block(1, 0, 1, duplicate_a),
            block(2, 1, 2, duplicate_b),
            block(3, 2, 2, "新增的零時長內容"),
            block(4, 3, 4, duplicate_a),
            block(5, 4, 5, duplicate_b),
        ],
    )

    assert proc.returncode == 2
    item = json.loads(proc.stdout)["failed_segments"][0]
    assert item["reasons"] == ["ratio", "zero_duration", "dup_text"]


def test_cross_segment_adjacent_duplicate_is_metric_only(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    duplicate = "跨段緊鄰重複必須納入觀測指標"
    first = [block(1, 0, 1, duplicate)]
    second = [block(1, 1, 2, duplicate)]
    write_srt(workdir / "_seg_0.srt", first)
    write_srt(workdir / "_seg_0_corrected.srt", first)
    write_srt(workdir / "_seg_1.srt", second)
    write_srt(workdir / "_seg_1_corrected.srt", second)
    preprocessed = tmp_path / "pre.srt"
    write_srt(preprocessed, first + second)

    proc = run_merge(workdir, preprocessed, tmp_path / "out.srt")

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["cross_dup_count"] == 1
    assert proc.stderr.count("WARNING:") == 1


def test_duplicate_cli_bounds_are_rejected_by_argparse(tmp_path):
    input_blocks = [block(1, 0, 1, "唯一內容")]
    for i, args in enumerate((
        ("--gate-dup-min-chars", "-1"),
        ("--gate-dup-min-texts", "0"),
    )):
        base = tmp_path / str(i)
        base.mkdir()
        proc, _ = run_single_segment(base, input_blocks, input_blocks, *args)
        assert proc.returncode == 2
        assert "usage:" in proc.stderr
        assert proc.stdout == ""


def test_duplicate_normalization_includes_ten_char_boundary_and_ignores_punctuation(tmp_path):
    cases = [
        ("一二三四五六七八九十", "一二三四五六七八九十"),
        # Punctuation-only differences intentionally normalize to one dup key.
        ("這句話真的已經好了嗎？", "這句話真的已經好了嗎。"),
    ]
    for i, (left, right) in enumerate(cases):
        base = tmp_path / str(i)
        base.mkdir()
        proc, _ = run_single_segment(
            base,
            [block(n, n, n + 1, f"原始唯一內容第{n}句") for n in range(3)],
            [
                block(1, 0, 1, left),
                block(2, 1, 2, "中間填充的唯一內容"),
                block(3, 2, 3, right),
            ],
            "--gate-dup-min-texts",
            "1",
        )
        assert proc.returncode == 2
        assert json.loads(proc.stdout)["failed_segments"][0]["reasons"] == ["dup_text"]


def test_zero_duration_baseline_uses_interval_multiset_not_start_allowlist(tmp_path):
    proc, _ = run_single_segment(
        tmp_path,
        [block(1, 1, 1, "原始零時長條目")],
        [
            block(1, 1, 1, "原始零時長條目"),
            block(2, 1, 0, "同起點但新造不同終點"),
        ],
    )

    assert proc.returncode == 2
    item = json.loads(proc.stdout)["failed_segments"][0]
    assert item["reasons"] == ["zero_duration"]
    assert item["zero_dur_examples"] == ["00:00:01,000 --> 00:00:00,000"]


def test_zero_duration_baseline_counts_identical_interval_multiplicity(tmp_path):
    proc, _ = run_single_segment(
        tmp_path,
        [block(1, 1, 1, "原始零時長條目")],
        [
            block(1, 1, 1, "原始零時長條目"),
            block(2, 1, 1, "第二份相同區間零時長"),
        ],
    )

    assert proc.returncode == 2
    item = json.loads(proc.stdout)["failed_segments"][0]
    assert item["reasons"] == ["zero_duration"]
    assert item["zero_dur_examples"] == ["00:00:01,000 --> 00:00:01,000"]


def test_duplicate_baseline_preserves_per_key_multiplicity(tmp_path):
    duplicate = "重數基準測試使用的完整長句內容"

    def interleaved(copies):
        blocks = []
        for n in range(copies):
            blocks.append(block(len(blocks) + 1, n * 4, n * 4 + 1, duplicate))
            blocks.append(block(len(blocks) + 1, n * 4 + 2, n * 4 + 3, f"唯一填充內容編號{n}足夠長"))
        return blocks

    variants = [
        (2, 4, 2),
        (0, 3, 2),
        (2, 2, 0),
    ]
    for i, (input_copies, output_copies, expected_code) in enumerate(variants):
        base = tmp_path / str(i)
        base.mkdir()
        input_blocks = interleaved(input_copies)
        output_blocks = interleaved(output_copies)
        if input_copies == 0:
            input_blocks = [block(1, 10, 11, "原始唯一內容足夠長度")]
        proc, _ = run_single_segment(base, input_blocks, output_blocks)
        assert proc.returncode == expected_code
        if expected_code == 2:
            assert json.loads(proc.stdout)["failed_segments"][0]["reasons"] == ["dup_text"]
        else:
            assert json.loads(proc.stdout)["warn_segments"] == []


def test_cross_duplicate_adjacent_patch_entries_are_not_same_segment_exempt(tmp_path):
    duplicate = "相鄰補丁條目的重複必須被觀測"
    pre_blocks = [
        block(1, 0, 1, "前方原始唯一內容"),
        block(2, 10, 11, duplicate),
        block(3, 20, 21, duplicate),
        block(4, 40, 41, "後方原始唯一內容"),
    ]
    proc, _ = run_single_segment(
        tmp_path,
        [pre_blocks[0], pre_blocks[3]],
        [pre_blocks[0], pre_blocks[3]],
        pre_blocks=pre_blocks,
    )

    assert proc.returncode == 0
    metrics = json.loads(proc.stdout)
    assert metrics["patched"] == 2
    assert metrics["cross_dup_count"] == 1
