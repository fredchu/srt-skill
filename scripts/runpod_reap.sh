#!/usr/bin/env bash
# 收屍工具：把還活著的 RunPod 機器列出來，超過時限的砍掉。
#
# 為什麼需要這支：cloud_asr.sh 自己有 trap，正常結束、逾時、Ctrl-C、
# 被 kill 都會自己砍機。**但 SIGKILL（kill -9）攔不住** —— 那是作業系統
# 的硬規定，任何程式都攔不下來。關掉整個終端機視窗、當機、斷電也一樣。
# 那時機器會留在雲端**一直計費**，而且沒有任何東西會告訴你。
#
# 所以這支要獨立於主流程執行。建議做法：
#   1. 每次跑完 ASR 之後手動跑一次 `runpod_reap.sh`
#   2. 或設成排程，例如每小時跑一次 `runpod_reap.sh --kill-older-than 60`
#
# 用法：
#   runpod_reap.sh                      只列出，不砍（預設）
#   runpod_reap.sh --kill-older-than 30 砍掉開機超過 30 分鐘的
#   runpod_reap.sh --kill-all           砍掉全部（慎用）
#
# 憑證：讀 RUNPOD_API_KEY 環境變數，或 ~/.config/runpod/api_key

set -uo pipefail

MAX_MIN=""
KILL_ALL=false

while [ $# -gt 0 ]; do
    case "$1" in
        --kill-older-than) MAX_MIN="${2:-}"; shift 2 ;;
        --kill-all)        KILL_ALL=true; shift ;;
        --help|-h)         sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)                 echo "未知選項：$1（用 --help 看說明）" >&2; exit 1 ;;
    esac
done

API_KEY="${RUNPOD_API_KEY:-}"
if [ -z "$API_KEY" ] && [ -r "$HOME/.config/runpod/api_key" ]; then
    API_KEY="$(cat "$HOME/.config/runpod/api_key")"
fi
if [ -z "$API_KEY" ]; then
    echo "找不到 RunPod 金鑰：設 RUNPOD_API_KEY，或建立 ~/.config/runpod/api_key" >&2
    exit 1
fi

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then echo "需要 python3" >&2; exit 1; fi

"$PY" - "$API_KEY" "${MAX_MIN:-}" "$KILL_ALL" <<'PYEOF'
import datetime
import json
import re
import sys
import urllib.request

api_key, max_min_raw, kill_all_raw = sys.argv[1], sys.argv[2], sys.argv[3]
max_min = float(max_min_raw) if max_min_raw else None
kill_all = kill_all_raw == "true"
HEADERS = {"Authorization": f"Bearer {api_key}", "User-Agent": "runpod-reap/1.0"}


def parse_runpod_time(value):
    """RunPod 回的是 Go 的時間格式：'2026-08-30 03:57:04.316 +0000 UTC'
    不是 ISO 8601，直接餵給 fromisoformat 會失敗。

    解析不出來時回 None。呼叫端必須把 None 當成【危險】而不是【安全】——
    算不出開機多久，代表不知道燒了多少錢。
    """
    if not value:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*([+-]\d{4})?", str(value))
    if not m:
        return None
    offset = m.group(3) or "+0000"
    try:
        return datetime.datetime.fromisoformat(
            f"{m.group(1)}T{m.group(2)}{offset[:3]}:{offset[3:]}"
        )
    except ValueError:
        return None


def api(path, method="GET"):
    req = urllib.request.Request(
        f"https://rest.runpod.io/v1{path}", method=method, headers=HEADERS
    )
    return urllib.request.urlopen(req, timeout=20)


try:
    pods = json.loads(api("/pods").read())
except Exception as exc:
    print(f"🔴 查不到機器清單，無法確認有沒有在燒錢：{exc}")
    sys.exit(1)

if not isinstance(pods, list):
    print(f"🔴 回應格式不對：{str(pods)[:200]}")
    sys.exit(1)

if not pods:
    print("✅ 目前沒有任何機器在跑")
    sys.exit(0)

now = datetime.datetime.now(datetime.timezone.utc)
killed = failed = 0

for pod in pods:
    pod_id = pod.get("id")
    name = pod.get("name", "?")
    cost = pod.get("costPerHr") or 0
    started = parse_runpod_time(pod.get("createdAt"))

    if started is None:
        print(f"🔴 {pod_id} ({name}) 算不出開機多久 — 請到 RunPod 網站人工確認")
        should_kill = kill_all
        age_text = "未知"
    else:
        mins = (now - started).total_seconds() / 60
        age_text = f"{mins:.1f} 分，約 US${cost * mins / 60:.2f}"
        should_kill = kill_all or (max_min is not None and mins > max_min)
        mark = "🔴" if should_kill else "  "
        print(f"{mark} {pod_id} ({name}) 已跑 {age_text}，狀態={pod.get('desiredStatus')}")

    if should_kill:
        try:
            api(f"/pods/{pod_id}", method="DELETE")
            print("     └ 已砍掉")
            killed += 1
        except Exception as exc:
            print(f"     └ ❌ 砍不掉：{exc}")
            failed += 1

if max_min is None and not kill_all:
    print("\n（只列出沒有砍。要砍請加 --kill-older-than <分鐘> 或 --kill-all）")
else:
    print(f"\n砍掉 {killed} 台，失敗 {failed} 台")

sys.exit(1 if failed else 0)
PYEOF
