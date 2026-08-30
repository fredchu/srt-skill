from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import MOCK_TIMEOUTS

REPO_ROOT = Path(__file__).resolve().parents[2]
FALLBACK = REPO_ROOT / "scripts" / "hallucination_fallback.sh"

REQUIRED_TOOLS = ["bash", "ffmpeg", "jq", "scp", "ssh", "python3"]
MISSING_TOOLS = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
pytestmark = pytest.mark.skipif(MISSING_TOOLS, reason=f"missing tools: {', '.join(MISSING_TOOLS)}")


def _make_media(path: Path, seconds: int = 2) -> Path:
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


def _write_hallucinated_srt(path: Path) -> Path:
    path.write_text(
        "\n".join(
            [
                "1",
                "00:00:00,000 --> 00:00:00,900",
                "哈哈哈哈哈哈哈哈哈哈哈哈哈哈",
                "",
                "2",
                "00:00:01,000 --> 00:00:01,800",
                "原本正常",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _mock_cloud_env(tmp: Path) -> dict[str, str]:
    ssh_key = tmp / "id_ed25519"
    ssh_pub = tmp / "id_ed25519.pub"
    ssh_key.write_text("dummy private key\n", encoding="utf-8")
    ssh_pub.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFakeFakeFakeFakeFakeFake fake@local\n", encoding="utf-8")

    payload = json.dumps(
        [
            {
                "start": 0.0,
                "end": 0.8,
                "text": "修復字幕",
                "words": [],
            }
        ],
        ensure_ascii=False,
    )

    env = os.environ.copy()
    env.update(
        {
            "SRT_ASR_ENGINE": "runpod",
            "RUNPOD_API_KEY": "test-token",
            "SSH_PRIVATE_KEY": str(ssh_key),
            "SSH_PUBLIC_KEY_PATH": str(ssh_pub),
            "CLOUD_ASR_TEST_HOOK": "provisioning_retry",
            "CLOUD_ASR_MOCK_FAIL_LABEL": "nope",
            "CLOUD_ASR_MOCK_READY_FROM_ATTEMPT": "1",
            "CLOUD_ASR_TEST_JSON_PAYLOAD": payload,
            "COST_CAP_USD": "0.123",
            "MAX_POD_ATTEMPTS": "1",
            "PATH": os.environ.get("PATH", ""),
            **MOCK_TIMEOUTS,
        }
    )
    return env


def test_runpod_branch_calls_cloud_largev3_with_fix_basename_and_inherits_env(tmp_path: Path) -> None:
    srt = _write_hallucinated_srt(tmp_path / "final.srt")
    media = _make_media(tmp_path / "clip.wav")
    prompt = "在Seeking Alpha上面\n第二行"
    env = _mock_cloud_env(tmp_path)

    proc = subprocess.run(
        ["bash", str(FALLBACK), str(srt), str(media), "00:00:00", "00:00:01", prompt],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    combined = proc.stdout + proc.stderr
    assert "budget cap: 0.123 USD" in combined

    evidence_match = re.search(r"RunPod evidence dir: (.+)", combined)
    assert evidence_match, combined
    evidence_dir = Path(evidence_match.group(1).strip())
    assert evidence_dir.exists(), evidence_dir

    request = json.loads((evidence_dir / "asr_request.json").read_text(encoding="utf-8"))
    assert request["model_id"] == "Systran/faster-whisper-large-v3"
    assert request["audio_path"] == "/root/in/_fix_segment.flac"
    assert request["json_path"] == "/root/out/_fix_segment.cloud.json"
    assert request["initial_prompt"] == prompt
    assert (evidence_dir / "prompt_sent.txt").read_text(encoding="utf-8") == prompt

    assert (tmp_path / "_fix_segment.cloud.json").exists(), "cloud basename 必須沿用 _fix_segment"
    assert not (tmp_path / "env").exists(), "每次 runpod fallback 不應再占用 WORK_DIR/env"


    patched = srt.read_text(encoding="utf-8")
    assert "修復字幕" in patched
    assert "哈哈哈哈哈哈哈哈哈哈哈哈哈哈" not in patched


def test_runpod_branch_supports_repeat_runs_in_same_workdir_and_preserves_evidence(tmp_path: Path) -> None:
    srt = _write_hallucinated_srt(tmp_path / "final.srt")
    media = _make_media(tmp_path / "clip.wav")
    env = _mock_cloud_env(tmp_path)

    proc1 = subprocess.run(
        ["bash", str(FALLBACK), str(srt), str(media), "00:00:00", "00:00:01", "第一次"],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )
    assert proc1.returncode == 0, proc1.stderr

    proc2 = subprocess.run(
        ["bash", str(FALLBACK), str(srt), str(media), "00:00:00", "00:00:01", "第二次"],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )
    assert proc2.returncode == 0, proc2.stderr

    match1 = re.search(r"RunPod evidence dir: (.+)", proc1.stdout + proc1.stderr)
    match2 = re.search(r"RunPod evidence dir: (.+)", proc2.stdout + proc2.stderr)
    assert match1 and match2

    evidence1 = Path(match1.group(1).strip())
    evidence2 = Path(match2.group(1).strip())
    assert evidence1 != evidence2
    assert evidence1.exists()
    assert evidence2.exists()
    assert (evidence1 / "asr_request.json").exists()
    assert (evidence2 / "asr_request.json").exists()

    run_dirs = sorted((tmp_path / ".cloud_asr_runs").glob("run.*"))
    assert len(run_dirs) >= 2
    assert (tmp_path / "_fix_segment.cloud.json").exists()


def test_runpod_branch_can_retry_same_workdir_after_failed_round(tmp_path: Path) -> None:
    srt = _write_hallucinated_srt(tmp_path / "final.srt")
    media = _make_media(tmp_path / "clip.wav")

    fail_env = _mock_cloud_env(tmp_path)
    fail_env["CLOUD_ASR_FAIL_STEP"] = "6"
    fail_proc = subprocess.run(
        ["bash", str(FALLBACK), str(srt), str(media), "00:00:00", "00:00:01", "先失敗"],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=fail_env,
    )
    assert fail_proc.returncode != 0

    ok_env = _mock_cloud_env(tmp_path)
    ok_proc = subprocess.run(
        ["bash", str(FALLBACK), str(srt), str(media), "00:00:00", "00:00:01", "再成功"],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=ok_env,
    )
    assert ok_proc.returncode == 0, ok_proc.stderr

    fail_match = re.search(r"RunPod evidence dir: (.+)", fail_proc.stdout + fail_proc.stderr)
    ok_match = re.search(r"RunPod evidence dir: (.+)", ok_proc.stdout + ok_proc.stderr)
    assert fail_match and ok_match
    assert Path(fail_match.group(1).strip()) != Path(ok_match.group(1).strip())


def test_mlx_branch_stays_local_and_keeps_whisper_args(tmp_path: Path) -> None:
    srt = _write_hallucinated_srt(tmp_path / "final.srt")
    media = _make_media(tmp_path / "clip.wav")

    trace = tmp_path / "mlx_args.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_mlx = fake_bin / "mlx_whisper"
    fake_mlx.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "trace=\"$MLX_TRACE\"\n"
        "printf '%s\\n' \"$@\" > \"$trace\"\n"
        "out_dir=\"\"\n"
        "out_name=\"\"\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    --output-dir) out_dir=\"$2\"; shift 2 ;;\n"
        "    --output-name) out_name=\"$2\"; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "cat > \"$out_dir/${out_name}.srt\" <<'EOF'\n"
        "1\n"
        "00:00:00,000 --> 00:00:00,800\n"
        "本地修復\n"
        "EOF\n",
        encoding="utf-8",
    )
    fake_mlx.chmod(0o755)

    prompt = "本地提示詞"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "SRT_ASR_ENGINE": "mlx",
            "MLX_TRACE": str(trace),
        }
    )

    proc = subprocess.run(
        ["bash", str(FALLBACK), str(srt), str(media), "00:00:00", "00:00:01", prompt],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    args = trace.read_text(encoding="utf-8").splitlines()
    assert "--model" in args
    assert "mlx-community/whisper-large-v3-mlx" in args
    assert "--initial-prompt" in args
    assert prompt in args

    patched = srt.read_text(encoding="utf-8")
    assert "本地修復" in patched
    assert "哈哈哈哈哈哈哈哈哈哈哈哈哈哈" not in patched
    assert not (tmp_path / "env").exists()
    assert not (tmp_path / "_fix_segment.cloud.json").exists()
