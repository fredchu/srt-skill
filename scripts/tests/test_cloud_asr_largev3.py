from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import MOCK_TIMEOUTS

REPO_ROOT = Path(__file__).resolve().parents[2]
CLOUD_ASR = REPO_ROOT / "scripts" / "cloud_asr.sh"

REQUIRED_TOOLS = ["bash", "ffmpeg", "jq", "scp", "ssh", "python3"]
MISSING_TOOLS = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
pytestmark = pytest.mark.skipif(MISSING_TOOLS, reason=f"missing tools: {', '.join(MISSING_TOOLS)}")


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


def _mock_env(tmp: Path) -> dict[str, str]:
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
            "CLOUD_ASR_MOCK_READY_FROM_ATTEMPT": "1",
            "CLOUD_ASR_TEST_JSON_PAYLOAD": json.dumps(
                [
                    {
                        "start": 0.0,
                        "end": 0.8,
                        "text": "hello",
                        "words": [],
                    }
                ],
                ensure_ascii=False,
            ),
            "PATH": os.environ.get("PATH", ""),
            **MOCK_TIMEOUTS,
        }
    )
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


def _with_terms_file(args: list[str], tmp: Path) -> list[str]:
    if "__TERMS__" not in args:
        return list(args)
    terms = tmp / "terms.txt"
    terms.write_text("Alpha\nBravo\n", encoding="utf-8")
    return [str(terms) if token == "__TERMS__" else token for token in args]


def _pod_attempts_text(out_dir: Path) -> str:
    path = out_dir / "env" / "pod_attempts.txt"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _assert_single_create_and_terminate(out_dir: Path) -> None:
    attempts = _pod_attempts_text(out_dir)
    assert attempts.count("action=create result=success") == 1, attempts
    assert attempts.count("action=terminate result=success") == 1, attempts


@pytest.mark.parametrize(
    "mode_args",
    [
        ["--breeze", "--initial-prompt", "x"],
        ["--vv", "--initial-prompt", "x"],
        ["--largev3", "--terms", "__TERMS__"],
        ["--breeze", "--terms", "__TERMS__"],
        ["--largev3", "--json"],
    ],
    ids=[
        "breeze+initial-prompt",
        "vv+initial-prompt",
        "largev3+terms",
        "breeze+terms",
        "largev3+json",
    ],
)
def test_paid_gate_invalid_combinations_fail_pre_create(tmp_path: Path, mode_args: list[str]) -> None:
    wav = _make_wav(tmp_path / "in.wav")
    out = tmp_path / "out"
    env = _mock_env(tmp_path)

    args = [str(wav), str(out), "demo", "zh", *_with_terms_file(mode_args, tmp_path)]
    proc = _run_cloud_asr(args, env=env)

    assert proc.returncode != 0
    assert "invalid argument" in proc.stderr
    assert "creating RunPod pod name=" not in proc.stderr
    assert "action=create" not in _pod_attempts_text(out)


@pytest.mark.parametrize(
    "mode_args",
    [
        ["--breeze"],
        ["--vv", "--terms", "__TERMS__", "--json"],
        ["--largev3", "--initial-prompt", "保留關鍵術語"],
    ],
    ids=["breeze-legal", "vv-legal-with-terms-json", "largev3-legal-with-prompt"],
)
def test_paid_gate_legal_controls_create_and_terminate_once(tmp_path: Path, mode_args: list[str]) -> None:
    wav = _make_wav(tmp_path / "in.wav")
    out = tmp_path / "out"
    env = _mock_env(tmp_path)

    args = [str(wav), str(out), "demo", "zh", *_with_terms_file(mode_args, tmp_path)]
    proc = _run_cloud_asr(args, env=env)

    assert proc.returncode == 0, proc.stderr
    _assert_single_create_and_terminate(out)


def test_mode_flag_four_states(tmp_path: Path) -> None:
    wav = _make_wav(tmp_path / "in.wav")
    out = tmp_path / "out"
    env = _mock_env(tmp_path)

    missing = _run_cloud_asr([str(wav), str(out), "demo", "zh"], env=env)
    assert missing.returncode != 0
    assert "missing model flag" in missing.stderr

    turbo = _run_cloud_asr([str(wav), str(out), "demo", "zh", "--turbo"], env=env)
    assert turbo.returncode != 0
    assert "rejects --turbo" in turbo.stderr

    breeze = _run_cloud_asr([str(wav), str(out / "breeze"), "demo", "zh", "--breeze"], env=env)
    assert breeze.returncode == 0, breeze.stderr

    largev3 = _run_cloud_asr([str(wav), str(out / "largev3"), "demo", "zh", "--largev3"], env=env)
    assert largev3.returncode == 0, largev3.stderr


def test_initial_prompt_only_allowed_on_largev3(tmp_path: Path) -> None:
    wav = _make_wav(tmp_path / "in.wav")
    env = _mock_env(tmp_path)

    proc = _run_cloud_asr(
        [str(wav), str(tmp_path / "out"), "demo", "zh", "--breeze", "--initial-prompt", "x"],
        env=env,
    )

    assert proc.returncode != 0
    assert "only valid with --largev3" in proc.stderr


