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


# Tool-call / XML tag 洩漏清理（allowlist）：校正 subagent 偶爾把工具呼叫閉合 tag
# 寫進校正輸出的字幕尾（如 那這種...</content></invoke>），下游 force-split 又會把它
# 連同真字幕拆句，散成 </content></invoke> 整行、</ + content> 碎片、或行尾裸 <。
# 只針對「已知會洩漏的 tag 名稱」剝除（不用寬鬆 WORD>，避免誤刪 AAPL> / <BRK.B> 等
# 合法英文行）。剝除而非整行刪，才能保住 inline 接在中文後的真字幕。
_TT_NAME = r'(?:antml:)?(?:invoke|parameter|parameters|function_calls|content|tool_use|tool_result)'
_TOOL_TAG_RESIDUE = re.compile(
    r'</?' + _TT_NAME + r'(?:\s[^<>]*?)?/?>'      # 完整/半：<invoke name="x"> </content> <content>
    r'|<' + r'/?' + _TT_NAME + r'(?:\s[^<>]*)?$'  # 行尾殘缺開頭（split 切在 > 前）：…<invoke name="x"  …</content
    r'|^/?' + _TT_NAME + r'>'                      # 行首碎片（左 < 被 split 掉）：content>  /invoke>
    r'|^[\w:]+="[^"]*"\s*/?>'                      # 行首屬性續行碎片：name="x">
    r'|<[/]?\s*$'                                  # 行尾裸 < 或 </（右半被 split 掉）
)


def strip_tool_tag_residue(line: str) -> str:
    """反覆剝除一行內的 tool-call/XML tag 殘留（含相鄰多 tag），回傳剝乾淨的字幕文字。"""
    prev = None
    s = line
    while prev != s:
        prev = s
        s = _TOOL_TAG_RESIDUE.sub('', s).strip()
    return s


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
        clean_lines = []
        for l in b['text_lines']:
            if is_commentary_line(l):
                continue
            sanitized = strip_tool_tag_residue(l)  # 剝行內/行尾 tool-tag 殘留
            if not sanitized.strip():
                continue  # 整行都是 tag 殘留 → 丟棄
            clean_lines.append(sanitized)
        if not clean_lines:
            type_b += 1
            continue
        if clean_lines != b['text_lines']:
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
