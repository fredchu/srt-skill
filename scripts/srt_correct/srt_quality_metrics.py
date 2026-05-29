#!/usr/bin/env python3
"""
srt_quality_metrics.py — SRT 字幕校正品質量化指標 POC

用法:
    python3 srt_quality_metrics.py <machine_corrected.srt> <human_corrected.srt> [--json]

功能:
    1. 計算 CER（Character Error Rate）
    2. 分類錯誤統計（同音字、贅詞、專有名詞、格式）
    3. 嚴重度加權分數
    4. 品質報告（含紅燈/黃燈判斷）

指標選擇理由:
    - CER（字元錯誤率）: 最適合中文——中文無分詞問題，字元級比較最直觀
    - WER 不適用: 中文分詞歧義大，不同分詞器結果不同，無法穩定比較
    - BLEU/ROUGE 不適用: 這些是翻譯/摘要指標，字幕校正是「修正」不是「生成」
"""

import re
import sys
import os
import json
import difflib
from collections import Counter, defaultdict
from dataclasses import dataclass, field


# ============================================================
# SRT 解析（複用 srt_diff_report 的邏輯）
# ============================================================

@dataclass
class Sub:
    idx: int
    start_ms: int
    end_ms: int
    text: str

    @property
    def char_len(self):
        return len(self.text)


def ts_to_ms(ts):
    ts = ts.strip().replace(',', '.')
    h, m, rest = ts.split(':')
    parts = rest.split('.')
    s = int(parts[0])
    ms = int(parts[1]) if len(parts) > 1 else 0
    return int(h) * 3600000 + int(m) * 60000 + s * 1000 + ms


def ms_to_ts(ms):
    h = ms // 3600000; ms %= 3600000
    m = ms // 60000; ms %= 60000
    s = ms // 1000
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_srt(path):
    with open(path, 'r', encoding='utf-8-sig') as f:
        content = f.read()
    subs = []
    for block in re.split(r'\r?\n\s*\r?\n', content.strip()):
        lines = [l.strip('\r') for l in block.strip().split('\n')]
        if len(lines) < 2: continue
        idx_m = re.match(r'^(\d+)\s*$', lines[0].strip())
        if not idx_m: continue
        ts_m = re.match(r'(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})', lines[1].strip())
        if not ts_m: continue
        text = ' '.join(l.strip() for l in lines[2:] if l.strip())
        subs.append(Sub(int(idx_m.group(1)), ts_to_ms(ts_m.group(1)), ts_to_ms(ts_m.group(2)), text))
    return subs


# ============================================================
# 時間軸對齊
# ============================================================

def align_by_time(machine, human, threshold_ms=150):
    """回傳 [(machine_indices, human_indices), ...] 的對齊群組"""
    s_to_a, a_to_s = defaultdict(set), defaultdict(set)
    ai_start = 0
    for si, ss in enumerate(machine):
        for aj in range(ai_start, len(human)):
            aa = human[aj]
            if aa.end_ms < ss.start_ms - threshold_ms:
                ai_start = aj + 1; continue
            if aa.start_ms > ss.end_ms + threshold_ms: break
            ov = max(0, min(ss.end_ms, aa.end_ms) - max(ss.start_ms, aa.start_ms))
            if ov > threshold_ms:
                s_to_a[si].add(aj); a_to_s[aj].add(si)

    visited_s, visited_a = set(), set()
    groups = []

    def bfs(seed_s=None, seed_a=None):
        s_set, a_set, q_s, q_a = set(), set(), [], []
        if seed_s is not None: q_s.append(seed_s)
        if seed_a is not None: q_a.append(seed_a)
        while q_s or q_a:
            while q_s:
                si = q_s.pop()
                if si in s_set: continue
                s_set.add(si)
                for aj in s_to_a.get(si, set()):
                    if aj not in a_set: q_a.append(aj)
            while q_a:
                aj = q_a.pop()
                if aj in a_set: continue
                a_set.add(aj)
                for si in a_to_s.get(aj, set()):
                    if si not in s_set: q_s.append(si)
        return s_set, a_set

    for si in range(len(machine)):
        if si in visited_s: continue
        if si not in s_to_a:
            groups.append(({si}, set())); visited_s.add(si); continue
        s_set, a_set = bfs(seed_s=si)
        visited_s.update(s_set); visited_a.update(a_set)
        groups.append((s_set, a_set))
    for aj in range(len(human)):
        if aj not in visited_a:
            groups.append((set(), {aj}))

    groups.sort(key=lambda g: min(
        [machine[i].start_ms for i in g[0]] + [human[i].start_ms for i in g[1]] + [10**9]))
    return groups


