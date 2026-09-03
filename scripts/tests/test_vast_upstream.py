"""vast 引擎在上游各支的接線：subtitle.sh、hallucination_fallback.sh、srt_hallucination_fix.py、cloud_asr.sh 的 VV 模式。

全部用假 vastai（VAST_LIB_CLI）＋ CLOUD_ASR_TEST_HOOK，不開機、不花錢。
驗的是「vast 這個值有沒有被當成雲端引擎、平台有沒有正確傳到 cloud_asr.sh」。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from test_cloud_asr_vast import FAKE_VASTAI, OFFERS, RUNNING

SCRIPTS = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS.parent
SUBTITLE = SCRIPTS / "subtitle.sh"
FALLBACK = SCRIPTS / "hallucination_fallback.sh"
CLOUD_ASR = SCRIPTS / "cloud_asr.sh"


def _install_fake_vastai(tmp: Path) -> dict[str, str]:
    fake = tmp / "vastai"
    fake.write_text(FAKE_VASTAI, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    calls = tmp / "vastai_calls.log"
    calls.write_text("", encoding="utf-8")
    return {
        "VAST_API_KEY": "mock",
        "VAST_LIB_CLI": str(fake),
        "FAKE_CALLS": str(calls),
        "FAKE_SSH_KEYS_JSON": "[{}]",
        "FAKE_OFFERS_JSON": json.dumps(OFFERS),
        "FAKE_INSTANCE_JSON": json.dumps(RUNNING),
        "FAKE_INSTANCES_JSON": "[]",
    }


def _vastai_calls(tmp: Path) -> list[str]:
    return (tmp / "vastai_calls.log").read_text(encoding="utf-8").splitlines()


# ---------- subtitle.sh ----------

def test_subtitle_accepts_vast_engine_and_rejects_unknown(tmp_path: Path) -> None:
    env = dict(os.environ)
    r = subprocess.run(["bash", str(SUBTITLE), "--engine=vast", str(tmp_path / "nope.mp4")],
                       capture_output=True, text=True, env=env, timeout=60)
    out = r.stdout + r.stderr
    assert "未知引擎" not in out
    assert "找不到檔案" in out, out[-600:]
    r = subprocess.run(["bash", str(SUBTITLE), "--engine=lambda", str(tmp_path / "nope.mp4")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert "未知引擎：lambda" in (r.stdout + r.stderr)
    env["SRT_ASR_ENGINE"] = "vast"
    r = subprocess.run(["bash", str(SUBTITLE), str(tmp_path / "nope.mp4")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert "未知引擎" not in (r.stdout + r.stderr)


def test_subtitle_passes_engine_name_as_provider_to_cloud_asr() -> None:
    src = SUBTITLE.read_text(encoding="utf-8")
    # 平台名必須跟著引擎名走，且只給 cloud_asr.sh 這一次呼叫（不 export）
    assert 'CLOUD_ASR_PROVIDER="$ASR_ENGINE" "$CLOUD_ASR"' in src
    assert "export CLOUD_ASR_PROVIDER" not in src
    # 雲端判斷不准再寫死 runpod
    assert '[ "$ASR_ENGINE" = "runpod" ]' not in src


# ---------- hallucination_fallback.sh ----------

def test_fallback_vast_engine_runs_cloud_asr_with_vast_provider(tmp_path: Path) -> None:
    from test_hallucination_fallback_cloud import _mock_cloud_env, _make_media, _write_hallucinated_srt
    srt = _write_hallucinated_srt(tmp_path / "final.srt")
    media = _make_media(tmp_path / "clip.wav")
    env = _mock_cloud_env(tmp_path)
    env["SRT_ASR_ENGINE"] = "vast"
    env.update(_install_fake_vastai(tmp_path))
    proc = subprocess.run(["bash", str(FALLBACK), str(srt), str(media), "00:00:00", "00:00:01"],
                          cwd=REPO_ROOT, capture_output=True, text=True, env=env, timeout=240)
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined[-1500:]
    assert "Vast.ai cloud ASR run dir:" in combined
    calls = _vastai_calls(tmp_path)
    assert any("create instance" in c for c in calls), calls
    assert any("destroy instance" in c for c in calls), calls


# ---------- srt_hallucination_fix.py ----------

def test_fix_treats_vast_as_cloud_engine(tmp_path: Path, monkeypatch, capsys) -> None:
    sys.path.insert(0, str(SCRIPTS / "srt_correct"))
    import test_srt_hallucination_fix_runpod_call_dirs as t
    fixer = t.fixer
    t._reset_asr_state()
    input_srt = t._write_input_srt(tmp_path / "demo.srt")
    media = tmp_path / "demo.wav"; media.write_bytes(b"placeholder")
    fake_root = tmp_path / "fake_repo"
    t._write_fake_subtitle(fake_root / "scripts" / "subtitle.sh")
    monkeypatch.setattr(fixer, "__file__", str(fake_root / "scripts" / "srt_correct" / "srt_hallucination_fix.py"))
    count_file = tmp_path / "count.txt"
    monkeypatch.setenv("SRT_FIX_MOCK_COUNT_FILE", str(count_file))
    monkeypatch.setenv("SRT_ASR_ENGINE", "vast")
    monkeypatch.setenv("SRT_ASR_MAX_CALLS", "0")
    monkeypatch.setattr(fixer, "_extract_with_buffer", lambda _m, _a, _b, out: (Path(out).write_bytes(b"seg"), True)[1])
    monkeypatch.setattr(sys, "argv", ["srt_hallucination_fix.py", str(input_srt), str(media), "--breeze"])
    fixer.main()
    err = capsys.readouterr().err
    assert "Vast.ai call dir:" in err
    assert sorted(p.name[:9] for p in (tmp_path / ".cloud_asr_runs").glob("call-*")) == ["call-001.", "call-002.", "call-003."]
    assert not (tmp_path / "env").exists()


# ---------- cloud_asr.sh --vv on vast ----------

def test_vv_mode_on_vast_boots_and_terminates(tmp_path: Path) -> None:
    from test_cloud_asr_vv import _mock_env, _write, _run_cloud_asr, _make_wav  # type: ignore
    wav = _make_wav(tmp_path / "demo.wav")
    out = tmp_path / "out"; out.mkdir()
    raw_payload = _write(tmp_path / "vv.json", json.dumps([{"start": 0.0, "end": 0.5, "text": "你好"}], ensure_ascii=False))
    vv_env = _write(tmp_path / "vv_env.json", json.dumps({"transformers": "5.16.1"}))
    vv_run = _write(tmp_path / "vv_run.json", json.dumps({"parts": 1, "segments": 1}))
    env = _mock_env(tmp_path, payload=raw_payload, vv_env=vv_env, vv_run=vv_run)
    env["CLOUD_ASR_PROVIDER"] = "vast"
    env.update(_install_fake_vastai(tmp_path))
    proc = _run_cloud_asr([str(wav), str(out), "demo", "zh", "--vv"], env=env)
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert "action=boot_wait result=running" in proc.stderr
    assert "SSH ready at root@180.189.55.43 -p 41889" in proc.stderr
    assert "action=terminate result=success" in proc.stderr
    calls = _vastai_calls(tmp_path)
    assert any("create instance" in c and "--label cloud-asr-" in c for c in calls), calls
    assert any("destroy instance 9001 -y" in c for c in calls), calls
