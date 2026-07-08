#!/usr/bin/env python3

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_stage_artifacts.py"


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def valid_srt() -> str:
    return "1\n00:00:01,000 --> 00:00:02,000\nhello\n\n"


def run_check(marker: Path, *artifacts: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--marker", str(marker), *artifacts],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc, json.loads(proc.stdout)


def test_ready_srt_and_vv_json_newer_than_marker() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        marker = write(root / ".launch", "start")
        time.sleep(0.01)
        srt = write(root / "asr.srt", valid_srt())
        vv = write(root / "vv.json", '[{"Start": 1, "End": 2, "Content": "hello"}]')

        proc, payload = run_check(marker, f"srt:{srt}", f"vv-json:{vv}")

        assert proc.returncode == 0, proc.stderr
        assert payload["all_ready"] is True
        assert [a["status"] for a in payload["artifacts"]] == ["ready", "ready"]


def test_empty_or_zero_cue_srt_is_invalid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        marker = write(root / ".launch", "start")
        time.sleep(0.01)
        empty = write(root / "empty.srt", "")
        zero_cue = write(root / "zero.srt", "1\nhello\n\n")

        proc, payload = run_check(marker, f"srt:{empty}", f"srt:{zero_cue}")

        assert proc.returncode != 0
        assert payload["all_ready"] is False
        assert [a["status"] for a in payload["artifacts"]] == ["invalid", "invalid"]
        assert [a["reason"] for a in payload["artifacts"]] == ["empty_file", "no_valid_srt_cue"]


def test_srt_rejects_out_of_range_timestamps() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        marker = write(root / ".launch", "start")
        time.sleep(0.01)
        bad_minute = write(root / "bad_minute.srt", "1\n00:99:01,000 --> 00:00:02,000\nhello\n\n")
        bad_second = write(root / "bad_second.srt", "1\n00:00:99,000 --> 00:01:02,000\nhello\n\n")
        bad_ms = write(root / "bad_ms.srt", "1\n00:00:01,1000 --> 00:00:02,000\nhello\n\n")

        proc, payload = run_check(marker, f"srt:{bad_minute}", f"srt:{bad_second}", f"srt:{bad_ms}")

        assert proc.returncode != 0
        assert [a["status"] for a in payload["artifacts"]] == ["invalid", "invalid", "invalid"]
        assert [a["reason"] for a in payload["artifacts"]] == ["no_valid_srt_cue"] * 3


def test_srt_rejects_non_increasing_or_textless_cues() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        marker = write(root / ".launch", "start")
        time.sleep(0.01)
        backwards = write(root / "backwards.srt", "1\n00:00:02,000 --> 00:00:01,000\nhello\n\n")
        equal = write(root / "equal.srt", "1\n00:00:01,000 --> 00:00:01,000\nhello\n\n")
        textless = write(root / "textless.srt", "1\n00:00:01,000 --> 00:00:02,000\n\n")

        proc, payload = run_check(marker, f"srt:{backwards}", f"srt:{equal}", f"srt:{textless}")

        assert proc.returncode != 0
        assert [a["status"] for a in payload["artifacts"]] == ["invalid", "invalid", "invalid"]
        assert [a["reason"] for a in payload["artifacts"]] == ["no_valid_srt_cue"] * 3


def test_srt_allows_trailing_garbage_after_valid_cue() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        marker = write(root / ".launch", "start")
        time.sleep(0.01)
        srt = write(root / "with_garbage.srt", valid_srt() + "\nnot a cue\njust trailing garbage\n")

        proc, payload = run_check(marker, f"srt:{srt}")

        assert proc.returncode == 0, proc.stderr
        assert payload["all_ready"] is True
        assert payload["artifacts"][0]["status"] == "ready"
        assert payload["artifacts"][0]["count"] == 1


def test_bad_json_or_empty_vv_segments_are_invalid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        marker = write(root / ".launch", "start")
        time.sleep(0.01)
        bad = write(root / "bad.json", "{")
        empty = write(root / "empty.json", "[]")

        proc, payload = run_check(marker, f"vv-json:{bad}", f"vv-json:{empty}")

        assert proc.returncode != 0
        assert [a["status"] for a in payload["artifacts"]] == ["invalid", "invalid"]
        assert [a["reason"] for a in payload["artifacts"]] == ["json_parse_failed", "no_segments"]


def test_vv_json_allows_metadata_rows_but_rejects_malformed_speech() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        marker = write(root / ".launch", "start")
        time.sleep(0.01)
        with_metadata = write(
            root / "with_metadata.json",
            json.dumps([{"Start": 1, "End": 2, "Content": "hello"}, {"model": "vv", "duration": 12.3}]),
        )
        malformed_speech = write(
            root / "malformed_speech.json",
            json.dumps([{"Start": 1, "End": 2, "Content": "hello"}, {"type": "speech", "End": 3, "Content": "missing start"}]),
        )

        ready_proc, ready_payload = run_check(marker, f"vv-json:{with_metadata}")
        bad_proc, bad_payload = run_check(marker, f"vv-json:{malformed_speech}")

        assert ready_proc.returncode == 0, ready_proc.stderr
        assert ready_payload["all_ready"] is True
        assert ready_payload["artifacts"][0]["status"] == "ready"
        assert ready_payload["artifacts"][0]["count"] == 1
        assert bad_proc.returncode != 0
        assert bad_payload["artifacts"][0]["status"] == "invalid"
        assert bad_payload["artifacts"][0]["reason"] == "segment_missing_start_end_text"


def test_stale_artifact_older_than_marker() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        srt = write(root / "asr.srt", valid_srt())
        time.sleep(0.01)
        marker = write(root / ".launch", "start")

        proc, payload = run_check(marker, f"srt:{srt}")

        assert proc.returncode != 0
        assert payload["all_ready"] is False
        assert payload["artifacts"][0]["status"] == "stale"


def test_missing_artifact_is_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        marker = write(Path(tmp) / ".launch", "start")

        proc, payload = run_check(marker, f"srt:{Path(tmp) / 'missing.srt'}")

        assert proc.returncode != 0
        assert payload["all_ready"] is False
        assert payload["artifacts"][0]["status"] == "missing"


def test_captions_json_existing_shape_is_ready() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        marker = write(root / ".launch", "start")
        time.sleep(0.01)
        captions = write(root / "caps.json", '[{"time_s": 1.5, "caption": "slide", "terms": []}]')

        proc, payload = run_check(marker, f"captions-json:{captions}")

        assert proc.returncode == 0, proc.stderr
        assert payload["all_ready"] is True
        assert payload["artifacts"][0]["status"] == "ready"


def test_captions_json_requires_nonempty_caption_string_and_list_terms_when_present() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        marker = write(root / ".launch", "start")
        time.sleep(0.01)
        missing_terms = write(root / "missing_terms.json", '[{"time_s": 1.5, "caption": "slide"}]')
        empty_caption = write(root / "empty_caption.json", '[{"time_s": 1.5, "caption": "", "terms": []}]')
        non_string_caption = write(root / "non_string_caption.json", '[{"time_s": 1.5, "caption": 7, "terms": []}]')
        bad_terms = write(root / "bad_terms.json", '[{"time_s": 1.5, "caption": "slide", "terms": "term"}]')

        ready_proc, ready_payload = run_check(marker, f"captions-json:{missing_terms}")
        bad_proc, bad_payload = run_check(
            marker,
            f"captions-json:{empty_caption}",
            f"captions-json:{non_string_caption}",
            f"captions-json:{bad_terms}",
        )

        assert ready_proc.returncode == 0, ready_proc.stderr
        assert ready_payload["artifacts"][0]["status"] == "ready"
        assert bad_proc.returncode != 0
        assert [a["status"] for a in bad_payload["artifacts"]] == ["invalid", "invalid", "invalid"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