def test_largev3_model_and_empty_prompt_omission(tmp_path: Path) -> None:
    wav = _make_wav(tmp_path / "in.wav")
    out = tmp_path / "out"
    env = _mock_env(tmp_path)

    proc = _run_cloud_asr(
        [str(wav), str(out), "demo", "zh", "--largev3", "--initial-prompt", ""],
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    request = json.loads((out / "env" / "asr_request.json").read_text(encoding="utf-8"))
    assert request["model_id"] == "Systran/faster-whisper-large-v3"
    assert request["language"] == "zh"
    assert request["audio_path"] == "/root/in/demo.flac"
    assert request["json_path"] == "/root/out/demo.cloud.json"
    assert "initial_prompt" not in request
    assert (out / "env" / "prompt_sent.txt").read_text(encoding="utf-8") == ""


def test_largev3_prompt_special_chars_roundtrip_literal(tmp_path: Path) -> None:
    wav = _make_wav(tmp_path / "in.wav")
    out = tmp_path / "out"
    env = _mock_env(tmp_path)
    prompt = "第一行\nquotes: ' \" \\ 😀\n第二行"

    proc = _run_cloud_asr(
        [str(wav), str(out), "demo", "zh", "--largev3", "--initial-prompt", prompt],
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    request = json.loads((out / "env" / "asr_request.json").read_text(encoding="utf-8"))
    assert request["initial_prompt"] == prompt
    assert request["audio_path"] == "/root/in/demo.flac"
    assert request["json_path"] == "/root/out/demo.cloud.json"
    assert (out / "env" / "prompt_sent.txt").read_text(encoding="utf-8") == prompt


def test_initial_prompt_env_is_ignored_without_noise(tmp_path: Path) -> None:
    wav = _make_wav(tmp_path / "in.wav")
    out = tmp_path / "out"
    env = _mock_env(tmp_path)
    env["INITIAL_PROMPT"] = "from-env-should-be-ignored"

    proc = _run_cloud_asr([str(wav), str(out), "demo", "zh", "--largev3"], env=env)

    assert proc.returncode == 0, proc.stderr
    request = json.loads((out / "env" / "asr_request.json").read_text(encoding="utf-8"))
    assert "initial_prompt" not in request
    assert "INITIAL_PROMPT environment variable" not in (proc.stdout + proc.stderr)


def test_request_upload_happens_before_collect_evidence_and_asr(tmp_path: Path) -> None:
    wav = _make_wav(tmp_path / "in.wav")
    out = tmp_path / "out"
    env = _mock_env(tmp_path)

    proc = _run_cloud_asr([str(wav), str(out), "demo", "zh", "--largev3"], env=env)

    assert proc.returncode == 0, proc.stderr
    combined = proc.stdout + proc.stderr
    upload_index = combined.find("uploading ASR request JSON")
    evidence_index = combined.find("collecting pod evidence")
    asr_index = combined.find("running faster-whisper model")
    assert upload_index != -1, combined
    assert evidence_index != -1, combined
    assert asr_index != -1, combined
    assert upload_index < evidence_index < asr_index, combined


def test_breeze_request_model_unchanged(tmp_path: Path) -> None:
    wav = _make_wav(tmp_path / "in.wav")
    out = tmp_path / "out"
    env = _mock_env(tmp_path)

    proc = _run_cloud_asr([str(wav), str(out), "demo", "zh", "--breeze"], env=env)

    assert proc.returncode == 0, proc.stderr
    request = json.loads((out / "env" / "asr_request.json").read_text(encoding="utf-8"))
    assert request["model_id"] == "SoybeanMilk/faster-whisper-Breeze-ASR-25"
    assert request["audio_path"] == "/root/in/demo.flac"
    assert request["json_path"] == "/root/out/demo.cloud.json"
    assert "initial_prompt" not in request


def test_source_uses_safe_json_prompt_serialization_and_shared_request_contract() -> None:
    source = CLOUD_ASR.read_text(encoding="utf-8")

    assert "--arg initial_prompt \"$ASR_INITIAL_PROMPT\"" in source
    assert "if isinstance(initial_prompt, str) and initial_prompt:" in source
    assert 'transcribe_kwargs["initial_prompt"] = initial_prompt' in source
    assert "printf '%q' \"$ASR_INITIAL_PROMPT\"" not in source
    assert "INITIAL_PROMPT environment variable is not supported" not in source

    request_read_expr = 'Path(os.environ["CLOUD_ASR_REQUEST_JSON"]).read_text(encoding="utf-8")'
    assert source.count(request_read_expr) == 2
    assert source.count("export CLOUD_ASR_REQUEST_JSON=$remote_request_json_q") == 2

    upload_pos = source.index('info "uploading ASR request JSON')
    evidence_pos = source.find('info "collecting pod evidence', upload_pos)
    asr_pos = source.find('info "running faster-whisper model', evidence_pos)
    assert evidence_pos != -1
    assert asr_pos != -1
    assert upload_pos < evidence_pos < asr_pos

    assert 'if request.get("audio_path") != remote_flac_path:' in source
    assert 'if request.get("json_path") != remote_json_path:' in source

    for token in [
        '"condition_on_previous_text": False',
        '"temperature": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]',
        '"compression_ratio_threshold": 2.4',
        '"log_prob_threshold": -1.0',
        '"no_speech_threshold": 0.6',
        '"beam_size": 1',
        '"word_timestamps": True',
    ]:
        assert token in source
