"""vast_instance_lib.sh 的行為測試：用一支假的 vastai 可執行檔跑真的 lib 函式，不打 Vast.ai。

要驗的都是「錢」的規則：
- 價格上限在本機再濾一次，伺服器端回了超價的報價也不能開
- 開機拿不到 new_contract 就是失敗，且失敗原因要從 stdout 帶回（呼叫端在 $(…) 裡）
- 砍機後回查清單，清單裡還在就不准說砍成功；destroy 回非 0 但清單已不在，算成功
- SSH 端點優先直連（public_ipaddr + ports["22/tcp"]），ports 還是 null 時退回跳板 ssh_host/ssh_port
- exited / unknown / offline 是永遠起不來的狀態
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
LIB = SCRIPTS / "vast_instance_lib.sh"

# 假 vastai：把收到的參數記進 FAKE_CALLS，依子指令回 FAKE_* 環境變數指定的 JSON。
FAKE_VASTAI = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$FAKE_CALLS"
args=("$@")
# 去掉 --api-key X 與 --raw，剩下的第一、二個字才是子指令
sub=()
i=0
while [[ $i -lt ${#args[@]} ]]; do
    case "${args[$i]}" in
        --api-key) i=$((i+2)); continue ;;
        --raw) i=$((i+1)); continue ;;
    esac
    sub+=("${args[$i]}"); i=$((i+1))
done
case "${sub[0]} ${sub[1]}" in
    "search offers")   [[ "${FAKE_SEARCH_RC:-0}" -eq 0 ]] || exit "$FAKE_SEARCH_RC"; printf '%s\n' "$FAKE_OFFERS_JSON" ;;
    "create instance") [[ "${FAKE_CREATE_RC:-0}" -eq 0 ]] || { printf '%s\n' "${FAKE_CREATE_JSON:-}"; exit "$FAKE_CREATE_RC"; }; printf '%s\n' "$FAKE_CREATE_JSON" ;;
    "show instances")  [[ "${FAKE_LIST_RC:-0}" -eq 0 ]] || exit "$FAKE_LIST_RC"; printf '%s\n' "$FAKE_INSTANCES_JSON" ;;
    "show instance")   [[ -n "${FAKE_INSTANCE_JSON:-}" ]] || exit 1; printf '%s\n' "$FAKE_INSTANCE_JSON" ;;
    "destroy instance") exit "${FAKE_DESTROY_RC:-0}" ;;
    "stop instance"|"start instance") printf '{}\n' ;;
    *) exit 1 ;;
esac
"""


