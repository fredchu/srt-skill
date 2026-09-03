#!/usr/bin/env bash
# 收屍工具（Vast.ai 版）：把還活著的 Vast.ai 機器列出來，超過時限的砍掉。
#
# 為什麼需要這支：cloud_asr.sh／bookcast_cloud.sh 自己有 trap，正常結束、逾時、Ctrl-C、
# 被 kill 都會自己砍機。**但 SIGKILL（kill -9）攔不住**——關掉終端機、當機、斷電也一樣。
# 那時機器會留在雲端**一直計費**（停機的只算磁碟費，也是錢），而且沒有任何東西會告訴你。
# 跟 runpod_reap.sh 一樣：獨立於主流程，手動跑；**刻意不做定時自動收屍**（排程是盲的，
# 不知道那台在跑什麼）。
#
# 用法：
#   vast_reap.sh                      只列出，不砍（預設）
#   vast_reap.sh --kill-older-than 30 砍掉開機超過 30 分鐘的
#   vast_reap.sh --kill-all           砍掉全部（慎用）
#
# 憑證：VAST_API_KEY 環境變數，或 ~/.config/vastai/vast_api_key（vastai CLI 自己的位置）

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=vast_instance_lib.sh
source "$SCRIPT_DIR/vast_instance_lib.sh"

MAX_MIN=""
KILL_ALL=false
while [ $# -gt 0 ]; do
    case "$1" in
        --kill-older-than) MAX_MIN="${2:-}"; shift 2 ;;
        --kill-all)        KILL_ALL=true; shift ;;
        --help|-h)         sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)                 echo "未知選項：$1（用 --help 看說明）" >&2; exit 1 ;;
    esac
done
if [ -n "$MAX_MIN" ] && ! [[ "$MAX_MIN" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "--kill-older-than 要接分鐘數，收到：$MAX_MIN" >&2; exit 1
fi

vast_lib_load_api_key || { echo "找不到 Vast.ai 金鑰：設 VAST_API_KEY，或建立 $(vast_lib_api_key_file_hint)" >&2; exit 1; }
command -v "$VAST_LIB_CLI" >/dev/null 2>&1 || { echo "需要 vastai CLI（pip install vastai）" >&2; exit 1; }

# 查不到清單＝不知道有沒有在燒錢，這是紅燈不是綠燈。
if ! LIST="$(vast_lib_instances_live_json)"; then
    echo "🔴 查不到機器清單，無法確認有沒有在燒錢（vastai show instances 失敗）"
    exit 1
fi
N="$(jq 'length' <<<"$LIST")"
if [ "$N" = "0" ]; then
    echo "✅ 目前沒有任何 Vast.ai 機器在跑"
    exit 0
fi

NOW="$(date +%s)"
killed=0; failed=0
while IFS=$'\t' read -r id label status dph start; do
    [ -n "$id" ] || continue
    should_kill=false
    if [ -z "$start" ] || [ "$start" = "null" ]; then
        # 算不出開機多久＝不知道燒了多少錢，當【危險】不是【安全】
        age_text="未知（start_date 缺）"
        [ "$KILL_ALL" = true ] && should_kill=true
        mark="🔴"
    else
        mins="$(awk -v n="$NOW" -v s="$start" 'BEGIN{printf "%.1f", (n-s)/60}')"
        cost="$(awk -v m="$mins" -v d="$dph" 'BEGIN{printf "%.2f", d*m/60}')"
        age_text="${mins} 分，約 US\$${cost}"
        if [ "$KILL_ALL" = true ]; then should_kill=true
        elif [ -n "$MAX_MIN" ] && awk -v m="$mins" -v x="$MAX_MIN" 'BEGIN{exit !(m>x)}'; then should_kill=true; fi
        mark="  "; [ "$should_kill" = true ] && mark="🔴"
    fi
    echo "${mark} ${id} (${label:-無標籤}) 已跑 ${age_text}，狀態=${status}"
    if [ "$should_kill" = true ]; then
        if vast_lib_terminate_instance_once "$id"; then
            echo "   ✅ 已砍並確認消失"; killed=$((killed+1))
        else
            echo "   🔴 砍不掉：${VAST_LIB_TERMINATE_LAST_ERROR}"; failed=$((failed+1))
        fi
    fi
done < <(jq -r '.[] | [(.id|tostring), (.label // ""), (.actual_status // "?"), ((.dph_total // 0)|tostring), ((.start_date // "null")|tostring)] | @tsv' <<<"$LIST")

echo "共 ${N} 台；砍掉 ${killed}，失敗 ${failed}"
[ "$failed" -eq 0 ] || exit 1
[ "$KILL_ALL" = true ] || [ -n "$MAX_MIN" ] || echo "（只列出。要砍：--kill-older-than N 或 --kill-all）"
