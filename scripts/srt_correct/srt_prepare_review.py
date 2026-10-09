#!/usr/bin/env python3
"""Prepare Step 2c review inputs: unchanged entries, review prompt, term files with read receipts.

原本是 SKILL.md 裡的 inline python；2026-10-09 搬成腳本，術語改走 srt_prompt_files
（複查 prompt＝講者術語表＋投影片術語，一樣會撐爆 Read 單次上限）。
"""

import argparse
import json
import re
from pathlib import Path

from srt_prepare_segments import estimate_tokens
from srt_prompt_files import DEFAULT_MAX_TOKENS, write_prompt_files

REVIEW_TEMPLATE = """你是字幕複查員。以下字幕已經過一輪 ASR 校正但未被修改。
請逐條檢查是否有殘留的 ASR 錯誤。

## 術語表
{{TERMINOLOGY_SECTION}}
## 第一輪已發現的錯誤範例（供校準判斷標準）
{{EXAMPLES}}

## 重點檢查項目
- 同音字/近音字錯誤（如「機點」應為「基點」、「教育日」應為「交易日」）
- 術語表中的詞被 ASR 聽成別的詞
- 英文辨識錯誤（大小寫、拼寫）
- 重複字詞未清理

## 不要改的
- 專有名詞、人名、地名、作品名、時事用語 — 即使你不認識也不要改，講者可能在引用你不知道的時事、作品、流行語
- 語意通順、在上下文中說得通的條目

## 輸出格式（嚴格）
只輸出需要修改的條目，格式：
原始時間軸
校正後文字

**絕對禁止**：
- 輸出判斷說明，例如 `[通順，不改]`、`[備註：...]`、`[確認：...]`、`→`、「原文通順」、「不改」、「請確認語境」、「應是...」、「若上條...」、「但後文...」
- 輸出多行判斷邏輯（一條目對應一行純字幕，禁止把推理過程當字幕第二行）
- 輸出未閉合的引號、括號或方括號（`「`、`[` 必須在同一行完成配對）
- 輸出「無修改」「OK」這類佔位文字

如果該條目沒問題，**完全不輸出**（連時間軸都不要列）。
「校正後文字」必須是純字幕內容，不含任何符號標記、推理文字或內部判斷。
"""


def parse_srt(path):
    blocks = re.split(r"\n\n+", Path(path).read_text(encoding="utf-8").strip())
    result = {}
    for b in blocks:
        lines = b.strip().split("\n")
        if len(lines) >= 2 and "-->" in lines[1]:
            result[lines[1].strip()] = "\n".join(lines[2:])
    return result, blocks


def prepare(args):
    workdir = Path(args.workdir)
    pre_dict, _ = parse_srt(args.preprocessed)
    cor_dict, cor_blocks = parse_srt(args.corrected)

    unchanged = []
    for b in cor_blocks:
        lines = b.strip().split("\n")
        if len(lines) >= 2 and "-->" in lines[1]:
            tc = lines[1].strip()
            if tc in pre_dict and pre_dict[tc] == "\n".join(lines[2:]):
                unchanged.append(b)

    examples = []
    for tc, pre_text in pre_dict.items():
        if tc in cor_dict and pre_text != cor_dict[tc]:
            examples.append(f"{pre_text} → {cor_dict[tc]}")
            if len(examples) >= 10:
                break

    terms = Path(args.terms).read_text(encoding="utf-8")
    if args.slide_terms and Path(args.slide_terms).exists():
        terms += "\n\n## 本集投影片術語\n" + Path(args.slide_terms).read_text(encoding="utf-8")

    rules = REVIEW_TEMPLATE.replace("{{EXAMPLES}}", "\n".join(examples))
    prompt_files, prompt_tokens = write_prompt_files(
        workdir, "_review_prompt.txt", rules, terms, "_review_terms",
        "_review_seg_<N>_receipt.txt（<N> 是你負責的段號）", "_review_read_codes.json",
        args.prompt_max_tokens, estimate_tokens,
    )

    n_segs = 0
    for i in range(0, len(unchanged), args.seg_size):
        (workdir / f"_review_seg_{n_segs}.srt").write_text(
            "\n\n".join(unchanged[i:i + args.seg_size]) + "\n", encoding="utf-8")
        n_segs += 1

    return {
        "unchanged": len(unchanged),
        "review_segments": n_segs,
        "examples": len(examples),
        "prompt_files": prompt_files,
        "prompt_tokens": prompt_tokens,
    }


def main():
    parser = argparse.ArgumentParser(description="Prepare Step 2c review inputs.")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--preprocessed", required=True)
    parser.add_argument("--corrected", required=True, help="_2b_corrected.srt")
    parser.add_argument("--terms", required=True)
    parser.add_argument("--slide-terms")
    parser.add_argument("--seg-size", type=int, default=300)
    parser.add_argument("--prompt-max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    print(json.dumps(prepare(parser.parse_args()), ensure_ascii=False))


if __name__ == "__main__":
    main()