def _install_fake(tmp_path: Path) -> Path:
    fake = tmp_path / "vastai"
    fake.write_text(FAKE_VASTAI, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def run_lib(body: str, tmp_path: Path, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    fake = _install_fake(tmp_path)
    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update({"FAKE_CALLS": str(calls), "VAST_API_KEY": "fake", "VAST_LIB_CLI": str(fake)})
    env.update(env_extra or {})
    harness = f"set -uo pipefail\nsource {shlex.quote(str(LIB))}\n{body}\n"
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, env=env, timeout=60)


def calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()


def test_lib_has_valid_bash_syntax() -> None:
    subprocess.run(["bash", "-n", str(LIB)], check=True)


# ---------- 挑報價 ----------

OFFERS = [
    {"id": 3, "gpu_name": "RTX 5090", "dph_total": 0.55, "geolocation": "US", "reliability2": 0.99, "inet_down": 500},
    # 流量單價 0.05 超過預設上限 0.03，不能出現
    {"id": 8, "gpu_name": "RTX 5090", "dph_total": 0.30, "geolocation": "US", "inet_down_cost": 0.05, "inet_up_cost": 0.05},
    {"id": 7, "gpu_name": "RTX 5090", "dph_total": 0.20, "geolocation": "US", "public_ipaddr": "137.175.76.24"},
    {"id": 1, "gpu_name": "RTX 5090", "dph_total": 0.35, "geolocation": "US", "reliability2": 0.98, "inet_down": 300},
    {"id": 9, "gpu_name": "RTX 5090", "dph_total": 0.95, "geolocation": "KR", "reliability2": 0.99, "inet_down": 900},
]


def test_pick_offers_sorts_by_price_and_drops_over_cap_locally(tmp_path: Path) -> None:
    # 伺服器回了一張 0.95 的（假裝它沒理會 dph_total<=），本機必須自己濾掉
    r = run_lib('vast_lib_pick_offers "RTX 5090" 40 0.6', tmp_path, {"FAKE_OFFERS_JSON": json.dumps(OFFERS)})
    assert r.returncode == 0, r.stderr
    rows = [line.split("\t") for line in r.stdout.splitlines()]
    # 7 最便宜但 IP 在預設排除的 137.175. 網段，不能出現
    assert [row[0] for row in rows] == ["1", "3"]
    assert "0.35" in rows[0][1] and "RTX 5090" in rows[0][1]
    query = calls(tmp_path)[0]
    assert "gpu_name=RTX_5090" in query and "dph_total<=0.6" in query and 'geolocation notin ["CN","VN"]' in query


def test_pick_offers_ip_exclude_override_and_empty_disables(tmp_path: Path) -> None:
    env = {"FAKE_OFFERS_JSON": json.dumps(OFFERS)}
    r = run_lib('vast_lib_pick_offers "RTX 5090" 40 0.6 "" "" ""', tmp_path, env)
    assert [line.split("\t")[0] for line in r.stdout.splitlines()] == ["7", "1", "3"]
    r = run_lib('vast_lib_pick_offers "RTX 5090" 40 0.6 "" "" "137.175.,1.2."', tmp_path, env)
    assert [line.split("\t")[0] for line in r.stdout.splitlines()] == ["1", "3"]


def test_status_msg_accessor(tmp_path: Path) -> None:
    r = run_lib('vast_lib_instance_status_msg 1', tmp_path, {"FAKE_INSTANCE_JSON": json.dumps({"id": 1, "status_msg": "x: Pulling fs layer\n"})})
    assert r.stdout == "x: Pulling fs layer\n\n"


def test_pick_offers_requires_static_ip_by_default_and_can_be_disabled(tmp_path: Path) -> None:
    run_lib('vast_lib_pick_offers "RTX 5090" 40 0.6', tmp_path, {"FAKE_OFFERS_JSON": "[]"})
    q = calls(tmp_path)[0]
    assert " static_ip=true" in q and "cuda_vers>=12.9" in q
    run_lib('vast_lib_pick_offers "RTX 5090" 40 0.6', tmp_path, {"FAKE_OFFERS_JSON": "[]", "VAST_LIB_REQUIRE_STATIC_IP": "0", "VAST_LIB_MIN_CUDA": "12.8"})
    q = calls(tmp_path)[0]
    assert "static_ip=true" not in q and "cuda_vers>=12.8" in q


def test_pick_offers_returns_1_when_nothing_under_cap(tmp_path: Path) -> None:
    r = run_lib('vast_lib_pick_offers "RTX 5090" 40 0.30; echo "rc=$?"', tmp_path, {"FAKE_OFFERS_JSON": json.dumps(OFFERS)})
    assert r.stdout.strip() == "rc=1"


def test_pick_offers_returns_1_when_cli_fails(tmp_path: Path) -> None:
    r = run_lib('vast_lib_pick_offers "RTX 5090" 40 0.6; echo "rc=$?"', tmp_path,
                {"FAKE_OFFERS_JSON": "[]", "FAKE_SEARCH_RC": "3"})
    assert r.stdout.strip() == "rc=1"


def test_pick_offers_extra_query_and_geo_override(tmp_path: Path) -> None:
    run_lib('vast_lib_pick_offers "RTX 4090" 40 0.6 "" "inet_down>=800"', tmp_path, {"FAKE_OFFERS_JSON": "[]"})
    query = calls(tmp_path)[0]
    assert "geolocation notin" not in query
    assert query.rstrip().endswith("inet_down>=800 --order dph_total --limit 20 --raw")


# ---------- 開機 ----------

def test_create_instance_returns_new_contract(tmp_path: Path) -> None:
    r = run_lib('vast_lib_create_instance 123 img:tag 40 bookcast-x', tmp_path,
                {"FAKE_CREATE_JSON": json.dumps({"success": True, "new_contract": 777})})
    assert r.returncode == 0 and r.stdout.strip() == "777"
    c = calls(tmp_path)[0]
    for flag in ("create instance 123", "--image img:tag", "--disk 40", "--label bookcast-x", "--ssh", "--direct", "--cancel-unavail"):
        assert flag in c


def test_create_instance_failure_prints_reason_to_stdout(tmp_path: Path) -> None:
    # 呼叫端在 $(…) 裡呼叫，全域變數帶不回去，所以原因一定要走 stdout
    r = run_lib('out="$(vast_lib_create_instance 123 img 40 lbl)"; echo "rc=$? out=$out"', tmp_path,
                {"FAKE_CREATE_JSON": json.dumps({"success": False, "msg": "offer taken"}), "FAKE_CREATE_RC": "1"})
    assert r.stdout.startswith("rc=1 out=rc=1 resp=")
    assert "offer taken" in r.stdout


def test_create_instance_success_false_is_failure_even_with_rc0(tmp_path: Path) -> None:
    r = run_lib('vast_lib_create_instance 123 img 40 lbl; echo "rc=$?"', tmp_path,
                {"FAKE_CREATE_JSON": json.dumps({"success": False, "new_contract": 5})})
    assert r.stdout.splitlines()[-1] == "rc=1"


# ---------- 清單與依名字找 ----------

INSTANCES = [
    {"id": 10, "label": "bookcast-a", "actual_status": "running"},
    {"id": 11, "label": "bookcast-b", "actual_status": "destroyed"},
    {"id": 12, "label": "srt-c", "actual_status": "running"},
]


def test_find_by_label_ignores_destroyed_and_other_labels(tmp_path: Path) -> None:
    env = {"FAKE_INSTANCES_JSON": json.dumps(INSTANCES)}
    assert run_lib('vast_lib_find_live_instance_by_label bookcast-a', tmp_path, env).stdout.strip() == "10"
    assert run_lib('vast_lib_find_live_instance_by_label bookcast-b', tmp_path, env).stdout.strip() == ""
    assert run_lib('vast_lib_find_live_instance_by_label nope', tmp_path, env).stdout.strip() == ""


def test_live_in_list_distinguishes_absent_from_query_failure(tmp_path: Path) -> None:
    env = {"FAKE_INSTANCES_JSON": json.dumps(INSTANCES)}
    assert run_lib('vast_lib_instance_live_in_list 10; echo "rc=$?"', tmp_path, env).stdout.strip() == "rc=0"
    assert run_lib('vast_lib_instance_live_in_list 99; echo "rc=$?"', tmp_path, env).stdout.strip() == "rc=1"
    r = run_lib('vast_lib_instance_live_in_list 10; echo "rc=$?"', tmp_path, {**env, "FAKE_LIST_RC": "2"})
    assert r.stdout.strip() == "rc=2"


# ---------- SSH 端點與狀態 ----------

def test_ssh_endpoint_prefers_direct_port_map_over_proxy(tmp_path: Path) -> None:
    rec = {"id": 1, "ssh_host": "ssh2.vast.ai", "ssh_port": 14978, "public_ipaddr": "9.9.9.9",
           "ports": {"22/tcp": [{"HostPort": "50022"}]}}
    r = run_lib('vast_lib_ssh_endpoint 1', tmp_path, {"FAKE_INSTANCE_JSON": json.dumps(rec)})
    assert r.stdout == "9.9.9.9\t50022\n"


def test_ssh_endpoint_falls_back_to_proxy_while_ports_null(tmp_path: Path) -> None:
    # 2026-09-03 實測：loading 階段 ports=null、public_ipaddr 已有值，此時只能走跳板
    rec = {"id": 1, "ssh_host": "ssh2.vast.ai", "ssh_port": 14978, "public_ipaddr": "9.9.9.9", "ports": None}
    r = run_lib('vast_lib_ssh_endpoint 1', tmp_path, {"FAKE_INSTANCE_JSON": json.dumps(rec)})
    assert r.stdout == "ssh2.vast.ai\t14978\n"


def test_ssh_endpoint_rc1_when_not_ready_and_unwraps_list(tmp_path: Path) -> None:
    r = run_lib('vast_lib_ssh_endpoint 1; echo "rc=$?"', tmp_path,
                {"FAKE_INSTANCE_JSON": json.dumps([{"id": 1, "actual_status": None}])})
    assert r.stdout.strip() == "rc=1"


def test_status_lowercases_and_null_when_provisioning(tmp_path: Path) -> None:
    assert run_lib('vast_lib_instance_status 1', tmp_path, {"FAKE_INSTANCE_JSON": json.dumps({"id": 1, "actual_status": "Running"})}).stdout.strip() == "running"
    assert run_lib('vast_lib_instance_status 1', tmp_path, {"FAKE_INSTANCE_JSON": json.dumps({"id": 1})}).stdout.strip() == "null"


@pytest.mark.parametrize("status,dead", [("exited", True), ("unknown", True), ("offline", True),
                                          ("running", False), ("loading", False), ("null", False), ("created", False)])
def test_status_is_dead(tmp_path: Path, status: str, dead: bool) -> None:
    r = run_lib(f'vast_lib_status_is_dead {status}; echo "rc=$?"', tmp_path)
    assert r.stdout.strip() == ("rc=0" if dead else "rc=1")


# ---------- 砍機 ----------

def test_terminate_confirms_absence_from_list(tmp_path: Path) -> None:
    r = run_lib('vast_lib_terminate_instance_once 10; echo "rc=$?"', tmp_path,
                {"FAKE_INSTANCES_JSON": json.dumps([i for i in INSTANCES if i["id"] != 10])})
    assert r.stdout.strip() == "rc=0"
    assert any(c.startswith("destroy instance 10 -y") or "destroy instance 10 -y" in c for c in calls(tmp_path))


def test_terminate_refuses_success_when_still_listed(tmp_path: Path) -> None:
    r = run_lib('vast_lib_terminate_instance_once 10; echo "rc=$? err=$VAST_LIB_TERMINATE_LAST_ERROR"', tmp_path,
                {"FAKE_INSTANCES_JSON": json.dumps(INSTANCES)})
    assert r.stdout.strip().startswith("rc=1 err=Vast list still contains instance id=10")


def test_terminate_destroy_nonzero_but_absent_is_success(tmp_path: Path) -> None:
    r = run_lib('vast_lib_terminate_instance_once 10; echo "rc=$?"', tmp_path,
                {"FAKE_INSTANCES_JSON": "[]", "FAKE_DESTROY_RC": "1"})
    assert r.stdout.strip() == "rc=0"
    assert "already absent" in r.stderr


def test_terminate_list_failure_is_not_success(tmp_path: Path) -> None:
    r = run_lib('vast_lib_terminate_instance_once 10; echo "rc=$? err=$VAST_LIB_TERMINATE_LAST_ERROR"', tmp_path,
                {"FAKE_INSTANCES_JSON": "[]", "FAKE_LIST_RC": "2"})
    assert r.stdout.strip().startswith("rc=1 err=vastai show instances failed")


# ---------- 金鑰 ----------

def test_load_api_key_env_then_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "vastai").mkdir(parents=True)
    (home / ".config" / "vastai" / "vast_api_key").write_text("filekey\n", encoding="utf-8")
    env = dict(os.environ); env["HOME"] = str(home); env.pop("VAST_API_KEY", None)
    harness = f"set -u\nsource {shlex.quote(str(LIB))}\nvast_lib_load_api_key; echo \"rc=$? key=$VAST_API_KEY\""
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, env=env)
    assert r.stdout.strip() == "rc=0 key=filekey"
    env["HOME"] = str(tmp_path / "nohome")
    r = subprocess.run(["bash", "-c", harness.replace("$VAST_API_KEY", "${VAST_API_KEY:-}")], capture_output=True, text=True, env=env)
    assert r.stdout.strip() == "rc=1 key="


