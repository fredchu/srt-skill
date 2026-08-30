#!/usr/bin/env bash

set -euo pipefail
IFS=$'\n\t'
exec 1>&2

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNPOD_API_BASE="${RUNPOD_API_BASE:-https://api.runpod.io/v2}"
RUNPOD_IMAGE="runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
RUNPOD_GPU_TYPE_ID="NVIDIA GeForce RTX 4090"
RUNPOD_CONTAINER_DISK_GB=60
RUNPOD_ESTIMATED_RATE_PER_HR="${RUNPOD_ESTIMATED_RATE_PER_HR:-0.751}"
# 預算上限。⚠️ 這是【步驟之間】才檢查的軟上限，不是硬上限——
# check_cost_cap 只在各步驟的交界呼叫，單一步驟跑再久也不會被它中斷。
# 所以實際花費最多會超出上限「一個步驟」的量。
# SSH_COMMAND_TIMEOUT 放寬到 1800 秒之後，最壞情況是超出約 0.37 美元
# （1800 秒 × 0.751/小時）。要真正封頂請同時調低 SSH_COMMAND_TIMEOUT_SECONDS。
# 只租 CUDA 驅動 12.8 以上的主機。
#
# 2026-08-30 觀察：直連 SSH 埠不生成的問題，在 CUDA 12.4 的主機上三台全敗
# （a1/a2/a3，各等 425-431 秒仍為 null），而 12.8 與 13.0 各一台都成功
# （26 秒與 222 秒）。三台都在 US-NC-1，昨天成功那台也在 US-NC-1，
# **所以不是資料中心的問題，是 12.4 那批主機。**
#
# n 只有 2 對 3，是線索不是定論，但它是目前唯一能解釋
# 「同一個資料中心，有的 26 秒好、有的永遠不來」的變數。
#
# 用 minCudaVersion（數值比較）不用 allowedCudaVersions（精確比對）：
# openapi.json 寫明 allowed 是精確比對，**列了沒有機器提供的版本會直接回
# 容量錯誤而不是自動退回**。用 min 就不會把 13.0 / 13.2 那批誤排掉。
# 4090 目前 available 的版本：12.4、12.8、13.0、13.2
# （GET /v2/catalog/gpus?include=AVAILABILITY&product=POD 查的）。
#
# 設成空字串就不加這個約束（回到舊行為）。
RUNPOD_MIN_CUDA="${RUNPOD_MIN_CUDA:-12.8}"

COST_CAP_USD="${COST_CAP_USD:-0.5}"
SSH_PRIVATE_KEY="${SSH_PRIVATE_KEY:-$HOME/.ssh/id_ed25519}"
SSH_PUBLIC_KEY_PATH="${SSH_PUBLIC_KEY_PATH:-$HOME/.ssh/id_ed25519.pub}"
SSH_PROBE_TIMEOUT_SECONDS="${SSH_PROBE_TIMEOUT_SECONDS:-25}"
# 這兩個上限放寬過（2026-08-30），因為租到的機器彼此差很多：
#
# SSH_COMMAND_TIMEOUT 900 → 1800：
#   同一段遠端安裝指令，快的機器 19 秒、慢的機器 7 分 30 秒（實測，差 20 倍），
#   差別在那台機器連到套件庫的網速。900 秒對慢機器加上長音檔上傳會貼線，
#   一旦誤判逾時就是白花一次開機成本、還要重開一台。
#
# SSH_READY_TIMEOUT 180 → 420：
#   RunPod 的直連端口大約一半機率生不出來，這是已知的。但生得出來的那些，
#   實測有等到 3.7 分鐘才好的（2026-08-29 第二台）。180 秒會把它誤判成
#   壞掉的機器砍掉重開，等於白付一次冷啟動。
#   （超過上限仍然是換一台，不是等更久——那條策略沒變。）
SSH_COMMAND_TIMEOUT_SECONDS="${SSH_COMMAND_TIMEOUT_SECONDS:-1800}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-15}"
SSH_READY_TIMEOUT_SECONDS="${SSH_READY_TIMEOUT_SECONDS:-420}"
REMOTE_MODEL_ID="SoybeanMilk/faster-whisper-Breeze-ASR-25"
REMOTE_VV_MODEL_ID="microsoft/VibeVoice-ASR-HF"
REMOTE_AUDIO_DIR="/root/in"
REMOTE_OUTPUT_DIR="/root/out"
VV_TERMS_FILE=""
VV_TERMS_MAX=50
VV_JSON_ENABLED=false
ASR_MODE=""
COST_CAP_SECONDS="$({
    RUNPOD_ESTIMATED_RATE_PER_HR="$RUNPOD_ESTIMATED_RATE_PER_HR" \
    COST_CAP_USD="$COST_CAP_USD" \
    python3 - <<'PY'
import os
rate = float(os.environ["RUNPOD_ESTIMATED_RATE_PER_HR"])
cap = float(os.environ["COST_CAP_USD"])
print(int(cap / rate * 3600))
PY
})"

TMP_DIR=""
POD_ID=""
POD_NAME=""
POD_IP=""
POD_PORT=""
POD_ATTEMPT_STARTED_AT=0
TOTAL_BILLABLE_SECONDS=0
POD_TERMINATED=false
REMOTE_FLAC_PATH=""
REMOTE_EVIDENCE_SCRIPT=""
REMOTE_JSON_PATH=""
LOCAL_FLAC_PATH=""
LOCAL_JSON_PATH=""
LOCAL_SRT_PATH=""
REMOTE_CUDA_CHECK_SCRIPT=""
REMOTE_INSTALL_SCRIPT=""
REMOTE_ASR_SCRIPT=""
REMOTE_PREP_DIRS_SCRIPT=""
POD_TERMINATE_ATTEMPTS=3
POD_TERMINATE_BACKOFF_SECONDS=2
POD_ATTEMPTS_FILE=""
MOCK_READY_FROM_ATTEMPT="${READY_FROM:-${CLOUD_ASR_MOCK_READY_FROM_ATTEMPT:-3}}"
MOCK_DELETE_FAIL_ON_ATTEMPT="${DELETE_FAIL_ON_ATTEMPT:-${CLOUD_ASR_MOCK_DELETE_FAIL_ON_ATTEMPT:-0}}"
MOCK_FAIL_LABEL="${CLOUD_ASR_MOCK_FAIL_LABEL:-install_env}"
CLEANUP_IN_PROGRESS=false
TERMINATE_LAST_ERROR=""
SSH_BASE_OPTS=()
TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"

show_help() {
    cat <<EOF
Usage: cloud_asr.sh <wav_path> <output_dir> <output_basename> <language> (--breeze|--vv) [--terms FILE] [--terms-max N] [--json]

Run cloud ASR on a RunPod 4090 pod, fetch the raw JSON, and render the final SRT.

Arguments:
  <wav_path>          Input WAV path.
  <output_dir>        Output directory.
  <output_basename>   Output basename without extension.
  <language>          Transcription language.
  --breeze            Breeze cloud mode (existing behavior).
  --vv                VibeVoice cloud mode.
  --terms FILE        Optional VibeVoice prompt terms file.
  --terms-max N       Prompt terms limit (default: 50).
  --json              Keep <basename>_vibevoice.json (VV mode only).

Rejected:
  --turbo             Not tested on cloud.
  Missing mode flag   Rejected.
  INITIAL_PROMPT      Not accepted.

Environment:
  RUNPOD_API_KEY           Required RunPod API key.
  RUNPOD_API_BASE          REST API base (default: ${RUNPOD_API_BASE}).
  SSH_PRIVATE_KEY          SSH private key path (default: ${SSH_PRIVATE_KEY}).
  SSH_READY_TIMEOUT_SECONDS Max wait for direct SSH endpoint + probe (default: ${SSH_READY_TIMEOUT_SECONDS}).
  CLOUD_ASR_FAIL_STEP      Optional fault injection hook; set to 6 to fail before ASR.
  CLOUD_VV_FAIL_STEP       Optional VV fault injection hook; set to inference to fail before remote generate.
  CLOUD_ASR_TEST_HOOK      Mock hook: provisioning_retry, delete_failure, signal_cleanup.
  COST_CAP_USD             Override per-call budget cap (default: ${COST_CAP_USD}).

Fixed RunPod create payload:
  imageName                ${RUNPOD_IMAGE}
  gpuTypeIds               [${RUNPOD_GPU_TYPE_ID}]
  cloudType                SECURE
  gpuCount                 1
  containerDiskInGb        ${RUNPOD_CONTAINER_DISK_GB}
  ports                    [22/tcp]
  startSsh                uses account-registered key matching ${SSH_PUBLIC_KEY_PATH}
EOF
}

info() {
    printf '[cloud_asr.sh] %s - %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >&2
}

error() {
    printf '[cloud_asr.sh] ERROR: %s - %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >&2
}

die() {
    error "$1"
    exit "${2:-1}"
}

