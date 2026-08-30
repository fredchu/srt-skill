from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.srt_correct import srt_hallucination_fix as fixer


def _reset_asr_state() -> None:
    fixer._ASR_CALLS = 0
    fixer._ASR_HARD_STOP = False


def _write_input_srt(path: Path) -> Path:
    path.write_text(
        "\n".join(
            [
                "1",
                "00:00:00,000 --> 00:00:00,900",
                "哈哈哈哈哈哈哈哈哈哈哈哈哈哈",
                "",
                "2",
                "00:00:04,000 --> 00:00:05,000",
                "原本正常",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _write_fake_subtitle(script_path: Path) -> None:
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "wav=\"$1\"\n"
        "dir=\"$(dirname \"$wav\")\"\n"
        "base=\"$(basename \"$wav\" .wav)\"\n"
        "count_file=\"${SRT_FIX_MOCK_COUNT_FILE:?}\"\n"
        "n=0\n"
        "if [ -f \"$count_file\" ]; then n=\"$(cat \"$count_file\")\"; fi\n"
        "n=$((n + 1))\n"
        "printf '%s\\n' \"$n\" > \"$count_file\"\n"
        "mkdir -p \"$dir/env\"\n"
        "printf 'action=create result=success attempt=%s\\n' \"$n\" > \"$dir/env/pod_attempts.txt\"\n"
        "printf 'action=terminate result=success attempt=%s\\n' \"$n\" >> \"$dir/env/pod_attempts.txt\"\n"
        "printf 'pod-%s\\n' \"$n\" > \"$dir/env/pod_id.txt\"\n"
        "printf '%s\\n' \"${MAX_POD_ATTEMPTS:-}\" > \"$dir/env/max_pod_attempts.txt\"\n"
        "printf '%s\\n' \"${COST_CAP_USD:-}\" > \"$dir/env/cost_cap_usd.txt\"\n"
        "printf '{\\\"call\\\": %s}\\n' \"$n\" > \"$dir/${base}.cloud.json\"\n"
        "mode=\"${SRT_FIX_MOCK_MODE:-third_call_fix}\"\n"
        "if [ \"$mode\" = \"always_empty\" ]; then\n"
        "  : > \"$dir/${base}.srt\"\n"
        "elif [ \"$n\" -lt 3 ]; then\n"
        "cat > \"$dir/${base}.srt\" <<'EOF'\n"
        "1\n"
        "00:00:00,000 --> 00:00:00,800\n"
        "哈哈哈哈哈哈哈哈哈哈哈哈哈哈\n"
        "EOF\n"
        "else\n"
        "cat > \"$dir/${base}.srt\" <<'EOF'\n"
        "1\n"
        "00:00:00,000 --> 00:00:00,400\n"
        "修復第一句\n"
        "\n"
        "2\n"
        "00:00:00,500 --> 00:00:00,900\n"
        "修復第二句\n"
        "EOF\n"
        "fi\n"
        "echo \"mock subtitle call $n\"\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)


def test_runpod_retries_use_unique_call_dirs_and_keep_all_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    _reset_asr_state()

    work_dir = tmp_path
    input_srt = _write_input_srt(work_dir / "demo.srt")
    media = work_dir / "demo.wav"
    media.write_bytes(b"placeholder")

    fake_root = tmp_path / "fake_repo"
    fake_subtitle = fake_root / "scripts" / "subtitle.sh"
    _write_fake_subtitle(fake_subtitle)
    monkeypatch.setattr(fixer, "__file__", str(fake_root / "scripts" / "srt_correct" / "srt_hallucination_fix.py"))

    count_file = tmp_path / "count.txt"
    monkeypatch.setenv("SRT_FIX_MOCK_COUNT_FILE", str(count_file))
    monkeypatch.setenv("SRT_ASR_ENGINE", "runpod")
    monkeypatch.setenv("MAX_POD_ATTEMPTS", "1")
    monkeypatch.setenv("COST_CAP_USD", "0.111")
    monkeypatch.setenv("SRT_ASR_MAX_CALLS", "0")

    def _fake_extract(_media_path: str, _anomaly: dict, _buf_s: int, output_path: str) -> bool:
        Path(output_path).write_bytes(b"seg")
        return True

    monkeypatch.setattr(fixer, "_extract_with_buffer", _fake_extract)
    monkeypatch.setattr(sys, "argv", ["srt_hallucination_fix.py", str(input_srt), str(media), "--breeze"])

    fixer.main()
    captured = capsys.readouterr()

    assert "RunPod call dir:" in captured.err
    assert "RunPod evidence dir:" in captured.err

    runs_root = work_dir / ".cloud_asr_runs"
    call_dirs = sorted(p for p in runs_root.glob("call-*") if p.is_dir())
    assert len(call_dirs) == 3
    for i in range(1, 4):
        pref = f"call-{i:03d}."
        matched = [p for p in call_dirs if p.name.startswith(pref)]
        assert len(matched) == 1

    assert count_file.read_text(encoding="utf-8").strip() == "3"
    assert not (work_dir / "env").exists(), "runpod 證據應收斂到 call dir，不可回寫到原 work_dir"

    pod_ids: list[str] = []
    all_logs = ""
    for call_dir in call_dirs:
        env_dir = call_dir / "env"
        assert env_dir.exists()
        attempts_log = (env_dir / "pod_attempts.txt").read_text(encoding="utf-8")
        assert "action=create result=success" in attempts_log
        assert "action=terminate result=success" in attempts_log
        assert (env_dir / "max_pod_attempts.txt").read_text(encoding="utf-8").strip() == "1"
        assert (env_dir / "cost_cap_usd.txt").read_text(encoding="utf-8").strip() == "0.111"
        pod_ids.append((env_dir / "pod_id.txt").read_text(encoding="utf-8").strip())

        log_path = call_dir / "_asr_call.log"
        assert log_path.exists()
        all_logs += log_path.read_text(encoding="utf-8")
        assert (call_dir / "_hallucination_seg_0.srt").exists()
        assert (call_dir / "_hallucination_seg_0.cloud.json").exists()

    assert "refusing to overwrite" not in all_logs
    assert len(set(pod_ids)) == 3, "每次呼叫都要保留各自證據，不能互相覆蓋"

    patched = input_srt.read_text(encoding="utf-8")
    assert "修復第一句" in patched
    assert "哈哈哈哈哈哈哈哈哈哈哈哈哈哈" not in patched


def test_runpod_max_calls_2_circuit_breaks_third_attempt_and_keeps_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    _reset_asr_state()

    work_dir = tmp_path
    input_srt = _write_input_srt(work_dir / "demo.srt")
    media = work_dir / "demo.wav"
    media.write_bytes(b"placeholder")

    fake_root = tmp_path / "fake_repo"
    fake_subtitle = fake_root / "scripts" / "subtitle.sh"
    _write_fake_subtitle(fake_subtitle)
    monkeypatch.setattr(fixer, "__file__", str(fake_root / "scripts" / "srt_correct" / "srt_hallucination_fix.py"))

    count_file = tmp_path / "count.txt"
    monkeypatch.setenv("SRT_FIX_MOCK_COUNT_FILE", str(count_file))
    monkeypatch.setenv("SRT_FIX_MOCK_MODE", "always_empty")
    monkeypatch.setenv("SRT_ASR_ENGINE", "runpod")
    monkeypatch.setenv("SRT_ASR_MAX_CALLS", "2")

    def _fake_extract(_media_path: str, _anomaly: dict, _buf_s: int, output_path: str) -> bool:
        Path(output_path).write_bytes(b"seg")
        return True

    monkeypatch.setattr(fixer, "_extract_with_buffer", _fake_extract)
    monkeypatch.setattr(sys, "argv", ["srt_hallucination_fix.py", str(input_srt), str(media), "--breeze"])

    fixer.main()
    captured = capsys.readouterr()

    assert "已達 ASR 呼叫總量上限 2 次" in captured.err
    runs_root = work_dir / ".cloud_asr_runs"
    call_dirs = sorted(p for p in runs_root.glob("call-*") if p.is_dir())
    assert len(call_dirs) == 2
    assert all(not p.name.startswith("call-003.") for p in call_dirs)
    assert count_file.read_text(encoding="utf-8").strip() == "2"
    for call_dir in call_dirs:
        attempts_log = (call_dir / "env" / "pod_attempts.txt").read_text(encoding="utf-8")
        assert "action=create result=success" in attempts_log
        assert "action=terminate result=success" in attempts_log


def test_cleanup_keeps_cloud_env_evidence(tmp_path: Path) -> None:
    work_dir = tmp_path
    (work_dir / "_hallucination_seg_0.wav").write_bytes(b"seg")
    (work_dir / "_hallucination_seg_0.srt").write_text("1\n", encoding="utf-8")

    env_dir = work_dir / ".cloud_asr_runs" / "call-001.mock" / "env"
    env_dir.mkdir(parents=True, exist_ok=True)
    attempts_path = env_dir / "pod_attempts.txt"
    attempts_path.write_text("action=create result=success\n", encoding="utf-8")

    fixer._cleanup(str(work_dir), 0)

    assert not (work_dir / "_hallucination_seg_0.wav").exists()
    assert not (work_dir / "_hallucination_seg_0.srt").exists()
    assert attempts_path.exists()
    assert attempts_path.read_text(encoding="utf-8") == "action=create result=success\n"


def test_runpod_timeout_sets_hard_stop_and_prevents_next_anomaly_boot(tmp_path: Path, monkeypatch) -> None:
    _reset_asr_state()

    work_dir = tmp_path
    input_srt = work_dir / "demo.srt"
    input_srt.write_text(
        "\n".join(
            [
                "1",
                "00:00:00,000 --> 00:00:00,900",
                "哈哈哈哈哈哈哈哈哈哈哈哈哈哈",
                "",
                "2",
                "00:00:01,000 --> 00:00:01,500",
                "中間正常",
                "",
                "3",
                "00:00:20,000 --> 00:00:20,900",
                "呵呵呵呵呵呵呵呵呵呵呵呵",
                "",
            ]
        ),
        encoding="utf-8",
    )
    media = work_dir / "demo.wav"
    media.write_bytes(b"placeholder")

    fake_root = tmp_path / "fake_repo"
    monkeypatch.setattr(fixer, "__file__", str(fake_root / "scripts" / "srt_correct" / "srt_hallucination_fix.py"))

    monkeypatch.setenv("SRT_ASR_ENGINE", "runpod")

    def _fake_extract(_media_path: str, _anomaly: dict, _buf_s: int, output_path: str) -> bool:
        Path(output_path).write_bytes(b"seg")
        return True

    monkeypatch.setattr(fixer, "_extract_with_buffer", _fake_extract)

    seen: dict[str, int] = {"popen_calls": 0}

    class _TimeoutPopen:
        def __init__(self, cmd, stdout=None, stderr=None, text=None):
            _ = (stdout, stderr, text)
            self.cmd = cmd
            self.returncode = 124
            self._communicate_calls = 0
            seen["popen_calls"] += 1

        def communicate(self, timeout=None):
            _ = timeout
            self._communicate_calls += 1
            if self._communicate_calls == 1:
                raise subprocess.TimeoutExpired(self.cmd, timeout)
            self.returncode = 143
            return ("", "")

        def terminate(self):
            return None

        def kill(self):
            return None

    monkeypatch.setattr(fixer.subprocess, "Popen", _TimeoutPopen)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "srt_hallucination_fix.py",
            str(input_srt),
            str(media),
            "--breeze",
            "--gap-threshold",
            "999",
        ],
    )

    fixer.main()

    runs_root = work_dir / ".cloud_asr_runs"
    call_dirs = sorted(p for p in runs_root.glob("call-*") if p.is_dir())
    assert len(call_dirs) == 1
    assert seen["popen_calls"] == 1
    assert fixer._ASR_HARD_STOP is True


def test_local_engine_keeps_original_command_and_does_not_create_cloud_runs(tmp_path: Path, monkeypatch) -> None:
    _reset_asr_state()

    work_dir = tmp_path
    wav = work_dir / "_hallucination_seg_0.wav"
    wav.write_bytes(b"seg")

    fake_root = tmp_path / "fake_repo"
    fake_subtitle = fake_root / "scripts" / "subtitle.sh"
    fake_subtitle.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fixer, "__file__", str(fake_root / "scripts" / "srt_correct" / "srt_hallucination_fix.py"))

    monkeypatch.setenv("SRT_ASR_ENGINE", "mlx")

    seen: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, cmd, stdout=None, stderr=None, text=None):
            seen["cmd"] = cmd
            self.returncode = 0

        def communicate(self, timeout=None):
            _ = timeout
            (work_dir / "_hallucination_seg_0.srt").write_text(
                "1\n00:00:00,000 --> 00:00:00,500\n本地修復\n",
                encoding="utf-8",
            )
            return ("mlx ok\n", "")

        def terminate(self):
            return None

        def kill(self):
            return None

    monkeypatch.setattr(fixer.subprocess, "Popen", _FakePopen)

    srt_path = fixer.run_asr(str(wav), str(work_dir), use_breeze=True)

    assert srt_path == str(work_dir / "_hallucination_seg_0.srt")
    assert seen["cmd"] == [str(fake_subtitle), str(wav), "--breeze"]
    assert not (work_dir / ".cloud_asr_runs").exists()