def test_api_key_is_passed_explicitly_to_cli(tmp_path: Path) -> None:
    run_lib('vast_lib_instances_live_json >/dev/null', tmp_path, {"FAKE_INSTANCES_JSON": "[]", "VAST_API_KEY": "sekret"})
    assert calls(tmp_path)[0] == "--api-key sekret show instances --raw"


# ---------- args 模式開機與服務埠（book-translator 的 vLLM） ----------

def test_create_instance_args_mode_passes_env_and_args_without_ssh(tmp_path: Path) -> None:
    r = run_lib('vast_lib_create_instance_args 55 vllm/vllm-openai:v0.28.0 80 bt-x "-p 8000:8000 -e HF_TOKEN=t" --model m --port 8000 --api-key k',
                tmp_path, {"FAKE_CREATE_JSON": json.dumps({"success": True, "new_contract": 8801})})
    assert r.stdout.strip() == "8801", r.stderr
    c = calls(tmp_path)[0]
    # --raw 必須在 --args 前面，否則會被吃進容器參數
    assert "create instance 55 --image vllm/vllm-openai:v0.28.0 --disk 80 --label bt-x --cancel-unavail --env -p 8000:8000 -e HF_TOKEN=t --raw --args --model m --port 8000 --api-key k" in c
    assert not c.endswith("--raw")
    assert "--ssh" not in c and "--direct" not in c