# ============================================================
# 錯誤分類器
# ============================================================

# 已知同音字對（從 srt_diff_report 和 srt_preprocess 合併）
KNOWN_HOMOPHONES = {
    "長": "漲", "排": "盤", "撤": "測", "軍": "均", "金": "均",
    "刷": "跌", "解": "減", "骨": "股", "付": "復", "花": "畫",
    "投": "頭", "病": "兵", "山": "三", "在": "再", "敘": "訊",
    "需": "序", "扒": "巴", "級": "梯", "承": "成",
}

KNOWN_COMPOUND_HOMOPHONES = {
    "排斥": "盤勢", "無黨": "五檔", "持值": "遲滯",
    "城市": "程式", "食物": "實務", "張張滴滴": "漲漲跌跌",
    "事實管理": "市值管理", "事實級距": "市值級距",
    "集聚": "級距", "機具": "級距", "幾句": "級距",
}

KNOWN_FILLERS = {"這個", "好", "啦", "一個", "什麼", "的"}

# 專有名詞（金融術語）
FINANCIAL_TERMS = {
    "本益比", "本淨比", "市值", "級距", "五檔", "盤勢",
    "成交量", "成交額", "均線", "支撐", "壓力", "回撤",
    "減碼", "加碼", "漲停", "跌停", "護城河", "逢九",
    "估值", "晶圓", "投機",
}

# 多字同音/近音對（不在 KNOWN 字典中但屬同音字性質）
MULTI_CHAR_HOMOPHONES = {
    "固執": "估值", "電源": "晶圓", "頭期": "投機",
    "戰鬥": "站上", "限時爆": "現世報", "大事": "大肆",
}

# 語境判斷型（兩個詞都合理，需語境決定）
CONTEXT_PAIRS = {
    ("回撤", "回測"), ("回測", "回撤"),
}

ALL_KNOWN_PAIRS = {}
ALL_KNOWN_PAIRS.update(KNOWN_HOMOPHONES)
ALL_KNOWN_PAIRS.update(KNOWN_COMPOUND_HOMOPHONES)


@dataclass
class ErrorInstance:
    """一個具體的錯誤實例"""
    error_type: str       # HOMOPHONE_KNOWN, HOMOPHONE_NEW, FILLER, TERM_ERROR, MISHEAR, STRUCTURAL
    severity: int         # 1=輕微, 2=中等, 3=嚴重
    old_text: str
    new_text: str
    timestamp: str
    machine_idx: str
    context: str


def classify_error(op, old_text, new_text, full_context=""):
    """分類一個 diff 操作的錯誤類型和嚴重度

    分類優先順序：
    1. 已知同音字（字典比對）
    2. 多字同音/近音（擴展字典）
    3. 語境判斷型（兩詞都對，語境決定）
    4. 單字替換 → 新同音字候選 or 語意錯誤
    5. 贅詞刪除
    6. 插入
    7. 多字替換 → 專有名詞 or 聽錯
    """
    old = old_text.strip()
    new = new_text.strip()

    if not old and not new:
        return None  # whitespace change

    # 1. 已知同音字（含複合詞）
    if old in ALL_KNOWN_PAIRS and ALL_KNOWN_PAIRS[old] == new:
        return ("HOMOPHONE_KNOWN", 2)
    for k, v in ALL_KNOWN_PAIRS.items():
        if k in old and v in new and len(old) <= len(k) + 2:
            return ("HOMOPHONE_KNOWN", 2)

    # 2. 多字同音/近音對（v2 新增：修復 quality-researcher 指出的邊界案例）
    if op == 'replace' and old in MULTI_CHAR_HOMOPHONES:
        if MULTI_CHAR_HOMOPHONES[old] == new:
            return ("HOMOPHONE_NEW", 2)

    # 3. 語境判斷型（v2 新增：兩個詞都合理的情況）
    if op == 'replace' and (old, new) in CONTEXT_PAIRS:
        return ("CONTEXT_JUDGMENT", 2)

    # 4. 單字替換
    if op == 'replace' and len(old) == 1 and len(new) == 1:
        # 涉及專有名詞的嚴重度更高
        if any(term in full_context for term in ["市值", "級距", "本益比", "本淨比"]):
            return ("TERM_ERROR", 3)
        return ("HOMOPHONE_NEW", 2)

    # 5. 贅詞刪除
    if op == 'delete':
        if not old:
            return None
        if old in KNOWN_FILLERS:
            return ("FILLER_KNOWN", 1)
        if len(old) <= 3:
            return ("FILLER_CANDIDATE", 1)
        return ("DELETE_OTHER", 2)

    # 6. 插入（人工補字）
    if op == 'insert':
        if not new:
            return None
        return ("INSERT", 2)

    # 7. 多字替換
    if op == 'replace':
        # 專有名詞錯誤（嚴重）
        if new in FINANCIAL_TERMS or any(t in new for t in FINANCIAL_TERMS):
            return ("TERM_ERROR", 3)
        # 語意改變的替換（嚴重）
        if len(old) >= 2 and len(new) >= 2:
            return ("MISHEAR", 3)
        return ("REPLACE_OTHER", 2)

    return ("UNKNOWN", 1)


