#!/usr/bin/env python3
"""
srt_diff_report.py — SRT 字幕校正差異分析報告產生器 v4

用法:
    python3 srt_diff_report.py <sonnet版.srt> <austin版.srt> [輸出前綴]

v4 改進:
    - details 按對齊類型分組（TEXT_MOD / SPLIT+MOD / COMPLEX_MOD），不再混在一起
    - summary 統計按對齊類型拆分，區分可靠 diff vs 可能的對齊失敗
    - 漏改掃描加入排除詞，減少誤報
"""

import re
import sys
import os
import difflib
from collections import Counter, defaultdict
from dataclasses import dataclass, field


# ============================================================
# 設定：style guide 已知規則
# ============================================================

KNOWN_HOMOPHONES = {
    "長": "漲", "排": "盤", "撤": "測", "軍": "均", "金": "均",
    "刷": "跌", "解": "減", "骨": "股",
    "付": "復", "花": "畫", "投": "頭", "病": "兵", "山": "三",
    "在": "再", "敘": "訊", "需": "序", "扒": "巴", "級": "梯",
    "承": "成",
}

KNOWN_COMPOUND_HOMOPHONES = {
    "排斥": "盤勢", "無黨": "五檔", "持值": "遲滯",
    "城市": "程式", "食物": "實務", "張張滴滴": "漲漲跌跌",
}

KNOWN_FILLERS = {"這個", "好", "啦", "一個", "什麼", "的"}

ALL_KNOWN_PAIRS = {}
ALL_KNOWN_PAIRS.update(KNOWN_HOMOPHONES)
ALL_KNOWN_PAIRS.update(KNOWN_COMPOUND_HOMOPHONES)

# 漏改掃描規則: (錯字, 正確字, 觸發語境, 排除詞)
# 觸發語境為空=無條件觸發; 排除詞：出現在前後就跳過
SCAN_RULES = [
    ("長", "漲", ["幅", "停", "了一根", "多少", "勢", "跌", "紅", "根"],
                 ["長期", "長線", "長時間", "成長", "擅長", "長短", "長遠", "長久", "部長", "董事長", "會長"]),
    ("排", "盤", ["勢", "整", "面"],
                 ["排列", "排名", "排除", "排行", "安排"]),
    ("撤", "測", ["回", "預", "軟體", "工具"],
                 ["撤回", "撤退", "撤銷", "撤出"]),
    ("軍", "均", ["線", "值", "移動"], []),
    ("金", "均", ["線", "值", "移動"],
                 ["資金", "金額", "金融", "金錢", "黃金", "基金", "獎金", "現金", "金字"]),
    ("刷", "跌", ["一根", "下來", "破"], []),
    ("解", "減", ["碼", "持"],
                 ["了解", "理解", "解釋", "解決", "解答", "解讀", "解套", "解鎖"]),
    ("骨", "股", ["票", "價", "市"], []),
    ("付", "復", ["甦", "反", "回", "恢"],
                 ["付出", "支付", "付款", "交付", "付費"]),
    ("花", "畫", ["線", "圖", "趨勢"],
                 ["花費", "花錢", "花時間", "花了"]),
    ("投", "頭", ["部", "肩"],
                 ["投資", "投入", "投報", "投信", "投機", "投票", "投放", "投注"]),
    ("承", "成", ["交量", "交額"],
                 ["承受", "承擔", "承認", "承諾"]),
    ("持值", "遲滯", [], []),
    ("城市", "程式", ["交易"], []),
    ("食物", "實務", ["操作"], []),
    ("排斥", "盤勢", [], []),
    ("無黨", "五檔", [], []),
]


# ============================================================
# SRT 解析
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