fail_step() {
    if [[ "${CLOUD_ASR_FAIL_STEP:-}" == "$1" ]]; then
        die "fault injection: deliberately failing at step $1"
    fi
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

record_current_attempt_elapsed() {
    if [[ "$POD_ATTEMPT_STARTED_AT" -ne 0 ]]; then
        local now
        now="$(date +%s)"
        TOTAL_BILLABLE_SECONDS=$((TOTAL_BILLABLE_SECONDS + now - POD_ATTEMPT_STARTED_AT))
        POD_ATTEMPT_STARTED_AT=0
    fi
}

current_billable_seconds() {
    local active=0
    if [[ "$POD_ATTEMPT_STARTED_AT" -ne 0 ]]; then
        active=$(( $(date +%s) - POD_ATTEMPT_STARTED_AT ))
    fi
    printf '%s\n' "$((TOTAL_BILLABLE_SECONDS + active))"
}

check_cost_cap() {
    local billed_seconds
    billed_seconds="$(current_billable_seconds)"
    if (( billed_seconds > COST_CAP_SECONDS )); then
        die "estimated RunPod spend $COST_CAP_USD USD cap exceeded (${billed_seconds}s > ${COST_CAP_SECONDS}s at ~${RUNPOD_ESTIMATED_RATE_PER_HR}/hr)"
    fi
}

maybe_timeout() {
    local limit="$1"
    shift
    if [[ -n "$TIMEOUT_BIN" ]]; then
        "$TIMEOUT_BIN" "$limit" "$@"
        return $?
    fi
    # coreutils 未安裝時的純 bash 替代（乾淨的 macOS 沒有 timeout 也沒有 gtimeout）。
    # 不可以裸跑命令：這四個呼叫點全是 ssh/scp，卡住時不會產生任何訊號，
    # 於是 `trap cleanup EXIT INT TERM HUP QUIT` 永遠不觸發，pod 會一直計費。
    # 逾時一律回 124，與 coreutils timeout 一致（第 1023 行有檢查這個值）。
    #
    # `<&0` 不可省略：非互動 shell 的背景工作預設把 stdin 接到 /dev/null。
    # 實測（2026-08-30）不加就會讓 `ssh 'bash -se' <"$script_file"` 讀到空輸入，
    # 遠端靜默不做事而且不報錯。
    local fired="${TMPDIR:-/tmp}/cloud_asr_mt_$$_${RANDOM}.fired"
    rm -f "$fired"
    "$@" <&0 &
    local cmd_pid=$!
    (
        sleep "$limit"
        kill -0 "$cmd_pid" 2>/dev/null || exit 0
        : > "$fired"
        kill -TERM "$cmd_pid" 2>/dev/null
        sleep 3
        kill -KILL "$cmd_pid" 2>/dev/null
    ) &
    local watchdog_pid=$!
    local rc=0
    wait "$cmd_pid" || rc=$?
    kill -TERM "$watchdog_pid" 2>/dev/null || true
    wait "$watchdog_pid" 2>/dev/null || true
    if [[ -e "$fired" ]]; then rc=124; fi
    rm -f "$fired"
    return "$rc"
}

mock_read_file() {
    local file="$1"
    local default="${2-}"

    if [[ -r "$file" ]]; then
        tr -d '\r\n' <"$file"
    else
        printf '%s' "$default"
    fi
}

mock_write_file() {
    local file="$1"
    local value="$2"

    printf '%s\n' "$value" >"$file"
}

build_vv_prompt() {
    local terms_file="$1"
    local terms_max="$2"

    if [[ -z "$terms_file" ]]; then
        printf ''
        return 0
    fi

    python3 - "$terms_file" "$terms_max" <<'PY'
from __future__ import annotations

import pathlib
import sys

terms_path = pathlib.Path(sys.argv[1])
terms_max = int(sys.argv[2])
terms: list[str] = []
# ⚠️ 這裡照抄本地 vibevoice_asr.py:103 的做法（照檔案順序取前 N 個，無排序）。
# 這是已知缺陷而非設計；本輪刻意維持雲端與本地行為一致，不在此修排序策略。
# 詳見 company/_shared/collab/20260829-srt-cloud-asr-runpod/ISSUE-terms-truncation-silent-decay.md
for line in terms_path.read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    terms.append(line)
print(', '.join(terms[:terms_max]))
PY
}

append_pod_attempt_log() {
    local message="$1"
    local line

    line="[cloud_asr.sh] $(date '+%Y-%m-%d %H:%M:%S') - ${message}"
    printf '%s\n' "$line" >&2
    if [[ -n "$POD_ATTEMPTS_FILE" ]]; then
        printf '%s\n' "$line" >>"$POD_ATTEMPTS_FILE"
    fi
}

load_ssh_public_key() {
    local pubkey_path="$SSH_PUBLIC_KEY_PATH"
    local pubkey_text

    [[ -r "$pubkey_path" ]] || die "SSH public key not found: $pubkey_path"
    pubkey_text="$(<"$pubkey_path")"
    [[ -n "$pubkey_text" ]] || die "SSH public key is empty: $pubkey_path"
    if [[ "$pubkey_text" == *$'\n'* ]]; then
        die "SSH public key must be a single line: $pubkey_path"
    fi
    if ! jq -e -n --arg sshPublicKey "$pubkey_text" '
        $sshPublicKey
        | type == "string"
        and length > 0
        and (contains("\n") | not)
        and (split(" ") | length >= 2)
    ' >/dev/null; then
        die "SSH public key is invalid: $pubkey_path"
    fi
}

validate_create_pod_payload_file() {
    local payload_file="$1"

    jq -e '
        type == "object"
        and (.name | type == "string" and length > 0)
        and (.image | type == "string" and length > 0)
        and (.gpu.id == "NVIDIA GeForce RTX 4090")
        and (.gpu.count == 1)
        and (.gpu.minCudaVersion | type == "string" and length > 0)
        and (.cloud == "SECURE")
        and (.disk == 60)
        and (.ports | type == "array" and length == 1 and .[0] == "22/tcp")
        and (.startSsh == true)
    ' "$payload_file" >/dev/null
}

build_create_pod_payload() {
    local pod_name="$1"
    local payload_file="$2"

    jq -n \
        --arg name "$pod_name" \
        --arg image "$RUNPOD_IMAGE" \
        --arg mincuda "$RUNPOD_MIN_CUDA" \
        '{name:$name,image:$image,gpu:{id:"NVIDIA GeForce RTX 4090",count:1,minCudaVersion:$mincuda},cloud:"SECURE",disk:60,ports:["22/tcp"],startSsh:true}' \
        >"$payload_file"
    validate_create_pod_payload_file "$payload_file"
}

runpod_rest_request() {
    local mode="$1"
    local method="$2"
    local path="$3"
    local body_file="${4-}"
    local response_file stderr_file http_code curl_status body curl_err url

    if [[ -n "${CLOUD_ASR_TEST_HOOK:-}" ]]; then
        mock_runpod_rest_request "$mode" "$method" "$path" "$body_file"
        return $?
    fi

    response_file="${TMP_DIR}/runpod_${method}_$$.response"
    stderr_file="${TMP_DIR}/runpod_${method}_$$.stderr"
    url="${RUNPOD_API_BASE}${path}"
    : >"$response_file"
    : >"$stderr_file"

    if [[ -n "$body_file" ]]; then
        if http_code="$(curl -sS --connect-timeout 20 --max-time 120 \
            -o "$response_file" -w '%{http_code}' \
            -H "Authorization: Bearer $RUNPOD_API_KEY" \
            -H 'Content-Type: application/json' \
            -X "$method" \
            --data-binary "@$body_file" \
            "$url" 2>"$stderr_file")"; then
            :
        else
            curl_status=$?
            body="$(tr -d '\r\n' <"$response_file" || true)"
            curl_err="$(tr -d '\r\n' <"$stderr_file" || true)"
            case "$mode" in
                fatal)
                    die "RunPod API request failed (curl exit $curl_status) $method $path: ${body:-<empty>}${curl_err:+; curl: $curl_err}"
                    ;;
                nonfatal)
                    error "RunPod API request failed (curl exit $curl_status) $method $path: ${body:-<empty>}${curl_err:+; curl: $curl_err}"
                    return 1
                    ;;
                quiet)
                    return 1
                    ;;
            esac
        fi
    else
        if http_code="$(curl -sS --connect-timeout 20 --max-time 120 \
            -o "$response_file" -w '%{http_code}' \
            -H "Authorization: Bearer $RUNPOD_API_KEY" \
            -H 'Accept: application/json' \
            -X "$method" \
            "$url" 2>"$stderr_file")"; then
            :
        else
            curl_status=$?
            body="$(tr -d '\r\n' <"$response_file" || true)"
            curl_err="$(tr -d '\r\n' <"$stderr_file" || true)"
            case "$mode" in
                fatal)
                    die "RunPod API request failed (curl exit $curl_status) $method $path: ${body:-<empty>}${curl_err:+; curl: $curl_err}"
                    ;;
                nonfatal)
                    error "RunPod API request failed (curl exit $curl_status) $method $path: ${body:-<empty>}${curl_err:+; curl: $curl_err}"
                    return 1
                    ;;
                quiet)
                    return 1
                    ;;
            esac
        fi
    fi

    # DELETE 收到 404 代表「那台已經不在了」——那正是我們要的結果，不是失敗。
    #
    # 這不是理論情境：2026-08-30 全片那跑，外部的 runpod_reap.sh 先砍掉機器，
    # 腳本自己的 cleanup 隨後 DELETE 收到 404，被記成 `terminate result=failure`，
    # 於是保留了暫存目錄、回傳非零。**機器明明已經清乾淨了，卻報清理失敗。**
    #
    # 而 runpod_reap.sh 是 v1.8.0 才放進 repo 的，所以這個交互是我們自己造出來的：
    # 任何人照文件設排程跑收屍工具，都會撞到。
    if [[ "$method" == "DELETE" && "$http_code" == "404" && "$path" == /pods/* ]]; then
        info "pod already absent (HTTP 404 on DELETE $path) — treating as terminated"
        return 0
    fi

    if [[ ! "$http_code" =~ ^2 ]]; then
        body="$(tr -d '\r\n' <"$response_file" || true)"
        case "$mode" in
            fatal)
                die "RunPod API HTTP $http_code $method $path: ${body:-<empty>}"
                ;;
            nonfatal)
                error "RunPod API HTTP $http_code $method $path: ${body:-<empty>}"
                return 1
                ;;
            quiet)
                return 1
                ;;
        esac
    fi

    cat "$response_file"
}

mock_runpod_rest_request() {
    local mode="$1"
    local method="$2"
    local path="$3"
    local body_file="${4-}"
    local mock_dir create_count_file active_id_file active_name_file direct_ready_file deleted_file
    local create_count pod_name pod_id active_pod_id active_pod_name direct_ready pod_deleted

    mock_dir="${TMP_DIR}/mock_runpod"
    create_count_file="$mock_dir/create_count"
    active_id_file="$mock_dir/active_pod_id"
    active_name_file="$mock_dir/active_pod_name"
    direct_ready_file="$mock_dir/active_pod_direct_ready"
    deleted_file="$mock_dir/pod_deleted"
    mkdir -p "$mock_dir"
    [[ -f "$create_count_file" ]] || printf '0\n' >"$create_count_file"
    [[ -f "$active_id_file" ]] || : >"$active_id_file"
    [[ -f "$active_name_file" ]] || : >"$active_name_file"
    [[ -f "$direct_ready_file" ]] || printf 'false\n' >"$direct_ready_file"
    [[ -f "$deleted_file" ]] || printf 'false\n' >"$deleted_file"

    create_count="$(mock_read_file "$create_count_file" 0)"
    active_pod_id="$(mock_read_file "$active_id_file")"
    active_pod_name="$(mock_read_file "$active_name_file")"
    direct_ready="$(mock_read_file "$direct_ready_file" false)"
    pod_deleted="$(mock_read_file "$deleted_file" false)"

    case "$method $path" in
        "POST /pods")
            create_count=$((create_count + 1))
            pod_id="mock-pod-${create_count}"
            if [[ -n "$body_file" ]]; then
                pod_name="$(jq -r '.name // empty' "$body_file" 2>/dev/null || true)"
            fi
            mock_write_file "$create_count_file" "$create_count"
            mock_write_file "$active_id_file" "$pod_id"
            mock_write_file "$active_name_file" "$pod_name"
            if [[ "$create_count" -ge "$MOCK_READY_FROM_ATTEMPT" ]]; then
                mock_write_file "$direct_ready_file" true
            else
                mock_write_file "$direct_ready_file" false
            fi
            mock_write_file "$deleted_file" false
            printf '{"id":"%s","pod":{"id":"%s","name":"%s"},"name":"%s","status":"RUNNING"}\n' "$pod_id" "$pod_id" "$pod_name" "$pod_name"
            ;;
        "GET /pods")
            if [[ -n "$active_pod_id" && "$pod_deleted" == false ]]; then
                printf '{"pods":[{"id":"%s","name":"%s"}]}\n' "$active_pod_id" "$active_pod_name"
            else
                printf '{"pods":[]}\n'
            fi
            ;;
        "GET /pods/"*)
            if [[ -z "$active_pod_id" || "$pod_deleted" == true || "$path" != "/pods/$active_pod_id" ]]; then
                return 1
            fi
            if [[ "$direct_ready" == true ]]; then
                printf '{"id":"%s","name":"%s","runtime":{"ports":[{"type":"tcp","private":22,"public":2222,"ip":"127.0.0.1"}]},"ssh":{"direct":{"host":"127.0.0.1","port":2222}}}\n' "$active_pod_id" "$active_pod_name"
            else
                printf '{"id":"%s","name":"%s","runtime":{"ports":[]}}\n' "$active_pod_id" "$active_pod_name"
            fi
            ;;
        "DELETE /pods/"*)
            if [[ -z "$active_pod_id" || "$path" != "/pods/$active_pod_id" ]]; then
                return 1
            fi
            if [[ "$MOCK_DELETE_FAIL_ON_ATTEMPT" -ne 0 && "$create_count" -eq "$MOCK_DELETE_FAIL_ON_ATTEMPT" ]]; then
                case "$mode" in
                    fatal)
                        die "mock RunPod DELETE failed for pod id=$active_pod_id"
                        ;;
                    nonfatal)
                        error "mock RunPod DELETE failed for pod id=$active_pod_id"
                        return 1
                        ;;
                    quiet)
                        return 1
                        ;;
                esac
            fi
            mock_write_file "$deleted_file" true
            printf '{}\n'
            ;;
        *)
            case "$mode" in
                fatal)
                    die "mock RunPod API path not implemented: $method $path"
                    ;;
                nonfatal)
                    error "mock RunPod API path not implemented: $method $path"
                    return 1
                    ;;
                quiet)
                    return 1
                    ;;
            esac
            ;;
    esac
}

create_pod() {
    local pod_name payload_file response

    pod_name="cloud-asr-$(date -u +%Y%m%dT%H%M%SZ)-$$-a${ATTEMPT}"
    payload_file="${TMP_DIR}/create_pod.$$.json"
    build_create_pod_payload "$pod_name" "$payload_file"

    info "creating RunPod pod name=$pod_name"
    response="$(runpod_rest_request fatal POST '/pods' "$payload_file")"
    POD_ID="$(jq -r '.id // .pod.id // empty' <<<"$response" 2>/dev/null || true)"
    if [[ -z "$POD_ID" || "$POD_ID" == "null" ]]; then
        die "RunPod pod creation failed: missing response.id"
    fi
    POD_NAME="$pod_name"
    POD_TERMINATED=false
    append_pod_attempt_log "attempt=${ATTEMPT} pod_id=${POD_ID} pod_name=${POD_NAME} action=create result=success"
    info "created RunPod pod id=$POD_ID name=$POD_NAME"
}

query_pod_record() {
    local response record

    response="$(runpod_rest_request quiet GET "/pods/$POD_ID")" || return 1
    record="$(jq -c '
        if type == "object" and (.runtime? | type == "object") then .
        elif type == "object" and (.pod? | type == "object") then .pod
        elif type == "object" and (.data? | type == "object") and (.data.runtime? | type == "object") then .data
        else empty end
    ' <<<"$response" 2>/dev/null || true)"
    [[ -n "$record" ]] || return 1
    printf '%s\n' "$record"
}

get_pod_metadata() {
    local record

    record="$(query_pod_record)" || return 1
    jq -r '[.cudaVersion // "unknown", .dataCenterId // "unknown"] | @tsv' <<<"$record" 2>/dev/null
}

get_pod_ssh_endpoint() {
    local record ip port cuda_version data_center_id

    record="$(query_pod_record)"
    [[ -n "$record" ]] || return 1
    ip="$(jq -r '.ssh.direct.host // ((.runtime.ports // [])[]? | select((.type // "") == "tcp" and ((.private // .privatePort // empty | tostring) == "22")) | (.ip // empty))' <<<"$record" 2>/dev/null | head -n 1)"
    port="$(jq -r '(.ssh.direct.port // empty | tostring), ((.runtime.ports // [])[]? | select((.type // "") == "tcp" and ((.private // .privatePort // empty | tostring) == "22")) | (.public // .publicPort // empty | tostring))' <<<"$record" 2>/dev/null | grep -v '^$' | head -n 1)"
    cuda_version="$(jq -r '.cudaVersion // "unknown"' <<<"$record" 2>/dev/null)"
    data_center_id="$(jq -r '.dataCenterId // "unknown"' <<<"$record" 2>/dev/null)"
    if [[ -n "$ip" && -n "$port" && "$ip" != "null" && "$port" != "null" ]]; then
        printf '%s\t%s\t%s\t%s\n' "$ip" "$port" "$cuda_version" "$data_center_id"
        return 0
    fi
    return 1
}

pod_present_in_list() {
    local response

    response="$(runpod_rest_request quiet GET '/pods')" || return 2
    if jq -e --arg id "$POD_ID" '
        (if type == "array" then .
         elif type == "object" and (.pods? | type == "array") then .pods
         elif type == "object" and (.data? | type == "array") then .data
         else [] end)
        | any(.id == $id)
    ' <<<"$response" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

terminate_pod_once() {
    local list_has_pod list_status list_request_failed=false
    local -a errors=()

    if ! runpod_rest_request nonfatal DELETE "/pods/$POD_ID" >/dev/null; then
        errors+=("RunPod DELETE /pods/$POD_ID failed")
    else
        if pod_present_in_list; then
            list_has_pod=true
        else
            list_status=$?
            if [[ $list_status -eq 2 ]]; then
                errors+=("RunPod GET /pods failed after delete")
                list_request_failed=true
            else
                list_has_pod=false
            fi
        fi
        if [[ "$list_request_failed" == false && "${list_has_pod:-false}" == false ]]; then
            POD_TERMINATED=true
            info "confirmed RunPod pod id=$POD_ID absent from list after delete"
            append_pod_attempt_log "attempt=${ATTEMPT} pod_id=${POD_ID} action=terminate result=success cumulative_billable_seconds=${TOTAL_BILLABLE_SECONDS}"
            info "terminated RunPod pod id=$POD_ID"
            return 0
        fi
        if [[ "$list_request_failed" == false ]]; then
            errors+=("RunPod list still contains pod id=$POD_ID after delete")
        fi
    fi

    TERMINATE_LAST_ERROR=""
    local msg
    for msg in "${errors[@]}"; do
        if [[ -n "$TERMINATE_LAST_ERROR" ]]; then
            TERMINATE_LAST_ERROR+=" | "
        fi
        TERMINATE_LAST_ERROR+="$msg"
    done
    append_pod_attempt_log "attempt=${ATTEMPT} pod_id=${POD_ID} action=terminate result=failure cumulative_billable_seconds=${TOTAL_BILLABLE_SECONDS} error=${TERMINATE_LAST_ERROR}"
    return 1
}

terminate_pod() {
    if [[ "$POD_TERMINATED" == true || -z "$POD_ID" ]]; then
        return 0
    fi

    local attempt

    record_current_attempt_elapsed

    for ((attempt = 1; attempt <= POD_TERMINATE_ATTEMPTS; attempt++)); do
        if terminate_pod_once; then
            return 0
        fi
        if (( attempt < POD_TERMINATE_ATTEMPTS )); then
            info "RunPod pod termination attempt $attempt/$POD_TERMINATE_ATTEMPTS failed for id=$POD_ID; retrying in ${POD_TERMINATE_BACKOFF_SECONDS}s"
            sleep "$POD_TERMINATE_BACKOFF_SECONDS"
        fi
    done

    if [[ -n "$TERMINATE_LAST_ERROR" ]]; then
        error "RunPod pod termination failed for id=$POD_ID after $POD_TERMINATE_ATTEMPTS attempts: $TERMINATE_LAST_ERROR"
    else
        error "RunPod pod termination failed for id=$POD_ID after $POD_TERMINATE_ATTEMPTS attempts: no supported termination request was confirmed"
    fi
    return 1
}

handle_ssh_auth_failure() {
    local host="$1"
    local port="$2"
    error "Permission denied (publickey) when connecting to root@$host -p $port. Register the account-level SSH public key in https://console.runpod.io/user/settings and ensure $SSH_PRIVATE_KEY.pub is uploaded."
}

ssh_probe_ready() {
    local host="$1"
    local port="$2"
    local probe_log="$TMP_DIR/ssh_probe.log"
    local status output

    if [[ -n "${CLOUD_ASR_TEST_HOOK:-}" ]]; then
        return 0
    fi

    if maybe_timeout "$SSH_PROBE_TIMEOUT_SECONDS" ssh "${SSH_BASE_OPTS[@]}" -p "$port" "root@$host" true >"$probe_log" 2>&1; then
        return 0
    else
        status=$?
    fi
    output="$(cat "$probe_log" 2>/dev/null || true)"
    if [[ "$output" == *"Permission denied (publickey)"* ]]; then
        handle_ssh_auth_failure "$host" "$port"
        return 42
    fi
    return "$status"
}

ssh_run_script() {
    local label="$1"
    local host="$2"
    local port="$3"
    local script_file="$4"
    local log_file="$TMP_DIR/${label}.ssh.log"
    local status output

    if [[ -n "${CLOUD_ASR_TEST_HOOK:-}" ]]; then
        if [[ "${CLOUD_ASR_TEST_HOOK:-}" == "provisioning_retry" && "$MOCK_FAIL_LABEL" == "$label" ]]; then
            error "${label} failed against root@$host -p $port (mock)"
            return 1
        fi
        case "$label" in
            cuda_check)
                printf 'True\n'
                return 0
                ;;
            install_env)
                return 0
                ;;
            *)
                return 0
                ;;
        esac
    fi

    if maybe_timeout "$SSH_COMMAND_TIMEOUT_SECONDS" ssh "${SSH_BASE_OPTS[@]}" -p "$port" "root@$host" 'bash -se' <"$script_file" >"$log_file" 2>&1; then
        cat "$log_file"
        return 0
    else
        status=$?
    fi
    output="$(cat "$log_file" 2>/dev/null || true)"
    if [[ "$output" == *"Permission denied (publickey)"* ]]; then
        handle_ssh_auth_failure "$host" "$port"
        return 42
    fi
    error "$label failed against root@$host -p $port (exit $status): ${output:-<no output>}"
    return "$status"
}

scp_upload() {
    local local_path="$1"
    local host="$2"
    local port="$3"
    local remote_path="$4"
    local log_file="$TMP_DIR/scp_upload.log"
    local status output

    if [[ -n "${CLOUD_ASR_TEST_HOOK:-}" ]]; then
        return 0
    fi

    if maybe_timeout "$SSH_COMMAND_TIMEOUT_SECONDS" scp "${SSH_BASE_OPTS[@]}" -P "$port" "$local_path" "root@$host:$remote_path" >"$log_file" 2>&1; then
        return 0
    else
        status=$?
    fi
    output="$(cat "$log_file" 2>/dev/null || true)"
    if [[ "$output" == *"Permission denied (publickey)"* ]]; then
        handle_ssh_auth_failure "$host" "$port"
        return 42
    fi
    error "scp upload failed to root@$host -p $port: ${output:-<no output>}"
    return "$status"
}

scp_download() {
    local host="$1"
    local port="$2"
    local remote_path="$3"
    local local_path="$4"
    local log_file="$TMP_DIR/scp_download.log"
    local status output

    if [[ -n "${CLOUD_ASR_TEST_HOOK:-}" ]]; then
        mkdir -p "$(dirname "$local_path")"
        case "$(basename "$local_path")" in
            pod_id.txt)
                printf '%s\n' "$POD_ID" >"$local_path"
                ;;
            terms_sent.txt)
                printf '%s\n' "$VV_PROMPT" >"$local_path"
                ;;
            vv_env.json)
                if [[ -n "${CLOUD_ASR_TEST_VV_ENV_JSON_FILE:-}" && -r "$CLOUD_ASR_TEST_VV_ENV_JSON_FILE" ]]; then
                    cat "$CLOUD_ASR_TEST_VV_ENV_JSON_FILE" >"$local_path"
                else
                    printf '[]\n' >"$local_path"
                fi
                ;;
            vv_run.json)
                if [[ -n "${CLOUD_ASR_TEST_VV_RUN_JSON_FILE:-}" && -r "$CLOUD_ASR_TEST_VV_RUN_JSON_FILE" ]]; then
                    cat "$CLOUD_ASR_TEST_VV_RUN_JSON_FILE" >"$local_path"
                else
                    printf '[]\n' >"$local_path"
                fi
                ;;
            *.json)
                if [[ -n "${CLOUD_ASR_TEST_JSON_PAYLOAD_FILE:-}" && -r "$CLOUD_ASR_TEST_JSON_PAYLOAD_FILE" ]]; then
                    cat "$CLOUD_ASR_TEST_JSON_PAYLOAD_FILE" >"$local_path"
                elif [[ -n "${CLOUD_ASR_TEST_JSON_PAYLOAD:-}" ]]; then
                    printf '%s\n' "$CLOUD_ASR_TEST_JSON_PAYLOAD" >"$local_path"
                else
                    printf '[]\n' >"$local_path"
                fi
                ;;
            *)
                : >"$local_path"
                ;;
        esac
        return 0
    fi

    if maybe_timeout "$SSH_COMMAND_TIMEOUT_SECONDS" scp "${SSH_BASE_OPTS[@]}" -P "$port" "root@$host:$remote_path" "$local_path" >"$log_file" 2>&1; then
        return 0
    else
        status=$?
    fi
    output="$(cat "$log_file" 2>/dev/null || true)"
    if [[ "$output" == *"Permission denied (publickey)"* ]]; then
        handle_ssh_auth_failure "$host" "$port"
        return 42
    fi
    error "scp download failed from root@$host -p $port: ${output:-<no output>}"
    return "$status"
}

wait_for_ssh_ready() {
    local start now elapsed endpoint ip port cuda_version data_center_id metadata probe_status

    if [[ "${CLOUD_ASR_TEST_HOOK:-}" == "signal_cleanup" ]]; then
        info "test hook: blocking before direct endpoint for pod $POD_ID"
        while :; do
            sleep 1
        done
    fi

    start="$(date +%s)"
    while :; do
        if endpoint="$(get_pod_ssh_endpoint)"; then
            IFS=$'\t' read -r ip port cuda_version data_center_id <<<"$endpoint"
            if ssh_probe_ready "$ip" "$port"; then
                POD_IP="$ip"
                POD_PORT="$port"
                append_pod_attempt_log "attempt=${ATTEMPT} pod_id=${POD_ID} action=direct_wait result=ready host=${POD_IP} port=${POD_PORT} cuda_version=${cuda_version:-unknown} data_center_id=${data_center_id:-unknown}"
                info "SSH ready at root@$POD_IP -p $POD_PORT"
                return 0
            else
                probe_status=$?
            fi
            if [[ $probe_status -eq 42 ]]; then
                append_pod_attempt_log "attempt=${ATTEMPT} pod_id=${POD_ID} action=direct_wait result=auth_failure host=$ip port=$port cuda_version=${cuda_version:-unknown} data_center_id=${data_center_id:-unknown}"
                return 42
            fi
            info "SSH endpoint present but not ready yet at root@$ip -p $port"
        else
            info "waiting for RunPod SSH endpoint to appear for pod $POD_ID"
        fi

        now="$(date +%s)"
        elapsed=$((now - start))
        if (( elapsed >= SSH_READY_TIMEOUT_SECONDS )); then
            error "timed out after ${SSH_READY_TIMEOUT_SECONDS}s waiting for direct SSH on pod $POD_ID"
            metadata="$(get_pod_metadata 2>/dev/null || printf 'unknown\tunknown')"
            IFS=$'\t' read -r cuda_version data_center_id <<<"$metadata"
            append_pod_attempt_log "attempt=${ATTEMPT} pod_id=${POD_ID} action=direct_wait result=timeout elapsed=${elapsed}s cuda_version=${cuda_version:-unknown} data_center_id=${data_center_id:-unknown}"
            return 124
        fi
        check_cost_cap
        sleep "$POLL_INTERVAL_SECONDS"
    done
}

write_remote_scripts() {
    REMOTE_FLAC_PATH="${REMOTE_AUDIO_DIR}/${BASENAME}.flac"
    REMOTE_JSON_PATH="${REMOTE_OUTPUT_DIR}/${BASENAME}.cloud.json"
    REMOTE_CUDA_CHECK_SCRIPT="${TMP_DIR}/remote_cuda_check.sh"
    REMOTE_INSTALL_SCRIPT="${TMP_DIR}/remote_install_env.sh"
    REMOTE_ASR_SCRIPT="${TMP_DIR}/remote_asr.sh"
    REMOTE_PREP_DIRS_SCRIPT="${TMP_DIR}/remote_prep_dirs.sh"
    REMOTE_EVIDENCE_SCRIPT="${TMP_DIR}/remote_evidence.sh"

    cat >"$REMOTE_CUDA_CHECK_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
import torch
print(torch.cuda.is_available())
PY
EOF

    cat >"$REMOTE_INSTALL_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
python3 -m pip install --break-system-packages faster-whisper
EOF

    cat >"$REMOTE_PREP_DIRS_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
mkdir -p /root/in /root/out /root/bench
EOF

    local remote_model_id_q remote_audio_path_q remote_json_path_q remote_pod_id_q remote_env_dir_q
    printf -v remote_model_id_q '%q' "$REMOTE_MODEL_ID"
    printf -v remote_audio_path_q '%q' "$REMOTE_FLAC_PATH"
    printf -v remote_json_path_q '%q' "$REMOTE_JSON_PATH"
    printf -v remote_pod_id_q '%q' "$POD_ID"
    printf -v remote_env_dir_q '%q' "${REMOTE_OUTPUT_DIR}/env"

    cat >"$REMOTE_EVIDENCE_SCRIPT" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export CLOUD_ASR_POD_ID=$remote_pod_id_q
export CLOUD_ASR_ENV_DIR=$remote_env_dir_q
mkdir -p "\$CLOUD_ASR_ENV_DIR"
printf '%s\n' "\$CLOUD_ASR_POD_ID" > "\$CLOUD_ASR_ENV_DIR/pod_id.txt"
nvidia-smi > "\$CLOUD_ASR_ENV_DIR/nvidia-smi.txt" 2>&1
python3 - <<'PY' > "\$CLOUD_ASR_ENV_DIR/torch_version.txt"
import sys
import torch
print(f"python={sys.version.split()[0]}")
print(f"torch={torch.__version__}")
print(f"cuda={torch.version.cuda}")
PY
python3 -m pip list > "\$CLOUD_ASR_ENV_DIR/pip_list.txt"
EOF

    cat >"$REMOTE_ASR_SCRIPT" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export CLOUD_ASR_MODEL_ID=$remote_model_id_q
export CLOUD_ASR_AUDIO_PATH=$remote_audio_path_q
export CLOUD_ASR_JSON_PATH=$remote_json_path_q
export CLOUD_ASR_LANGUAGE=$(printf '%q' "$LANGUAGE")
python3 -u - <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

import torch
from faster_whisper import WhisperModel

model_id = os.environ["CLOUD_ASR_MODEL_ID"]
audio_path = Path(os.environ["CLOUD_ASR_AUDIO_PATH"])
json_path = Path(os.environ["CLOUD_ASR_JSON_PATH"])
language = os.environ["CLOUD_ASR_LANGUAGE"]

if not torch.cuda.is_available():
    raise SystemExit("torch.cuda.is_available() is False on the remote pod")

json_path.parent.mkdir(parents=True, exist_ok=True)
model = WhisperModel(model_id, device="cuda", compute_type="float16")
segments_iterator, _info = model.transcribe(
    str(audio_path),
    language=language,
    task="transcribe",
    condition_on_previous_text=False,
    temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
    compression_ratio_threshold=2.4,
    log_prob_threshold=-1.0,
    no_speech_threshold=0.6,
    beam_size=1,
    word_timestamps=True,
    vad_filter=False,
)

segments: list[dict[str, object]] = []
for segment in segments_iterator:
    words: list[dict[str, object]] = []
    for word in segment.words or []:
        words.append({
            "start": float(word.start),
            "end": float(word.end),
            "word": word.word,
            "probability": float(word.probability),
        })
    segments.append({
        "start": float(segment.start),
        "end": float(segment.end),
        "text": segment.text,
        "words": words,
    })

tmp_path = json_path.with_suffix(json_path.suffix + ".tmp")
tmp_path.write_text(json.dumps(segments, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp_path.replace(json_path)
print(f"wrote {json_path} ({len(segments)} segments)", flush=True)
PY
EOF

    chmod 700 "$REMOTE_CUDA_CHECK_SCRIPT" "$REMOTE_INSTALL_SCRIPT" "$REMOTE_ASR_SCRIPT" "$REMOTE_PREP_DIRS_SCRIPT"
}

write_vv_remote_scripts() {
    REMOTE_JSON_PATH="${REMOTE_OUTPUT_DIR}/${BASENAME}.vv.raw.json"
    REMOTE_CUDA_CHECK_SCRIPT="${TMP_DIR}/remote_vv_cuda_check.sh"
    REMOTE_INSTALL_SCRIPT="${TMP_DIR}/remote_vv_install_env.sh"
    REMOTE_PREP_DIRS_SCRIPT="${TMP_DIR}/remote_vv_prep_dirs.sh"
    REMOTE_EVIDENCE_SCRIPT="${TMP_DIR}/remote_vv_evidence.sh"
    REMOTE_ASR_SCRIPT="${TMP_DIR}/remote_vv_asr.sh"

    local remote_model_id_q remote_audio_path_q remote_json_path_q remote_prompt_q remote_env_dir_q remote_evidence_json_q remote_pod_id_q
    printf -v remote_model_id_q '%q' "$REMOTE_VV_MODEL_ID"
    printf -v remote_audio_path_q '%q' "$REMOTE_FLAC_PATH"
    printf -v remote_json_path_q '%q' "$REMOTE_JSON_PATH"
    printf -v remote_prompt_q '%q' "$VV_PROMPT"
    printf -v remote_env_dir_q '%q' "${REMOTE_OUTPUT_DIR}/env"
    printf -v remote_evidence_json_q '%q' "${REMOTE_OUTPUT_DIR}/env/vv_env.json"
    printf -v remote_pod_id_q '%q' "$POD_ID"

    cat >"$REMOTE_CUDA_CHECK_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
import torch
print(torch.cuda.is_available())
PY
EOF

    cat >"$REMOTE_INSTALL_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
python3 -m pip install --break-system-packages \
  'transformers==5.16.1' 'accelerate==1.13.0' 'huggingface-hub==1.5.0' \
  'librosa==0.11.0' 'soundfile==0.13.1' sentencepiece
command -v ffmpeg
command -v ffprobe
python3 - <<'PY'
import transformers
print(f"VibeVoice import gate: transformers={transformers.__version__}", flush=True)
if not transformers.__version__.startswith("5.16."):
    raise RuntimeError(f"expected transformers 5.16.x, got {transformers.__version__}")
from transformers import AutoProcessor, VibeVoiceAsrForConditionalGeneration
print("VibeVoice import gate: PASS AutoProcessor + VibeVoiceAsrForConditionalGeneration", flush=True)
PY
EOF

    cat >"$REMOTE_PREP_DIRS_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
mkdir -p /root/in /root/out /root/bench /root/out/env
EOF

    cat >"$REMOTE_EVIDENCE_SCRIPT" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export CLOUD_VV_MODEL_ID=$remote_model_id_q
export CLOUD_VV_PROMPT=$remote_prompt_q
export CLOUD_VV_ENV_DIR=$remote_env_dir_q
export CLOUD_VV_EVIDENCE_JSON=$remote_evidence_json_q
export CLOUD_VV_POD_ID=$remote_pod_id_q
mkdir -p "\$CLOUD_VV_ENV_DIR"
printf '%s\n' "\$CLOUD_VV_POD_ID" > "\$CLOUD_VV_ENV_DIR/pod_id.txt"
python3 - <<'PY' > "\$CLOUD_VV_EVIDENCE_JSON"
import json
import sys
from pathlib import Path

import huggingface_hub
import torch
import transformers

print(json.dumps({
    "python": sys.version.split()[0],
    "torch": torch.__version__,
    "cuda": torch.version.cuda,
    "transformers": transformers.__version__,
    "huggingface_hub": huggingface_hub.__version__,
    "cuda_available": torch.cuda.is_available(),
    "hf_home": str(Path.home() / ".cache" / "huggingface"),
}, ensure_ascii=False, indent=2))
PY
nvidia-smi > "\$CLOUD_VV_ENV_DIR/nvidia-smi.txt" 2>&1
python3 -m pip list > "\$CLOUD_VV_ENV_DIR/pip_list.txt"
printf '%s\n' "\$CLOUD_VV_MODEL_ID" > "\$CLOUD_VV_ENV_DIR/model_id.txt"
printf '%s\n' "\$CLOUD_VV_PROMPT" > "\$CLOUD_VV_ENV_DIR/prompt.txt"
printf '%s\n' "\$CLOUD_VV_PROMPT" > "\$CLOUD_VV_ENV_DIR/terms_sent.txt"
EOF

    cat >"$REMOTE_ASR_SCRIPT" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export CLOUD_VV_MODEL_ID=$remote_model_id_q
export CLOUD_VV_AUDIO_PATH=$remote_audio_path_q
export CLOUD_VV_JSON_PATH=$remote_json_path_q
export CLOUD_VV_PROMPT=$remote_prompt_q
export CLOUD_VV_ENV_DIR=$remote_env_dir_q
python3 -u - <<'PY'
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import torch
from huggingface_hub import snapshot_download
from transformers import AutoProcessor, VibeVoiceAsrForConditionalGeneration

MODEL_ID = os.environ["CLOUD_VV_MODEL_ID"]
AUDIO_PATH = Path(os.environ["CLOUD_VV_AUDIO_PATH"])
RAW_JSON_PATH = Path(os.environ["CLOUD_VV_JSON_PATH"])
EVIDENCE_DIR = Path(os.environ["CLOUD_VV_ENV_DIR"])
PROMPT = os.environ.get("CLOUD_VV_PROMPT", "")
MAX_PART_SEC = 3000.0
LONG_AUDIO_THRESHOLD_SEC = 55 * 60


def run_capture(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def ffprobe_duration(path: Path) -> float:
    proc = run_capture([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    return float(proc.stdout.strip())


def detect_silence_ends(path: Path) -> list[float]:
    proc = subprocess.run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-af", "silencedetect=noise=-30dB:d=0.5", "-f", "null", "-",
    ], check=True, text=True, capture_output=True)
    return [float(match.group(1)) for match in re.finditer(r"silence_end:\s*([0-9.]+)", proc.stderr)]


def choose_cut_points(total_sec: float, max_part_sec: float, silence_ends: list[float]) -> list[float]:
    if total_sec <= max_part_sec:
        return []
    part_count = int(math.ceil(total_sec / max_part_sec))
    spacing = total_sec / part_count
    usable_silences = sorted({round(s, 6) for s in silence_ends if 0 < s < total_sec})
    cuts: list[float] = []
    for i in range(1, part_count):
        ideal = spacing * i
        nearby = [s for s in usable_silences if s not in cuts and abs(s - ideal) <= 300.0]
        if nearby:
            chosen = min(nearby, key=lambda s: (abs(s - ideal), s))
        else:
            # A hard cut is safer than selecting a distant silence that creates a >60-minute part.
            chosen = ideal
        if 0 < chosen < total_sec and chosen not in cuts:
            cuts.append(chosen)
    return sorted(cuts)


def part_ranges(total_sec: float, cut_points: list[float]) -> list[dict[str, float]]:
    points = [0.0, *sorted(cut_points), total_sec]
    return [
        {"start": points[i], "end": points[i + 1], "dur": points[i + 1] - points[i]}
        for i in range(len(points) - 1)
    ]


def cut_part(media_file: Path, output_wav: Path, start: float, duration: float) -> None:
    subprocess.run([
        "ffmpeg", "-hide_banner", "-nostats", "-y",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", str(media_file),
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(output_wav),
    ], check=True)


def seconds_to_srt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def normalize_segment(segment: dict[str, object], offset: float) -> dict[str, object] | None:
    start = segment.get("Start", segment.get("start", segment.get("start_time", 0.0)))
    end = segment.get("End", segment.get("end", segment.get("end_time", 0.0)))
    text = segment.get("Content", segment.get("text", ""))
    if not isinstance(text, str):
        text = str(text)
    text = text.strip()
    if not text or text == "[Silence]":
        return None
    return {"start": float(start) + offset, "end": float(end) + offset, "text": text}


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def stitch_segments(segments: list[dict[str, object]]) -> list[dict[str, object]]:
    stitched: list[dict[str, object]] = []
    for segment in segments:
        if not stitched:
            stitched.append(segment)
            continue
        prev = stitched[-1]
        if compact_text(str(segment["text"])) == compact_text(str(prev["text"])) and float(segment["start"]) - float(prev["end"]) <= 1.0:
            prev["end"] = max(float(prev["end"]), float(segment["end"]))
            continue
        if float(segment["start"]) < float(prev["end"]):
            segment = dict(segment)
            segment["start"] = float(prev["end"])
        if float(segment["end"]) <= float(segment["start"]):
            continue
        stitched.append(segment)
    return stitched


def write_json(path: Path, value: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def transcribe_part(processor, model, audio: Path, prompt: str) -> tuple[list[dict[str, object]], float]:
    request = {"audio": str(audio)}
    if prompt:
        request["prompt"] = prompt
    inputs = processor.apply_transcription_request(**request).to(model.device, model.dtype)
    started = time.monotonic()
    output_ids = model.generate(**inputs, max_new_tokens=32768, do_sample=False)
    inference_s = time.monotonic() - started
    generated = output_ids[:, inputs["input_ids"].shape[1]:]
    parsed = processor.decode(generated, return_format="parsed")
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], list):
        parsed = parsed[0]
    segments: list[dict[str, object]] = []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                normalized = normalize_segment(item, 0.0)
                if normalized is not None:
                    segments.append(normalized)
    return segments, inference_s


if not torch.cuda.is_available():
    raise SystemExit("torch.cuda.is_available() is False on the remote pod")

RAW_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

started_at = time.monotonic()
snapshot_started = time.monotonic()
snapshot = snapshot_download(MODEL_ID)
snapshot_download_s = time.monotonic() - snapshot_started
processor_started = time.monotonic()
processor = AutoProcessor.from_pretrained(snapshot, local_files_only=True)
processor_load_s = time.monotonic() - processor_started
model_started = time.monotonic()
# Do not force attn_implementation on the composite model: doing so propagates
# SDPA into the acoustic-tokenizer encoder, which does not support it. The
# verified transformers 5.16.1 recipe selects SDPA for the top-level decoder
# while leaving incompatible submodels on their supported implementation.
model = VibeVoiceAsrForConditionalGeneration.from_pretrained(
    snapshot,
    local_files_only=True,
    device_map="auto",
)
model_load_s = time.monotonic() - model_started
implementation = getattr(model.config, "_attn_implementation", None)
print(f"VibeVoice attention gate: model.config._attn_implementation={implementation}", flush=True)
if implementation not in ("sdpa", "flash_attention_2"):
    raise RuntimeError(
        f"unsafe attention implementation {implementation!r}; expected sdpa or flash_attention_2"
    )

audio_seconds = ffprobe_duration(AUDIO_PATH)
silence_ends = detect_silence_ends(AUDIO_PATH)
cut_points = choose_cut_points(audio_seconds, MAX_PART_SEC if audio_seconds > LONG_AUDIO_THRESHOLD_SEC else max(MAX_PART_SEC, audio_seconds), silence_ends)
ranges = part_ranges(audio_seconds, cut_points)
for part_index, part in enumerate(ranges, start=1):
    if float(part["dur"]) > 3600.0:
        raise RuntimeError(
            f"VibeVoice part {part_index} exceeds 60-minute limit: "
            f"duration={float(part['dur']):.3f}s cut_points={cut_points}"
        )

merged_segments: list[dict[str, object]] = []
chunk_timings: list[dict[str, object]] = []
with tempfile.TemporaryDirectory(prefix="vvparts-", dir=str(RAW_JSON_PATH.parent)) as tmpdir:
    tmpdir_path = Path(tmpdir)
    for index, part in enumerate(ranges, start=1):
        part_wav = tmpdir_path / f"part{index}.wav"
        cut_part(AUDIO_PATH, part_wav, float(part["start"]), float(part["dur"]))
        chunk_started = time.monotonic()
        chunk_segments, infer_s = transcribe_part(processor, model, part_wav, PROMPT)
        chunk_elapsed = time.monotonic() - chunk_started
        offset = float(part["start"])
        for segment in chunk_segments:
            merged_segments.append({
                "start": float(segment["start"]) + offset,
                "end": float(segment["end"]) + offset,
                "text": str(segment["text"]),
            })
        chunk_timings.append({
            "index": index,
            "offset": offset,
            "duration": float(part["dur"]),
            "segments": len(chunk_segments),
            "inference_s": infer_s,
            "chunk_elapsed_s": chunk_elapsed,
        })

merged_segments = stitch_segments(sorted(merged_segments, key=lambda item: (float(item["start"]), float(item["end"]))))
write_json(RAW_JSON_PATH, merged_segments)
write_json(EVIDENCE_DIR / "vv_run.json", {
    "model_id": MODEL_ID,
    "snapshot": snapshot,
    "transformers": __import__("transformers").__version__,
    "torch": torch.__version__,
    "cuda": torch.version.cuda,
    "implementation": implementation,
    "snapshot_download_s": snapshot_download_s,
    "processor_load_s": processor_load_s,
    "model_load_s": model_load_s,
    "audio_seconds": audio_seconds,
    "cut_points": cut_points,
    "parts": len(ranges),
    "segments": len(merged_segments),
    "chunk_timings": chunk_timings,
    "total_elapsed_s": time.monotonic() - started_at,
    "prompt_terms": len([term for term in PROMPT.split(', ') if term]) if PROMPT else 0,
})
print(json.dumps({
    "snapshot_download_s": snapshot_download_s,
    "processor_load_s": processor_load_s,
    "model_load_s": model_load_s,
    "parts": len(ranges),
    "segments": len(merged_segments),
    "implementation": implementation,
}, ensure_ascii=False, indent=2))
PY
EOF

    chmod 700 "$REMOTE_CUDA_CHECK_SCRIPT" "$REMOTE_INSTALL_SCRIPT" "$REMOTE_ASR_SCRIPT" "$REMOTE_PREP_DIRS_SCRIPT" "$REMOTE_EVIDENCE_SCRIPT"
}

run_vv_mode() {
    VV_PROMPT=""
    if [[ -n "$VV_TERMS_FILE" ]]; then
        [[ -r "$VV_TERMS_FILE" ]] || die "VV terms file not found: $VV_TERMS_FILE"
        case "$VV_TERMS_MAX" in
            ''|*[!0-9]*) die "invalid --terms-max: $VV_TERMS_MAX" ;;
        esac
        VV_PROMPT="$(build_vv_prompt "$VV_TERMS_FILE" "$VV_TERMS_MAX")"
        local vv_terms_total vv_terms_sent
        vv_terms_total="$(awk '!/^[[:space:]]*(#|$)/ { count++ } END { print count + 0 }' "$VV_TERMS_FILE")"
        vv_terms_sent="$vv_terms_total"
        if (( vv_terms_sent > VV_TERMS_MAX )); then vv_terms_sent="$VV_TERMS_MAX"; fi
        info "terms: 送出 ${vv_terms_sent} / 共 ${vv_terms_total} 個（取檔案前 ${VV_TERMS_MAX}，未排序）"
    fi

    info "budget cap: ${COST_CAP_USD} USD (~${COST_CAP_SECONDS}s at ~${RUNPOD_ESTIMATED_RATE_PER_HR}/hr)"

    ATTEMPT=0
    while :; do
        ATTEMPT=$((ATTEMPT + 1))
        if (( ATTEMPT > 3 )); then
            die "direct endpoint unavailable after 3 pods; giving up"
        fi

        create_pod
        POD_ATTEMPT_STARTED_AT="$(date +%s)"
        REMOTE_FLAC_PATH="${REMOTE_AUDIO_DIR}/${BASENAME}.flac"
        write_vv_remote_scripts

        if wait_for_ssh_ready; then
            status=0
        else
            status=$?
        fi
        if [[ $status -eq 0 ]]; then
            break
        fi
        if [[ $status -eq 124 ]]; then
            if ! terminate_pod; then
                die "timed out waiting for direct SSH on pod $POD_ID and termination failed: ${TERMINATE_LAST_ERROR:-<unknown>}"
            fi
            if (( ATTEMPT >= 3 )); then
                die "direct endpoint unavailable after 3 pods; giving up"
            fi
            continue
        fi

        if [[ $status -eq 42 ]]; then
            if ! terminate_pod; then
                die "SSH public key denied by RunPod backend after pod $POD_ID could not be terminated: ${TERMINATE_LAST_ERROR:-<unknown>}"
            fi
            die "SSH public key denied by RunPod backend"
        fi

        if ! terminate_pod; then
            die "failed while waiting for direct SSH on pod $POD_ID and termination failed: ${TERMINATE_LAST_ERROR:-<unknown>}"
        fi
        die "failed while waiting for direct SSH on pod $POD_ID"
    done

    info "direct endpoint ready on pod $POD_ID"
    check_cost_cap

    if cuda_probe_output="$(ssh_run_script "cuda_check" "$POD_IP" "$POD_PORT" "$REMOTE_CUDA_CHECK_SCRIPT")"; then
        case "$(tail -n 1 <<<"$cuda_probe_output")" in
            True)
                :
                ;;
            False)
                if ! terminate_pod; then
                    die "torch.cuda.is_available() returned False on pod $POD_ID and termination failed: ${TERMINATE_LAST_ERROR:-<unknown>}"
                fi
                die "torch.cuda.is_available() returned False on pod $POD_ID"
                ;;
            *)
                if ! terminate_pod; then
                    die "torch.cuda.is_available() returned unexpected output on pod $POD_ID and termination failed: ${TERMINATE_LAST_ERROR:-<unknown>}"
                fi
                die "torch.cuda.is_available() returned unexpected output on pod $POD_ID"
                ;;
        esac
    else
        status=$?
        if [[ $status -eq 42 ]]; then
            if ! terminate_pod; then
                die "SSH public key denied by RunPod backend after pod $POD_ID could not be terminated: ${TERMINATE_LAST_ERROR:-<unknown>}"
            fi
            die "SSH public key denied by RunPod backend"
        fi
        if ! terminate_pod; then
            die "torch.cuda.is_available() failed on pod $POD_ID and termination failed: ${TERMINATE_LAST_ERROR:-<unknown>}"
        fi
        die "torch.cuda.is_available() failed on pod $POD_ID (exit $status)"
    fi
    info "CUDA validated on pod $POD_ID"
    check_cost_cap

    if ssh_run_script "install_env" "$POD_IP" "$POD_PORT" "$REMOTE_INSTALL_SCRIPT"; then
        :
    else
        status=$?
        terminate_pod || true
        if [[ $status -eq 42 ]]; then
            die "SSH public key denied by RunPod backend"
        fi
        die "install_env failed on pod $POD_ID (exit $status)"
    fi
    check_cost_cap

    LOCAL_FLAC_PATH="${TMP_DIR}/${BASENAME}.flac"
    LOCAL_JSON_PATH="${TMP_DIR}/${BASENAME}.vv.raw.json"
    LOCAL_SRT_PATH="${OUTPUT_DIR}/${BASENAME}_vibevoice.srt"
    local LOCAL_VV_JSON_PATH="${TMP_DIR}/${BASENAME}_vibevoice.json"
    if [[ "$VV_JSON_ENABLED" == true ]]; then
        LOCAL_VV_JSON_PATH="${OUTPUT_DIR}/${BASENAME}_vibevoice.json"
    fi

    info "converting input WAV to FLAC"
    ffmpeg -y -i "$WAV_PATH" -ar 16000 -ac 1 -c:a flac -compression_level 12 "$LOCAL_FLAC_PATH" >/dev/null 2>&1
    check_cost_cap

    info "preparing remote directories on root@$POD_IP -p $POD_PORT"
    if ssh_run_script "prepare_remote_dirs" "$POD_IP" "$POD_PORT" "$REMOTE_PREP_DIRS_SCRIPT"; then
        :
    else
        status=$?
        terminate_pod || true
        if [[ $status -eq 42 ]]; then
            die "SSH public key denied by RunPod backend"
        fi
        die "remote directory preparation failed on pod $POD_ID (exit $status)"
    fi
    check_cost_cap

    info "uploading FLAC to root@$POD_IP -p $POD_PORT"
    if scp_upload "$LOCAL_FLAC_PATH" "$POD_IP" "$POD_PORT" "$REMOTE_AUDIO_DIR/${BASENAME}.flac"; then
        :
    else
        status=$?
        terminate_pod || true
        if [[ $status -eq 42 ]]; then
            die "SSH public key denied by RunPod backend"
        fi
        die "FLAC upload failed to root@$POD_IP -p $POD_PORT (exit $status)"
    fi
    check_cost_cap

    info "collecting pod evidence on pod $POD_ID before fault injection"
    if ssh_run_script "collect_evidence" "$POD_IP" "$POD_PORT" "$REMOTE_EVIDENCE_SCRIPT"; then
        :
    else
        status=$?
        terminate_pod || true
        if [[ $status -eq 42 ]]; then
            die "SSH public key denied by RunPod backend"
        fi
        die "evidence collection failed on pod $POD_ID (exit $status)"
    fi

    mkdir -p "$OUTPUT_DIR/env"
    for evidence_path in "$OUTPUT_DIR/env/vv_env.json" "$OUTPUT_DIR/env/pod_id.txt" "$OUTPUT_DIR/env/nvidia-smi.txt" "$OUTPUT_DIR/env/pip_list.txt" "$OUTPUT_DIR/env/terms_sent.txt"; do
        if [[ -e "$evidence_path" ]]; then
            die "refusing to overwrite existing evidence file: $evidence_path"
        fi
    done
    info "downloading pod evidence from pod $POD_ID"
    if scp_download "$POD_IP" "$POD_PORT" "${REMOTE_OUTPUT_DIR}/env/vv_env.json" "$OUTPUT_DIR/env/vv_env.json"; then
        :
    else
        status=$?
        terminate_pod || true
        if [[ $status -eq 42 ]]; then
            die "SSH public key denied by RunPod backend"
        fi
        die "vv evidence download failed from root@$POD_IP -p $POD_PORT (exit $status)"
    fi
    if scp_download "$POD_IP" "$POD_PORT" "${REMOTE_OUTPUT_DIR}/env/pod_id.txt" "$OUTPUT_DIR/env/pod_id.txt"; then
        :
    else
        status=$?
        terminate_pod || true
        if [[ $status -eq 42 ]]; then
            die "SSH public key denied by RunPod backend"
        fi
        die "pod id evidence download failed from root@$POD_IP -p $POD_PORT (exit $status)"
    fi
    for evidence_name in nvidia-smi.txt pip_list.txt terms_sent.txt; do
        if scp_download "$POD_IP" "$POD_PORT" "${REMOTE_OUTPUT_DIR}/env/${evidence_name}" "$OUTPUT_DIR/env/${evidence_name}"; then
            :
        else
            status=$?
            terminate_pod || true
            if [[ $status -eq 42 ]]; then die "SSH public key denied by RunPod backend"; fi
            die "VV evidence ${evidence_name} download failed from root@$POD_IP -p $POD_PORT (exit $status)"
        fi
    done
    if [[ "$(tr -d '\r\n' < "$OUTPUT_DIR/env/pod_id.txt")" != "$POD_ID" ]]; then
        terminate_pod || true
        die "pod id evidence mismatch after download: expected $POD_ID"
    fi
    if [[ -f "$OUTPUT_DIR/env/vv_env.json" ]]; then
        true
    fi
    check_cost_cap

    if [[ "${CLOUD_VV_FAIL_STEP:-}" == "inference" ]]; then
        die "fault injection: deliberately failing before VV inference"
    fi
    info "running VibeVoice-ASR-HF sdpa bf16 on pod $POD_ID"
    if ssh_run_script "asr" "$POD_IP" "$POD_PORT" "$REMOTE_ASR_SCRIPT"; then
        :
    else
        status=$?
        terminate_pod || true
        if [[ $status -eq 42 ]]; then
            die "SSH public key denied by RunPod backend"
        fi
        die "VV ASR failed on pod $POD_ID (exit $status)"
    fi
    check_cost_cap

    info "downloading raw JSON from pod $POD_ID"
    if scp_download "$POD_IP" "$POD_PORT" "$REMOTE_JSON_PATH" "$LOCAL_JSON_PATH"; then
        :
    else
        status=$?
        terminate_pod || true
        if [[ $status -eq 42 ]]; then
            die "SSH public key denied by RunPod backend"
        fi
        die "VV JSON download failed from root@$POD_IP -p $POD_PORT (exit $status)"
    fi
    if scp_download "$POD_IP" "$POD_PORT" "${REMOTE_OUTPUT_DIR}/env/vv_run.json" "$OUTPUT_DIR/env/vv_run.json"; then
        :
    else
        status=$?
        terminate_pod || true
        if [[ $status -eq 42 ]]; then
            die "SSH public key denied by RunPod backend"
        fi
        die "VV timing evidence download failed from root@$POD_IP -p $POD_PORT (exit $status)"
    fi
    check_cost_cap

    info "normalizing VibeVoice JSON"
    python3 "$SCRIPT_DIR/cloud_to_srt.py" vv "$LOCAL_JSON_PATH" "$LOCAL_VV_JSON_PATH"
    check_cost_cap

    info "rendering VibeVoice SRT"
    python3 - "$LOCAL_VV_JSON_PATH" "$LOCAL_SRT_PATH" <<'PY'
from __future__ import annotations

import json
import pathlib
import sys


def ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        millis = 999
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


src = pathlib.Path(sys.argv[1])
dst = pathlib.Path(sys.argv[2])
payload = json.loads(src.read_text(encoding="utf-8"))
if not isinstance(payload, list):
    raise SystemExit("VV JSON is not a list")
blocks: list[str] = []
index = 0
for item in payload:
    if not isinstance(item, dict):
        continue
    text = str(item.get("text", "")).strip()
    if not text:
        continue
    index += 1
    start = float(item.get("start", 0.0))
    end = float(item.get("end", 0.0))
    blocks.append("\n".join([str(index), f"{ts(start)} --> {ts(end)}", text]))
dst.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")
PY
    check_cost_cap

    if [[ -f "$OUTPUT_DIR/env/vv_env.json" ]]; then
        :
    fi
    info "completed cloud VV ASR: $LOCAL_SRT_PATH"
    return 0
}

# shellcheck disable=SC2329 # invoked via trap EXIT/INT/TERM/HUP/QUIT
cleanup() {
    local status=$?
    local preserve_tmp_dir=false

    if [[ "$CLEANUP_IN_PROGRESS" == true ]]; then
        return "$status"
    fi
    CLEANUP_IN_PROGRESS=true
    trap - EXIT INT TERM HUP QUIT
    set +e

    # 先把還活著的子程序收掉，再做其他清理。
    #
    # 為什麼不能省：maybe_timeout 在沒有 coreutils 的機器上會把命令丟到背景跑。
    # 收到 SIGTERM 時 trap 會在父程序這邊觸發，但那個背景的 ssh/scp **不會死**。
    # 它可能在父程序已經宣告失敗之後，才把半成品 SRT 寫進 OUTPUT_DIR。
    #
    # 而 subtitle.sh 找輸出的方式是「比標記檔新的 ${BASENAME}*.srt」（第 359 行）。
    # 使用者 Ctrl-C 之後立刻重跑同一支影片，第二次就會把第一次的半成品撿走
    # 當成自己的產物——不報錯，資料是錯的。這是靜默失效，不是浪費資源。
    #
    # 在 macOS 內建 bash 3.2 上實測過：SIGTERM 後確實留下存活的子程序。
    # （Fable review-20 指出真正的風險不是計費而是髒資料。）
    pkill -TERM -P $$ 2>/dev/null || true

    if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
        if [[ -f "$REMOTE_CUDA_CHECK_SCRIPT" ]]; then rm -f "$REMOTE_CUDA_CHECK_SCRIPT"; fi
        if [[ -f "$REMOTE_INSTALL_SCRIPT" ]]; then rm -f "$REMOTE_INSTALL_SCRIPT"; fi
        if [[ -f "$REMOTE_EVIDENCE_SCRIPT" ]]; then rm -f "$REMOTE_EVIDENCE_SCRIPT"; fi
        if [[ -f "$REMOTE_ASR_SCRIPT" ]]; then rm -f "$REMOTE_ASR_SCRIPT"; fi
        if [[ -f "$REMOTE_PREP_DIRS_SCRIPT" ]]; then rm -f "$REMOTE_PREP_DIRS_SCRIPT"; fi
        if [[ -n "$LOCAL_FLAC_PATH" && -f "$LOCAL_FLAC_PATH" ]]; then rm -f "$LOCAL_FLAC_PATH"; fi
        if [[ "$POD_TERMINATED" == false && -n "$POD_ID" ]]; then
            info "cleaning up pod id=$POD_ID"
            if ! terminate_pod; then
                preserve_tmp_dir=true
                error "CLEANUP FAILURE: RunPod pod id=$POD_ID could not be terminated after $POD_TERMINATE_ATTEMPTS attempts; leaving pod id intact for manual cleanup"
                if [[ "$status" -eq 0 ]]; then
                    status=1
                fi
            fi
        fi
        if [[ "$preserve_tmp_dir" == false ]]; then
            rm -rf "$TMP_DIR"
        fi
    fi
    exit "$status"
}

if [[ $# -eq 1 && ( "$1" == "--help" || "$1" == "-h" ) ]]; then
    show_help
    exit 0
fi

if [[ $# -lt 5 ]]; then
    error "missing model flag: only --breeze is supported"
    show_help
    exit 2
fi

WAV_PATH="$1"
OUTPUT_DIR="$2"
BASENAME="$3"
LANGUAGE="$4"
shift 4
MODEL_FLAGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --breeze|--turbo|--vv)
            MODEL_FLAGS+=("$1")
            shift
            ;;
        --terms)
            if [[ $# -lt 2 ]]; then
                die "missing value for --terms"
            fi
            VV_TERMS_FILE="$2"
            shift 2
            ;;
        --terms-max)
            if [[ $# -lt 2 ]]; then
                die "missing value for --terms-max"
            fi
            VV_TERMS_MAX="$2"
            shift 2
            ;;
        --json)
            VV_JSON_ENABLED=true
            shift
            ;;
        *)
            die "invalid argument: $1"
            ;;
    esac
done

case "${#MODEL_FLAGS[@]}" in
    1)
        case "${MODEL_FLAGS[0]}" in
            --breeze)
                ASR_MODE="breeze"
                ;;
            --turbo)
                die "cloud ASR only supports --breeze; this configuration was not tested on cloud. Use subtitle.sh --breeze or revert to --engine=mlx."
                ;;
            --vv)
                ASR_MODE="vv"
                ;;
            *)
                die "invalid model flag: ${MODEL_FLAGS[0]}"
                ;;
        esac
        ;;
    2)
        if [[ ( "${MODEL_FLAGS[0]}" == "--breeze" && "${MODEL_FLAGS[1]}" == "--turbo" ) || ( "${MODEL_FLAGS[0]}" == "--turbo" && "${MODEL_FLAGS[1]}" == "--breeze" ) ]]; then
            die "cloud ASR only supports --breeze; --breeze and --turbo cannot both be set."
        fi
        die "invalid model flag combination: ${MODEL_FLAGS[*]}"
        ;;
    *)
        die "cloud ASR only supports --breeze"
        ;;
esac

if [[ "$ASR_MODE" == "breeze" && ( -n "$VV_TERMS_FILE" || "$VV_TERMS_MAX" != "50" || "$VV_JSON_ENABLED" == true ) ]]; then
    die "VV options are only valid with --vv"
fi

# `[[ -v VAR ]]` 需要 bash 4.2 以上，但 macOS 內建的 /bin/bash 是 3.2.57
# （蘋果因授權問題二十年沒更新），shebang 的 `env bash` 在沒裝 brew bash 的
# 機器上就會指到它，整支腳本在這行炸掉並回 exit 2。
# `${VAR+x}` 是 3.2 就有的等價寫法：變數有設定就展開成 x，即使是空字串。
if [[ -n "${INITIAL_PROMPT+x}" ]]; then
    die "INITIAL_PROMPT is not accepted by cloud_asr.sh"
fi

if [[ ! -f "$WAV_PATH" ]]; then
    die "input audio not found: $WAV_PATH"
fi

load_runpod_api_key() {
    if [[ -n "${RUNPOD_API_KEY:-}" ]]; then
        return 0
    fi

    local api_key_file="$HOME/.config/runpod/api_key"
    if [[ -r "$api_key_file" ]]; then
        RUNPOD_API_KEY="$(<"$api_key_file")"
        if [[ -n "${RUNPOD_API_KEY:-}" ]]; then
            return 0
        fi
    fi

    printf '[cloud_asr.sh] ERROR: %s - %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "missing RunPod credential: set RUNPOD_API_KEY or create $api_key_file" >&2
    exit 1
}

load_runpod_api_key
load_ssh_public_key

if [[ -z "$BASENAME" || "$BASENAME" == */* ]]; then
    die "output basename must be a single path component"