# ============================================================
# CER 計算
# ============================================================

def compute_cer(machine_text, human_text):
    """計算 Character Error Rate = edit_distance / len(reference)"""
    if not human_text:
        return 0.0 if not machine_text else 1.0

    # 用 difflib 計算編輯操作數
    ops = difflib.SequenceMatcher(None, machine_text, human_text).get_opcodes()
    edits = 0
    for op, s0, s1, a0, a1 in ops:
        if op == 'equal':
            continue
        edits += max(s1 - s0, a1 - a0)

    return edits / len(human_text)


# ============================================================
# 主分析
# ============================================================

@dataclass
class QualityReport:
    """品質報告的資料結構"""
    machine_path: str = ""
    human_path: str = ""
    machine_count: int = 0
    human_count: int = 0

    # 整體指標
    cer: float = 0.0
    total_changes: int = 0
    same_ratio: float = 0.0

    # 分類統計
    error_counts: dict = field(default_factory=dict)
    severity_counts: dict = field(default_factory=dict)

    # 加權品質分數（0-100，越高越好）
    quality_score: float = 0.0

    # 嚴重錯誤時間密度（每分鐘幾個）
    severe_per_minute: float = 0.0

    # 各類錯誤實例
    errors: list = field(default_factory=list)

    # 紅燈/黃燈
    alerts: list = field(default_factory=list)