def align_by_time(sonnet, austin, threshold_ms=150):
    s_to_a, a_to_s = defaultdict(set), defaultdict(set)
    ai_start = 0
    for si, ss in enumerate(sonnet):
        for aj in range(ai_start, len(austin)):
            aa = austin[aj]
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

    for si in range(len(sonnet)):
        if si in visited_s: continue
        if si not in s_to_a:
            groups.append(({si}, set())); visited_s.add(si); continue
        s_set, a_set = bfs(seed_s=si)
        visited_s.update(s_set); visited_a.update(a_set)
        groups.append((s_set, a_set))
    for aj in range(len(austin)):
        if aj not in visited_a:
            groups.append((set(), {aj}))

    groups.sort(key=lambda g: min(
        [sonnet[i].start_ms for i in g[0]] + [austin[i].start_ms for i in g[1]] + [10**9]))
    return groups


# ============================================================
# 差異分類
# ============================================================

def classify_group(s_set, a_set, sonnet, austin):
    ns, na = len(s_set), len(a_set)
    if ns == 0: return "ADD"
    if na == 0: return "DEL"
    s_t = ' '.join(sonnet[i].text for i in sorted(s_set))
    a_t = ' '.join(austin[i].text for i in sorted(a_set))
    same = (s_t == a_t)
    if ns == 1 and na == 1: return "SAME" if same else "TEXT_MOD"
    if ns == 1 and na > 1:  return "SPLIT" if same else "SPLIT+MOD"
    if ns > 1 and na == 1:  return "MERGE" if same else "MERGE+MOD"
    return "SAME" if same else "COMPLEX_MOD"


# ============================================================
# 文字改動
# ============================================================

@dataclass
class Change:
    change_type: str
    old_text: str
    new_text: str
    category: str = ""
    context_s: str = ""
    context_a: str = ""
    timestamp: str = ""
    sonnet_idx: str = ""
    austin_idx: str = ""
    align_type: str = ""   # v4: 來源對齊類型


def extract_changes(s_text, a_text):
    changes = []
    for op, s0, s1, a0, a1 in difflib.SequenceMatcher(None, s_text, a_text).get_opcodes():
        if op == 'equal': continue
        changes.append(Change(op, s_text[s0:s1], a_text[a0:a1]))
    return changes


def categorize_change(ch):
    old, new = ch.old_text.strip(), ch.new_text.strip()
    if ch.change_type == 'delete':
        if not old: return "WHITESPACE"
        if old in KNOWN_FILLERS: return "FILLER_KNOWN"
        if len(old) <= 3: return "FILLER_CANDIDATE"
        return "DELETE_OTHER"
    if ch.change_type == 'insert':
        return "WHITESPACE" if not new else "INSERT"
    if ch.change_type == 'replace':
        if old in ALL_KNOWN_PAIRS and ALL_KNOWN_PAIRS[old] == new:
            return "HOMOPHONE_KNOWN"
        for k, v in ALL_KNOWN_PAIRS.items():
            if k in old and v in new and len(old) <= len(k) + 2:
                return "HOMOPHONE_KNOWN"
        if len(old) == 1 and len(new) == 1:
            return "HOMOPHONE_NEW_CANDIDATE"
        if len(old) >= 2 or len(new) >= 2:
            return "REPLACE_MULTI"
        return "REPLACE_OTHER"
    return "UNKNOWN"


# ============================================================
# 漏改掃描（v4: 加排除詞）
# ============================================================

def scan_missed(subs):
    findings = []
    for sub in subs:
        text = sub.text
        for rule in SCAN_RULES:
            wrong, correct, contexts = rule[0], rule[1], rule[2]
            excludes = rule[3] if len(rule) > 3 else []
            pos = 0
            while True:
                pos = text.find(wrong, pos)
                if pos == -1: break
                # 排除詞檢查
                w_start = max(0, pos - 5)
                w_end = min(len(text), pos + len(wrong) + 5)
                window = text[w_start:w_end]
                if any(ex in window for ex in excludes):
                    pos += 1; continue
                # 語境檢查
                if contexts and not any(c in window for c in contexts):
                    pos += 1; continue
                sn_start = max(0, pos - 8)
                sn_end = min(len(text), pos + len(wrong) + 8)
                findings.append({
                    "idx": sub.idx, "ts": ms_to_ts(sub.start_ms),
                    "wrong": wrong, "correct": correct,
                    "snippet": text[sn_start:sn_end], "full_text": text,
                })
                pos += 1
    return findings


