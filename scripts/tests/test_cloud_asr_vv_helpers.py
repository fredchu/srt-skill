from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
CLOUD_ASR = REPO_ROOT / "scripts" / "cloud_asr.sh"


def load_helper_ns() -> dict[str, object]:
    source = CLOUD_ASR.read_text(encoding="utf-8")
    start = source.index("def detect_silence_ends(path: Path) -> list[float]:")
    end = source.index("if not torch.cuda.is_available()", start)
    snippet = (
        "from __future__ import annotations\n"
        "import math\n"
        "import re\n"
        "import subprocess\n"
        "from pathlib import Path\n\n"
        + source[start:end]
    )
    ns: dict[str, object] = {}
    exec(snippet, ns)
    return ns


def test_detect_silence_ends_regex_parses_real_tokens() -> None:
    ns = load_helper_ns()

    class FakeSubprocess:
        def run(self, *args, **kwargs):  # noqa: ANN001, ANN002
            return SimpleNamespace(
                stderr=(
                    "[silencedetect] silence_end: 12.345 | silence_duration: 0.5\n"
                    "silence_end: 67.89\n"
                    "silence_end: not-a-number\n"
                )
            )

    ns["subprocess"] = FakeSubprocess()
    detect_silence_ends = ns["detect_silence_ends"]

    assert detect_silence_ends(Path("dummy.wav")) == [12.345, 67.89]


def test_compact_text_removes_all_whitespace() -> None:
    ns = load_helper_ns()
    compact_text = ns["compact_text"]

    assert compact_text("  a\tb \n c\r\n") == "abc"


def test_109min_chunk_helper_multpart_offset_monotonic_and_boundary_dedup() -> None:
    ns = load_helper_ns()
    choose_cut_points = ns["choose_cut_points"]
    part_ranges = ns["part_ranges"]
    stitch_segments = ns["stitch_segments"]

    cuts = choose_cut_points(109 * 60, 3000, [2177.0, 2181.0, 3001.0, 4361.0, 5000.0])
    assert cuts == [2181.0, 4361.0]

    ranges = part_ranges(109 * 60, cuts)
    assert len(ranges) == 3
    assert ranges[0] == {"start": 0.0, "end": 2181.0, "dur": 2181.0}
    assert ranges[1] == {"start": 2181.0, "end": 4361.0, "dur": 2180.0}
    assert ranges[2] == {"start": 4361.0, "end": 6540, "dur": 2179.0}

    segments = [
        {"start": ranges[0]["end"] - 0.6, "end": ranges[0]["end"] - 0.1, "text": " tail "},
        {"start": ranges[1]["start"] + 0.1, "end": ranges[1]["start"] + 0.6, "text": "tail"},
        {"start": ranges[2]["start"] + 10.0, "end": ranges[2]["start"] + 11.0, "text": "after"},
    ]
    stitched = stitch_segments(sorted(segments, key=lambda item: (float(item["start"]), float(item["end"]))))

    assert len(stitched) == 2
    assert stitched[0]["start"] < stitched[0]["end"] < stitched[1]["start"]
    assert [item["start"] for item in stitched] == sorted(item["start"] for item in stitched)
    assert stitched[0]["text"].strip() == "tail"


def test_sparse_or_distant_silence_never_creates_overlong_part() -> None:
    ns = load_helper_ns()
    choose_cut_points = ns["choose_cut_points"]
    part_ranges = ns["part_ranges"]

    scenarios = [
        (6600.0, [100.0, 6000.0], [2200.0, 4400.0]),
        (6600.0, [50.0, 80.0, 120.0, 200.0], [2200.0, 4400.0]),
        (6600.0, [], [2200.0, 4400.0]),
        (6600.0, [1000.0, 2100.0, 3200.0, 4400.0, 5500.0], [2100.0, 4400.0]),
    ]
    for duration, silences, expected in scenarios:
        cuts = choose_cut_points(duration, 3000.0, silences)
        assert cuts == expected
        assert max(part["dur"] for part in part_ranges(duration, cuts)) <= 3600.0


def test_remote_runner_has_explicit_60_minute_part_guard() -> None:
    source = CLOUD_ASR.read_text(encoding="utf-8")
    assert 'float(part["dur"]) > 3600.0' in source
    assert "exceeds 60-minute limit" in source
    assert "cut_points={cut_points}" in source


def test_import_gate_is_pinned_to_transformers_516x_and_stdout_pass() -> None:
    source = CLOUD_ASR.read_text(encoding="utf-8")
    assert "transformers==5.16.1" in source
    assert 'expected transformers 5.16.x' in source
    assert "PASS AutoProcessor + VibeVoiceAsrForConditionalGeneration" in source
