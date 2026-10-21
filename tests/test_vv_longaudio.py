import json
import subprocess
import sys
from pathlib import Path

from vv_longaudio import choose_cut_points, merge_json_parts, part_ranges


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "vv_longaudio.py"


def test_choose_cut_points_uses_nearest_silence_to_ideal_points():
    cuts = choose_cut_points(
        total_sec=9000,
        max_part_sec=3000,
        silence_ends=[1000, 2800, 3200, 5900, 6100, 8500],
    )
    assert cuts == [2800, 5900]


def test_merge_json_parts_offsets_multiple_time_key_styles(tmp_path):
    first = tmp_path / "part1_vibevoice.json"
    second = tmp_path / "part2_vibevoice.json"
    first.write_text(
        json.dumps([{"Start": 1.0, "End": 2.5, "Content": "one"}]),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps({"segments": [{"start": 0.25, "end": 1.0, "content": "two"}]}),
        encoding="utf-8",
    )

    merged = merge_json_parts([first, second], offsets=[0, 3000])

    assert merged == [
        {"Start": 1.0, "End": 2.5, "Content": "one"},
        {"start": 3000.25, "end": 3001.0, "content": "two"},
    ]


def test_part_ranges_from_cut_points():
    assert part_ranges(10, [3, 7]) == [
        {"start": 0.0, "end": 3, "dur": 3.0},
        {"start": 3, "end": 7, "dur": 4},
        {"start": 7, "end": 10, "dur": 3},
    ]


def test_dry_run_prints_plan_without_vibevoice(tmp_path):
    media = tmp_path / "silent.wav"
    terms = tmp_path / "terms.txt"
    output = tmp_path / "merged.json"
    terms.write_text("NVDA\n", encoding="utf-8")
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            "1",
            str(media),
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(media),
            "--terms",
            str(terms),
            "--output-json",
            str(output),
            "--dry-run",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    summary = json.loads(proc.stdout.strip().splitlines()[-1])
    assert summary["parts"] == 1
    assert summary["cut_points"] == []
    assert summary["segments"] == 0
    assert summary["output_json"] == str(output)
    assert summary["plan"]["parts"][0]["dur"] > 0
    assert not output.exists()


def test_single_part_keeps_output_json_named_like_part_json(tmp_path, monkeypatch):
    import vv_longaudio

    media = tmp_path / "talk.mkv"
    media.write_bytes(b"")
    terms = tmp_path / "terms.txt"
    terms.write_text("NVDA\n", encoding="utf-8")
    output = tmp_path / "talk_vibevoice.json"

    def fake_run(vv_script, wav, terms, terms_max, output_srt, **kwargs):
        output.write_text(json.dumps([{"start": 0.0, "end": 1.0, "text": "hi"}]), encoding="utf-8")

    monkeypatch.setattr(vv_longaudio, "ffprobe_duration", lambda path: 120.0)
    monkeypatch.setattr(vv_longaudio, "detect_silence_ends", lambda path: [])
    monkeypatch.setattr(vv_longaudio, "run_vibevoice", fake_run)

    rc = vv_longaudio.main([
        str(media), "--terms", str(terms), "--output-json", str(output),
        "--lock-file", str(tmp_path / "gpu.lock"),
    ])

    assert rc == 0
    assert json.loads(output.read_text(encoding="utf-8"))[0]["text"] == "hi"
