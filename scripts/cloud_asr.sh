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
COST_CAP_USD="${COST_CAP_USD:-0.5}"
SSH_PRIVATE_KEY="${SSH_PRIVATE_KEY:-$HOME/.ssh/id_ed25519}"
SSH_PUBLIC_KEY_PATH="${SSH_PUBLIC_KEY_PATH:-$HOME/.ssh/id_ed25519.pub}"
SSH_PROBE_TIMEOUT_SECONDS="${SSH_PROBE_TIMEOUT_SECONDS:-25}"
SSH_COMMAND_TIMEOUT_SECONDS="${SSH_COMMAND_TIMEOUT_SECONDS:-900}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-15}"
SSH_READY_TIMEOUT_SECONDS="${SSH_READY_TIMEOUT_SECONDS:-180}"
REMOTE_MODEL_ID="SoybeanMilk/faster-whisper-Breeze-ASR-25"
REMOTE_AUDIO_DIR="/root/in"
REMOTE_OUTPUT_DIR="/root/out"
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
Usage: cloud_asr.sh <wav_path> <output_dir> <output_basename> <language> --breeze

Run cloud ASR on a RunPod 4090 pod, fetch the raw JSON, and render the final SRT.

Arguments:
  <wav_path>          Input WAV path.
  <output_dir>        Output directory.
  <output_basename>   Output basename without extension.
  <language>          Transcription language.
  --breeze            Required. The only supported cloud mode.

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
    else
        "$@"
    fi
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
        '{name:$name,image:$image,gpu:{id:"NVIDIA GeForce RTX 4090",count:1},cloud:"SECURE",disk:60,ports:["22/tcp"],startSsh:true}' \
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

get_pod_ssh_endpoint() {
    local record ip port

    record="$(query_pod_record)"
    [[ -n "$record" ]] || return 1
    ip="$(jq -r '.ssh.direct.host // ((.runtime.ports // [])[]? | select((.type // "") == "tcp" and ((.private // .privatePort // empty | tostring) == "22")) | (.ip // empty))' <<<"$record" 2>/dev/null | head -n 1)"
    port="$(jq -r '(.ssh.direct.port // empty | tostring), ((.runtime.ports // [])[]? | select((.type // "") == "tcp" and ((.private // .privatePort // empty | tostring) == "22")) | (.public // .publicPort // empty | tostring))' <<<"$record" 2>/dev/null | grep -v '^$' | head -n 1)"
    if [[ -n "$ip" && -n "$port" && "$ip" != "null" && "$port" != "null" ]]; then
        printf '%s\t%s\n' "$ip" "$port"
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
        case "$local_path" in
            *.json)
                printf '[]\n' >"$local_path"
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
    local start now elapsed endpoint ip port probe_status

    if [[ "${CLOUD_ASR_TEST_HOOK:-}" == "signal_cleanup" ]]; then
        info "test hook: blocking before direct endpoint for pod $POD_ID"
        while :; do
            sleep 1
        done
    fi

    start="$(date +%s)"
    while :; do
        if endpoint="$(get_pod_ssh_endpoint)"; then
            IFS=$'\t' read -r ip port <<<"$endpoint"
            if ssh_probe_ready "$ip" "$port"; then
                POD_IP="$ip"
                POD_PORT="$port"
                append_pod_attempt_log "attempt=${ATTEMPT} pod_id=${POD_ID} action=direct_wait result=ready host=${POD_IP} port=${POD_PORT}"
                info "SSH ready at root@$POD_IP -p $POD_PORT"
                return 0
            else
                probe_status=$?
            fi
            if [[ $probe_status -eq 42 ]]; then
                append_pod_attempt_log "attempt=${ATTEMPT} pod_id=${POD_ID} action=direct_wait result=auth_failure host=$ip port=$port"
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
            append_pod_attempt_log "attempt=${ATTEMPT} pod_id=${POD_ID} action=direct_wait result=timeout elapsed=${elapsed}s"
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

if [[ $# -gt 6 ]]; then
    error "invalid number of arguments"
    show_help
    exit 2
fi

WAV_PATH="$1"
OUTPUT_DIR="$2"
BASENAME="$3"
LANGUAGE="$4"
MODEL_FLAGS=("${@:5}")

case "${#MODEL_FLAGS[@]}" in
    1)
        case "${MODEL_FLAGS[0]}" in
            --breeze)
                ;;
            --turbo)
                die "cloud ASR only supports --breeze; this configuration was not tested on cloud. Use subtitle.sh --breeze or revert to --engine=mlx."
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

if [[ -v INITIAL_PROMPT ]]; then
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
