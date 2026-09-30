"""CLI contract tests for seg_watchdog.py (real files, real waiting process)."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

SCRIPT = Path(__file__).resolve().parents[1] / "seg_watchdog.py"
SRT = "1\n00:00:01,000 --> 00:00:02,000\nhi\n\n"
PAT = "_seg_{n}_corrected.srt"


def cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True, check=False, timeout=10)


def seg(workdir, n, text=SRT):
    path = workdir / PAT.replace("{n}", str(n))
    path.write_text(text, encoding="utf-8")
    return path


def run(workdir, spec, *extra, timeout=0.3):
    return cli("--workdir", workdir, "--pattern", PAT, "--segments", spec,
               "--timeout", timeout, "--interval", 0.05, *extra)


def test_already_complete_exits_zero_and_status_matches_stdout(tmp_path):
    seg(tmp_path, 0)
    seg(tmp_path, 1)
    result = run(tmp_path, "0-1")
    assert result.returncode == 0, result.stderr
    printed = json.loads(result.stdout)
    assert printed["status"] == "complete" and printed["missing"] == []
    assert json.loads((tmp_path / "_watchdog_status.json").read_text()) == printed


def test_timeout_reports_missing(tmp_path):
    seg(tmp_path, 0)
    result = run(tmp_path, "0-2")
    assert result.returncode == 3
    data = json.loads(result.stdout)
    assert data["status"] == "timeout"
    assert data["done"] == [0] and data["missing"] == [1, 2]


def test_waits_for_output_written_later(tmp_path):
    seg(tmp_path, 0)
    proc = subprocess.Popen([sys.executable, str(SCRIPT), "--workdir", str(tmp_path),
                             "--pattern", PAT, "--segments", "0-1",
                             "--timeout", "5", "--interval", "0.05"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(0.3)
        assert proc.poll() is None
        assert json.loads((tmp_path / "_watchdog_status.json").read_text())["status"] == "waiting"
        seg(tmp_path, 1)
        out, err = proc.communicate(timeout=5)
        assert proc.returncode == 0, err
        assert json.loads(out)["status"] == "complete"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_srt_without_arrow_or_empty_is_not_done(tmp_path):
    seg(tmp_path, 0, "half written text\n")
    seg(tmp_path, 1, "")
    data = json.loads(run(tmp_path, "0-1").stdout)
    assert data["missing"] == [0, 1]


def test_since_ignores_stale_outputs(tmp_path):
    old = seg(tmp_path, 0)
    marker = tmp_path / ".launch"
    marker.touch()
    past = marker.stat().st_mtime - 10
    os.utime(old, (past, past))
    result = run(tmp_path, "0", "--since", marker)
    assert result.returncode == 3
    seg(tmp_path, 0)
    now = marker.stat().st_mtime + 5
    os.utime(tmp_path / "_seg_0_corrected.srt", (now, now))
    assert run(tmp_path, "0", "--since", marker).returncode == 0


def test_review_fixes_pattern_accepts_no_fixes(tmp_path):
    (tmp_path / "_review_seg_0_fixes.txt").write_text("NO_FIXES\n")
    result = cli("--workdir", tmp_path, "--pattern", "_review_seg_{n}_fixes.txt",
                 "--segments", "0", "--timeout", 0.2, "--interval", 0.05)
    assert result.returncode == 0, result.stderr


def test_segment_spec_parsing(tmp_path):
    data = json.loads(run(tmp_path, "0,2,5-7").stdout)
    assert data["segments"] == [0, 2, 5, 6, 7]


def test_bad_arguments_exit_two(tmp_path):
    base = ["--workdir", tmp_path, "--timeout", 0.1]
    assert cli(*base, "--pattern", "_seg.srt", "--segments", "0").returncode == 2
    assert cli(*base, "--pattern", PAT, "--segments", "3-1").returncode == 2
    assert cli(*base, "--pattern", PAT, "--segments", "x").returncode == 2
    assert cli(*base, "--pattern", PAT, "--segments", "0", "--interval", 0).returncode == 2
    assert cli(*base, "--pattern", PAT, "--segments", "0",
               "--since", tmp_path / "nope").returncode == 2
    assert cli("--workdir", tmp_path / "absent", "--pattern", PAT,
               "--segments", "0").returncode == 2
