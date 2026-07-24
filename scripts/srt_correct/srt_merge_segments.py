#!/usr/bin/env python3
"""Merge corrected Step 2b SRT segments with structural quality gates."""

import argparse
import glob
import json
import re
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path


TS_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})"
)


def ts_to_ms(ts):
    h, m, rest = ts.strip().replace(".", ",").split(":")
    s, ms = rest.split(",")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def ms_to_ts(ms):
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    frac = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{frac:03d}"


def split_blocks(path):
    content = Path(path).read_text(encoding="utf-8-sig")
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content:
        return []
    return re.split(r"\n\n+", content)


def parse_strict_blocks(path, repair_repeated_lines=False):
    entries = []
    for block in split_blocks(path):
        lines = block.strip().split("\n")
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        match = TS_RE.match(lines[1].strip())
        if not match:
            continue
        text_lines = [line.strip() for line in lines[2:] if line.strip()]
        if repair_repeated_lines and len(text_lines) > 1:
            for i in range(len(text_lines) - 1, 0, -1):
                ratio = SequenceMatcher(None, text_lines[i - 1], text_lines[i]).ratio()
                if ratio > 0.7:
                    keep = text_lines[i] if len(text_lines[i]) >= len(text_lines[i - 1]) else text_lines[i - 1]
                    text_lines = text_lines[:i - 1] + [keep] + text_lines[i + 1:]
        entries.append({
            "start_ms": ts_to_ms(match.group(1)),
            "end_ms": ts_to_ms(match.group(2)),
            "text_lines": text_lines,
        })
    return entries


def max_duration_sec(entries):
    if not entries:
        return 0.0
    return max(max(0, entry["end_ms"] - entry["start_ms"]) for entry in entries) / 1000.0


def entry_text(entry):
    return "\n".join(entry["text_lines"])


def dup_keys(entries, min_chars):
    occurrences = {}
    for i, entry in enumerate(sorted(entries, key=lambda item: (item["start_ms"], item["end_ms"]))):
        key = re.sub(r"[\W_]", "", entry_text(entry))
        if len(key) >= min_chars:
            occurrences.setdefault(key, []).append((i, entry_text(entry)))
    return {
        key: items
        for key, items in occurrences.items()
        if len(items) >= 2 and items[-1][0] - items[0][0] >= 2
    }


def collect_corrected_files(workdir):
    files = glob.glob(str(Path(workdir) / "_seg_*_corrected.srt"))
    return sorted(files, key=lambda f: int(re.search(r"_seg_(\d+)", f).group(1)))


def gate_segments(args, corrected_files):
    failed = []
    warned = []
    per_segment = []

    for corrected_path in corrected_files:
        match = re.search(r"_seg_(\d+)_corrected\.srt$", corrected_path)
        n = int(match.group(1))
        input_entries = parse_strict_blocks(Path(args.workdir) / f"_seg_{n}.srt")
        output_entries = parse_strict_blocks(corrected_path)
        input_count = len(input_entries)
        output_count = len(output_entries)
        ratio = output_count / input_count if input_count else 1.0
        max_dur = max_duration_sec(output_entries)
        item = {
            "n": n,
            "ratio": round(ratio, 6),
            "max_dur_sec": round(max_dur, 3),
            "input": input_count,
            "output": output_count,
        }
        per_segment.append(item)

        reasons = []
        if ratio < args.gate_ratio_fail or (
            ratio < args.gate_ratio_warn and max_dur > args.gate_max_dur_sec
        ):
            reasons.append("ratio")

        input_zero = Counter(
            (entry["start_ms"], entry["end_ms"])
            for entry in input_entries
            if entry["end_ms"] <= entry["start_ms"]
        )
        output_zero = Counter(
            (entry["start_ms"], entry["end_ms"])
            for entry in output_entries
            if entry["end_ms"] <= entry["start_ms"]
        )
        new_zero = output_zero - input_zero
        if new_zero:
            reasons.append("zero_duration")
            item["zero_dur_examples"] = [
                f"{ms_to_ts(start)} --> {ms_to_ts(end)}"
                for (start, end), count in new_zero.items()
                for _ in range(count)
            ][:5]

        new_dups = []
        if args.gate_dup_min_chars:
            input_counts = Counter(
                re.sub(r"[\W_]", "", entry_text(entry)) for entry in input_entries
            )
            for key, occurrences in dup_keys(output_entries, args.gate_dup_min_chars).items():
                new_extra = max(0, len(occurrences) - max(1, input_counts[key]))
                if new_extra:
                    new_dups.append((occurrences[0][1], new_extra))
        if len(new_dups) >= args.gate_dup_min_texts or any(
            new_extra >= args.gate_dup_min_texts for _, new_extra in new_dups
        ):
            reasons.append("dup_text")
            item["dup_examples"] = [text for text, _ in new_dups[:5]]

        if reasons:
            item["reasons"] = reasons
            failed.append(item)
        elif ratio < args.gate_ratio_warn or len(new_dups) == 1:
            if len(new_dups) == 1:
                item["dup_warn"] = [new_dups[0][0]]
            warned.append(item)

    return failed, warned, per_segment


