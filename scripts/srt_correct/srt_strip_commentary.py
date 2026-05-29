#!/usr/bin/env python3
"""清理 SRT 中 LLM 複查 subagent 殘留的 commentary。

兩類污染：
- Type A: block 第一行是真字幕，後面是 commentary 行 → 保留第一行，刪 commentary 行
- Type B: block 整體都是 commentary chunk（真字幕被覆蓋）→ 整 block 刪除

判斷 commentary 行：
- 以 `→`、`[` 開頭
- 等於 `不改]`、`非 ASR 錯誤`
- 結尾 `]` 且含判斷詞（不改 / 通順 / 確認 / OK / 可讀性）
- 以「應是「、若上條、但後文、但可加逗號、請確認、此條通順、原文通順、確認為股票代號」開頭
"""
import re
import sys
from pathlib import Path


def is_commentary_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if s.startswith('→') or s.startswith('['):
        return True
    if s in ('不改]', '非 ASR 錯誤'):
        return True
    if s.endswith(']') and any(k in s for k in ['不改', '通順', '確認', 'OK', '可讀性']):
        return True
    starts = ['應是「', '若上條', '但後文', '但可加逗號', '請確認',
             '此條通順', '原文通順', '確認為股票代號']
    if any(s.startswith(k) for k in starts):
        return True
    return False


def parse_blocks(raw: str):
    out = []
    for b in re.split(r'\n\n+', raw):
        lines = b.strip().split('\n')
        if len(lines) >= 2 and '-->' in lines[1]:
            out.append({'header': lines[:2], 'text_lines': lines[2:], 'raw': b})
        else:
            out.append({'header': None, 'text_lines': [], 'raw': b})
    return out


def clean(path: Path) -> tuple[int, int, int]:
    raw = path.read_text(encoding='utf-8').strip()
    parsed = parse_blocks(raw)

    # Pass 1: 標記每個 block 是否被 commentary 污染（>=1 行 commentary）
    polluted = [
        b['header'] is not None and any(is_commentary_line(l) for l in b['text_lines'])
        for b in parsed
    ]

    # Pass 2: 標記「夾在污染區中間 + 不平衡引號」的 block 為 Type C
    type_c_idx = set()
    n = len(parsed)
    for i in range(n):
        b = parsed[i]
        if b['header'] is None or polluted[i]:
            continue
        prev_p = i > 0 and polluted[i - 1]
        next_p = i < n - 1 and polluted[i + 1]
        if not (prev_p and next_p):
            continue
        text = '\n'.join(b['text_lines'])
        if text.count('「') != text.count('」'):
            type_c_idx.add(i)

    # Pass 3: 套用清理
    out = []
    type_a = type_b = type_c = 0
    for i, b in enumerate(parsed):
        if b['header'] is None:
            out.append(b['raw'])
            continue
        if i in type_c_idx:
            type_c += 1
            continue  # 整 block 刪除
        clean_lines = [l for l in b['text_lines'] if not is_commentary_line(l)]
        if not clean_lines:
            type_b += 1
            continue
        if len(clean_lines) < len(b['text_lines']):
            type_a += 1
        out.append('\n'.join(b['header'] + clean_lines))

    # 重編號
    fixed = []
    idx = 1
    for b in out:
        lines = b.split('\n')
        if len(lines) >= 2 and '-->' in lines[1]:
            lines[0] = str(idx)
            idx += 1
        fixed.append('\n'.join(lines))

    path.write_text('\n\n'.join(fixed) + '\n', encoding='utf-8')
    return type_a, type_b, type_c


def main():
    for arg in sys.argv[1:]:
        p = Path(arg)
        a, b, c = clean(p)
        print(f'{p.name}: Type A cleaned={a}, Type B deleted={b}, Type C deleted={c}')


if __name__ == '__main__':
    main()