# ============================================================
# 主分析（v4: 改動標記來源對齊類型）
# ============================================================

@dataclass
class GroupInfo:
    """一組對齊的完整資訊"""
    cat: str
    ts: str
    s_idxs: str
    a_idxs: str
    s_texts: list = field(default_factory=list)  # [(idx, text, char_len), ...]
    a_texts: list = field(default_factory=list)
    s_combined: str = ""
    a_combined: str = ""
    changes: list = field(default_factory=list)  # [Change, ...]


def analyze(sonnet, austin, groups):
    R = {
        "cat_counts": Counter(),
        "group_infos": [],          # 所有非 SAME 的 GroupInfo
        "deletes": [],
        "adds": [],
        "splits": [],
        "merges": [],
        # v4: 按對齊類型分組的改動
        "changes_by_align": defaultdict(list),  # align_type -> [Change]
    }

    for s_set, a_set in groups:
        cat = classify_group(s_set, a_set, sonnet, austin)
        R["cat_counts"][cat] += 1
        if cat == "SAME": continue

        times = [sonnet[i].start_ms for i in s_set] + [austin[i].start_ms for i in a_set]
        ts = ms_to_ts(min(times)) if times else "??:??:??"
        s_idxs = ','.join(f"#{sonnet[i].idx}" for i in sorted(s_set))
        a_idxs = ','.join(f"#{austin[i].idx}" for i in sorted(a_set))

        gi = GroupInfo(
            cat=cat, ts=ts, s_idxs=s_idxs, a_idxs=a_idxs,
            s_texts=[(sonnet[i].idx, sonnet[i].text, sonnet[i].char_len) for i in sorted(s_set)],
            a_texts=[(austin[i].idx, austin[i].text, austin[i].char_len) for i in sorted(a_set)],
        )

        if cat == "DEL":
            for si in sorted(s_set):
                R["deletes"].append({"ts": ts, "s_idx": f"#{sonnet[si].idx}",
                                     "text": sonnet[si].text, "char_len": sonnet[si].char_len})
            R["group_infos"].append(gi)
            continue
        if cat == "ADD":
            for aj in sorted(a_set):
                R["adds"].append({"ts": ts, "a_idx": f"#{austin[aj].idx}",
                                  "text": austin[aj].text, "char_len": austin[aj].char_len})
            R["group_infos"].append(gi)
            continue

        if cat in ("SPLIT", "SPLIT+MOD"):
            R["splits"].append(gi)
        if cat in ("MERGE", "MERGE+MOD"):
            R["merges"].append(gi)

        gi.s_combined = ' '.join(sonnet[si].text for si in sorted(s_set))
        gi.a_combined = ' '.join(austin[aj].text for aj in sorted(a_set))

        if gi.s_combined != gi.a_combined:
            for ch in extract_changes(gi.s_combined, gi.a_combined):
                ch.context_s, ch.context_a = gi.s_combined, gi.a_combined
                ch.timestamp, ch.sonnet_idx, ch.austin_idx = ts, s_idxs, a_idxs
                ch.align_type = cat
                ch.category = categorize_change(ch)
                gi.changes.append(ch)
                R["changes_by_align"][cat].append(ch)

        R["group_infos"].append(gi)

    return R


# ============================================================
# 工具函數
# ============================================================

def ctx_snip(text, keyword, window=8):
    pos = text.find(keyword)
    if pos == -1: return text[:20]
    return text[max(0, pos-window):min(len(text), pos+len(keyword)+window)]


