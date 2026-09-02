#!/usr/bin/env bash
# runpod_pod_lib.sh — RunPod 機器生命週期的共用函式
#
# 給 srt 的 cloud_asr.sh 與 bookcast 的 bookcast_cloud.sh 共用（2026-09-02 抽出）。
# 兩支腳本原本各自寫了一份「打 API、讀金鑰、解清單、確認砍機」，
# 修一邊漏另一邊——例如 bookcast 學到「清單裡剛砍的機器可能還以 TERMINATED
# 留一陣子」而加了過濾，cloud_asr.sh 沒有；cloud_asr.sh 學到「DELETE 收到 404
# 代表已經不在」，bookcast 沒有。放在一起兩邊就都有。
#
# 這個檔**只管 API 與機器狀態**，刻意不做的事：
#   - 不擁有 POD_ID、不設 trap、不開機（payload 兩邊不同：bookcast 要持久磁碟、
#     cloud_asr 要 CUDA 版本篩選與換機重試）、不管費用上限與紀錄。
#     bash 每個訊號只有一個 trap 槽，lib 一設 trap 就會吃掉呼叫端自己的。
#   - 不印自己的格式：預設 runpod_lib_info/error/die 印 [runpod] 前綴，
#     呼叫端 source 之後重新定義這三個函式就能接回自己的 log 格式。
#
# 需求：bash 3.2+（macOS 內建）、curl、jq。用法：
#   source "<path>/runpod_pod_lib.sh"
#   runpod_lib_load_api_key || die "找不到金鑰"
#   id="$(runpod_lib_find_live_pod_by_name "bookcast-foo")"
#   runpod_lib_terminate_pod_once "$id" || echo "$RUNPOD_LIB_TERMINATE_LAST_ERROR"
#
# 測試替身：設 RUNPOD_LIB_TRANSPORT=<函式名> 就不打 curl。替身簽名
#   <函式> METHOD PATH BODY_FILE   （BODY_FILE 可為空字串）
# stdout 印回應 body；rc 0＝HTTP 2xx、rc 44＝模擬 HTTP 404、其他＝請求失敗
# （失敗的訊息由替身自己印，lib 不重印）。呼叫當下 RUNPOD_LIB_REQUEST_MODE
# 會是這次的 mode（fatal/nonfatal/quiet），替身要照 mode 決定要不要 die。

RUNPOD_API_BASE="${RUNPOD_API_BASE:-https://api.runpod.io/v2}"
RUNPOD_LIB_TRANSPORT="${RUNPOD_LIB_TRANSPORT:-}"
RUNPOD_LIB_REQUEST_MODE=""
RUNPOD_LIB_TERMINATE_LAST_ERROR=""

runpod_lib_info() {
    printf '[runpod] %s\n' "$*" >&2
}

runpod_lib_error() {
    printf '[runpod] ERROR: %s\n' "$*" >&2
}

runpod_lib_die() {
    runpod_lib_error "$*"
    exit 1
}

# 金鑰：環境變數優先，其次 ~/.config/runpod/api_key。
# rc 1＝兩邊都沒有；不自己 exit，訊息與退場方式由呼叫端決定。
# ⚠️ 讀得到檔案就會真的開機。要「不花錢」測參數，把 RUNPOD_API_BASE 指到
# 一個不存在的位址（例如 http://127.0.0.1:9），不要只 unset 環境變數。
runpod_lib_load_api_key() {
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
    return 1
}

runpod_lib_api_key_file_hint() {
    printf '%s\n' "$HOME/.config/runpod/api_key"
}

