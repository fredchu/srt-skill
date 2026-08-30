from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import LIVE_DEFAULTS, MOCK_TIMEOUTS, apply_fast_mock_timeouts

REPO_ROOT = Path(__file__).resolve().parents[2]
CLOUD_ASR = REPO_ROOT / "scripts" / "cloud_asr.sh"

REQUIRED_TOOLS = ["bash", "ffmpeg", "git", "jq", "scp", "ssh", "python3"]
MISSING_TOOLS = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
pytestmark = pytest.mark.skipif(MISSING_TOOLS, reason=f"missing tools: {', '.join(MISSING_TOOLS)}")


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _make_wav(path: Path, seconds: int = 1) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            str(seconds),
            str(path),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return path


def _mock_env(tmp: Path, *, payload: Path | None = None, vv_env: Path | None = None, vv_run: Path | None = None) -> dict[str, str]:
    ssh_key = tmp / "id_ed25519"
    ssh_pub = tmp / "id_ed25519.pub"
    ssh_key.write_text("dummy private key\n", encoding="utf-8")
    ssh_pub.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFakeFakeFakeFakeFakeFake fake@local\n", encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "RUNPOD_API_KEY": "test-token",
            "SSH_PRIVATE_KEY": str(ssh_key),
            "SSH_PUBLIC_KEY_PATH": str(ssh_pub),
            "CLOUD_ASR_TEST_HOOK": "provisioning_retry",
            "CLOUD_ASR_MOCK_FAIL_LABEL": "nope",
            "CLOUD_ASR_TEST_JSON_PAYLOAD_FILE": str(payload) if payload else "",
            "CLOUD_ASR_TEST_VV_ENV_JSON_FILE": str(vv_env) if vv_env else "",
            "CLOUD_ASR_TEST_VV_RUN_JSON_FILE": str(vv_run) if vv_run else "",
            "CLOUD_ASR_MOCK_READY_FROM_ATTEMPT": "1",
            "PATH": os.environ.get("PATH", ""),
            **MOCK_TIMEOUTS,
        }
    )
    for key in ["CLOUD_ASR_TEST_JSON_PAYLOAD_FILE", "CLOUD_ASR_TEST_VV_ENV_JSON_FILE", "CLOUD_ASR_TEST_VV_RUN_JSON_FILE"]:
        if not env[key]:
            env.pop(key)
    return env


def _run_cloud_asr(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(CLOUD_ASR), *args],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )


def test_fixture_speeds_up_mock_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUD_ASR_TEST_HOOK", "provisioning_retry")
    for name, value in LIVE_DEFAULTS.items():
        monkeypatch.setenv(name, value)
    apply_fast_mock_timeouts(monkeypatch)
    assert {name: os.environ[name] for name in MOCK_TIMEOUTS} == MOCK_TIMEOUTS