def fmt_histogram(subs, label):
    if not subs: return f"--- {label} (0 條) ---"
    lens = sorted([s.char_len for s in subs])
    n = len(lens)
    bk = [("0-15", 0, 15), ("16-25", 16, 25), ("26-35", 26, 35),
          ("36-45", 36, 45), ("46-55", 46, 55), ("56+", 56, 99999)]
    cts = {name: sum(1 for l in lens if lo <= l <= hi) for name, lo, hi in bk}
    avg, p50, p90 = sum(lens)/n, lens[n//2], lens[int(n*0.9)]
    lines = [f"--- {label} (共{n}條, 平均{avg:.1f}字, P50={p50}字, P90={p90}字, 最長={lens[-1]}字) ---"]
    for name, _, _ in bk:
        c = cts[name]; pct = f"{c*100/n:.1f}%"; bar = "█" * int(c * 40 / n)
        lines.append(f"  {name:>5s}字: {c:4d} ({pct:>6s}) {bar}")
    return '\n'.join(lines)


def fmt_change_line(ch):
    """單行格式化一個改動"""
    if ch.change_type == 'delete': desc = f"刪「{ch.old_text}」"
    elif ch.change_type == 'insert': desc = f"加「{ch.new_text}」"
    else: desc = f"「{ch.old_text}」→「{ch.new_text}」"
    return f"  [{ch.timestamp}] {ch.sonnet_idx}→{ch.austin_idx}  {desc}"


def fmt_change_full(ch):
    """完整格式化（含上下文）"""
    lines = [fmt_change_line(ch)]
    lines.append(f"    S: {ch.context_s}")
    lines.append(f"    A: {ch.context_a}")
    return '\n'.join(lines)


def fmt_group(gi):
    """格式化一個 GroupInfo"""
    lines = [f"  [{gi.ts}] {gi.s_idxs} ↔ {gi.a_idxs}"]
    for idx, text, cl in gi.s_texts:
        lines.append(f"    S#{idx} [{cl}字]: {text}")
    for idx, text, cl in gi.a_texts:
        lines.append(f"    A#{idx} [{cl}字]: {text}")
    if gi.changes:
        descs = []
        for ch in gi.changes:
            if ch.category == "WHITESPACE": continue
            if ch.change_type == 'delete': descs.append(f"刪「{ch.old_text}」")
            elif ch.change_type == 'insert': descs.append(f"加「{ch.new_text}」")
            else: descs.append(f"「{ch.old_text}」→「{ch.new_text}」")
        if descs:
            lines.append(f"    Δ: {' / '.join(descs)}")
    return '\n'.join(lines)


def count_changes_by_category(changes):
    """統計一組 changes 的分類"""
    c = Counter(ch.category for ch in changes if ch.category != "WHITESPACE")
    return c


# ============================================================
# summary.txt
# ============================================================

def gen_summary(sonnet, austin, R, missed, s_path, a_path):
    o = []
    w = o.append

    w("╔══════════════════════════════════════════════════════════════╗")
    w("║          SRT 校正差異分析摘要  (v4)                        ║")
    w("╠══════════════════════════════════════════════════════════════╣")
    w(f"║ Sonnet 版: {os.path.basename(s_path)} ({len(sonnet)} 條)")
    w(f"║ Austin 版: {os.path.basename(a_path)} ({len(austin)} 條)")
    w("╚══════════════════════════════════════════════════════════════╝")
    w("")

    # ==== 1. 字幕長度 ====
    w("=" * 60)
    w("  1. 字幕長度分析")
    w("=" * 60)
    w("")
    w(fmt_histogram(sonnet, "Sonnet 版"))
    w("")
    w(fmt_histogram(austin, "Austin 版"))
    w("")
    long_s = [s for s in sonnet if s.char_len > 40]
    w(f"Sonnet 超長(>40字): {len(long_s)}條 ({len(long_s)*100/len(sonnet):.1f}%)")
    w("")

    splits = R["splits"]
    if splits:
        orig = [cl for gi in splits for _, _, cl in gi.s_texts]
        post = sorted([cl for gi in splits for _, _, cl in gi.a_texts])
        p90 = post[int(len(post) * 0.9)]
        w(f"Austin 拆句統計 (共 {len(splits)} 組):")
        w(f"  拆前: 平均{sum(orig)/len(orig):.1f}字, 最長{max(orig)}字")
        w(f"  拆後: 平均{sum(post)/len(post):.1f}字, P50={post[len(post)//2]}字, P90={p90}字")
        w(f"  → 建議字幕長度上限: {p90} 字")
        w("")

    # ==== 2. 差異類型統計 ====
    w("=" * 60)
    w("  2. 差異類型統計")
    w("=" * 60)
    w("")

    cc = R["cat_counts"]; total = sum(cc.values())
    for c in ["SAME","TEXT_MOD","SPLIT","SPLIT+MOD","MERGE","MERGE+MOD","COMPLEX_MOD","DEL","ADD"]:
        n = cc.get(c, 0); pct = f"{n*100/total:.1f}%" if total else "0%"
        w(f"  {c:<14s}: {n:4d} ({pct:>6s})")
    w(f"  {'TOTAL':<14s}: {total:4d}")
    w("")

    # v4: 按對齊類型拆分的改動統計
    w("文字改動細分（按對齊可靠度分組）:")
    w("")

    reliable_types = ["TEXT_MOD", "SPLIT+MOD", "MERGE+MOD"]
    unreliable_types = ["COMPLEX_MOD"]

    for label, types, note in [
        ("可靠（1:1 或 1:N 對齊）", reliable_types, ""),
        ("不可靠（N:M 對齊，可能含假 diff）", unreliable_types, " ⚠"),
    ]:
        changes_in_group = []
        for t in types:
            changes_in_group.extend(R["changes_by_align"].get(t, []))
        cc_sub = count_changes_by_category(changes_in_group)
        total_sub = sum(cc_sub.values())

        w(f"  【{label}】共 {total_sub} 處{note}")
        if total_sub > 0:
            for cat_name, display in [
                ("HOMOPHONE_KNOWN", "已知同音字"), ("HOMOPHONE_NEW_CANDIDATE", "新同音字候選"),
                ("FILLER_KNOWN", "已知贅詞刪除"), ("FILLER_CANDIDATE", "疑似贅詞"),
                ("REPLACE_MULTI", "多字替換"), ("DELETE_OTHER", "其他刪除"),
                ("REPLACE_OTHER", "其他替換"), ("INSERT", "插入"),
            ]:
                if cc_sub.get(cat_name, 0) > 0:
                    w(f"    {display:<12s}: {cc_sub[cat_name]:4d}")
        w("")

    w(f"  整條刪除: {len(R['deletes']):4d} 條")
    w(f"  整條新增: {len(R['adds']):4d} 條")
    w("")

    # ==== 3. Style guide 建議 ====
    w("=" * 60)
    w("  3. STYLE GUIDE 修改建議草稿")
    w("=" * 60)
    w("")
    w("（程式自動產生，需 Claude 審閱。以下僅使用可靠對齊的數據。）")
    w("")

    # 只用可靠對齊的改動
    reliable_changes = []
    for t in reliable_types:
        reliable_changes.extend(R["changes_by_align"].get(t, []))

    # 3a 新同音字（只從可靠改動中提取）
    new_homo = defaultdict(list)
    for ch in reliable_changes:
        if ch.category == "HOMOPHONE_NEW_CANDIDATE":
            new_homo[(ch.old_text, ch.new_text)].append(ch)

    if new_homo:
        w("--- 3a. 建議新增同音字對 ---")
        w("")
        for (old, new), insts in sorted(new_homo.items(), key=lambda x: -len(x[1])):
            w(f"  {old}→{new}（出現 {len(insts)} 次）")
            for inst in insts[:3]:
                w(f"    [{inst.timestamp}] S: ...{ctx_snip(inst.context_s, old)}...")
                w(f"               A: ...{ctx_snip(inst.context_a, new)}...")
            if len(insts) > 3: w(f"    ...還有 {len(insts)-3} 處")
            w("")
    else:
        w("--- 3a. 可靠對齊中無新同音字對 ---")
        w("")

    # 3b 贅詞
    filler_cands = [ch for ch in reliable_changes if ch.category == "FILLER_CANDIDATE"]
    if filler_cands:
        w("--- 3b. 疑似新贅詞 ---")
        w("")
        for word, count in Counter(ch.old_text.strip() for ch in filler_cands).most_common():
            w(f"  刪「{word}」（{count} 次）")
        w("")

    # 3c 長度
    if splits:
        w("--- 3c. 建議新增字幕長度規則 ---")
        w(f"  單條字幕不超過 {p90} 字，超過時在語意停頓處拆成多條。")
        w("")

    # 3d 刪除
    dels = R["deletes"]
    if dels:
        w("--- 3d. 整條刪除分析 ---")
        w("")
        short = [d for d in dels if d["char_len"] <= 3]
        med = [d for d in dels if 4 <= d["char_len"] <= 8]
        lng = [d for d in dels if d["char_len"] > 8]
        if short:
            w(f"  獨立語氣詞 (≤3字): {len(short)} 條")
            for word, c in Counter(d["text"] for d in short).most_common(10):
                w(f"    「{word}」× {c}")
        if med:
            w(f"  疑似幻覺 (4-8字): {len(med)} 條")
            for d in med[:10]: w(f"    [{d['ts']}] {d['s_idx']}: {d['text']}")
        if lng:
            w(f"  較長刪除 (>8字): {len(lng)} 條")
            for d in lng[:10]: w(f"    [{d['ts']}] {d['s_idx']}: {d['text']}")
        w("")

    # ==== 4. 模糊案例 ====
    w("=" * 60)
    w("  4. 需要 Claude 判斷的模糊案例")
    w("=" * 60)
    w("")

    # 可靠的模糊
    reliable_ambiguous = [ch for ch in reliable_changes
                          if ch.category in ("REPLACE_MULTI", "DELETE_OTHER", "REPLACE_OTHER", "INSERT")]
    w(f"  可靠對齊中的模糊改動: {len(reliable_ambiguous)} 處")
    w(f"    （在 details 的 Section A-D 中，建議上傳讓 Claude 判斷）")
    w("")

    # 不可靠的
    complex_changes = R["changes_by_align"].get("COMPLEX_MOD", [])
    complex_non_ws = [ch for ch in complex_changes if ch.category != "WHITESPACE"]
    w(f"  COMPLEX_MOD 中的改動: {len(complex_non_ws)} 處")
    w(f"    （對齊不可靠，大部分可能是假 diff。")
    w(f"     在 details 的 Section E 中列出完整群組供人工檢視。）")
    w("")

    # ==== 5. 漏改 ====
    w("=" * 60)
    w("  5. 疑似漏改（Austin 版仍存在的可疑用字）")
    w("=" * 60)
    w("")
    w("Austin 最終版中命中 style guide 錯字規則的條目。")
    w("已排除常見合法用法（如「投資」不觸發「投→頭」）。")
    w("")
    if missed:
        by_type = defaultdict(list)
        for m in missed: by_type[(m["wrong"], m["correct"])].append(m)
        for (wrong, correct), items in sorted(by_type.items(), key=lambda x: -len(x[1])):
            w(f"  「{wrong}」可能應為「{correct}」（{len(items)} 處）:")
            for item in items[:5]:
                w(f"    [{item['ts']}] #{item['idx']}: ...{item['snippet']}...")
            if len(items) > 5: w(f"    ...還有 {len(items)-5} 處")
            w("")
    else:
        w("  未發現疑似漏改。")
    w("")

    return '\n'.join(o)


# ============================================================
# details.txt（v4: 按對齊類型分組）
# ============================================================

def gen_details(sonnet, austin, R, max_ex=15):
    o = []
    w = o.append

    w("╔══════════════════════════════════════════════════════════════╗")
    w("║          SRT 校正差異 — 分類明細  (v4)                     ║")
    w("╚══════════════════════════════════════════════════════════════╝")
    w("")
    w("本檔按「對齊可靠度」分組：")
    w("  Section A-D: 可靠對齊（TEXT_MOD / SPLIT+MOD / MERGE+MOD）的改動")
    w("  Section E:   不可靠對齊（COMPLEX_MOD）的完整群組")
    w("  Section F:   拆句明細")
    w("  Section G:   整條刪除/新增")
    w("")

    # ========== 可靠對齊的改動 ==========

    reliable_types = ["TEXT_MOD", "SPLIT+MOD", "MERGE+MOD"]
    reliable_changes = []
    for t in reliable_types:
        reliable_changes.extend(R["changes_by_align"].get(t, []))

    # A: TEXT_MOD 完整群組（最有價值的分析對象）
    text_mod_groups = [gi for gi in R["group_infos"] if gi.cat == "TEXT_MOD"]
    if text_mod_groups:
        w("=" * 60)
        w(f"  A. TEXT_MOD 完整群組（1:1 對應，共 {len(text_mod_groups)} 組）")
        w("=" * 60)
        w("")
        for gi in text_mod_groups[:max_ex]:
            w(fmt_group(gi))
            w("")
        rem = len(text_mod_groups) - max_ex
        if rem > 0: w(f"  ...省略 {rem} 組")
        w("")

    # B: SPLIT+MOD 完整群組
    split_mod_groups = [gi for gi in R["group_infos"] if gi.cat == "SPLIT+MOD"]
    if split_mod_groups:
        w("=" * 60)
        w(f"  B. SPLIT+MOD 完整群組（1:N 拆句+修改，共 {len(split_mod_groups)} 組）")
        w("=" * 60)
        w("")
        for gi in split_mod_groups[:max_ex]:
            w(fmt_group(gi))
            w("")
        rem = len(split_mod_groups) - max_ex
        if rem > 0: w(f"  ...省略 {rem} 組")
        w("")

    # C: MERGE+MOD 完整群組
    merge_mod_groups = [gi for gi in R["group_infos"] if gi.cat == "MERGE+MOD"]
    if merge_mod_groups:
        w("=" * 60)
        w(f"  C. MERGE+MOD 完整群組（N:1 合併+修改，共 {len(merge_mod_groups)} 組）")
        w("=" * 60)
        w("")
        for gi in merge_mod_groups[:max_ex]:
            w(fmt_group(gi))
            w("")
        rem = len(merge_mod_groups) - max_ex
        if rem > 0: w(f"  ...省略 {rem} 組")
        w("")

    # D: 可靠改動中的新同音字候選（含完整上下文）
    new_homo = defaultdict(list)
    for ch in reliable_changes:
        if ch.category == "HOMOPHONE_NEW_CANDIDATE":
            new_homo[(ch.old_text, ch.new_text)].append(ch)
    if new_homo:
        w("=" * 60)
        w("  D. 新同音字候選（可靠對齊，完整上下文）")
        w("=" * 60)
        w("")
        for (old, new), insts in sorted(new_homo.items(), key=lambda x: -len(x[1])):
            w(f"「{old}」→「{new}」({len(insts)} 次)")
            for inst in insts[:max_ex]:
                w(f"  [{inst.timestamp}] {inst.sonnet_idx}→{inst.austin_idx}")
                w(f"    S: {inst.context_s}")
                w(f"    A: {inst.context_a}")
            w("")

    # ========== 不可靠對齊 ==========

    # E: COMPLEX_MOD 完整群組
    complex_groups = [gi for gi in R["group_infos"] if gi.cat == "COMPLEX_MOD"]
    if complex_groups:
        w("=" * 60)
        w(f"  E. COMPLEX_MOD 完整群組（N:M 對齊，共 {len(complex_groups)} 組）")
        w("  ⚠ 對齊不可靠，需要人工判斷哪些是真正的改動")
        w("=" * 60)
        w("")
        for gi in complex_groups[:max_ex]:
            w(fmt_group(gi))
            w("")
        rem = len(complex_groups) - max_ex
        if rem > 0: w(f"  ...省略 {rem} 組")
        w("")

    # ========== 結構性變動 ==========

    # F: 純拆句（無文字改動）
    pure_splits = [gi for gi in R["group_infos"] if gi.cat == "SPLIT"]
    if pure_splits:
        w("=" * 60)
        w(f"  F. 純拆句（SPLIT，無文字改動，共 {len(pure_splits)} 組）")
        w("=" * 60)
        w("")
        for gi in pure_splits[:max_ex]:
            w(fmt_group(gi))
            w("")
        rem = len(pure_splits) - max_ex
        if rem > 0: w(f"  ...省略 {rem} 組")
        w("")

    # G: 刪除與新增
    if R["deletes"]:
        w("=" * 60)
        w(f"  G1. 整條刪除（共 {len(R['deletes'])} 條）")
        w("=" * 60)
        w("")
        for d in R["deletes"]:
            w(f"  [{d['ts']}] {d['s_idx']} [{d['char_len']}字]: {d['text']}")
        w("")

    if R["adds"]:
        w("=" * 60)
        w(f"  G2. Austin 新增（共 {len(R['adds'])} 條）")
        w("=" * 60)
        w("")
        for a in R["adds"]:
            w(f"  [{a['ts']}] {a['a_idx']} [{a['char_len']}字]: {a['text']}")
        w("")

    return '\n'.join(o)


# ============================================================
# Main
# ============================================================

def main():
    if len(sys.argv) < 3:
        print("用法: python3 srt_diff_report.py <sonnet版.srt> <austin版.srt> [輸出前綴]")
        print()
        print("範例:")
        print("  python3 srt_diff_report.py video1_sonnet.srt video1_austin.srt video1")
        print("  → 產生 video1_summary.txt 和 video1_details.txt")
        sys.exit(1)

    s_path, a_path = sys.argv[1], sys.argv[2]
    if len(sys.argv) > 3:
        prefix = sys.argv[3]
    else:
        from datetime import datetime
        prefix = f"diff_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    print(f"解析 Sonnet 版: {s_path}")
    sonnet = parse_srt(s_path)
    print(f"  → {len(sonnet)} 條")

    print(f"解析 Austin 版: {a_path}")
    austin = parse_srt(a_path)
    print(f"  → {len(austin)} 條")

    print("時間軸對齊...")
    groups = align_by_time(sonnet, austin)
    print(f"  → {len(groups)} 組配對")

    print("分析差異...")
    R = analyze(sonnet, austin, groups)

    print("掃描 Austin 版疑似漏改...")
    missed = scan_missed(austin)
    print(f"  → {len(missed)} 處疑似")

    sum_path = f"{prefix}_summary.txt"
    det_path = f"{prefix}_details.txt"

    print("生成報告...")
    with open(sum_path, 'w', encoding='utf-8') as f:
        f.write(gen_summary(sonnet, austin, R, missed, s_path, a_path))
    with open(det_path, 'w', encoding='utf-8') as f:
        f.write(gen_details(sonnet, austin, R))

    ss = os.path.getsize(sum_path)
    ds = os.path.getsize(det_path)
    print(f"\n完成！")
    print(f"  {sum_path}: {ss:,} bytes ({ss/1024:.1f} KB) ← 必傳給 Claude")
    print(f"  {det_path}: {ds:,} bytes ({ds/1024:.1f} KB) ← 備用")
    print(f"\n步驟:")
    print(f"  1. 先上傳 {sum_path} 給 Claude 分析")
    print(f"  2. 如果 Claude 需要看明細，再上傳 {det_path}")


if __name__ == '__main__':
    main()
