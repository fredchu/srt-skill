#!/usr/bin/env python3
"""Wait on disk for a batch of parallel subagent outputs; report stragglers.

Usage:
    seg_watchdog.py --workdir DIR --pattern '_seg_{n}_corrected.srt' --segments 0-6
                    [--timeout 420] [--interval 30] [--since MARKER]

An output counts as done when it is a non-empty UTF-8 file (newer than --since
if given); *.srt must also contain a '-->' line. Status is rewritten to
<workdir>/_watchdog_status.json every round so a reaped run can be inspected.
Exit 0 when all arrive, 3 on timeout (missing IDs in the JSON), 2 for bad
arguments, 130 when interrupted. Runs in the foreground by design.
"""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time

STATUS_NAME = "_watchdog_status.json"


def parse_segments(spec):
    ids = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty item in segment spec {spec!r}")
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            if lo < 0 or hi < lo:
                raise ValueError(f"bad range {part!r}")
            ids.update(range(lo, hi + 1))
        else:
            n = int(part)
            if n < 0:
                raise ValueError(f"negative segment {n}")
            ids.add(n)
    if not ids:
        raise ValueError("no segments")
    return sorted(ids)


def positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return seconds


def is_done(path, since):
    try:
        st = path.stat()
        if not path.is_file() or st.st_size == 0:
            return False
        if since is not None and st.st_mtime <= since:
            return False
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if path.suffix == ".srt":
        return any("-->" in line for line in text.splitlines())
    return bool(text.strip())


def write_status(workdir, status):
    path = workdir / STATUS_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--pattern", required=True, help="output name containing {n}")
    parser.add_argument("--segments", required=True, help="e.g. 0-6 or 0,2,5-7")
    parser.add_argument("--timeout", type=positive_seconds, default=420.0)
    parser.add_argument("--interval", type=positive_seconds, default=30.0)
    parser.add_argument("--since", type=Path, help="only count outputs newer than this file")
    args = parser.parse_args(argv)

    if "{n}" not in args.pattern:
        parser.error("--pattern must contain {n}")
    if not args.workdir.is_dir():
        parser.error(f"workdir is not a directory: {args.workdir}")
    try:
        segments = parse_segments(args.segments)
    except ValueError as exc:
        parser.error(f"--segments: {exc}")
    since = None
    if args.since is not None:
        if not args.since.exists():
            parser.error(f"--since file does not exist: {args.since}")
        since = args.since.stat().st_mtime

    started = time.monotonic()
    status = {"pattern": args.pattern, "segments": segments}
    try:
        while True:
            done = [n for n in segments
                    if is_done(args.workdir / args.pattern.replace("{n}", str(n)), since)]
            missing = [n for n in segments if n not in done]
            elapsed = time.monotonic() - started
            state = ("complete" if not missing
                     else "timeout" if elapsed >= args.timeout else "waiting")
            status.update(done=done, missing=missing, elapsed_s=round(elapsed, 3),
                          status=state, updated_at=datetime.now(timezone.utc).isoformat())
            write_status(args.workdir, status)
            if state != "waiting":
                print(json.dumps(status, ensure_ascii=False))
                return 0 if state == "complete" else 3
            time.sleep(min(args.interval, args.timeout - elapsed))
    except KeyboardInterrupt:
        status.update(status="interrupted", updated_at=datetime.now(timezone.utc).isoformat())
        write_status(args.workdir, status)
        return 130


if __name__ == "__main__":
    sys.exit(main())
