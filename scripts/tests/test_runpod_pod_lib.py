"""runpod_pod_lib.sh 的行為測試：用替身 transport 跑真的 lib 函式，不打 RunPod。

這些行為原本散在 cloud_asr.sh 與 bookcast_cloud.sh 兩邊，各自學到一半：
- bookcast 學到「清單裡剛砍的機器會以 TERMINATED 留一陣子」→ 要過濾
- cloud_asr 學到「DELETE 收到 404 代表已經不在」→ 算成功，但清單裡還在的假 404 不能吞
- 兩邊都學到「v2 的 GET /pods 沒有伺服器端 name 過濾」→ 名字要在本機比對
抽成共用後，這裡是唯一一份行為規格；兩支腳本的測試只驗「有沒有正確委派」。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
LIB = SCRIPTS / "runpod_pod_lib.sh"

FAKE_TRANSPORT = r"""
fake_transport() {
    local method="$1" path="$2" body_file="$3"
    printf '%s %s %s\n' "$method" "$path" "$(cat "$body_file" 2>/dev/null | tr -d '\n')" >>"$FAKE_CALLS"
    case "$method $path" in
        "GET /pods")
            [[ "${FAKE_LIST_RC:-0}" -eq 0 ]] || return "${FAKE_LIST_RC}"
            printf '%s\n' "$FAKE_PODS_JSON" ;;
        "GET /pods/"*)
            [[ -n "${FAKE_POD_JSON:-}" ]] || return 1
            printf '%s\n' "$FAKE_POD_JSON" ;;
        "DELETE /pods/"*)
            return "${FAKE_DELETE_RC:-0}" ;;
        "POST /pods/"*"/action")
            printf '{}\n' ;;
        *)
            return 1 ;;
    esac
}
RUNPOD_LIB_TRANSPORT=fake_transport
"""


def run_lib(body: str, tmp_path: Path, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update({"FAKE_CALLS": str(calls), "RUNPOD_API_KEY": "fake"})
    env.update(env_extra or {})
    harness = (
        "set -uo pipefail\n"
        f"source {shlex.quote(str(LIB))}\n"
        f"{FAKE_TRANSPORT}\n"
        f"{body}\n"
    )
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, env=env, timeout=60)


def calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()


def test_lib_has_valid_bash_syntax() -> None:
    subprocess.run(["bash", "-n", str(LIB)], check=True)


@pytest.mark.parametrize(
    "shape",
    [
        lambda pods: pods,
        lambda pods: {"pods": pods},
        lambda pods: {"data": pods},
    ],
    ids=["bare-array", "wrapped-pods", "wrapped-data"],
)
def test_live_list_unwraps_every_v2_shape_and_drops_terminated(tmp_path: Path, shape) -> None:
    pods = [
        {"id": "alive", "name": "bookcast-x", "status": "RUNNING"},
        {"id": "dead", "name": "bookcast-x", "status": "TERMINATED"},
        {"id": "dying", "name": "bookcast-x", "status": "Terminating"},
    ]
    r = run_lib("runpod_lib_pods_live_json", tmp_path, {"FAKE_PODS_JSON": json.dumps(shape(pods))})
    assert r.returncode == 0, r.stderr
    assert [p["id"] for p in json.loads(r.stdout)] == ["alive"]


def test_live_list_request_failure_is_rc2_not_empty(tmp_path: Path) -> None:
    r = run_lib("runpod_lib_pods_live_json; echo rc=$?", tmp_path, {"FAKE_PODS_JSON": "[]", "FAKE_LIST_RC": "1"})
    assert "rc=2" in r.stdout


def test_pod_live_in_list_rc_codes(tmp_path: Path) -> None:
    pods = json.dumps([{"id": "a", "status": "RUNNING"}, {"id": "t", "status": "TERMINATED"}])
    body = (
        "runpod_lib_pod_live_in_list a; echo a=$?\n"
        "runpod_lib_pod_live_in_list t; echo t=$?\n"
        "runpod_lib_pod_live_in_list zzz; echo zzz=$?\n"
    )
    r = run_lib(body, tmp_path, {"FAKE_PODS_JSON": pods})
    assert r.stdout.split() == ["a=0", "t=1", "zzz=1"], r.stdout + r.stderr
    r = run_lib("runpod_lib_pod_live_in_list a; echo rc=$?", tmp_path, {"FAKE_PODS_JSON": pods, "FAKE_LIST_RC": "1"})
    assert "rc=2" in r.stdout


def test_find_live_pod_by_name_filters_client_side_and_ignores_terminated(tmp_path: Path) -> None:
    pods = json.dumps({"pods": [
        {"id": "other", "name": "cloud-asr-1", "status": "RUNNING"},
        {"id": "old", "name": "bookcast-fixture", "status": "TERMINATED"},
        {"id": "target", "name": "bookcast-fixture", "status": "RUNNING"},
    ]})
    r = run_lib('runpod_lib_find_live_pod_by_name bookcast-fixture', tmp_path, {"FAKE_PODS_JSON": pods})
    assert r.stdout.strip() == "target", r.stderr
    r = run_lib('runpod_lib_find_live_pod_by_name nobody; echo "rc=$?"', tmp_path, {"FAKE_PODS_JSON": pods})
    assert r.stdout.split() == ["rc=0"]


def test_ssh_endpoint_prefers_ssh_direct_then_runtime_ports(tmp_path: Path) -> None:
    direct = json.dumps({"id": "p", "runtime": {"ports": []}, "ssh": {"direct": {"host": "1.2.3.4", "port": 2222}}})
    r = run_lib("runpod_lib_ssh_endpoint p", tmp_path, {"FAKE_POD_JSON": direct})
    assert r.stdout.rstrip("\n") == "1.2.3.4\t2222"

    ports = json.dumps({"pod": {"id": "p", "runtime": {"ports": [
        {"type": "tcp", "private": 22, "public": 40022, "ip": "5.6.7.8"}]}}})
    r = run_lib("runpod_lib_ssh_endpoint p", tmp_path, {"FAKE_POD_JSON": ports})
    assert r.stdout.rstrip("\n") == "5.6.7.8\t40022"

    nothing = json.dumps({"id": "p", "runtime": {"ports": []}})
    r = run_lib("runpod_lib_ssh_endpoint p; echo rc=$?", tmp_path, {"FAKE_POD_JSON": nothing})
    assert r.stdout.split() == ["rc=1"]

    legacy_v1_only = json.dumps({"id": "p", "publicIp": "9.9.9.9", "portMappings": {"22": 1}})
    r = run_lib("runpod_lib_ssh_endpoint p; echo rc=$?", tmp_path, {"FAKE_POD_JSON": legacy_v1_only})
    assert r.stdout.split() == ["rc=1"], "v1 欄位不得被當成端點"


def test_pod_action_uses_v2_action_endpoint_with_json_body(tmp_path: Path) -> None:
    r = run_lib("runpod_lib_pod_action p stop; echo rc=$?; runpod_lib_pod_action p start; echo rc=$?", tmp_path)
    assert r.stdout.split() == ["rc=0", "rc=0"], r.stderr
    assert calls(tmp_path) == [
        'POST /pods/p/action {"action":"stop"}',
        'POST /pods/p/action {"action":"start"}',
    ]
    r = run_lib("runpod_lib_pod_action p explode; echo rc=$?", tmp_path)
    assert "rc=1" in r.stdout
    assert calls(tmp_path) == []


def _terminate(tmp_path: Path, delete_rc: str, listed_after: bool) -> subprocess.CompletedProcess[str]:
    pods = json.dumps({"pods": [{"id": "p", "status": "RUNNING"}]}) if listed_after else "[]"
    body = 'runpod_lib_terminate_pod_once p; echo "rc=$?"; echo "err=$RUNPOD_LIB_TERMINATE_LAST_ERROR"'
    return run_lib(body, tmp_path, {"FAKE_PODS_JSON": pods, "FAKE_DELETE_RC": delete_rc})


def test_terminate_succeeds_only_when_pod_leaves_live_list(tmp_path: Path) -> None:
    r = _terminate(tmp_path, "0", listed_after=False)
    assert "rc=0" in r.stdout
    assert calls(tmp_path) == ["DELETE /pods/p ", "GET /pods "]

    r = _terminate(tmp_path, "0", listed_after=True)
    assert "rc=1" in r.stdout
    assert "still contains pod id=p" in r.stdout


def test_terminate_treats_404_as_gone_but_not_a_fake_404(tmp_path: Path) -> None:
    r = _terminate(tmp_path, "44", listed_after=False)
    assert "rc=0" in r.stdout
    assert "pod already absent" in r.stderr

    r = _terminate(tmp_path, "44", listed_after=True)
    assert "rc=1" in r.stdout, "API 說 404 但清單裡還在，不可以當成砍成功"
    assert "still contains pod id=p" in r.stdout


def test_terminate_reports_delete_failure(tmp_path: Path) -> None:
    r = _terminate(tmp_path, "1", listed_after=True)
    assert "rc=1" in r.stdout
    assert "err=RunPod DELETE /pods/p failed" in r.stdout
    assert calls(tmp_path) == ["DELETE /pods/p "], "DELETE 失敗就不該再去查清單"


def test_load_api_key_env_then_file_then_fail(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "runpod").mkdir(parents=True)
    body = 'runpod_lib_load_api_key; echo "rc=$? key=${RUNPOD_API_KEY:-}"'

    r = run_lib(body, tmp_path, {"HOME": str(home), "RUNPOD_API_KEY": "from-env"})
    assert "rc=0 key=from-env" in r.stdout

    (home / ".config" / "runpod" / "api_key").write_text("from-file\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("RUNPOD_API_KEY", None)
    env.update({"HOME": str(home), "FAKE_CALLS": str(tmp_path / "calls.log")})
    harness = f"set -uo pipefail\nsource {shlex.quote(str(LIB))}\n{body}\n"
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, env=env, timeout=60)
    assert "rc=0 key=from-file" in r.stdout, r.stderr

    (home / ".config" / "runpod" / "api_key").unlink()
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, env=env, timeout=60)
    assert "rc=1 key=" in r.stdout, r.stderr


def test_caller_can_override_log_functions(tmp_path: Path) -> None:
    body = (
        'runpod_lib_info() { echo "MINE: $*" >&2; }\n'
        "runpod_lib_terminate_pod_once p; echo rc=$?"
    )
    r = run_lib(body, tmp_path, {"FAKE_PODS_JSON": "[]", "FAKE_DELETE_RC": "44"})
    assert "rc=0" in r.stdout
    assert "MINE: pod already absent" in r.stderr
    assert "[runpod]" not in r.stderr


def test_port_endpoint_from_record_reads_runtime_ports_by_private_port(tmp_path: Path) -> None:
    rec = {"id": "p1", "runtime": {"ports": [
        {"private": 22, "public": 34446, "type": "tcp", "ip": "195.26.233.3"},
        {"private": 8000, "public": 30181, "type": "tcp", "ip": "195.26.233.3"},
    ]}}
    r = run_lib(f"runpod_lib_port_endpoint_from_record '{json.dumps(rec)}' 8000", tmp_path)
    assert r.stdout == "195.26.233.3\t30181\n"
    r = run_lib(f"runpod_lib_port_endpoint_from_record '{json.dumps({'id': 'p1', 'runtime': {'ports': []}})}' 8000; echo rc=$?", tmp_path)
    assert r.stdout.strip() == "rc=1"
