#!/usr/bin/env python3
"""Prepare SRT correction segments and references for Step 2b."""

import argparse
import json
import os
import re
import sys
from pathlib import Path


_TOKEN_ENCODER = None
_TOKENIZER = None


VV_SECTION = """

## 交叉參考：VibeVoice ASR

以下每段字幕會附帶另一個 ASR 引擎（VibeVoice，有 hotwords 注入）對同一段音檔的辨識結果。
VibeVoice 的英文專有名詞和部分中文財經術語辨識較準確，但語氣詞過多。

使用規則：
1. 英文 ticker / 專有名詞（如 ETF 代碼）→ 以 VibeVoice 版本為準
2. 中文財經術語 → 如果 VibeVoice 的詞彙更合理且語境正確，採用之
3. 語氣詞（哦、呃、嗯）→ 忽略 VibeVoice 多出的部分
4. 已知 VibeVoice 錯誤（不要採用）：
   - 「值信」「指信」應為「質性」
   - 「MOT」應為「MOAT」
   - 「SHD」應為「SPHD」
   - 「KVW」應為「KWEB」
   - 「BOZ」應為「BOTZ」
   - 「QILD」應為「QYLD」
   - 「XILP」應為「XLP」
   - 「SkyYY」應為「SKYY」
5. 不確定時 → 保留 Breeze（主 ASR）的版本

VibeVoice 參考文字會寫在每段的 _vv_ref_<N>.txt 檔案中。
"""


CAPTION_SECTION = """

## 畫面截圖描述（帶時間戳）

以下每段字幕會附帶影片畫面的 VLM 描述，標示了該時間點畫面顯示的內容（投影片、圖表、人物等）。

使用規則：
1. 畫面描述中的英文術語/ticker/人名 → 以畫面為準（這是 ground truth）
2. 如果 ASR 文字跟畫面描述的術語不一致 → 優先信任畫面
3. 畫面描述提供語境，幫助判斷同音字校正方向

畫面描述會寫在每段的 _caption_ref_<N>.txt 檔案中。
"""


def _get_token_encoder():
    global _TOKEN_ENCODER, _TOKENIZER
    if _TOKENIZER is not None:
        return _TOKEN_ENCODER

    try:
        import tiktoken

        _TOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
        _TOKENIZER = "tiktoken"
    except Exception:
        _TOKEN_ENCODER = False
        _TOKENIZER = "heuristic"

    print(f"srt_prepare_segments: token estimator: {_TOKENIZER}", file=sys.stderr)
    return _TOKEN_ENCODER


def tokenizer_name():
    if _TOKENIZER is None:
        estimate_tokens("")
    return _TOKENIZER


def estimate_tokens(text):
    global _TOKEN_ENCODER, _TOKENIZER
    enc = _get_token_encoder()
    if enc:
        try:
            return len(enc.encode(text))
        except Exception:
            _TOKEN_ENCODER = False
            _TOKENIZER = "heuristic"
            print("srt_prepare_segments: token estimator: heuristic", file=sys.stderr)

    # len(text) is conservative for full SRT blocks as used here because the
    # index/timestamp/newline ASCII dilutes Chinese payload text. This is
    # format-dependent, not a general Chinese-text tokenizer substitute.
    return len(text)


def _chunk_dynamic_with_tokens(blocks, block_tokens, max_tokens, max_entries):
    if max_tokens <= 0 or max_entries <= 0:
        raise ValueError("max_tokens and max_entries must be positive")

    segments = []
    segment_tokens = []
    current = []
    current_tokens = 0

    for block, block_token_count in zip(blocks, block_tokens):
        if block_token_count > max_tokens:
            print(
                "srt_prepare_segments: WARNING: single block "
                f"{block_token_count} tokens exceeds max_tokens {max_tokens}; "
                "emitting as its own oversized segment",
                file=sys.stderr,
            )

        would_exceed_tokens = current and current_tokens + block_token_count > max_tokens
        would_exceed_entries = current and len(current) + 1 > max_entries
        if would_exceed_tokens or would_exceed_entries:
            segments.append(current)
            segment_tokens.append(current_tokens)
            current = []
            current_tokens = 0

        current.append(block)
        current_tokens += block_token_count

    if current:
        segments.append(current)
        segment_tokens.append(current_tokens)

    return segments, segment_tokens


def chunk_dynamic(blocks, max_tokens, max_entries):
    block_tokens = [estimate_tokens(block + "\n\n") for block in blocks]
    segments, _ = _chunk_dynamic_with_tokens(blocks, block_tokens, max_tokens, max_entries)
    return segments


def split_blocks(content):
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content:
        return []
    return re.split(r"\n\n+", content)


def ts_to_ms(ts):
    h, m, rest = ts.strip().replace(".", ",").split(":")
    s, ms = rest.split(",")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def parse_srt_time_ms(block):
    lines = block.strip().split("\n")
    if len(lines) >= 2 and "-->" in lines[1]:
        start, end = [part.strip() for part in lines[1].split("-->", 1)]
        return ts_to_ms(start), ts_to_ms(end)
    return None, None


def segment_bounds(blocks):
    if not blocks:
        return None, None
    first_start, _ = parse_srt_time_ms(blocks[0])
    _, last_end = parse_srt_time_ms(blocks[-1])
    return first_start, last_end


