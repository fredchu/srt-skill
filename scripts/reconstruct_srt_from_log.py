#!/usr/bin/env python3
"""Rebuild SRT from mlx_whisper verbose stdout."""

from __future__ import annotations

import re
import sys
from pathlib import Path


TIMESTAMPED_LINE_RE = re.compile(
    r"^\s*\[(?P<start>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\]\s*(?P<text>.*)$"
)
TRACEBACK_MARKER = "Traceback (most recent call last)"
TRACEBACK_TAIL_RE = re.compile(re.escape(TRACEBACK_MARKER) + r"\s*:")
MUSIC_ONLY_RE = re.compile(r"^[♪\s]+$")


def srt_time(ts: str) -> str:
    parts = ts.replace(",", ".").split(":")
    if len(parts) == 2:
        hours = "00"
        minutes, seconds = parts
    else:
        hours, minutes, seconds = parts
    return f"{int(hours):02d}:{int(minutes):02d}:{seconds.replace('.', ',')}"


def parse(text: str) -> list[tuple[str, str, str]]:
    cues = []
    # ponytail: mlx_whisper verbose currently emits one complete segment per
    # timestamped line; if that changes, add explicit continuation markers.
    for line in text.splitlines():
        match = TIMESTAMPED_LINE_RE.match(line)
        if not match:
            continue
        cue_text = match.group("text")
        traceback_match = TRACEBACK_TAIL_RE.search(cue_text)
        if traceback_match:
            cue_text = cue_text[: traceback_match.start()]
        cue_text = cue_text.strip()
        if not cue_text or MUSIC_ONLY_RE.fullmatch(cue_text):
            continue
        cues.append((srt_time(match.group("start")), srt_time(match.group("end")), cue_text))
    return cues


def write_srt(cues: list[tuple[str, str, str]], path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        for index, (start, end, text) in enumerate(cues, 1):
            f.write(f"{index}\n{start} --> {end}\n{text}\n\n")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: reconstruct_srt_from_log.py <mlx_stdout_log> <output_srt>", file=sys.stderr)
        return 2

    input_path = Path(argv[0])
    output_path = Path(argv[1])
    try:
        cues = parse(input_path.read_text(encoding="utf-8-sig", errors="replace"))
        write_srt(cues, output_path)
    except OSError as exc:
        print(f"reconstruct_srt_from_log.py: {exc}", file=sys.stderr)
        return 2

    return 0 if cues else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