def clamp_end_times(entries):
    for i in range(len(entries) - 1):
        if entries[i]["end_ms"] > entries[i + 1]["start_ms"]:
            entries[i]["end_ms"] = entries[i + 1]["start_ms"]


def coverage_patch(entries, preprocessed_path):
    pre_entries = parse_strict_blocks(preprocessed_path)
    merged_starts = {entry["start_ms"] for entry in entries}
    patched = 0

    for pre_entry in pre_entries:
        if pre_entry["start_ms"] in merged_starts:
            continue
        in_gap = False
        for i in range(len(entries) - 1):
            gap = entries[i + 1]["start_ms"] - entries[i]["end_ms"]
            if gap > 15000 and entries[i]["end_ms"] <= pre_entry["start_ms"] <= entries[i + 1]["start_ms"]:
                in_gap = True
                break
        if in_gap:
            pre_entry["seg"] = None
            entries.append(pre_entry)
            merged_starts.add(pre_entry["start_ms"])
            patched += 1

    if patched:
        entries.sort(key=lambda entry: entry["start_ms"])
        clamp_end_times(entries)
    return patched


def write_srt(entries, output_path):
    with open(output_path, "w", encoding="utf-8") as f:
        for i, entry in enumerate(entries, 1):
            f.write(f"{i}\n")
            f.write(f"{ms_to_ts(entry['start_ms'])} --> {ms_to_ts(entry['end_ms'])}\n")
            f.write("\n".join(entry["text_lines"]))
            f.write("\n\n")


def cross_dup_count(entries, min_chars):
    if not min_chars:
        return 0
    positions = {}
    for i, entry in enumerate(entries):
        key = re.sub(r"[\W_]", "", entry_text(entry))
        if len(key) >= min_chars:
            positions.setdefault(key, []).append(i)

    count = 0
    for indexes in positions.values():
        for offset, left in enumerate(indexes):
            for right in indexes[offset + 1:]:
                if (
                    right - left == 1
                    and entries[left].get("seg") == entries[right].get("seg")
                    and entries[left].get("seg") is not None
                ):
                    continue
                count += 1
    return count


def merge(args):
    corrected_files = collect_corrected_files(args.workdir)
    failed, warned, per_segment = gate_segments(args, corrected_files)
    if failed:
        print(json.dumps({"gate": "fail", "failed_segments": failed}, ensure_ascii=False))
        return 2

    entries = []
    for path in corrected_files:
        n = int(re.search(r"_seg_(\d+)_corrected\.srt$", path).group(1))
        segment_entries = parse_strict_blocks(path, repair_repeated_lines=True)
        for entry in segment_entries:
            entry["seg"] = n
        entries.extend(segment_entries)

    entries.sort(key=lambda entry: entry["start_ms"])
    clamp_end_times(entries)
    patched = coverage_patch(entries, args.preprocessed)

    entries.sort(key=lambda entry: entry["start_ms"])
    clamp_end_times(entries)
    merged_zero_dur_count = sum(
        1 for entry in entries if entry["end_ms"] <= entry["start_ms"]
    )
    merged_cross_dup_count = cross_dup_count(entries, args.gate_dup_min_chars)
    if merged_zero_dur_count or merged_cross_dup_count:
        print(
            "WARNING: merged output contains "
            f"{merged_zero_dur_count} zero-duration entries and "
            f"{merged_cross_dup_count} duplicate-text pairs",
            file=sys.stderr,
        )
    write_srt(entries, args.output)

    durations = [(entry["end_ms"] - entry["start_ms"]) / 1000.0 for entry in entries]
    metrics = {
        "gate": "pass",
        "entries": len(entries),
        "patched": patched,
        "warn_segments": warned,
        "per_segment": [
            {"n": item["n"], "ratio": item["ratio"], "max_dur_sec": item["max_dur_sec"]}
            for item in per_segment
        ],
        "max_dur_sec": round(max(durations) if durations else 0.0, 3),
        "over_12s_count": sum(1 for duration in durations if duration > 12),
        "merged_zero_dur_count": merged_zero_dur_count,
        "cross_dup_count": merged_cross_dup_count,
    }
    print(json.dumps(metrics, ensure_ascii=False))
    return 0


def main():
    def nonnegative(value):
        parsed = int(value)
        if parsed < 0:
            raise argparse.ArgumentTypeError("must be >= 0")
        return parsed

    def positive(value):
        parsed = int(value)
        if parsed < 1:
            raise argparse.ArgumentTypeError("must be >= 1")
        return parsed

    parser = argparse.ArgumentParser(description="Merge corrected SRT segments with quality gates.")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--preprocessed", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seg-size", type=int, default=300)
    parser.add_argument("--gate-ratio-fail", type=float, default=0.55)
    parser.add_argument("--gate-ratio-warn", type=float, default=0.80)
    parser.add_argument("--gate-max-dur-sec", type=float, default=15)
    parser.add_argument("--gate-dup-min-chars", type=nonnegative, default=10)
    parser.add_argument("--gate-dup-min-texts", type=positive, default=2)
    sys.exit(merge(parser.parse_args()))


if __name__ == "__main__":
    main()