def read_json_list(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def vv_time_ms(segment, *names):
    for name in names:
        if name in segment:
            return float(segment.get(name) or 0) * 1000
    return 0.0


def extract_vv_reference(vv_segments, seg_start_ms, seg_end_ms):
    parts = []
    for seg in vv_segments:
        vv_start = vv_time_ms(seg, "Start", "start", "start_time")
        vv_end = vv_time_ms(seg, "End", "end", "end_time")
        if vv_end > seg_start_ms and vv_start < seg_end_ms:
            text = seg.get("Content", seg.get("text", "")).strip()
            if text and text != "[Silence]":
                parts.append(text)
    return "\n".join(parts)


def extract_caption_reference(captions, seg_start_ms, seg_end_ms):
    parts = []
    for caption in captions:
        time_s = float(caption.get("time_s", 0))
        cap_ms = time_s * 1000
        if seg_start_ms - 30000 <= cap_ms <= seg_end_ms + 30000:
            minutes, seconds = divmod(int(time_s), 60)
            parts.append(f'[{minutes:02d}:{seconds:02d}] {caption.get("caption", "")}')
            if caption.get("terms"):
                parts.append(f'        術語: {", ".join(caption["terms"])}')
    return "\n".join(parts)


def build_system_prompt(prompt_template, terms, slide_terms, has_vv, has_captions):
    term_section = terms
    if slide_terms:
        term_section += "\n\n## 本集投影片術語\n" + slide_terms
    system_prompt = prompt_template.replace("{{TERMINOLOGY_SECTION}}", term_section)
    if has_vv:
        system_prompt += VV_SECTION
    if has_captions:
        system_prompt += CAPTION_SECTION
    return system_prompt


def write_segment_files(workdir, segments, vv_segments, captions):
    for idx, segment in enumerate(segments):
        (workdir / f"_seg_{idx}.srt").write_text(
            "\n\n".join(segment) + "\n", encoding="utf-8"
        )

        if idx > 0:
            ctx_lines = []
            for block in segments[idx - 1][-5:]:
                lines = block.strip().split("\n")
                text_lines = [line for line in lines[2:] if not re.match(r"\d+:\d+:\d+", line)]
                ctx_lines.extend(text_lines)
            (workdir / f"_ctx_{idx}.txt").write_text("\n".join(ctx_lines), encoding="utf-8")

        first_start, last_end = segment_bounds(segment)
        if vv_segments:
            if first_start is not None and last_end is not None:
                vv_ref = extract_vv_reference(vv_segments, first_start, last_end)
            else:
                vv_ref = ""
            (workdir / f"_vv_ref_{idx}.txt").write_text(
                vv_ref if vv_ref else "NO_VV_REFERENCE", encoding="utf-8"
            )

        if captions:
            if first_start is not None and last_end is not None:
                caption_ref = extract_caption_reference(captions, first_start, last_end)
            else:
                caption_ref = ""
            (workdir / f"_caption_ref_{idx}.txt").write_text(
                caption_ref if caption_ref else "NO_CAPTIONS", encoding="utf-8"
            )


def prepare(args):
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    prompt_template = Path(args.prompt_template).read_text(encoding="utf-8")
    terms = Path(args.terms).read_text(encoding="utf-8")
    slide_terms = ""
    if args.slide_terms and Path(args.slide_terms).exists():
        slide_terms = Path(args.slide_terms).read_text(encoding="utf-8")

    vv_segments = read_json_list(args.vv_json)
    captions = read_json_list(args.captions_json)

    system_prompt = build_system_prompt(
        prompt_template, terms, slide_terms, bool(vv_segments), bool(captions)
    )
    (workdir / "_system_prompt.txt").write_text(system_prompt, encoding="utf-8")

    blocks = split_blocks(Path(args.preprocessed).read_text(encoding="utf-8"))
    block_tokens = [estimate_tokens(block + "\n\n") for block in blocks]
    if args.seg_size is not None:
        if args.seg_size <= 0:
            raise ValueError("seg_size must be positive")
        segments = [blocks[i:i + args.seg_size] for i in range(0, len(blocks), args.seg_size)]
        segment_tokens = [
            sum(block_tokens[i:i + args.seg_size]) for i in range(0, len(blocks), args.seg_size)
        ]
        strategy = "fixed"
    else:
        segments, segment_tokens = _chunk_dynamic_with_tokens(
            blocks, block_tokens, args.max_tokens, args.max_entries
        )
        strategy = "dynamic"
    write_segment_files(workdir, segments, vv_segments, captions)

    return {
        "strategy": strategy,
        "tokenizer": tokenizer_name(),
        "total_blocks": len(blocks),
        "segments": [len(segment) for segment in segments],
        "segment_tokens": segment_tokens,
        "vv_segments": len(vv_segments),
        "captions": len(captions),
    }


def main():
    parser = argparse.ArgumentParser(description="Prepare segmented SRT correction inputs.")
    parser.add_argument("preprocessed")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--prompt-template", required=True)
    parser.add_argument("--terms", required=True)
    parser.add_argument("--slide-terms")
    parser.add_argument("--vv-json")
    parser.add_argument("--captions-json")
    parser.add_argument("--seg-size", type=int, default=None)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--max-entries", type=int, default=200)
    args = parser.parse_args()
    print(json.dumps(prepare(args), ensure_ascii=False))


if __name__ == "__main__":
    main()