fi

require_cmd curl
require_cmd jq
require_cmd ssh
require_cmd scp
require_cmd ffmpeg
require_cmd python3

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cloud_asr.XXXXXX")"
SSH_BASE_OPTS=(
    -i "$SSH_PRIVATE_KEY"
    -o BatchMode=yes
    -o IdentitiesOnly=yes
    -o StrictHostKeyChecking=accept-new
    -o UserKnownHostsFile="$TMP_DIR/known_hosts"
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=4
    -o ConnectTimeout=20
)

if [[ ! -f "$SSH_PRIVATE_KEY" ]]; then
    die "SSH private key not found: $SSH_PRIVATE_KEY"
fi

mkdir -p "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR/env"
POD_ATTEMPTS_FILE="$OUTPUT_DIR/env/pod_attempts.txt"
: >"$POD_ATTEMPTS_FILE"
trap cleanup EXIT INT TERM HUP QUIT

if [[ "$ASR_MODE" == "vv" ]]; then
    run_vv_mode
    exit $?
fi

info "budget cap: ${COST_CAP_USD} USD (~${COST_CAP_SECONDS}s at ~${RUNPOD_ESTIMATED_RATE_PER_HR}/hr)"

ATTEMPT=0
while :; do
    ATTEMPT=$((ATTEMPT + 1))
    if (( ATTEMPT > 3 )); then
        die "direct endpoint unavailable after 3 pods; giving up"
    fi

    create_pod
    POD_ATTEMPT_STARTED_AT="$(date +%s)"
    write_remote_scripts

    if wait_for_ssh_ready; then
        status=0
    else
        status=$?
    fi
    if [[ $status -eq 0 ]]; then
        break
    fi
    if [[ $status -eq 124 ]]; then
        if ! terminate_pod; then
            die "timed out waiting for direct SSH on pod $POD_ID and termination failed: ${TERMINATE_LAST_ERROR:-<unknown>}"
        fi
        if (( ATTEMPT >= 3 )); then
            die "direct endpoint unavailable after 3 pods; giving up"
        fi
        continue
    fi

    if [[ $status -eq 42 ]]; then
        if ! terminate_pod; then
            die "SSH public key denied by RunPod backend after pod $POD_ID could not be terminated: ${TERMINATE_LAST_ERROR:-<unknown>}"
        fi
        die "SSH public key denied by RunPod backend"
    fi

    if ! terminate_pod; then
        die "failed while waiting for direct SSH on pod $POD_ID and termination failed: ${TERMINATE_LAST_ERROR:-<unknown>}"
    fi
    die "failed while waiting for direct SSH on pod $POD_ID"
