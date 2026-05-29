#!/usr/bin/env python3
"""SRT post-processing: punctuation restoration + terminology correction.

Reads SRT → applies sherpa-onnx punctuation per entry → applies terminology
regex → writes SRT. Designed as Step 4.5 in subtitle.sh pipeline.

Usage:
    python3 postprocess_srt.py input.srt [output.srt] [--no-punctuation] [--no-terminology] [--stats]
"""

import re
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

# sherpa-onnx punctuation model
MODEL_NAME = "sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8"
DOWNLOAD_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "punctuation-models/{}.tar.bz2"
)
DEFAULT_MODEL_DIR = Path.home() / ".cache" / "sherpa-onnx-models"

# Half-width → full-width CJK punctuation mapping
_HALF_TO_FULL = {
    ',': '，',
    '.': '。',
    '?': '？',
    '!': '！',
    ':': '：',
    ';': '；',
}

# CJK Unicode ranges for detecting CJK context
_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]')


def half_to_full_punct(text: str) -> str:
    """Convert half-width punctuation to full-width when adjacent to CJK chars."""
    result = list(text)
    for i, ch in enumerate(result):
        if ch in _HALF_TO_FULL:
            prev_cjk = i > 0 and _CJK_RE.match(result[i - 1])
            next_cjk = i + 1 < len(result) and _CJK_RE.match(result[i + 1])
            if prev_cjk or next_cjk:
                result[i] = _HALF_TO_FULL[ch]
    return ''.join(result)


def _ensure_model(model_dir: Path) -> Path:
    """Download and extract the punctuation model if not present."""
    model_file = model_dir / MODEL_NAME / "model.int8.onnx"
    if model_file.exists():
        return model_file
    model_dir.mkdir(parents=True, exist_ok=True)
    url = DOWNLOAD_URL.format(MODEL_NAME)
    archive_path = model_dir / f"{MODEL_NAME}.tar.bz2"
    print(f"下載標點模型: {url} ...", file=sys.stderr)
    urllib.request.urlretrieve(url, archive_path)
    print(f"解壓中: {model_dir} ...", file=sys.stderr)
    with tarfile.open(archive_path, "r:bz2") as tar:
        tar.extractall(path=model_dir, filter="data")
    archive_path.unlink()
    if not model_file.exists():
        raise FileNotFoundError(f"模型解壓後找不到: {model_file}")
    size_mb = model_file.stat().st_size / 1e6
    print(f"模型就緒: {model_file} ({size_mb:.0f} MB)", file=sys.stderr)
    return model_file


_punct_engine = None


def _get_punct_engine(model_dir: Path = DEFAULT_MODEL_DIR):
    """Get or create the punctuation engine (singleton)."""
    global _punct_engine
    if _punct_engine is None:
        import sherpa_onnx
        model_path = str(_ensure_model(model_dir))
        config = sherpa_onnx.OfflinePunctuationConfig(
            model=sherpa_onnx.OfflinePunctuationModelConfig(
                ct_transformer=model_path,
            ),
        )
        _punct_engine = sherpa_onnx.OfflinePunctuation(config)
    return _punct_engine


def parse_srt(path: str) -> list[dict]:
    """Parse SRT file into list of {start_ms, end_ms, text} dicts."""
    with open(path, 'r', encoding='utf-8-sig') as f:
        content = f.read()
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    entries = []
    ts_re = re.compile(r'(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})')
    for block in re.split(r'\n\s*\n', content.strip()):
        lines = block.strip().split('\n')
        if len(lines) < 2:
            continue
        if not re.match(r'^\d+\s*$', lines[0].strip()):
            continue
        m = ts_re.match(lines[1].strip())
        if not m:
            continue
        text = '\n'.join(l.strip() for l in lines[2:] if l.strip())
        entries.append({
            "start_ms": _ts_to_ms(m.group(1)),
            "end_ms": _ts_to_ms(m.group(2)),
            "text": text,
        })
    return entries


def write_srt(entries: list[dict], path: str):
    """Write entries to SRT file."""
    with open(path, 'w', encoding='utf-8') as f:
        for i, e in enumerate(entries, 1):
            f.write(f"{i}\n")
            f.write(f"{_ms_to_ts(e['start_ms'])} --> {_ms_to_ts(e['end_ms'])}\n")
            f.write(f"{e['text']}\n\n")


def _ts_to_ms(ts: str) -> int:
    ts = ts.strip().replace(',', '.')
    h, m, rest = ts.split(':')
    parts = rest.split('.')
    s = int(parts[0])
    ms = int(parts[1]) if len(parts) > 1 else 0
    return int(h) * 3600000 + int(m) * 60000 + s * 1000 + ms


def _ms_to_ts(ms: int) -> str:
    h = ms // 3600000; ms %= 3600000
    m = ms // 60000; ms %= 60000
    s = ms // 1000; frac = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{frac:03d}"


def process_srt(input_path: str, output_path: str, *,
                do_punctuation: bool = True, do_terminology: bool = True,
                show_stats: bool = False):
    """Main processing: read SRT → punctuation + terminology → write SRT."""
    entries = parse_srt(input_path)
    punct_count = 0
    term_count = 0

    punct_engine = None
    if do_punctuation:
        punct_engine = _get_punct_engine()

    if do_terminology:
        from terminology_rules import apply_terminology_regex

    for e in entries:
        original = e["text"]

        if punct_engine:
            text = punct_engine.add_punctuation(original)
            text = half_to_full_punct(text)
            if text != original:
                punct_count += 1
            e["text"] = text

        if do_terminology:
            before = e["text"]
            e["text"] = apply_terminology_regex(e["text"])
            if e["text"] != before:
                term_count += 1

    write_srt(entries, output_path)

    if show_stats:
        print(f"\n後處理: {len(entries)}條, "
              f"標點修改{punct_count}條, 術語修改{term_count}條",
              file=sys.stderr)


def main():
    args = sys.argv[1:]
    show_stats = "--stats" in args
    no_punct = "--no-punctuation" in args
    no_term = "--no-terminology" in args
    args = [a for a in args if not a.startswith("--")]

    if not args:
        print("用法: python3 postprocess_srt.py input.srt [output.srt] [--no-punctuation] [--no-terminology] [--stats]",
              file=sys.stderr)
        sys.exit(1)

    input_path = args[0]
    output_path = args[1] if len(args) > 1 else input_path

    start = time.time()
    process_srt(input_path, output_path,
                do_punctuation=not no_punct,
                do_terminology=not no_term,
                show_stats=show_stats)
    elapsed = time.time() - start
    print(f"後處理完成 ({elapsed:.1f}s) → {output_path}")


if __name__ == "__main__":
    main()