def test_port_endpoint_from_record_reads_mapped_port_or_rc1(tmp_path: Path) -> None:
    rec = {"id": 1, "public_ipaddr": "9.9.9.9", "ports": {"22/tcp": [{"HostPort": "41889"}], "8000/tcp": [{"HostPort": "40123"}]}}
    r = run_lib(f"vast_lib_port_endpoint_from_record '{json.dumps(rec)}' 8000", tmp_path)
    assert r.stdout == "9.9.9.9\t40123\n"
    r = run_lib(f"vast_lib_port_endpoint_from_record '{json.dumps({'id': 1, 'public_ipaddr': '9.9.9.9', 'ports': None})}' 8000; echo rc=$?", tmp_path)
    assert r.stdout.strip() == "rc=1"


def test_create_parses_python_dict_style_response_from_args_mode(tmp_path: Path) -> None:
    # 2026-09-04 實測：args 模式 vastai 印 "Started. {'success': True, 'new_contract': 49767561, ...}"
    # 不是 JSON；解析失敗會被當「開不起來」而換下一張，一口氣漏了三台。
    resp = "Started. {'success': True, 'new_contract': 49767561, 'instance_api_key': 'abc'}"
    r = run_lib(f"vast_lib_parse_new_contract {json.dumps(resp)}; echo", tmp_path)
    assert r.stdout.strip() == "49767561"
    r = run_lib("vast_lib_parse_new_contract \"{'success': False, 'new_contract': 5}\"; echo", tmp_path)
    assert r.stdout.strip() == ""
    r = run_lib("vast_lib_parse_new_contract '{\"success\": true, \"new_contract\": 7}'; echo", tmp_path)
    assert r.stdout.strip() == "7"