done

info "direct endpoint ready on pod $POD_ID"
check_cost_cap

if cuda_probe_output="$(ssh_run_script "cuda_check" "$POD_IP" "$POD_PORT" "$REMOTE_CUDA_CHECK_SCRIPT")"; then
    case "$(tail -n 1 <<<"$cuda_probe_output")" in
        True)
            :
            ;;
        False)
            if ! terminate_pod; then
                die "torch.cuda.is_available() returned False on pod $POD_ID and termination failed: ${TERMINATE_LAST_ERROR:-<unknown>}"
            fi
            die "torch.cuda.is_available() returned False on pod $POD_ID"
            ;;
        *)
            if ! terminate_pod; then
                die "torch.cuda.is_available() returned unexpected output on pod $POD_ID and termination failed: ${TERMINATE_LAST_ERROR:-<unknown>}"
            fi
            die "torch.cuda.is_available() returned unexpected output on pod $POD_ID"
            ;;
    esac
else
    status=$?
    if [[ $status -eq 42 ]]; then
        if ! terminate_pod; then
            die "SSH public key denied by RunPod backend after pod $POD_ID could not be terminated: ${TERMINATE_LAST_ERROR:-<unknown>}"
        fi
        die "SSH public key denied by RunPod backend"
    fi
    if ! terminate_pod; then
        die "torch.cuda.is_available() failed on pod $POD_ID and termination failed: ${TERMINATE_LAST_ERROR:-<unknown>}"
    fi
    die "torch.cuda.is_available() failed on pod $POD_ID (exit $status)"
