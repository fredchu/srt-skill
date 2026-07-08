#!/usr/bin/env python3
"""Single-shot strict artifact readiness check for background SRT stages."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


SRT_TIMING = re.compile(
    r"^(?P<sh>[0-9]{2}):(?P<sm>[0-9]{2}):(?P<ss>[0-9]{2}),(?P<sms>[0-9]{3}) --> "
    r"(?P<eh>[0-9]{2}):(?P<em>[0-9]{2}):(?P<es>[0-9]{2}),(?P<ems>[0-9]{3})$"
)
KINDS = {"srt", "vv-json", "vv_json", "captions-json", "captions_json"}
START_KEYS = ("Start", "start", "start_time")
END_KEYS = ("End", "end", "end_time")
TEXT_KEYS = ("Content", "content", "Text", "text")


def newer_than_marker(path: Path, marker: Path) -> bool:
    return marker.exists() and path.stat().st_mtime > marker.stat().st_mtime


def valid_srt(path: Path) -> tuple[bool, str, int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    count = 0
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines()]
        timing_index = 1 if len(lines) > 1 and lines[0].isdigit() else 0
        if not lines or "-->" not in "\n".join(lines):
            continue
        if timing_index >= len(lines):
            return False, "no_valid_srt_cue", count
        match = SRT_TIMING.fullmatch(lines[timing_index])
        if not match:
            return False, "no_valid_srt_cue", count
        parts = {key: int(value) for key, value in match.groupdict().items()}
        if any(parts[key] >= 60 for key in ("sm", "ss", "em", "es")) or any(parts[key] >= 1000 for key in ("sms", "ems")):
            return False, "no_valid_srt_cue", count
        start = ((parts["sh"] * 60 + parts["sm"]) * 60 + parts["ss"]) * 1000 + parts["sms"]
        end = ((parts["eh"] * 60 + parts["em"]) * 60 + parts["es"]) * 1000 + parts["ems"]
        if end <= start or not any(line for line in lines[timing_index + 1 :]):
            return False, "no_valid_srt_cue", count
        count += 1
    return count > 0, "ok" if count else "no_valid_srt_cue", count


def as_segments(payload: object) -> list[object]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("segments", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return [payload]
    return []


def get_any(item: dict[str, object], names: tuple[str, ...]) -> object:
    for name in names:
        if name in item:
            return item[name]
    return None


def usable_segment(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    start = get_any(item, START_KEYS)
    end = get_any(item, END_KEYS)
    text = get_any(item, TEXT_KEYS)
    try:
        float(start)  # type: ignore[arg-type]
        float(end)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return bool(str(text or "").strip())


def declares_speech(item: dict[str, object]) -> bool:
    kind = get_any(item, ("type", "kind", "row_type", "category"))
    return isinstance(kind, str) and kind.lower() in {"speech", "segment", "transcript", "utterance"}


def valid_vv_json(path: Path) -> tuple[bool, str, int]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "json_parse_failed", 0
    segments = as_segments(payload)
    if not segments:
        return False, "no_segments", 0
    usable_count = 0
    for item in segments:
        if not isinstance(item, dict):
            return False, "segment_missing_start_end_text", len(segments)
        has_text_field = any(key in item for key in TEXT_KEYS)
        if not has_text_field and not declares_speech(item):
            continue
        if not usable_segment(item):
            return False, "segment_missing_start_end_text", len(segments)
        usable_count += 1
    if usable_count == 0:
        return False, "no_segments", 0
    return True, "ok", usable_count


def usable_caption(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    if "time_s" not in item or "caption" not in item:
        return False
    try:
        float(item["time_s"])
    except (TypeError, ValueError):
        return False
    caption = item["caption"]
    if not isinstance(caption, str) or not caption.strip():
        return False
    return "terms" not in item or isinstance(item["terms"], list)


def valid_captions_json(path: Path) -> tuple[bool, str, int]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "json_parse_failed", 0
    captions = payload.get("captions") if isinstance(payload, dict) else payload
    if not isinstance(captions, list) or not captions:
        return False, "no_captions", 0
    if not all(usable_caption(item) for item in captions):
        return False, "caption_missing_time_caption_terms", len(captions)
    return True, "ok", len(captions)


def check(kind: str, raw_path: str, marker: Path) -> dict[str, object]:
    path = Path(raw_path)
    result: dict[str, object] = {"type": kind.replace("_", "-"), "path": raw_path}
    if not path.exists():
        return result | {"status": "missing", "reason": "path_missing"}
    if not newer_than_marker(path, marker):
        return result | {"status": "stale", "reason": "marker_missing_or_artifact_not_newer"}
    if path.stat().st_size == 0:
        return result | {"status": "invalid", "reason": "empty_file"}
    validator = valid_srt if kind == "srt" else valid_captions_json if kind in {"captions-json", "captions_json"} else valid_vv_json
    ok, reason, count = validator(path)
    return result | {"status": "ready" if ok else "invalid", "reason": reason, "count": count}


def parse_artifact(spec: str) -> tuple[str, str]:
    kind, sep, path = spec.partition(":")
    if not sep or kind not in KINDS or not path:
        raise argparse.ArgumentTypeError(f"artifact must be TYPE:PATH, TYPE in {sorted(KINDS)}")
    return kind, path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marker", required=True, help="Launch marker touched before the background job started")
    parser.add_argument("artifacts", nargs="+", type=parse_artifact, help="Artifact spec, e.g. srt:/tmp/out.srt")
    args = parser.parse_args(argv)

    marker = Path(args.marker)
    artifacts = [check(kind, path, marker) for kind, path in args.artifacts]
    all_ready = all(item["status"] == "ready" for item in artifacts)
    print(json.dumps({"marker": str(marker), "all_ready": all_ready, "artifacts": artifacts}, ensure_ascii=False, indent=2))
    return 0 if all_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