def test_pick_offers_ranks_by_estimated_run_cost_not_hourly_price(tmp_path: Path) -> None:
    # A 每小時便宜但流量貴；B 每小時貴但流量免費。跑 2 小時＋下載 30 GB 時 B 該排前面。
    offers = [
        {"id": 1, "gpu_name": "RTX 5090", "dph_total": 0.40, "inet_down_cost": 0.026, "inet_up_cost": 0.026},
        {"id": 2, "gpu_name": "RTX 5090", "dph_total": 0.48, "inet_down_cost": 0.0, "inet_up_cost": 0.0},
    ]
    r = run_lib('vast_lib_pick_offers "RTX 5090" 40 0.6', tmp_path,
                {"FAKE_OFFERS_JSON": json.dumps(offers), "VAST_LIB_EST_HOURS": "2", "VAST_LIB_EST_DOWN_GB": "30", "VAST_LIB_EST_UP_GB": "0"})
    rows = [line.split("\t") for line in r.stdout.splitlines()]
    assert [row[0] for row in rows] == ["2", "1"], r.stdout
    assert "預估 0.96 USD" in rows[0][1] and "預估 1.58 USD" in rows[1][1]
    # 短跑（0.1 小時、5 GB）時 A 便宜：0.04+0.13=0.17 vs 0.048
    r = run_lib('vast_lib_pick_offers "RTX 5090" 40 0.6', tmp_path,
                {"FAKE_OFFERS_JSON": json.dumps(offers), "VAST_LIB_EST_HOURS": "0.1", "VAST_LIB_EST_DOWN_GB": "5", "VAST_LIB_EST_UP_GB": "0"})
    assert [line.split("\t")[0] for line in r.stdout.splitlines()] == ["2", "1"]


def test_pick_offers_drops_offers_over_inet_cost_cap(tmp_path: Path) -> None:
    r = run_lib('vast_lib_pick_offers "RTX 5090" 40 0.6', tmp_path, {"FAKE_OFFERS_JSON": json.dumps(OFFERS)})
    ids = [line.split("\t")[0] for line in r.stdout.splitlines()]
    assert "8" not in ids and ids == ["1", "3"]
    r = run_lib('vast_lib_pick_offers "RTX 5090" 40 0.6', tmp_path, {"FAKE_OFFERS_JSON": json.dumps(OFFERS), "VAST_LIB_MAX_INET_COST": "0.1"})
    ids = [line.split("\t")[0] for line in r.stdout.splitlines()]
    assert "8" in ids and ids[0] == "1"  # 放行了，但 0.3×0.3＋5×0.05 的預估比 1 貴，排後面