fi
info "CUDA validated on pod $POD_ID"
check_cost_cap

if ssh_run_script "install_env" "$POD_IP" "$POD_PORT" "$REMOTE_INSTALL_SCRIPT"; then
    :
else
    status=$?
    terminate_pod || true
    if [[ $status -eq 42 ]]; then
        die "SSH public key denied by RunPod backend"
    fi
    die "install_env failed on pod $POD_ID (exit $status)"
fi
check_cost_cap

LOCAL_FLAC_PATH="${TMP_DIR}/${BASENAME}.flac"
REMOTE_FLAC_PATH="${REMOTE_AUDIO_DIR}/${BASENAME}.flac"
REMOTE_JSON_PATH="${REMOTE_OUTPUT_DIR}/${BASENAME}.cloud.json"
LOCAL_JSON_PATH="${OUTPUT_DIR}/${BASENAME}.cloud.json"
LOCAL_SRT_PATH="${OUTPUT_DIR}/${BASENAME}.srt"

info "converting input WAV to FLAC"
ffmpeg -y -i "$WAV_PATH" -ar 16000 -ac 1 -c:a flac -compression_level 12 "$LOCAL_FLAC_PATH" >/dev/null 2>&1
check_cost_cap

info "preparing remote directories on root@$POD_IP -p $POD_PORT"
if ssh_run_script "prepare_remote_dirs" "$POD_IP" "$POD_PORT" "$REMOTE_PREP_DIRS_SCRIPT"; then
    :
