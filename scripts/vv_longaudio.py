#!/usr/bin/env python3
"""
Split long media for VibeVoice ASR, run parts serially, and merge JSON/SRT output.
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import json
import math
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable


LONG_AUDIO_THRESHOLD_SEC = 55 * 60


def run_capture(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def ffprobe_duration(path: Path) -> float:
    proc = run_capture(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    return float(proc.stdout.strip())


def detect_silence_ends(path: Path) -> list[float]:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "silencedetect=noise=-30dB:d=0.5",
            "-f",
            "null",
            "-",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return [float(match.group(1)) for match in re.finditer(r"silence_end:\s*([0-9.]+)", proc.stderr)]


def choose_cut_points(total_sec: float, max_part_sec: float, silence_ends: Iterable[float]) -> list[float]:
    if total_sec <= max_part_sec:
        return []
    part_count = int(math.ceil(total_sec / max_part_sec))
    spacing = total_sec / part_count
    usable_silences = sorted({round(s, 6) for s in silence_ends if 0 < s < total_sec})
    cuts: list[float] = []
    for i in range(1, part_count):
        ideal = spacing * i
        if usable_silences:
            available = [s for s in usable_silences if s not in cuts]
            chosen = min(available or usable_silences, key=lambda s: (abs(s - ideal), s))
        else:
            chosen = ideal
        if 0 < chosen < total_sec and chosen not in cuts:
            cuts.append(chosen)
    return sorted(cuts)


def part_ranges(total_sec: float, cut_points: Iterable[float]) -> list[dict[str, float]]:
    points = [0.0, *sorted(cut_points), total_sec]
    return [
        {"start": points[i], "end": points[i + 1], "dur": points[i + 1] - points[i]}
        for i in range(len(points) - 1)
    ]


def cut_part(media_file: Path, output_wav: Path, start: float, duration: float) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(media_file),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(output_wav),
        ],
        check=True,
    )


@contextmanager
def gpu_lock(lock_file: Path):
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"waiting for GPU lock: {lock_file}", file=sys.stderr, flush=True)
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def run_vibevoice(vv_script: Path, wav: Path, terms: Path, terms_max: int, output_srt: Path,
                  slide_terms: Path | None = None, slide_terms_max: int = 25) -> None:
    cmd = [
        sys.executable,
        str(vv_script),
        str(wav),
        "--terms",
        str(terms),
        "--terms-max",
        str(terms_max),
    ]
    if slide_terms is not None:
        cmd += ["--slide-terms", str(slide_terms), "--slide-terms-max", str(slide_terms_max)]
    cmd += ["--json", "--output", str(output_srt)]
    subprocess.run(cmd, check=True)


def find_unique_part_json(part_wav: Path) -> Path:
    matches = [Path(p) for p in glob.glob(glob.escape(str(part_wav.with_suffix(""))) + "*_vibevoice.json")]
    if len(matches) != 1:
        raise RuntimeError(f"expected one JSON for {part_wav.name}, found {len(matches)}: {matches}")
    return matches[0]


def find_unique_json_for_stems(stems: Iterable[Path]) -> Path:
    matches: list[Path] = []
    for stem in stems:
        matches.extend(Path(p) for p in glob.glob(glob.escape(str(stem.with_suffix(""))) + "*_vibevoice.json"))
    unique = sorted(set(matches))
    if len(unique) != 1:
        raise RuntimeError(f"expected one VibeVoice JSON, found {len(unique)}: {unique}")
    return unique[0]


def _segments_from_payload(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("segments", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
    raise ValueError("unsupported VibeVoice JSON shape")


def _time_key(segment: dict[str, object], names: tuple[str, ...]) -> str:
    for name in names:
        if name in segment:
            return name
    raise KeyError(f"missing time key from {names}")


def _text_value(segment: dict[str, object]) -> str:
    for key in ("Content", "content", "text", "Text"):
        value = segment.get(key)
        if value is not None:
            return str(value)
    return ""


def merge_json_parts(part_json_paths: list[Path], offsets: list[float]) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    for path, offset in zip(part_json_paths, offsets):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for segment in _segments_from_payload(payload):
            start_key = _time_key(segment, ("Start", "start", "start_time"))
            end_key = _time_key(segment, ("End", "end", "end_time"))
            segment[start_key] = float(segment[start_key]) + offset
            segment[end_key] = float(segment[end_key]) + offset
            merged.append(segment)
    return merged


def seconds_to_srt_ts(seconds: float) -> str:
    ms_total = int(round(seconds * 1000))
    h = ms_total // 3600000
    ms_total %= 3600000
    m = ms_total // 60000
    ms_total %= 60000
    s = ms_total // 1000
    ms = ms_total % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments: list[dict[str, object]], path: Path) -> None:
    chunks: list[str] = []
    for index, segment in enumerate(segments, start=1):
        start_key = _time_key(segment, ("Start", "start", "start_time"))
        end_key = _time_key(segment, ("End", "end", "end_time"))
        chunks.append(
            "\n".join(
                [
                    str(index),
                    f"{seconds_to_srt_ts(float(segment[start_key]))} --> {seconds_to_srt_ts(float(segment[end_key]))}",
                    _text_value(segment),
                ]
            )
        )
    path.write_text("\n\n".join(chunks) + ("\n" if chunks else ""), encoding="utf-8")


def cleanup(paths: Iterable[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run VibeVoice ASR on long audio by splitting at silence.")
    parser.add_argument("media_file", type=Path)
    parser.add_argument("--terms", required=True, type=Path)
    parser.add_argument("--terms-max", type=int, default=50)
    parser.add_argument("--slide-terms", type=Path, default=None)
    parser.add_argument("--slide-terms-max", type=int, default=25)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--output-srt", type=Path)
    parser.add_argument("--max-part-sec", type=float, default=3000)
    parser.add_argument("--vv-script", type=Path, default=Path(os.environ.get("SRT_VV_SCRIPT", os.path.expanduser("~/dev/vibevoice-poc/vibevoice_asr.py"))))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lock-file", type=Path, default=Path("/tmp/srt_gpu.lock"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    total_sec = ffprobe_duration(args.media_file)
    silence_ends = detect_silence_ends(args.media_file)
    split_limit = args.max_part_sec if total_sec > LONG_AUDIO_THRESHOLD_SEC else max(args.max_part_sec, total_sec)
    cut_points = choose_cut_points(total_sec, split_limit, silence_ends)
    ranges = part_ranges(total_sec, cut_points)

    if args.dry_run:
        summary = {
            "parts": len(ranges),
            "cut_points": cut_points,
            "segments": 0,
            "output_json": str(args.output_json),
            "plan": {"parts": ranges},
        }
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    part_wavs: list[Path] = []
    part_srts: list[Path] = []
    part_jsons: list[Path] = []
    stem = args.media_file.with_suffix("")

    try:
        for index, part in enumerate(ranges, start=1):
            part_wav = stem.with_name(f"{stem.name}_vvpart{index}.wav")
            part_srt = stem.with_name(f"{stem.name}_vvpart{index}.srt")
            if len(ranges) == 1 and not cut_points:
                vv_input = args.media_file
            else:
                cut_part(args.media_file, part_wav, part["start"], part["dur"])
                part_wavs.append(part_wav)
                vv_input = part_wav
            part_srts.append(part_srt)
            with gpu_lock(args.lock_file):
                run_vibevoice(args.vv_script, vv_input, args.terms, args.terms_max, part_srt,
                              slide_terms=args.slide_terms, slide_terms_max=args.slide_terms_max)
            if vv_input == part_wav:
                part_jsons.append(find_unique_part_json(part_wav))
            else:
                part_jsons.append(find_unique_json_for_stems([args.media_file, part_srt]))

        offsets = [part["start"] for part in ranges]
        merged = merge_json_parts(part_jsons, offsets)
        args.output_json.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.output_srt:
            write_srt(merged, args.output_srt)
        summary = {
            "parts": len(ranges),
            "cut_points": cut_points,
            "segments": len(merged),
            "output_json": str(args.output_json),
        }
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    finally:
        cleanup([*part_wavs, *part_srts, *part_jsons])


if __name__ == "__main__":
    sys.exit(main())
