#!/usr/bin/env bash
# vast_instance_lib.sh — Vast.ai 機器生命週期的共用函式
#
# 與 runpod_pod_lib.sh 同一個位置、同一套分工（2026-09-03 新增，先給 bookcast 用）。
# RunPod 走 REST；Vast.ai 這裡走官方 CLI `vastai`（1.6.0 實測），因為它的 REST
# 端點沒有公開的穩定契約，而 CLI 的 --raw 輸出就是 API 的 JSON 原樣。
#
# Vast.ai 與 RunPod 三個不一樣的地方，呼叫端要知道：
#   1. 沒有「給我一張 4090」這種開機法。要先 search offers 拿到一張報價單，
#      再拿報價 id 開機；報價可能在你按下去之前被別人租走，開機失敗要換下一張。
#   2. 機器沒有 name，只有 label。「依名字找活的」在這裡是依 label 找。
#   3. 停機（stop）保留整顆本機磁碟，不用另掛持久碟；但重新啟動要看原主機
#      有沒有空 GPU，跟 RunPod 停機一樣可能啟動不了。
#
# 這個檔**只管 CLI 與機器狀態**，刻意不做的事跟 runpod_pod_lib.sh 一致：
#   不擁有 INSTANCE_ID、不設 trap、不管費用上限與紀錄、不印自己的格式
#   （呼叫端 source 後重定義 vast_lib_info/error/die 接回自己的 log）。
#
# 需求：bash 3.2+、jq、vastai（pip/uv 裝的 CLI）。用法：
#   source "<path>/vast_instance_lib.sh"
#   vast_lib_load_api_key || die "找不到金鑰"
#   rows="$(vast_lib_pick_offers "RTX 5090" 40 0.6)"     # 每行 "報價id<TAB>摘要"，便宜在前
#   id="$(vast_lib_create_instance "$offer" "$image" 40 "bookcast-foo")"
#   vast_lib_terminate_instance_once "$id" || echo "$VAST_LIB_TERMINATE_LAST_ERROR"
#
# 測試替身：設 VAST_LIB_CLI=<可執行檔路徑> 就不跑真的 vastai。替身收到的參數
# 跟真 CLI 一模一樣（含 --raw），stdout 印 JSON，rc 非 0＝指令失敗。

VAST_LIB_CLI="${VAST_LIB_CLI:-vastai}"
VAST_LIB_TERMINATE_LAST_ERROR=""
# 每次 CLI 呼叫的秒數上限。CLI 自己有 --retry，但沒有總逾時；沒這層的話
# 一次卡住的查詢會讓上層輪詢停擺，機器在那邊繼續計費。
VAST_LIB_CLI_TIMEOUT="${VAST_LIB_CLI_TIMEOUT:-90}"
# 主機驅動支援的 CUDA 下限。要跟映像的 CUDA 版本對齊（vastai/pytorch …-cuda-12.9-… 就是 12.9）：
# 2026-09-03 土耳其 4090（驅動 570、CUDA 12.8）過了 12.8 的篩選，torch 卻報
# 「Error 804: forward compatibility was attempted on non supported HW」——GeForce 不能向前相容，
# 驅動比映像舊就是不能用。成功的韓國 5090 是 13.0。
VAST_LIB_MIN_CUDA="${VAST_LIB_MIN_CUDA:-12.9}"
# 只挑固定 IP 的主機。2026-09-03 四台 4090 四敗（loading 零訊息／拉一半卡住／SSH 不通）都是
# static_ip=false、hosting_type=0 的家用線路；五次五過的韓國 5090 是 static_ip=true、hosting_type=1。
# 官方文件也建議有穩定連線需求就過濾 static_ip=true（家用路由器的埠轉發壞掉＝直連永遠不通）。
# 代價：當下 4090 16 台剩 6 台、5090 35 台剩 17 台，最便宜價差不到 0.03 美元。設 0 關掉。
VAST_LIB_REQUIRE_STATIC_IP="${VAST_LIB_REQUIRE_STATIC_IP:-1}"

vast_lib_info() {
    printf '[vast] %s\n' "$*" >&2
}

vast_lib_error() {
    printf '[vast] ERROR: %s\n' "$*" >&2
}

vast_lib_die() {
    vast_lib_error "$*"
    exit 1
}