else
    status=$?
    terminate_pod || true
    if [[ $status -eq 42 ]]; then
        die "SSH public key denied by RunPod backend"
    fi
    die "remote directory preparation failed on pod $POD_ID (exit $status)"
fi
check_cost_cap

info "uploading FLAC to root@$POD_IP -p $POD_PORT"
if scp_upload "$LOCAL_FLAC_PATH" "$POD_IP" "$POD_PORT" "$REMOTE_FLAC_PATH"; then
    :
else
    status=$?
    terminate_pod || true
    if [[ $status -eq 42 ]]; then
        die "SSH public key denied by RunPod backend"
    fi
    die "FLAC upload failed to root@$POD_IP -p $POD_PORT (exit $status)"
fi
check_cost_cap

info "collecting pod evidence on pod $POD_ID before fault injection"
if ssh_run_script "collect_evidence" "$POD_IP" "$POD_PORT" "$REMOTE_EVIDENCE_SCRIPT"; then
    :
else
    status=$?
    terminate_pod || true
    if [[ $status -eq 42 ]]; then
        die "SSH public key denied by RunPod backend"
    fi
    die "evidence collection failed on pod $POD_ID (exit $status)"
fi

mkdir -p "$OUTPUT_DIR/env"
for evidence_path in "$OUTPUT_DIR/env/nvidia-smi.txt" "$OUTPUT_DIR/env/pod_id.txt"; do
    if [[ -e "$evidence_path" ]]; then
        die "refusing to overwrite existing evidence file: $evidence_path"
    fi
