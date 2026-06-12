#!/usr/bin/env python3
"""
Learn recurring terminology corrections from preprocessed/final SRT pairs.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path


PUNCT_CHARS = set("，。、？！「」：；,.?! \t\r\n")
PRONOUNS = {"他", "她", "它"}


@dataclass(frozen=True)
class SrtEntry:
    start_ms: int
    end_ms: int
    text: str


def ts_to_ms(ts: str) -> int:
    ts = ts.strip().replace(",", ".")
    h, m, rest = ts.split(":")
    s, ms = rest.split(".")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def ms_to_ts(ms: int) -> str:
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    frac = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{frac:03d}"


def parse_srt(path: Path) -> list[SrtEntry]:
    content = path.read_text(encoding="utf-8-sig")
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    entries: list[SrtEntry] = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3 or not re.fullmatch(r"\d+", lines[0].strip()):
            continue
        match = re.match(
            r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})",
            lines[1].strip(),
        )
        if not match:
            continue
        text = "\n".join(line.strip() for line in lines[2:] if line.strip())
        entries.append(SrtEntry(ts_to_ms(match.group(1)), ts_to_ms(match.group(2)), text))
    return entries


def is_noise(wrong: str, correct: str) -> bool:
    wrong = wrong.strip()
    correct = correct.strip()
    if not wrong or not correct:
        return True
    if len(wrong) > 10 or len(correct) > 10:
        return True
    if wrong.lower() == correct.lower():
        return True
    if all(ch in PUNCT_CHARS for ch in wrong) or all(ch in PUNCT_CHARS for ch in correct):
        return True
    if wrong in PRONOUNS and correct in PRONOUNS and wrong != correct:
        return True
    return False


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _expand_cjk_term(before: str, after: str, i1: int, i2: int, j1: int, j2: int) -> tuple[str, str]:
    wrong = before[i1:i2].strip()
    correct = after[j1:j2].strip()
    if wrong in PRONOUNS and correct in PRONOUNS:
        return wrong, correct

    left_extra = ""
    if (
        len(wrong) == 1
        and len(correct) == 1
        and i1 > 0
        and j1 > 0
        and before[i1 - 1] == after[j1 - 1]
        and _is_cjk(before[i1 - 1])
    ):
        left_extra = before[i1 - 1]

    right_extra = ""
    if (
        i2 < len(before)
        and j2 < len(after)
        and before[i2] == after[j2]
        and _is_cjk(before[i2])
        and not left_extra
        and (len(wrong) >= 2 or len(correct) >= 2)
    ):
        right_extra = before[i2]

    return f"{left_extra}{wrong}{right_extra}", f"{left_extra}{correct}{right_extra}"


def replacements_between(before: str, after: str) -> list[tuple[str, str]]:
    matcher = SequenceMatcher(None, before, after)
    replacements: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        raw_wrong = before[i1:i2].strip()
        raw_correct = after[j1:j2].strip()
        if is_noise(raw_wrong, raw_correct):
            continue
        wrong, correct = _expand_cjk_term(before, after, i1, i2, j1, j2)
        if not is_noise(wrong, correct):
            replacements.append((wrong, correct))
    return replacements


def learn_candidates(pairs: list[tuple[Path, Path]], min_count: int) -> dict[tuple[str, str], dict[str, object]]:
    counts: Counter[tuple[str, str]] = Counter()
    examples: defaultdict[tuple[str, str], list[str]] = defaultdict(list)

    for before_path, after_path in pairs:
        before_by_start = {entry.start_ms: entry for entry in parse_srt(before_path)}
        after_by_start = {entry.start_ms: entry for entry in parse_srt(after_path)}
        for start_ms in sorted(before_by_start.keys() & after_by_start.keys()):
            before = before_by_start[start_ms]
            after = after_by_start[start_ms]
            for wrong, correct in replacements_between(before.text, after.text):
                key = (wrong, correct)
                counts[key] += 1
                if len(examples[key]) < 3:
                    examples[key].append(f"{ms_to_ts(start_ms)}: {before.text} => {after.text}")

    return {
        key: {"count": count, "examples": examples[key]}
        for key, count in counts.items()
        if count >= min_count
    }


def _term_word_from_line(line: str) -> str:
    return re.split(r"[（(→]", line, maxsplit=1)[0].strip()


def _clean_decl_part(text: str) -> str:
    return re.split(r"[（(]", text, maxsplit=1)[0].strip()


def parse_terms(path: Path) -> tuple[set[str], set[tuple[str, str]]]:
    words: set[str] = set()
    arrows: set[tuple[str, str]] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        word = _term_word_from_line(line)
        if word:
            words.add(word)
        if "→" in line:
            wrong, correct = line.split("→", 1)
            wrong = _clean_decl_part(wrong)
            correct = _clean_decl_part(correct)
            if wrong and correct:
                arrows.add((wrong, correct))
    return words, arrows


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def parse_preprocess_rules(path: Path) -> set[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    wanted = {"AUTO_REPLACE_COMMON", "AUTO_REPLACE_BREEZE", "PREFIX_REPLACE"}
    rules: set[tuple[str, str]] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        if not names & wanted:
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            continue
        for item in node.value.elts:
            if not isinstance(item, (ast.Tuple, ast.List)) or len(item.elts) < 2:
                continue
            wrong = _literal_string(item.elts[0])
            correct = _literal_string(item.elts[1])
            if wrong is not None and correct is not None:
                rules.add((wrong, correct))
    return rules


def suggest_destination(wrong: str) -> str:
    compact = wrong.strip()
    is_ascii_token = bool(re.fullmatch(r"[A-Za-z0-9&.+#_-]+", compact))
    if len(compact) >= 2 and not is_ascii_token:
        return "preprocess"
    return "terms"


def enrich_candidates(
    candidates: dict[tuple[str, str], dict[str, object]],
    term_words: set[str],
    term_arrows: set[tuple[str, str]],
    preprocess_rules: set[tuple[str, str]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for (wrong, correct), data in candidates.items():
        rows.append(
            {
                "wrong": wrong,
                "correct": correct,
                "count": data["count"],
                "suggestion": suggest_destination(wrong),
                "examples": data["examples"],
                "already_in_terms": correct in term_words or (wrong, correct) in term_arrows,
                "already_in_preprocess": (wrong, correct) in preprocess_rules,
            }
        )
    rows.sort(key=lambda row: (-int(row["count"]), str(row["wrong"]), str(row["correct"])))
    return rows


def render_markdown(rows: list[dict[str, object]]) -> str:
    lines = [
        "| count | correction | suggestion | example | already |",
        "| ---: | --- | --- | --- | --- |",
    ]
    for row in rows:
        flags = []
        if row["already_in_terms"]:
            flags.append("terms")
        if row["already_in_preprocess"]:
            flags.append("preprocess")
        example = str(row["examples"][0] if row["examples"] else "").replace("|", "\\|")
        correction = f"{row['wrong']}→{row['correct']}".replace("|", "\\|")
        lines.append(
            f"| {row['count']} | {correction} | {row['suggestion']} | {example} | {', '.join(flags) or '-'} |"
        )
    return "\n".join(lines)


def parse_pair(value: str) -> tuple[Path, Path]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("--pairs must be formatted as <2a.srt>:<2c_final.srt>")
    left, right = value.split(":", 1)
    return Path(left), Path(right)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Learn recurring SRT terminology correction candidates.")
    parser.add_argument("--pairs", action="append", type=parse_pair, required=True, help="<2a.srt>:<2c_final.srt>")
    parser.add_argument("--terms", required=True, type=Path, help="terms.txt path")
    parser.add_argument("--preprocess", required=True, type=Path, help="srt_preprocess.py path")
    parser.add_argument("--min-count", type=int, default=2, help="minimum recurring correction count")
    parser.add_argument("--json", type=Path, help="optional machine-readable JSON output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    term_words, term_arrows = parse_terms(args.terms)
    preprocess_rules = parse_preprocess_rules(args.preprocess)
    candidates = learn_candidates(args.pairs, args.min_count)
    rows = enrich_candidates(candidates, term_words, term_arrows, preprocess_rules)

    print(render_markdown(rows))
    if args.json:
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