# 金鑰：環境變數 VAST_API_KEY 優先，其次 ~/.config/vastai/vast_api_key（CLI 自己的位置）。
# rc 1＝兩邊都沒有。有環境變數時每次呼叫都明確帶 --api-key，不賭 CLI 認不認那個變數。
vast_lib_load_api_key() {
    if [[ -n "${VAST_API_KEY:-}" ]]; then
        return 0
    fi
    local f="$HOME/.config/vastai/vast_api_key"
    if [[ -r "$f" ]]; then
        VAST_API_KEY="$(tr -d '[:space:]' <"$f")"
        [[ -n "$VAST_API_KEY" ]] && return 0
    fi
    return 1
}

vast_lib_api_key_file_hint() {
    printf '%s\n' "$HOME/.config/vastai/vast_api_key"
}

# 跑一次 CLI，永遠帶 --raw。stdout＝JSON；rc＝CLI 的 rc。
# 不印錯誤：呼叫端決定要不要講、怎麼講（quiet 查詢在輪詢裡不該洗版）。
vast_lib_cli() {
    local -a cmd
    if command -v timeout >/dev/null 2>&1; then
        cmd=(timeout "$VAST_LIB_CLI_TIMEOUT" "$VAST_LIB_CLI")
    else
        cmd=("$VAST_LIB_CLI")
    fi
    if [[ -n "${VAST_API_KEY:-}" ]]; then
        cmd+=(--api-key "$VAST_API_KEY")
    fi
    # --raw 一律放在 --args 前面：--args 會把後面所有東西吃進容器參數，放最後就變成 vLLM 的 --raw
    # （2026-09-04 book-translator 首跑：vllm: error: unrecognized arguments: --raw，容器反覆重啟）。
    local -a pre=() post=() a; local seen_args=0
    for a in "$@"; do
        if [[ $seen_args -eq 0 && "$a" == "--args" ]]; then seen_args=1; fi
        if [[ $seen_args -eq 0 ]]; then pre+=("$a"); else post+=("$a"); fi
    done
    "${cmd[@]}" "${pre[@]}" --raw ${post[@]+"${post[@]}"} 2>/dev/null
}