def analyze_quality(machine, human, groups):
    """分析品質，回傳 QualityReport"""
    report = QualityReport()
    report.machine_count = len(machine)
    report.human_count = len(human)

    # 1. 整體 CER
    m_full = ' '.join(s.text for s in machine)
    h_full = ' '.join(s.text for s in human)
    report.cer = compute_cer(m_full, h_full)

    # 2. 逐群組分析
    error_counts = Counter()
    severity_counts = Counter()
    same_count = 0
    total_groups = len(groups)
    all_errors = []

    for s_set, a_set in groups:
        if not s_set or not a_set:
            continue

        s_text = ' '.join(machine[i].text for i in sorted(s_set))
        a_text = ' '.join(human[i].text for i in sorted(a_set))

        if s_text == a_text:
            same_count += 1
            continue

        # 取時間戳
        times = [machine[i].start_ms for i in s_set]
        ts = ms_to_ts(min(times))
        m_idxs = ','.join(f"#{machine[i].idx}" for i in sorted(s_set))

        # 提取改動
        for op, s0, s1, a0, a1 in difflib.SequenceMatcher(None, s_text, a_text).get_opcodes():
            if op == 'equal':
                continue
            old_frag = s_text[s0:s1]
            new_frag = a_text[a0:a1]
            result = classify_error(op, old_frag, new_frag, a_text)
            if result is None:
                continue

            err_type, severity = result
            error_counts[err_type] += 1
            severity_counts[severity] += 1

            all_errors.append(ErrorInstance(
                error_type=err_type,
                severity=severity,
                old_text=old_frag,
                new_text=new_frag,
                timestamp=ts,
                machine_idx=m_idxs,
                context=s_text[:50],
            ))

    report.error_counts = dict(error_counts)
    report.severity_counts = dict(severity_counts)
    report.total_changes = sum(error_counts.values())
    report.same_ratio = same_count / total_groups if total_groups > 0 else 0
    report.errors = all_errors

    # 3. 加權品質分數
    # 公式：100 - (嚴重錯誤*3 + 中等錯誤*1 + 輕微錯誤*0.3) / total_subs * 100
    total_subs = len(machine)
    weighted_penalty = (
        severity_counts.get(3, 0) * 3.0 +
        severity_counts.get(2, 0) * 1.0 +
        severity_counts.get(1, 0) * 0.3
    )
    report.quality_score = max(0, 100 - (weighted_penalty / total_subs * 100))

    # 4. 嚴重錯誤時間密度
    if human:
        total_minutes = (human[-1].end_ms - human[0].start_ms) / 60000.0
        if total_minutes > 0:
            report.severe_per_minute = severity_counts.get(3, 0) / total_minutes

    # 5. 紅燈/黃燈判斷
    if report.cer > 0.10:
        report.alerts.append(("RED", f"CER={report.cer:.1%} 超過 10% 門檻"))
    elif report.cer > 0.05:
        report.alerts.append(("YELLOW", f"CER={report.cer:.1%} 介於 5%-10%"))

    if severity_counts.get(3, 0) > total_subs * 0.02:
        report.alerts.append(("RED", f"嚴重錯誤 {severity_counts.get(3,0)} 個，超過字幕數的 2%"))
    elif severity_counts.get(3, 0) > total_subs * 0.01:
        report.alerts.append(("YELLOW", f"嚴重錯誤 {severity_counts.get(3,0)} 個，超過字幕數的 1%"))

    if report.quality_score < 90:
        report.alerts.append(("RED", f"品質分數 {report.quality_score:.1f} 低於 90 分"))
    elif report.quality_score < 95:
        report.alerts.append(("YELLOW", f"品質分數 {report.quality_score:.1f} 低於 95 分"))

    term_errors = error_counts.get("TERM_ERROR", 0)
    if term_errors > 5:
        report.alerts.append(("RED", f"專有名詞錯誤 {term_errors} 個"))
    elif term_errors > 2:
        report.alerts.append(("YELLOW", f"專有名詞錯誤 {term_errors} 個"))

    if report.severe_per_minute > 1.0:
        report.alerts.append(("RED", f"嚴重錯誤密度 {report.severe_per_minute:.1f}/分鐘，觀眾體驗差"))
    elif report.severe_per_minute > 0.5:
        report.alerts.append(("YELLOW", f"嚴重錯誤密度 {report.severe_per_minute:.1f}/分鐘"))

    if not report.alerts:
        report.alerts.append(("GREEN", "所有指標正常"))

    return report


# ============================================================
# 報告格式化
# ============================================================

