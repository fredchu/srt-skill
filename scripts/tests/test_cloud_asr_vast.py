"""cloud_asr.sh 走 Vast.ai（CLOUD_ASR_PROVIDER=vast）時的守門與換機行為。

用假 vastai 可執行檔（VAST_LIB_CLI）＋ CLOUD_ASR_TEST_HOOK（ssh/scp 替身）跑真腳本，不開機、不花錢。
驗的是錢的分界：
- 帳號沒 SSH 公鑰 → 開機前就停，一張報價都不試
- 沒有低於價格上限的報價 → 不呼叫 create
- 拉映像停滯 → 當「直連沒生成」砍機換一台，受 MAX_POD_ATTEMPTS 限制
- 死狀態（exited）→ 同上
- 一切正常 → 走完 boot_wait=running、SSH ready、CUDA、砍機確認，直連位置優先於跳板
- 預設 runpod 路徑不受影響（不設 CLOUD_ASR_PROVIDER 時看不到任何 vastai 呼叫）
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
CLOUD_ASR = SCRIPTS / "cloud_asr.sh"

FAKE_VASTAI = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$FAKE_CALLS"
sub=(); i=0; args=("$@")
while [[ $i -lt ${#args[@]} ]]; do
    case "${args[$i]}" in --api-key) i=$((i+2)); continue ;; --raw) i=$((i+1)); continue ;; esac
    sub+=("${args[$i]}"); i=$((i+1))
done
case "${sub[0]} ${sub[1]}" in
    "show ssh-keys")    printf '%s\n' "$FAKE_SSH_KEYS_JSON" ;;
    "search offers")    printf '%s\n' "${FAKE_OFFERS_JSON:-[]}" ;;
    "create instance")  n=$(grep -c "create instance" "$FAKE_CALLS"); printf '{"success":true,"new_contract":%s}\n' "$((9000 + n))" ;;
    "show instances")   printf '%s\n' "${FAKE_INSTANCES_JSON:-[]}" ;;
    "show instance")    printf '%s\n' "$FAKE_INSTANCE_JSON" ;;
    "destroy instance") exit 0 ;;
    *) exit 1 ;;
esac
"""

OFFERS = [{"id": i, "gpu_name": "RTX 5090", "dph_total": 0.30 + i * 0.01, "geolocation": "KR", "public_ipaddr": "180.189.55.43"} for i in range(1, 6)]
RUNNING = {"id": 9001, "actual_status": "running", "ssh_host": "ssh2.vast.ai", "ssh_port": 1,
           "public_ipaddr": "180.189.55.43", "ports": {"22/tcp": [{"HostPort": "41889"}]}, "cuda_max_good": 12.9, "geolocation": "KR"}


def _run(tmp_path: Path, env_extra: dict[str, str], provider: str | None = "vast") -> tuple[str, list[str], int]:
    wav = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "anullsrc=r=16000:cl=mono", "-t", "1", str(wav)], check=True)
    out = tmp_path / "out"; out.mkdir()
    fake = tmp_path / "vastai"; fake.write_text(FAKE_VASTAI, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    calls = tmp_path / "calls.log"; calls.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "CLOUD_ASR_TEST_HOOK": "provisioning_retry",
        # 預設值放這裡而不是假指令裡：bash 的 ${VAR:-[{}]} 會被 } 截斷
        "FAKE_SSH_KEYS_JSON": "[{}]",
        "FAKE_INSTANCE_JSON": json.dumps(RUNNING),
        "RUNPOD_API_KEY": "mock",
        "VAST_API_KEY": "mock",
        "VAST_LIB_CLI": str(fake),
        "FAKE_CALLS": str(calls),
        "POLL_INTERVAL_SECONDS": "1",
        "SSH_READY_TIMEOUT_SECONDS": "3",
        "SSH_PROBE_TIMEOUT_SECONDS": "1",
        "MAX_POD_ATTEMPTS": "2",
    })
    if provider:
        env["CLOUD_ASR_PROVIDER"] = provider
    else:
        env.pop("CLOUD_ASR_PROVIDER", None)
    env.update(env_extra)
    r = subprocess.run(["bash", str(CLOUD_ASR), str(wav), str(out), "demo", "zh", "--breeze"],
                       capture_output=True, text=True, env=env, timeout=240)
    return r.stdout + r.stderr, calls.read_text(encoding="utf-8").splitlines(), r.returncode