# 依規格挑報價。stdout＝一行一張，格式 "報價id<TAB>一行摘要"，時價由低到高；
# 沒有符合的印空、rc 1。摘要跟 id 印在同一行，是因為呼叫端多半在 $(…) 裡呼叫，
# 函式內設的全域變數傳不出去（bash 子殼），所以不能「先拿 id 再另外查摘要」。
#   GPU        顯卡名，可含空白（"RTX 5090"），這裡會換成 Vast 查詢語法要的底線
#   DISK_GB    要的磁碟
#   MAX_DPH    每小時美元上限（含磁碟），超過的直接不列
#   GEO_EXCLUDE  可省；逗號分隔的國碼，沒傳＝CN,VN（跨境傳 wav 慢），傳空字串＝不排除
#   EXTRA      可省；附加在查詢字串尾端的條件（例如 "inet_down>=500"）
#   IP_EXCLUDE 可省；逗號分隔的 public_ipaddr 前綴，本機過濾。沒傳＝137.175.,207.246.98.,144.202.115.
#              （2026-09-03 實測：這個美國註冊的網段有三台主機拉映像 20 分鐘零進度，
#              Vast 自己的 geolocation 一下標 US 一下標 CN；台灣主機 3 分鐘拉完。
#              geolocation 靠 IP 反查，擋不住這種「IP 在美國、機器在中國」的主機，
#              只能用 IP 前綴擋。）
# CUDA 下限由 VAST_LIB_MIN_CUDA（預設 12.9）決定，換映像時要一起改。
# 為什麼要 verified + reliability + direct_port_count：
#   沒 verified 的機器 CUDA 可能不能用；沒直連埠就沒有直連 SSH，整條流程跑不起來
#   （跟 RunPod 社群雲要 --public-ip 是同一件事）。
vast_lib_pick_offers() {
    local gpu="$1" disk_gb="$2" max_dph="$3" geo_exclude="${4-CN,VN}" extra="${5-}" ip_exclude="${6-137.175.,207.246.98.,144.202.115.}"
    local gpu_q query geo_q offers
    gpu_q="${gpu// /_}"
    geo_q="$(printf '%s' "$geo_exclude" | tr ',' '\n' | sed '/^$/d' | sed 's/^/"/; s/$/"/' | paste -sd, -)"
    query="gpu_name=${gpu_q} num_gpus=1 rentable=true verified=true reliability>=0.98 cuda_vers>=${VAST_LIB_MIN_CUDA} disk_space>=${disk_gb} direct_port_count>=1 inet_up>=100 inet_down>=100 dph_total<=${max_dph}"
    [[ "$VAST_LIB_REQUIRE_STATIC_IP" == "1" ]] && query="$query static_ip=true"
    [[ -n "$geo_q" ]] && query="$query geolocation notin [${geo_q}]"
    [[ -n "$extra" ]] && query="$query $extra"
    offers="$(vast_lib_cli search offers "$query" --order dph_total --limit 20)" || return 1
    # 伺服器端的 dph_total<= 只是提示，本機再濾一次：上限是錢的事，不能只信對方。
    jq -r --argjson cap "$max_dph" --arg ipx "$ip_exclude" '
        ($ipx | split(",") | map(select(length > 0))) as $prefixes
        | (if type == "array" then . elif type == "object" and (.offers? | type == "array") then .offers else [] end)
        | map(select((.dph_total // 1e9) <= $cap))
        | map(select((.public_ipaddr // "") as $ip | any($prefixes[]; . as $p | $ip | startswith($p)) | not))
        | sort_by(.dph_total)
        | .[]
        | "\(.id)\t\(.gpu_name) \(.dph_total | tostring | .[0:5]) USD/h \(.geolocation // "?") 可靠度 \(.reliability2 // .reliability // "?" | tostring | .[0:5]) 下載 \(.inet_down // "?" | tostring | .[0:5]) Mbps"
    ' <<<"$offers" 2>/dev/null | grep -v '^$' || return 1
}

# 從 create 的回應撈 new_contract。--ssh 模式回 JSON；args 模式（2026-09-04 實測）回
# 「Started. {'success': True, 'new_contract': 49767561, ...}」——Python 字典的字串，不是 JSON。
# 兩種都吃。呼叫端拿不到 id 時**還是要依 label 回查**：解析失敗不等於沒開機（那天連漏三台）。
vast_lib_parse_new_contract() {
    local resp="$1" id
    id="$(jq -r 'select(type == "object") | select(.success == true) | .new_contract // empty' <<<"$resp" 2>/dev/null || true)"
    if [[ -z "$id" ]]; then
        id="$(sed -n "s/.*'new_contract': *\([0-9][0-9]*\).*/\1/p" <<<"$resp" | head -n 1)"
        [[ -n "$id" ]] && ! grep -q "'success': *True" <<<"$resp" && id=""
    fi
    printf '%s' "$id"
}

# 開機。rc 0：stdout＝新機器 id。rc 1（報價被搶、餘額不足、參數錯）：stdout＝錯誤原文。
# 錯誤走 stdout 而不是全域變數，因為呼叫端幾乎都在 $(…) 裡呼叫，子殼裡設的變數傳不回去。
#   OFFER_ID  報價 id
#   IMAGE     docker 映像
#   DISK_GB   本機磁碟（停機時保留）
#   LABEL     機器標籤，之後「依名字找活的」靠它
#   ONSTART   可省；開機腳本內容
# --ssh --direct：直連 SSH（不經 Vast 的跳板，rsync 快得多）
# --cancel-unavail：排不到資源就直接失敗，不要建一台「停機中」的機器躺著算磁碟費
vast_lib_create_instance() {
    local offer_id="$1" image="$2" disk_gb="$3" label="$4" onstart="${5:-}"
    local resp id rc=0
    local -a args=(create instance "$offer_id" --image "$image" --disk "$disk_gb"
                   --label "$label" --ssh --direct --cancel-unavail)
    [[ -n "$onstart" ]] && args+=(--onstart-cmd "$onstart")
    resp="$(vast_lib_cli "${args[@]}")" || rc=$?
    id="$(vast_lib_parse_new_contract "$resp")"
    if [[ -n "$id" ]]; then
        printf '%s\n' "$id"
        return 0
    fi
    printf 'rc=%s resp=%s\n' "$rc" "$(tr -d '\r\n' <<<"${resp:-<empty>}")"
    return 1
}

# 開機（args 模式，不注入 SSH）：容器直接跑映像的 ENTRYPOINT，參數由 ARGS 給，服務埠用 ENV 的
# "-p 8000:8000" 對外開。給「開一個 HTTP 伺服器讓本機打」的用法（book-translator 的 vLLM，2026-09-04）。
# rc 0：stdout＝新機器 id；rc 1：stdout＝錯誤原文（理由同上面那個函式）。
#   OFFER_ID IMAGE DISK_GB LABEL ENV_STR  然後 ARGS...（原樣接在 --args 後面）
# ENV_STR 是 vastai 的 --env 字串，例如 '-p 8000:8000 -e HF_TOKEN=xxx'。
vast_lib_create_instance_args() {
    local offer_id="$1" image="$2" disk_gb="$3" label="$4" env_str="$5"; shift 5
    local resp id rc=0
    local -a args=(create instance "$offer_id" --image "$image" --disk "$disk_gb"
                   --label "$label" --cancel-unavail --env "$env_str" --args "$@")
    resp="$(vast_lib_cli "${args[@]}")" || rc=$?
    id="$(vast_lib_parse_new_contract "$resp")"
    if [[ -n "$id" ]]; then
        printf '%s\n' "$id"
        return 0
    fi
    printf 'rc=%s resp=%s\n' "$rc" "$(tr -d '\r\n' <<<"${resp:-<empty>}")"
    return 1
}

# stdout＝"host<TAB>port"：某個容器埠（例如 8000）對外的公網位址。ports 在容器起來前是 null，rc 1。
vast_lib_port_endpoint_from_record() {
    local record="$1" port="$2"
    local host hostport
    host="$(jq -r '.public_ipaddr // empty' <<<"$record" 2>/dev/null)"
    hostport="$(jq -r --arg p "${port}/tcp" '((.ports // {})[$p] // [] | .[0].HostPort // empty) | tostring' <<<"$record" 2>/dev/null)"
    if [[ -n "$host" && -n "$hostport" && "$host" != "null" && "$hostport" != "null" ]]; then
        printf '%s\t%s\n' "$host" "$hostport"
        return 0
    fi
    return 1
}

# 存活機器清單，正規化成 JSON 陣列。rc 2＝查詢失敗（跟「清單是空的」分開）。
# Vast 砍掉的機器會從清單消失，但保險起見仍濾掉 actual_status 帶 destroy 的。
vast_lib_instances_live_json() {
    local response
    response="$(vast_lib_cli show instances)" || return 2
    jq -c '
        (if type == "array" then .
         elif type == "object" and (.instances? | type == "array") then .instances
         else [] end)
        | map(select(((.actual_status // "") | ascii_downcase | test("destroy")) | not))
    ' <<<"$response" 2>/dev/null || return 2
}

# rc 0＝在存活清單裡 / 1＝不在 / 2＝查詢失敗
vast_lib_instance_live_in_list() {
    local instance_id="$1"
    local list
    list="$(vast_lib_instances_live_json)" || return 2
    if jq -e --argjson id "$instance_id" 'any((.id | tonumber) == $id)' <<<"$list" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

# stdout＝同 label 且存活的第一台的 id；沒有就空字串（查詢失敗也是空字串）
vast_lib_find_live_instance_by_label() {
    local want="$1"
    local list
    list="$(vast_lib_instances_live_json 2>/dev/null)" || return 0
    jq -r --arg want "$want" 'map(select(.label == $want)) | .[0].id // empty' <<<"$list" 2>/dev/null || true
}

# 單台紀錄。show instance 有時回單一物件、有時回一個元素的陣列，兩種都拆。rc 1＝查不到。
vast_lib_instance_record() {
    local instance_id="$1"
    local response record
    response="$(vast_lib_cli show instance "$instance_id")" || return 1
    record="$(jq -c '
        (if type == "array" then .[0] else . end)
        | select(type == "object" and (.id? != null))
    ' <<<"$response" 2>/dev/null || true)"
    [[ -n "$record" ]] || return 1
    printf '%s\n' "$record"
}

# stdout＝actual_status（小寫；還沒配置到時是 "null"）。rc 1＝查不到。
vast_lib_instance_status() {
    local record
    record="$(vast_lib_instance_record "$1")" || return 1
    jq -r '(.actual_status // "null") | ascii_downcase' <<<"$record"
}

# stdout＝status_msg（docker pull 的最後幾行，loading 期間用來看有沒有進度）。rc 1＝查不到。
vast_lib_instance_status_msg() {
    local record
    record="$(vast_lib_instance_record "$1")" || return 1
    jq -r '.status_msg // ""' <<<"$record"
}

# 這幾個狀態永遠不會變成 running（官方文件），等下去只是燒磁碟費。
vast_lib_status_is_dead() {
    case "$1" in
        exited|unknown|offline) return 0 ;;
        *) return 1 ;;
    esac
}

# stdout＝"host<TAB>port"。優先直連（public_ipaddr + ports["22/tcp"][0].HostPort），
# 沒有才退回 ssh_host/ssh_port——那是 Vast 的跳板（ssh2.vast.ai），rsync 幾 GB wav
# 走跳板慢很多。官方 SDK 的 ssh_url() 順序相反（先 ssh_host），這裡刻意反過來。
# 注意 ports 在容器起來前是 null，所以開機初期只拿得到跳板；呼叫端第一次拿到
# 端點就定案，這是已知的取捨（2026-09-03 實測：loading 階段 ports=null）。
# rc 1＝連跳板都還沒生成。
vast_lib_ssh_endpoint_from_record() {
    local record="$1"
    local host port
    host="$(jq -r 'select((.ports // {})["22/tcp"] // [] | length > 0) | .public_ipaddr // empty' <<<"$record" 2>/dev/null)"
    port="$(jq -r '((.ports // {})["22/tcp"] // [] | .[0].HostPort // empty) | tostring' <<<"$record" 2>/dev/null)"
    if [[ -z "$host" || -z "$port" || "$host" == "null" || "$port" == "null" ]]; then
        host="$(jq -r '.ssh_host // empty' <<<"$record" 2>/dev/null)"
        port="$(jq -r '.ssh_port // empty | tostring' <<<"$record" 2>/dev/null)"
    fi
    if [[ -n "$host" && -n "$port" && "$host" != "null" && "$port" != "null" ]]; then
        printf '%s\t%s\n' "$host" "$port"
        return 0
    fi
    return 1
}

vast_lib_ssh_endpoint() {
    local record
    record="$(vast_lib_instance_record "$1")" || return 1
    vast_lib_ssh_endpoint_from_record "$record"
}

# 停機 / 啟動。rc 非 0＝指令失敗（quiet，不印）。
vast_lib_instance_action() {
    local instance_id="$1" action="$2"
    case "$action" in
        stop|start) ;;
        *) vast_lib_error "unknown instance action: $action"; return 1 ;;
    esac
    vast_lib_cli "$action" instance "$instance_id" >/dev/null
}

# 砍一台，並確認它真的從存活清單消失。只有確認消失才回 0。
# 「destroy 沒報錯」不算：要回查清單。rc 1 時原因在 VAST_LIB_TERMINATE_LAST_ERROR。
# destroy 對已經不存在的 id 會回非 0；那時再查清單，不在就算成功（同 RunPod 的 404 規則）。
# shellcheck disable=SC2034  # VAST_LIB_TERMINATE_LAST_ERROR 是給呼叫端讀的
vast_lib_terminate_instance_once() {
    local instance_id="$1"
    local destroy_rc=0 list_status=0
    VAST_LIB_TERMINATE_LAST_ERROR=""

    vast_lib_cli destroy instance "$instance_id" -y >/dev/null || destroy_rc=$?

    vast_lib_instance_live_in_list "$instance_id" || list_status=$?
    case "$list_status" in
        1)
            [[ "$destroy_rc" -ne 0 ]] && vast_lib_info "instance $instance_id already absent (destroy rc=$destroy_rc) — treating as terminated"
            return 0 ;;
        2) VAST_LIB_TERMINATE_LAST_ERROR="vastai show instances failed after destroy (destroy rc=$destroy_rc)"; return 1 ;;
        *) VAST_LIB_TERMINATE_LAST_ERROR="Vast list still contains instance id=$instance_id after destroy (destroy rc=$destroy_rc)"; return 1 ;;
    esac
}
