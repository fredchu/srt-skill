"""CLI contract tests for stage_timer.py (real child processes, no mocks)."""

import json
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "stage_timer.py"


def cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True, check=False)


def summary(workdir):
    result = cli("summary", "--workdir", workdir, "--json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["stages"]


def test_run_records_start_end_and_duration(tmp_path):
    workdir = tmp_path / "new"
    result = cli("run", "--workdir", workdir, "demo", "--", sys.executable,
                 "-c", "import time; time.sleep(0.05); print('child output')")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "child output\n"
    events = [json.loads(line) for line in (workdir / "_timing.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events] == ["start", "end"]
    assert events[0]["id"] == events[1]["id"]
    assert events[0]["command"][0] == sys.executable
    assert all(event["timestamp"] for event in events)
    stages = summary(workdir)
    assert len(stages) == 1
    assert stages[0]["stage"] == "demo"
    assert stages[0]["status"] == "ok"
    assert stages[0]["exit_code"] == 0
    assert stages[0]["seconds"] >= 0.05
    assert "demo" in cli("summary", "--workdir", workdir).stdout


def test_failure_and_repeated_stage_are_appended(tmp_path):
    for code in (3, 0):
        result = cli("run", "--workdir", tmp_path, "asr", "--", sys.executable,
                     "-c", f"import sys; sys.exit({code})")
        assert result.returncode == code
    stages = summary(tmp_path)
    assert len(stages) == 2
    assert [stage["status"] for stage in stages] == ["failed", "ok"]
    assert [stage["exit_code"] for stage in stages] == [3, 0]
    assert stages[0]["id"] != stages[1]["id"]


def test_missing_executable_still_ends_stage(tmp_path):
    result = cli("run", "--workdir", tmp_path, "missing", "--",
                 "this-command-does-not-exist-stage-timer-test")
    assert result.returncode == 127
    assert summary(tmp_path)[0]["status"] == "failed"
    assert summary(tmp_path)[0]["exit_code"] == 127


def test_unfinished_stage_and_empty_summary(tmp_path):
    assert summary(tmp_path) == []
    (tmp_path / "_timing.jsonl").write_text(json.dumps({
        "event": "start", "id": "uncompleted", "stage": "extract",
        "timestamp": "2026-01-01T00:00:00+00:00"}) + "\n")
    assert summary(tmp_path)[0]["status"] == "running"
    assert summary(tmp_path)[0]["seconds"] is None


def test_invalid_command_and_corrupt_log(tmp_path):
    result = cli("run", "--workdir", tmp_path, "bad", "--")
    assert result.returncode != 0
    assert not (tmp_path / "_timing.jsonl").exists()
    (tmp_path / "_timing.jsonl").write_text("not json\n")
    cli("run", "--workdir", tmp_path, "after", "--", sys.executable, "-c", "pass")
    result = cli("summary", "--workdir", tmp_path, "--json")
    assert result.returncode == 0
    assert "skipped invalid timing event" in result.stderr
    assert [s["stage"] for s in json.loads(result.stdout)["stages"]] == ["after"]


def test_start_end_pairs_latest_open_run_and_total(tmp_path):
    assert cli("start", "--workdir", tmp_path, "correct").returncode == 0
    assert cli("start", "--workdir", tmp_path, "review").returncode == 0
    assert cli("end", "--workdir", tmp_path, "correct").returncode == 0
    stages = {s["stage"]: s for s in summary(tmp_path)}
    assert stages["correct"]["status"] == "ok"
    assert stages["correct"]["seconds"] >= 0
    assert stages["review"]["status"] == "running"
    assert cli("end", "--workdir", tmp_path, "review", "--status", "failed").returncode == 0
    data = json.loads(cli("summary", "--workdir", tmp_path, "--json").stdout)
    assert [s["status"] for s in data["stages"]] == ["ok", "failed"]
    assert data["total_seconds"] >= 0
    text = cli("summary", "--workdir", tmp_path).stdout
    assert text.splitlines()[-1].startswith("TOTAL")


def test_end_without_start_fails(tmp_path):
    result = cli("end", "--workdir", tmp_path, "nothing")
    assert result.returncode != 0
    assert "no unfinished start" in result.stderr