def test_no_ssh_key_stops_before_any_offer(tmp_path: Path) -> None:
    log, calls, rc = _run(tmp_path, {"FAKE_SSH_KEYS_JSON": "[]", "FAKE_OFFERS_JSON": json.dumps(OFFERS)})
    assert rc != 0 and "no SSH key on the Vast.ai account" in log
    assert not any("search offers" in c or "create instance" in c for c in calls)


def test_no_offer_under_cap_stops_before_create(tmp_path: Path) -> None:
    log, calls, rc = _run(tmp_path, {"FAKE_OFFERS_JSON": json.dumps(OFFERS), "VAST_MAX_DPH": "0.20"})
    assert rc != 0 and "no Vast.ai offer matches" in log
    assert not any("create instance" in c for c in calls)


def test_stalled_pull_swaps_instance_up_to_max_attempts(tmp_path: Path) -> None:
    stuck = {"id": 9001, "actual_status": "loading", "ssh_host": "ssh2.vast.ai", "ssh_port": 1,
             "status_msg": "tag: Pulling from vastai/pytorch\n"}
    log, calls, rc = _run(tmp_path, {"FAKE_OFFERS_JSON": json.dumps(OFFERS), "FAKE_INSTANCE_JSON": json.dumps(stuck),
                                     "VAST_BOOT_STALL_MIN": "0"})
    assert rc != 0
    assert log.count("action=create result=success") == 2, log[-1500:]
    assert log.count("action=boot_wait result=stalled") == 2
    assert log.count("action=terminate result=success") == 2
    assert "after 2 pod(s); giving up" in log
    assert [c for c in calls if "destroy instance" in c] and len([c for c in calls if "destroy instance" in c]) == 2


def test_dead_status_swaps_instance(tmp_path: Path) -> None:
    dead = {"id": 9001, "actual_status": "exited", "ssh_host": "ssh2.vast.ai", "ssh_port": 1}
    log, calls, rc = _run(tmp_path, {"FAKE_OFFERS_JSON": json.dumps(OFFERS), "FAKE_INSTANCE_JSON": json.dumps(dead),
                                     "MAX_POD_ATTEMPTS": "1"})
    assert rc != 0
    assert "action=boot_wait result=dead status=exited" in log
    assert "action=terminate result=success" in log
    assert "after 1 pod(s); giving up" in log


def test_happy_path_uses_direct_endpoint_and_terminates(tmp_path: Path) -> None:
    log, calls, rc = _run(tmp_path, {"FAKE_OFFERS_JSON": json.dumps(OFFERS), "FAKE_INSTANCE_JSON": json.dumps(RUNNING)})
    assert "action=boot_wait result=running" in log
    assert "SSH ready at root@180.189.55.43 -p 41889" in log, log[-1500:]
    assert "CUDA validated" in log
    assert "action=terminate result=success" in log
    assert any("create instance 1 " in c and "vastai/pytorch:2.8.0-cu128-cuda-12.9-mini-py312-2026-09-01" in c and "--label cloud-asr-" in c for c in calls), calls
    assert any("destroy instance 9001 -y" in c for c in calls)


def test_default_provider_never_touches_vastai(tmp_path: Path) -> None:
    log, calls, rc = _run(tmp_path, {"FAKE_OFFERS_JSON": json.dumps(OFFERS)}, provider=None)
    assert calls == [], calls
    assert "creating RunPod pod" in log
