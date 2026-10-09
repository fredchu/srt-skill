#!/usr/bin/env python3
"""Split a long subagent prompt into files the Read tool returns whole, with read receipts.

Read 工具單次約回 25K token，超過就靜默截斷（2026-10-09 技術分析-9月-01：4 段校正全被截在
第 1190/1588 行，丟了禁止事項、輸出格式、VV 與畫面規則）。所以：
- 規則留在主檔，術語切成每份 <= max_tokens 的 _terms_K 檔
- 每個檔最後一行放隨機讀取驗證碼，subagent 讀完要把全部驗證碼抄進回條檔
- 合併前比對回條，缺碼＝沒讀完，當作失敗重派
"""

import argparse
import json
import secrets
import sys
from pathlib import Path

CODE_PREFIX = "【讀取驗證碼】"
DEFAULT_MAX_TOKENS = 12000


def split_by_tokens(text, max_tokens, estimate):
    chunks, current, current_tokens = [], [], 0
    for line in text.splitlines(keepends=True):
        tokens = estimate(line)
        if current and current_tokens + tokens > max_tokens:
            chunks.append("".join(current))
            current, current_tokens = [], 0
        current.append(line)
        current_tokens += tokens
    if current:
        chunks.append("".join(current))
    return chunks


def write_prompt_files(workdir, main_name, main_text, terms_text, terms_prefix,
                       receipt_name, codes_name, max_tokens, estimate):
    """Write main prompt + term chunks, each ending with a read code.

    main_text must contain {{TERMINOLOGY_SECTION}}; it is replaced by a pointer to the term files.
    receipt_name is shown to the subagent verbatim, e.g. "_seg_<N>_receipt.txt".
    """
    workdir = Path(workdir)
    chunks = split_by_tokens(terms_text, max_tokens, estimate) if terms_text.strip() else []
    term_names = [f"{terms_prefix}_{k}.txt" for k in range(1, len(chunks) + 1)]
    names = [main_name, *term_names]

    pointer = (
        "術語表放在另外的檔案：" + "、".join(term_names) + "。開始校正前必須全部讀完（見本檔開頭的讀取須知）。\n"
        if term_names else "（本次沒有術語表）\n"
    )
    header = (
        "# 讀取須知（先讀這段）\n"
        f"本提示分成 {len(names)} 個檔，全部讀完才能開始：\n"
        + "".join(f"{i}. {name}\n" for i, name in enumerate(names, 1))
        + f"每個檔的最後一行是「{CODE_PREFIX}…」。全部讀完後，把每個檔的驗證碼依序一行一個，"
        f"用 Write 寫進 {receipt_name}。\n"
        "讀檔時若顯示內容被截斷（truncated），用 offset/limit 分段讀，直到看見該檔的驗證碼為止。\n\n"
    )
    bodies = [header + main_text.replace("{{TERMINOLOGY_SECTION}}", pointer), *chunks]

    codes, token_counts = [], []
    for name, body in zip(names, bodies):
        code = secrets.token_hex(4)
        text = body.rstrip("\n") + f"\n\n{CODE_PREFIX}{code}\n"
        (workdir / name).write_text(text, encoding="utf-8")
        codes.append(code)
        token_counts.append(estimate(text))
        if token_counts[-1] > max_tokens * 1.5:
            print(f"srt_prompt_files: WARNING: {name} 約 {token_counts[-1]} token，"
                  f"超過上限 {max_tokens} 的 1.5 倍，Read 可能截斷", file=sys.stderr)

    (workdir / codes_name).write_text(
        json.dumps({"files": names, "codes": codes}, ensure_ascii=False), encoding="utf-8")
    return names, token_counts


def check_receipts(workdir, codes_name, receipt_pattern, segments):
    """Return {segment: missing_count} for segments whose receipt lacks any code."""
    workdir = Path(workdir)
    codes = json.loads((workdir / codes_name).read_text(encoding="utf-8"))["codes"]
    missing = {}
    for n in segments:
        path = workdir / receipt_pattern.format(n=n)
        got = path.read_text(encoding="utf-8") if path.exists() else ""
        lack = [code for code in codes if code not in got]
        if lack:
            missing[n] = len(lack)
    return missing


def parse_segments(spec):
    start, _, end = spec.partition("-")
    return list(range(int(start), int(end or start) + 1))


def main():
    parser = argparse.ArgumentParser(description="Check subagent read receipts.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check")
    check.add_argument("--workdir", required=True)
    check.add_argument("--codes", required=True, help="e.g. _review_read_codes.json")
    check.add_argument("--pattern", required=True, help="e.g. _review_seg_{n}_receipt.txt")
    check.add_argument("--segments", required=True, help="e.g. 0-3")
    args = parser.parse_args()
    missing = check_receipts(args.workdir, args.codes, args.pattern, parse_segments(args.segments))
    print(json.dumps({"ok": not missing, "missing": missing}, ensure_ascii=False))
    sys.exit(3 if missing else 0)


if __name__ == "__main__":
    main()