def test_fixture_does_not_touch_live_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a hook, live defaults must never be shortened to mock values."""
    monkeypatch.delenv("CLOUD_ASR_TEST_HOOK", raising=False)
    for name, value in LIVE_DEFAULTS.items():
        monkeypatch.setenv(name, value)
    apply_fast_mock_timeouts(monkeypatch)
    assert {name: os.environ[name] for name in LIVE_DEFAULTS} == LIVE_DEFAULTS


def test_live_defaults_match_cloud_asr_sh() -> None:
    """Keep copied live defaults synchronized with the production script."""
    source = CLOUD_ASR.read_text(encoding="utf-8")
    for name, expected in LIVE_DEFAULTS.items():
        match = re.search(rf'^{name}="\$\{{{name}:-(\d+)\}}"', source, re.MULTILINE)
        assert match, f"cloud_asr.sh 找不到 {name} 的預設值定義"
        assert match.group(1) == expected, (
            f"{name} 漂移了：cloud_asr.sh 是 {match.group(1)}，"
            f"conftest 的 LIVE_DEFAULTS 還寫 {expected}"
        )


def test_help_mentions_vv_terms_json() -> None:
    proc = _run_cloud_asr(["--help"])

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert "--vv" in proc.stderr
    assert "--terms FILE" in proc.stderr
    assert "--json" in proc.stderr


def test_breeze_rejects_json_only_flag() -> None:
    proc = _run_cloud_asr(["/nope.wav", "/tmp/out", "base", "zh", "--breeze", "--json"])

    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "VV options are only valid with --vv" in proc.stderr


def test_vv_mock_json_srt_terms_and_canary(tmp_path: Path) -> None:
    wav = _make_wav(tmp_path / "input.wav", seconds=1)
    out = tmp_path / "out"
    terms = _write(
        tmp_path / "terms.txt",
        "# comment\nAlpha\n\nZQXV Capital\nGamma\n",
    )
    raw_payload = _write(
        tmp_path / "vv_raw.json",
        json.dumps(
            [
                {"Start": 0, "End": 60, "Content": " Hello world ", "Speaker": "A"},
                {"Start": 60, "End": 61, "Content": ""},
            ],
            ensure_ascii=False,
        ),
    )
    vv_env = _write(tmp_path / "vv_env.json", json.dumps({"transformers": "5.16.1"}, ensure_ascii=False))
    vv_run = _write(tmp_path / "vv_run.json", json.dumps({"parts": 1, "segments": 1}, ensure_ascii=False))
    env = _mock_env(tmp_path, payload=raw_payload, vv_env=vv_env, vv_run=vv_run)

    proc = _run_cloud_asr([
        str(wav),
        str(out),
        "demo",
        "zh",
        "--vv",
        "--terms",
        str(terms),
        "--terms-max",
        "2",
        "--json",
    ], env=env)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert "terms: 送出 2 / 共 3 個" in proc.stderr

    json_path = out / "demo_vibevoice.json"
    srt_path = out / "demo_vibevoice.srt"
    assert json_path.exists()
    assert srt_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload == [{"start": 0.0, "end": 60.0, "text": "Hello world"}]
    assert all(sorted(row) == ["end", "start", "text"] for row in payload)
    assert "Speaker" not in json_path.read_text(encoding="utf-8")

    assert srt_path.read_text(encoding="utf-8") == "1\n00:00:00,000 --> 00:01:00,000\nHello world\n"

    terms_sent = (out / "env" / "terms_sent.txt").read_text(encoding="utf-8").strip()
    assert terms_sent == "Alpha, ZQXV Capital"
    assert "ZQXV Capital" not in json_path.read_text(encoding="utf-8")


def test_vv_without_json_does_not_preserve_vv_json(tmp_path: Path) -> None:
    wav = _make_wav(tmp_path / "input.wav", seconds=1)
    out = tmp_path / "out"
    raw_payload = _write(
        tmp_path / "vv_raw.json",
        json.dumps([{"Start": 0, "End": 60, "Content": "Hello world"}], ensure_ascii=False),
    )
    env = _mock_env(tmp_path, payload=raw_payload)

    proc = _run_cloud_asr([str(wav), str(out), "demo", "zh", "--vv"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert not (out / "demo_vibevoice.json").exists()
    assert (out / "demo_vibevoice.srt").exists()


def test_vv_missing_audio_path_fails_without_stdout(tmp_path: Path) -> None:
    out = tmp_path / "out"
    env = _mock_env(tmp_path)

    proc = _run_cloud_asr([str(tmp_path / "missing.wav"), str(out), "demo", "zh", "--vv"], env=env)

    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "missing.wav" in proc.stderr


def test_vv_inference_failure_cleans_up_after_evidence(tmp_path: Path) -> None:
    wav = _make_wav(tmp_path / "input.wav", seconds=1)
    out = tmp_path / "out"
    raw_payload = _write(
        tmp_path / "vv_raw.json",
        json.dumps([{"Start": 0, "End": 60, "Content": "Hello world"}], ensure_ascii=False),
    )
    env = _mock_env(tmp_path, payload=raw_payload)
    env["CLOUD_VV_FAIL_STEP"] = "inference"

    proc = _run_cloud_asr([str(wav), str(out), "demo", "zh", "--vv"], env=env)

    assert proc.returncode != 0
    assert proc.stdout == ""
    assert (out / "env" / "pod_id.txt").read_text(encoding="utf-8").strip() == "mock-pod-1"
    assert (out / "env" / "terms_sent.txt").read_text(encoding="utf-8") == "\n"
    assert "action=terminate result=success" in (out / "env" / "pod_attempts.txt").read_text(encoding="utf-8")


def test_vv_install_gate_and_canary_are_pinned_in_source() -> None:
    source = CLOUD_ASR.read_text(encoding="utf-8")
    assert "transformers==5.16.1" in source
    assert 'expected transformers 5.16.x' in source
    assert "PASS AutoProcessor + VibeVoiceAsrForConditionalGeneration" in source
