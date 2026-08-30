#!/usr/bin/env python3
"""把雲端 ASR 的原始輸出轉成 /srt pipeline 下游吃得下的格式。

雲端 benchmark 產的是研究用的原始 JSON，下游要的是：
  Breeze / Whisper → 真正的 .srt 檔（subtitle.sh 的格式）
  VibeVoice        → 小寫 start/end/text 的繁體 JSON（OpenCC s2twp）

用法：
  cloud_to_srt.py breeze <雲端 json> <輸出 .srt>
  cloud_to_srt.py vv     <雲端 json> <輸出 .json>
"""
import json, sys, pathlib


def ts(x: float) -> str:
    h = int(x // 3600); m = int((x % 3600) // 60); s = x % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def _segments(raw):
    """雲端有三種容器：裸 list、{"segments":…}、{"chunks":…}。"""
    if isinstance(raw, list):
        return raw
    for k in ("segments", "chunks"):
        if isinstance(raw.get(k), list):
            return raw[k]
    raise SystemExit("找不到 segments/chunks，這不是預期的雲端輸出")


def _span(seg):
    """時間欄位有三種寫法：timestamp=[a,b]、start/end、Start/End。"""
    if isinstance(seg.get("timestamp"), (list, tuple)):
        return float(seg["timestamp"][0]), float(seg["timestamp"][1])
    for lo, hi in (("start", "end"), ("Start", "End")):
        if lo in seg:
            return float(seg[lo]), float(seg[hi])
    raise SystemExit(f"segment 沒有可辨識的時間欄位: {sorted(seg)[:6]}")


def _text(seg):
    for k in ("text", "Content"):
        if k in seg:
            return (seg[k] or "").strip()
    raise SystemExit(f"segment 沒有文字欄位: {sorted(seg)[:6]}")


def to_srt(src, dst):
    segs = _segments(json.load(open(src, encoding="utf-8")))
    out, n = [], 0
    for seg in segs:
        t = _text(seg)
        if not t:
            continue
        n += 1
        a, b = _span(seg)
        out.append(f"{n}\n{ts(a)} --> {ts(b)}\n{t}\n")
    pathlib.Path(dst).write_text("\n".join(out), encoding="utf-8")
    return n


def to_vv_json(src, dst):
    # VibeVoice varies between simplified, traditional, and mixed Chinese.
    # Match the local production SRT path: unconditional s2twp is effectively
    # idempotent on traditional input and makes downstream references stable.
    from opencc import OpenCC

    converter = OpenCC("s2twp")
    segs = _segments(json.load(open(src, encoding="utf-8")))
    out = []
    for seg in segs:
        t = converter.convert(_text(seg))
        if not t:
            continue
        a, b = _span(seg)
        out.append({"start": a, "end": b, "text": t})
    pathlib.Path(dst).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(out)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("breeze", "vv"):
        sys.exit(__doc__)
    mode, src, dst = sys.argv[1:]
    n = to_srt(src, dst) if mode == "breeze" else to_vv_json(src, dst)
    print(f"{mode}: {n} 條 → {dst}")