done
info "downloading pod evidence from pod $POD_ID"
if scp_download "$POD_IP" "$POD_PORT" "${REMOTE_OUTPUT_DIR}/env/nvidia-smi.txt" "$OUTPUT_DIR/env/nvidia-smi.txt"; then
    :
else
    status=$?
    terminate_pod || true
    if [[ $status -eq 42 ]]; then
        die "SSH public key denied by RunPod backend"
    fi
    die "nvidia-smi evidence download failed from root@$POD_IP -p $POD_PORT (exit $status)"
fi
if scp_download "$POD_IP" "$POD_PORT" "${REMOTE_OUTPUT_DIR}/env/pod_id.txt" "$OUTPUT_DIR/env/pod_id.txt"; then
    :
else
    status=$?
    terminate_pod || true
    if [[ $status -eq 42 ]]; then
        die "SSH public key denied by RunPod backend"
    fi
    die "pod id evidence download failed from root@$POD_IP -p $POD_PORT (exit $status)"
fi
if [[ "$(tr -d '\r\n' < "$OUTPUT_DIR/env/pod_id.txt")" != "$POD_ID" ]]; then
    terminate_pod || true
    die "pod id evidence mismatch after download: expected $POD_ID"
fi
check_cost_cap

fail_step 6
info "running faster-whisper Breeze-ASR-25 CT2 FP16 on pod $POD_ID"
if ssh_run_script "asr" "$POD_IP" "$POD_PORT" "$REMOTE_ASR_SCRIPT"; then
    :
else
    status=$?
    terminate_pod || true
    if [[ $status -eq 42 ]]; then
        die "SSH public key denied by RunPod backend"
    fi
    die "ASR failed on pod $POD_ID (exit $status)"
fi
check_cost_cap

info "downloading raw JSON from pod $POD_ID"
if scp_download "$POD_IP" "$POD_PORT" "$REMOTE_JSON_PATH" "$LOCAL_JSON_PATH"; then
    :
else
    status=$?
    terminate_pod || true
    if [[ $status -eq 42 ]]; then
        die "SSH public key denied by RunPod backend"
    fi
    die "JSON download failed from root@$POD_IP -p $POD_PORT (exit $status)"
fi
check_cost_cap

info "converting JSON to SRT"
python3 "$SCRIPT_DIR/cloud_to_srt.py" breeze "$LOCAL_JSON_PATH" "$LOCAL_SRT_PATH"
check_cost_cap

info "completed cloud ASR: $LOCAL_SRT_PATH"
exit 0