# 一次 REST 請求。
#   MODE    fatal（失敗就 die）/ nonfatal（印錯回 1）/ quiet（不印回 1）
#   METHOD  GET / POST / DELETE
#   PATH    以 / 開頭，接在 RUNPOD_API_BASE 後面
#   BODY_FILE  可省；JSON 檔路徑
# stdout＝回應 body。DELETE /pods/<id> 收到 404 視為成功（那台已經不在，
# 正是我們要的結果；2026-08-30 reap 先砍、cleanup 撞 404 被記成失敗的事故）。
runpod_lib_request() {
    local mode="$1"
    local method="$2"
    local path="$3"
    local body_file="${4-}"
    local response_file stderr_file http_code curl_status body curl_err url

    response_file="$(mktemp "${TMPDIR:-/tmp}/runpod_lib.XXXXXX")"
    stderr_file="$(mktemp "${TMPDIR:-/tmp}/runpod_lib.XXXXXX")"

    if [[ -n "$RUNPOD_LIB_TRANSPORT" ]]; then
        local transport_rc
        RUNPOD_LIB_REQUEST_MODE="$mode"
        transport_rc=0
        "$RUNPOD_LIB_TRANSPORT" "$method" "$path" "$body_file" >"$response_file" || transport_rc=$?
        RUNPOD_LIB_REQUEST_MODE=""
        case "$transport_rc" in
            0) http_code=200 ;;
            44) http_code=404 ;;
            *)
                rm -f "$response_file" "$stderr_file"
                return "$transport_rc"
                ;;
        esac
    else
        url="${RUNPOD_API_BASE}${path}"
        curl_status=0
        if [[ -n "$body_file" ]]; then
            http_code="$(curl -sS --connect-timeout 20 --max-time 120 \
                -o "$response_file" -w '%{http_code}' \
                -H "Authorization: Bearer $RUNPOD_API_KEY" \
                -H 'Content-Type: application/json' \
                -X "$method" \
                --data-binary "@$body_file" \
                "$url" 2>"$stderr_file")" || curl_status=$?
        else
            http_code="$(curl -sS --connect-timeout 20 --max-time 120 \
                -o "$response_file" -w '%{http_code}' \
                -H "Authorization: Bearer $RUNPOD_API_KEY" \
                -H 'Accept: application/json' \
                -X "$method" \
                "$url" 2>"$stderr_file")" || curl_status=$?
        fi
        if [[ "$curl_status" -ne 0 ]]; then
            body="$(tr -d '\r\n' <"$response_file" || true)"
            curl_err="$(tr -d '\r\n' <"$stderr_file" || true)"
            rm -f "$response_file" "$stderr_file"
            case "$mode" in
                fatal)
                    runpod_lib_die "RunPod API request failed (curl exit $curl_status) $method $path: ${body:-<empty>}${curl_err:+; curl: $curl_err}"
                    ;;
                nonfatal)
                    runpod_lib_error "RunPod API request failed (curl exit $curl_status) $method $path: ${body:-<empty>}${curl_err:+; curl: $curl_err}"
                    ;;
            esac
            return 1
        fi
    fi

    if [[ "$method" == "DELETE" && "$http_code" == "404" && "$path" == /pods/* ]]; then
        runpod_lib_info "pod already absent (HTTP 404 on DELETE $path) — treating as terminated"
        rm -f "$response_file" "$stderr_file"
        return 0
    fi

    if [[ ! "$http_code" =~ ^2 ]]; then
        body="$(tr -d '\r\n' <"$response_file" || true)"
        rm -f "$response_file" "$stderr_file"
        case "$mode" in
            fatal)
                runpod_lib_die "RunPod API HTTP $http_code $method $path: ${body:-<empty>}"
                ;;
            nonfatal)
                runpod_lib_error "RunPod API HTTP $http_code $method $path: ${body:-<empty>}"
                ;;
        esac
        return 1
    fi

    cat "$response_file"
    rm -f "$response_file" "$stderr_file"
}

# 存活機器清單，正規化成 JSON 陣列。
#   - v2 的 GET /pods 回應可能是裸陣列、{pods:[…]} 或 {data:[…]}，三種都拆
#   - 濾掉 status 含 terminat 的：剛砍掉的機器可能在清單裡留一陣子，
#     不濾掉的話每一次正常砍機都會被判成砍不掉
#   - ⚠️ v2 的 GET /pods 沒有 ?name= 之類的伺服器端過濾，傳了會被忽略並回
#     帳號裡全部機器；所以名字過濾一律在本機做（見 find_live_pod_by_name）
# rc 2＝請求失敗（跟「清單是空的」分開，呼叫端不能把查不到當成不在）。
runpod_lib_pods_live_json() {
    local response
    response="$(runpod_lib_request quiet GET '/pods')" || return 2
    jq -c '
        (if type == "array" then .
         elif type == "object" and (.pods? | type == "array") then .pods
         elif type == "object" and (.data? | type == "array") then .data
         else [] end)
        | map(select((.status // "") | ascii_downcase | test("terminat") | not))
    ' <<<"$response" 2>/dev/null || return 2
}

# rc 0＝在存活清單裡 / 1＝不在 / 2＝請求失敗
runpod_lib_pod_live_in_list() {
    local pod_id="$1"
    local pods
    pods="$(runpod_lib_pods_live_json)" || return 2
    if jq -e --arg id "$pod_id" 'any(.id == $id)' <<<"$pods" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

# stdout＝同名且存活的第一台的 id；沒有就空字串（請求失敗也是空字串）
runpod_lib_find_live_pod_by_name() {
    local want="$1"
    local pods
    pods="$(runpod_lib_pods_live_json 2>/dev/null)" || return 0
    jq -r --arg want "$want" 'map(select(.name == $want)) | .[0].id // empty' <<<"$pods" 2>/dev/null || true
}

# 單台機器的紀錄，拆掉 .pod / .data 包裝。rc 1＝查不到或格式不認得。
runpod_lib_pod_record() {
    local pod_id="$1"
    local response record
    response="$(runpod_lib_request quiet GET "/pods/$pod_id")" || return 1
    record="$(jq -c '
        if type == "object" and (.runtime? | type == "object") then .
        elif type == "object" and (.pod? | type == "object") then .pod
        elif type == "object" and (.data? | type == "object") and (.data.runtime? | type == "object") then .data
        elif type == "object" and (.id? | type == "string") then .
        else empty end
    ' <<<"$response" 2>/dev/null || true)"
    [[ -n "$record" ]] || return 1
    printf '%s\n' "$record"
}

# stdout＝"host<TAB>port"。先看 v2 的 ssh.direct，再退回 runtime.ports 裡 22/tcp。
# ⚠️ v2 沒有 publicIp / portMappings（那是 v1）。用舊欄位不會報錯，是空轉：
# 開機成功 → 解不出位置 → 等到逾時 → 砍機。rc 1＝端點還沒生成。
runpod_lib_ssh_endpoint_from_record() {
    local record="$1"
    local host port
    host="$(jq -r '.ssh.direct.host // ((.runtime.ports // [])[]? | select((.type // "") == "tcp" and ((.private // .privatePort // empty | tostring) == "22")) | (.ip // empty))' <<<"$record" 2>/dev/null | head -n 1)"
    port="$(jq -r '(.ssh.direct.port // empty | tostring), ((.runtime.ports // [])[]? | select((.type // "") == "tcp" and ((.private // .privatePort // empty | tostring) == "22")) | (.public // .publicPort // empty | tostring))' <<<"$record" 2>/dev/null | grep -v '^$' | head -n 1)"
    if [[ -n "$host" && -n "$port" && "$host" != "null" && "$port" != "null" ]]; then
        printf '%s\t%s\n' "$host" "$port"
        return 0
    fi
    return 1
}

runpod_lib_ssh_endpoint() {
    local pod_id="$1"
    local record
    record="$(runpod_lib_pod_record "$pod_id")" || return 1
    runpod_lib_ssh_endpoint_from_record "$record"
}

# 停機 / 啟動。v2 把 stop/start 併成 POST /pods/<id>/action 帶 JSON body；
# v1 的 /pods/<id>/stop 路徑已經不存在。rc 非 0＝請求失敗（quiet，不印）。
runpod_lib_pod_action() {
    local pod_id="$1"
    local action="$2"
    local body_file
    case "$action" in
        stop|start|restart) ;;
        *) runpod_lib_error "unknown pod action: $action"; return 1 ;;
    esac
    body_file="$(mktemp "${TMPDIR:-/tmp}/runpod_lib.XXXXXX")"
    printf '{"action":"%s"}\n' "$action" >"$body_file"
    local rc=0
    runpod_lib_request quiet POST "/pods/$pod_id/action" "$body_file" >/dev/null || rc=$?
    rm -f "$body_file"
    return "$rc"
}

# 砍一台，並確認它真的從存活清單消失。只有確認消失才回 0。
# 「DELETE 沒報錯」不算：要回查清單。rc 1 時原因在 RUNPOD_LIB_TERMINATE_LAST_ERROR。
# 重試與紀錄由呼叫端做（cloud_asr 有每次嘗試的 ledger，bookcast 沒有）。
runpod_lib_terminate_pod_once() {
    local pod_id="$1"
    local list_status
    RUNPOD_LIB_TERMINATE_LAST_ERROR=""

    if ! runpod_lib_request nonfatal DELETE "/pods/$pod_id" >/dev/null; then
        RUNPOD_LIB_TERMINATE_LAST_ERROR="RunPod DELETE /pods/$pod_id failed"
        return 1
    fi

    list_status=0
    runpod_lib_pod_live_in_list "$pod_id" || list_status=$?
    case "$list_status" in
        1) return 0 ;;
        2) RUNPOD_LIB_TERMINATE_LAST_ERROR="RunPod GET /pods failed after delete"; return 1 ;;
        *) RUNPOD_LIB_TERMINATE_LAST_ERROR="RunPod list still contains pod id=$pod_id after delete"; return 1 ;;
    esac
}