def format_report(report):
    """產出人類可讀的品質報告"""
    o = []
    w = o.append

    w("=" * 60)
    w("  SRT 字幕校正品質報告")
    w("=" * 60)
    w("")
    w(f"  機器校正版: {os.path.basename(report.machine_path)} ({report.machine_count} 條)")
    w(f"  人工校正版: {os.path.basename(report.human_path)} ({report.human_count} 條)")
    w("")

    # 警示燈號
    w("--- 警示狀態 ---")
    for level, msg in report.alerts:
        icon = {"RED": "[!!!]", "YELLOW": "[! ]", "GREEN": "[ OK]"}[level]
        w(f"  {icon} {msg}")
    w("")

    # 核心指標
    w("--- 核心指標 ---")
    w(f"  CER（字元錯誤率）  : {report.cer:.2%}")
    w(f"  一致率（SAME 比例）: {report.same_ratio:.1%}")
    w(f"  總改動數           : {report.total_changes}")
    w(f"  品質分數（加權）    : {report.quality_score:.1f} / 100")
    w(f"  嚴重錯誤密度       : {report.severe_per_minute:.2f} 個/分鐘")
    w("")

    # 嚴重度分佈
    w("--- 嚴重度分佈 ---")
    for sev, label in [(3, "嚴重（語意錯誤）"), (2, "中等（同音字/用字）"), (1, "輕微（贅詞/格式）")]:
        count = report.severity_counts.get(sev, 0)
        w(f"  {label}: {count}")
    w("")

    # 錯誤類型分佈
    w("--- 錯誤類型分佈 ---")
    type_labels = {
        "HOMOPHONE_KNOWN": "已知同音字",
        "HOMOPHONE_NEW": "新同音字",
        "CONTEXT_JUDGMENT": "語境判斷差異",
        "TERM_ERROR": "專有名詞錯誤",
        "MISHEAR": "聽錯/語意錯誤",
        "FILLER_KNOWN": "已知贅詞",
        "FILLER_CANDIDATE": "疑似贅詞",
        "DELETE_OTHER": "其他刪除",
        "INSERT": "人工補字",
        "REPLACE_OTHER": "其他替換",
        "UNKNOWN": "未分類",
    }
    for err_type, label in type_labels.items():
        count = report.error_counts.get(err_type, 0)
        if count > 0:
            w(f"  {label:<12s}: {count:4d}")
    w("")

    # 嚴重錯誤明細（top 20）
    severe = [e for e in report.errors if e.severity == 3]
    if severe:
        w("--- 嚴重錯誤明細（前 20 筆）---")
        for e in severe[:20]:
            if e.old_text and e.new_text:
                w(f"  [{e.timestamp}] {e.machine_idx} 「{e.old_text}」→「{e.new_text}」 [{e.error_type}]")
            elif e.old_text:
                w(f"  [{e.timestamp}] {e.machine_idx} 刪「{e.old_text}」 [{e.error_type}]")
            else:
                w(f"  [{e.timestamp}] {e.machine_idx} 加「{e.new_text}」 [{e.error_type}]")
        if len(severe) > 20:
            w(f"  ...還有 {len(severe) - 20} 筆")
        w("")

    # 品質建議
    w("--- 品質改善建議 ---")
    if report.error_counts.get("HOMOPHONE_NEW", 0) > 3:
        w(f"  * 有 {report.error_counts['HOMOPHONE_NEW']} 個新同音字候選，建議加入 style guide")
    if report.error_counts.get("TERM_ERROR", 0) > 0:
        w(f"  * 有 {report.error_counts['TERM_ERROR']} 個專有名詞錯誤，建議加入 preprocess 自動替換規則")
    if report.error_counts.get("MISHEAR", 0) > 5:
        w(f"  * 有 {report.error_counts['MISHEAR']} 個聽錯/語意錯誤，這類需要 LLM 語境理解才能修正")
    filler_total = report.error_counts.get("FILLER_KNOWN", 0) + report.error_counts.get("FILLER_CANDIDATE", 0)
    if filler_total > 10:
        w(f"  * 有 {filler_total} 個贅詞問題，建議擴充 preprocess 的贅詞規則")
    w("")

    # 分數解讀
    w("--- 分數解讀 ---")
    w("  95-100: 優秀 — 機器校正品質接近人工")
    w("  90-95 : 良好 — 少量人工修正即可")
    w("  80-90 : 需改善 — 較多錯誤需要人工處理")
    w("  <80   : 不合格 — 機器校正效果不佳，需調整策略")
    w("")

    return '\n'.join(o)


def report_to_dict(report):
    """轉為 dict 以便 JSON 輸出"""
    return {
        "machine_path": report.machine_path,
        "human_path": report.human_path,
        "machine_count": report.machine_count,
        "human_count": report.human_count,
        "cer": round(report.cer, 4),
        "same_ratio": round(report.same_ratio, 4),
        "total_changes": report.total_changes,
        "quality_score": round(report.quality_score, 1),
        "severe_per_minute": round(report.severe_per_minute, 2),
        "error_counts": report.error_counts,
        "severity_counts": {str(k): v for k, v in report.severity_counts.items()},
        "alerts": [{"level": level, "message": msg} for level, msg in report.alerts],
    }


# ============================================================
# Main
# ============================================================

def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    use_json = "--json" in sys.argv

    if len(args) < 2:
        print("用法: python3 srt_quality_metrics.py <machine.srt> <human.srt> [--json]")
        print()
        print("指標說明:")
        print("  CER = 字元錯誤率（越低越好）")
        print("  品質分數 = 100 - 加權懲罰（越高越好）")
        print("  嚴重度: 3=語意錯誤, 2=同音字, 1=贅詞")
        sys.exit(1)

    m_path, h_path = args[0], args[1]

    machine = parse_srt(m_path)
    human = parse_srt(h_path)

    groups = align_by_time(machine, human)
    report = analyze_quality(machine, human, groups)
    report.machine_path = m_path
    report.human_path = h_path

    if use_json:
        print(json.dumps(report_to_dict(report), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))


if __name__ == '__main__':
    main()
