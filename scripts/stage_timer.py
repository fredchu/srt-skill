#!/usr/bin/env python3
"""Time a pipeline stage and append start/end events to <workdir>/_timing.jsonl.

Usage:
    stage_timer.py run --workdir DIR STAGE -- command [args ...]
    stage_timer.py start --workdir DIR STAGE
    stage_timer.py end --workdir DIR STAGE [--status ok|failed|skipped]
    stage_timer.py summary --workdir DIR [--json]

Use start/end for stages that cannot be wrapped as one command (e.g. cloud
subagents); end closes the most recent unfinished start of the same stage.

Each run has a unique ID, so repeated or overlapping stage names stay distinct.
An unfinished start event is reported as running (with no duration).
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


LOG_NAME = "_timing.jsonl"


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def append_event(path, event):
    data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
    try:
        # One append write per event keeps concurrent short records together.
        if os.write(fd, data) != len(data):
            raise OSError("short write to timing log")
    finally:
        os.close(fd)


def run_stage(workdir, stage, command):
    # argparse removes the `--` separator before REMAINDER on some Python versions.
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("run requires STAGE -- command [args ...]")
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / LOG_NAME
    run_id = uuid.uuid4().hex
    started = time.monotonic()
    append_event(path, {"event": "start", "id": run_id, "stage": stage,
                        "timestamp": timestamp(), "command": command})
    code = 1
    error = None
    try:
        result = subprocess.run(command, check=False)
        code = result.returncode if result.returncode >= 0 else 128 - result.returncode
    except FileNotFoundError as exc:
        code = 127
        error = str(exc)
        print(f"stage_timer: {exc}", file=sys.stderr)
    except PermissionError as exc:
        code = 126
        error = str(exc)
        print(f"stage_timer: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        code = 130
    finally:
        event = {"event": "end", "id": run_id, "stage": stage,
                 "timestamp": timestamp(), "seconds": max(0.0, time.monotonic() - started),
                 "status": "ok" if code == 0 else "failed", "exit_code": code}
        if error is not None:
            event["error"] = error
        append_event(path, event)
    return code


def start_stage(workdir, stage):
    workdir.mkdir(parents=True, exist_ok=True)
    append_event(workdir / LOG_NAME, {"event": "start", "id": uuid.uuid4().hex,
                                      "stage": stage, "timestamp": timestamp()})
    return 0


def end_stage(workdir, stage, status):
    path = workdir / LOG_NAME
    open_runs = [s for s in summarize(path, quiet=True)["stages"]
                 if s["stage"] == stage and s["status"] == "running"]
    if not open_runs:
        raise ValueError(f"no unfinished start for stage {stage!r}")
    run = open_runs[-1]
    started = datetime.fromisoformat(run["started_at"])
    append_event(path, {"event": "end", "id": run["id"], "stage": stage,
                        "timestamp": timestamp(),
                        "seconds": max(0.0, (datetime.now(timezone.utc) - started).total_seconds()),
                        "status": status, "exit_code": None})
    return 0


def clock(ts):
    return datetime.fromisoformat(ts).astimezone().strftime("%H:%M:%S")


def duration(seconds):
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def summarize(path, quiet=False):
    stages = []
    by_id = {}
    if not path.exists():
        return {"stages": stages, "total_seconds": None}
    with path.open(encoding="utf-8") as stream:
        for lineno, line in enumerate(stream, 1):
            try:
                event = json.loads(line)
                kind = event["event"]
                run_id = event["id"]
                if kind == "start":
                    if run_id in by_id:
                        raise ValueError("duplicate start ID")
                    stage = {"id": run_id, "stage": event["stage"],
                             "started_at": event["timestamp"], "ended_at": None,
                             "seconds": None, "status": "running", "exit_code": None}
                    stages.append(stage)
                    by_id[run_id] = stage
                elif kind == "end":
                    stage = by_id[run_id]
                    if stage["ended_at"] is not None or stage["stage"] != event["stage"]:
                        raise ValueError("duplicate or mismatched end event")
                    stage.update(ended_at=event["timestamp"], seconds=event["seconds"],
                                 status=event["status"], exit_code=event["exit_code"])
                else:
                    raise ValueError("unknown event type")
            except (ValueError, KeyError, TypeError) as exc:
                # A broken line must not hide the rest of the timing report.
                if not quiet:
                    print(f"stage_timer: {path}:{lineno}: skipped invalid timing event: {exc}",
                          file=sys.stderr)
    ends = [s["ended_at"] for s in stages if s["ended_at"]]
    total = None
    if stages and ends:
        total = (max(datetime.fromisoformat(t) for t in ends)
                 - min(datetime.fromisoformat(s["started_at"]) for s in stages)).total_seconds()
    return {"stages": stages, "total_seconds": total}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="action", required=True)
    run = subparsers.add_parser("run", help="time a command and preserve its exit status")
    run.add_argument("--workdir", type=Path, required=True)
    run.add_argument("stage")
    run.add_argument("command", nargs=argparse.REMAINDER)
    start = subparsers.add_parser("start", help="mark a stage as started")
    start.add_argument("--workdir", type=Path, required=True)
    start.add_argument("stage")
    end = subparsers.add_parser("end", help="close the latest unfinished start of a stage")
    end.add_argument("--workdir", type=Path, required=True)
    end.add_argument("stage")
    end.add_argument("--status", choices=["ok", "failed", "skipped"], default="ok")
    summary = subparsers.add_parser("summary", help="show all stage runs in this workdir")
    summary.add_argument("--workdir", type=Path, required=True)
    summary.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "run":
            return run_stage(args.workdir, args.stage, args.command)
        if args.action == "start":
            return start_stage(args.workdir, args.stage)
        if args.action == "end":
            return end_stage(args.workdir, args.stage, args.status)
        result = summarize(args.workdir / LOG_NAME)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            if not result["stages"]:
                print("no timing records")
            for stage in sorted(result["stages"], key=lambda s: s["started_at"]):
                end_at = "-" if stage["ended_at"] is None else clock(stage["ended_at"])
                took = "-" if stage["seconds"] is None else duration(stage["seconds"])
                print(f"{stage['stage']}\t{clock(stage['started_at'])}\t{end_at}\t{took}\t{stage['status']}")
            if result["total_seconds"] is not None:
                print(f"TOTAL\t\t\t{duration(result['total_seconds'])}")
        return 0
    except (OSError, ValueError) as exc:
        parser.exit(1, f"stage_timer: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
