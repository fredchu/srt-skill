"""Terminology regex rules for ASR output correction.

Ported from VerbatimFlow. Only contains universal ASR misrecognition rules.
Project-specific rules (financial homophones) stay in srt_preprocess.py
for now (漸進合併 strategy).
"""

import re

TERMINOLOGY_RULES = [
    # (pattern, replacement, flags)
    # English patterns: \b + IGNORECASE + ASCII for correct CJK boundary behavior.
    # Chinese patterns: no \b (ineffective on CJK).

    # --- 英文術語：空格合併 ---
    (r'\bGit\s+Hub\b', 'GitHub', re.IGNORECASE | re.ASCII),
    (r'\bOpen\s+AI\b', 'OpenAI', re.IGNORECASE | re.ASCII),
    (r'\bChat\s+GPT\b', 'ChatGPT', re.IGNORECASE | re.ASCII),
    (r'\bOpen\s+CC\b', 'OpenCC', re.IGNORECASE | re.ASCII),

    # --- 中文音譯 ---
    (r'偷坑', 'token', 0),
    (r'集聚', '級距', 0),
]

# Pre-sorted by pattern length (longest first) for specificity
_SORTED_RULES = sorted(TERMINOLOGY_RULES, key=lambda r: len(r[0]), reverse=True)


def apply_terminology_regex(text: str) -> str:
    """Apply terminology corrections using regex patterns."""
    for pattern, replacement, flags in _SORTED_RULES:
        text = re.sub(pattern, replacement, text, flags=flags)
    return text
