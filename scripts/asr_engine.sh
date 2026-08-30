#!/bin/bash
# ASR 引擎解析（被 source，不單獨執行）
#
# 優先序：CLI 旗標 > 該步驟的環境變數 > 全域環境變數 > mlx
#
# 為什麼要分步驟：同一台機器對不同模型的答案可能不同。
# 8GB 的 M2 MBA 上，Breeze（權重 2.9 GB）跑得動，
# 但 VibeVoice（4-bit 權重 5.3 GiB）貼著 Metal 工作集上限（8GB 機器約 5.3-5.5 GB），
# 超過是直接報 buffer 錯不是變慢。所以那台的正確配置是混合的。
#
# 用法：
#   source asr_engine.sh
#   ASR_ENGINE="$(srt_resolve_engine BREEZE "$cli_value")"

srt_validate_engine() {
    case "$1" in
        mlx|runpod) return 0 ;;
        *) return 1 ;;
    esac
}

# srt_resolve_engine <STEP> [cli_value]
#   STEP: BREEZE | VV | FALLBACK
srt_resolve_engine() {
    local step="$1" cli="${2:-}" per_step global resolved var

    var="SRT_${step}_ENGINE"
    per_step="${!var:-}"
    global="${SRT_ASR_ENGINE:-}"

    if [ -n "$cli" ]; then
        resolved="$cli"
    elif [ -n "$per_step" ]; then
        resolved="$per_step"
    elif [ -n "$global" ]; then
        resolved="$global"
    else
        resolved="mlx"
    fi

    if ! srt_validate_engine "$resolved"; then
        echo "ERROR: 未知引擎：${resolved}（可用 mlx 或 runpod）" >&2
        echo "       來源優先序：CLI 旗標 > ${var} > SRT_ASR_ENGINE > 預設 mlx" >&2
        return 1
    fi
    printf '%s' "$resolved"
}
