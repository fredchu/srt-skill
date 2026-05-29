#!/usr/bin/env python3
"""
srt_preprocess.py — LLM 校正前的機械性預處理

在丟給 Sonnet 之前，先用程式處理掉不需要語言判斷的部分：
  1. 字幕超過 20 字 → 自動拆句
  2. 高信度同音字 + 語境 → 自動替換
  3. 獨立語氣詞整條 → 自動刪除
  4. Whisper 重複辨識 → 自動刪除
  5. 高信度贅詞 → 自動刪除

用法:
    python3 srt_preprocess.py input.srt [output.srt] [--stats]

不加 output 時，輸出到 input_preprocessed.srt
加 --stats 會在 stderr 印出處理統計
"""

import re
import sys

import jieba
from opencc import OpenCC

jieba.initialize()
_t2s = OpenCC('t2s')
import os
from dataclasses import dataclass, field

# ============================================================
# 設定
# ============================================================

MAX_CHAR_LEN = 20  # 字幕長度上限

# 高信度自動替換：(錯字, 正確字, 必須出現的語境, 不可出現的排除詞)
# 只放「幾乎不可能誤判」的規則
# 規則壓縮原則：前綴匹配合併同源錯誤、移除與 LLM prompt 重疊項
AUTO_REPLACE_COMMON = [
    # 金融術語 —— 無語境限制（任何 ASR 都會犯）
    ("排斥", "盤勢", [], []),
    ("無黨", "五檔", [], []),
    ("五當", "五檔", [], []),
    ("持值", "遲滯", [], []),
    ("張張滴滴", "漲漲跌跌", [], []),
    ("燕歸陣傳", "言歸正傳", [], []),
    ("直升壓力", "支撐壓力", [], []),
    ("雄識", "熊市", [], []),
    ("血率", "斜率", [], []),
    # 英文縮寫修正
    ("gtp", "GDP", [], []),
    ("GTP", "GDP", [], []),
    # 有觸發語境且排除詞明確的單字
    ("骨", "股", ["票", "價", "市"], ["骨頭", "骨架", "骨氣"]),
    ("承", "成", ["交量", "交額"], ["承受", "承擔", "承認", "承諾"]),
    # 需要語境的多字組
    ("城市", "程式", ["交易"], []),
    ("食物", "實務", ["操作"], []),
    # Austin 校正高頻：不分 ASR 引擎都會犯
    ("空投", "空頭", [], ["空投幣", "空投活動"]),
    ("SMP", "S&P", [], []),
    # 「道瓊」系列 — 每次 LLM 跑都會修的高頻錯字
    ("道球", "道瓊", [], []),
    ("喝到酒", "道瓊", [], []),
    # 「升級型藥」→「生技新藥」— 每次都出現 3+ 次
    ("升級型藥", "生技新藥", [], []),
    # 同音高頻
    ("議價", "溢價", ["折價", "股價", "沒有"], ["議價能力", "議價空間"]),
    ("槓感", "槓桿", [], []),
    ("練股", "煉蠱", [], []),
    ("王仁勳", "黃仁勳", [], []),
    # 4 月技術分析學習：軍/G/周/更/心/英/型/音/中音 高頻
    ("軍線", "均線", [], []),
    ("30軍", "30均", [], []),
    ("300軍", "300均", [], []),
    ("三十軍", "30均", [], []),
    ("三百軍", "300均", [], []),
    ("30G", "30均", [], []),
    ("300G", "300均", [], []),
    ("周線", "週線", [], []),
    ("周期", "週期", [], []),
    ("三更", "三根", [], ["半夜三更", "三更半夜"]),
    ("兩更", "兩根", [], []),
    ("中心", "中陰", ["轉換期", "區", "很短"], ["中心思想", "市中心", "以...為中心"]),
    ("中英", "中陰", [], ["中英文", "中英對照"]),
    ("中音", "中陰", [], ["中音聲", "高中音"]),
    ("中鷹", "中陰", [], []),
    ("型態", "形態", [], []),
    # 4 月技術分析第二輪學習：均線同音家族
    ("賽斯均", "30均", [], []),
    ("30句", "30均", [], []),
    ("300句", "300均", [], []),
    ("三十句", "30均", [], []),
    ("三百句", "300均", [], []),
    ("30圈", "30均", [], []),
    ("300圈", "300均", [], []),
    ("三十圈", "30均", [], []),
    ("30群", "30均", [], []),
    ("300群", "300均", [], []),
    ("粥線", "週線", [], []),
    ("一粥", "一週", [], []),
    ("軸線", "週線", [], []),
    # 4 月學習：金融術語固定替換
    ("解碼", "減碼", [], []),
    ("撒爆", "傻爆", [], []),
    ("行程", "形成", ["30 天", "20 天", "30天", "20天"], ["旅遊行程", "行程表"]),
    ("建廠", "建倉", [], []),
    ("黏線", "年線", [], []),
    ("年限", "年線", [], ["保固年限", "使用年限", "服役年限"]),
    ("警線", "頸線", [], []),
    ("平凡", "頻繁", ["碰得", "出現", "進出"], ["平凡的", "平凡人"]),
    ("增殺", "真殺", [], []),
    ("多投", "多頭", [], ["多投幾次", "多投資"]),
]

# Breeze ASR 特有錯誤（--breeze 時才啟用）
AUTO_REPLACE_BREEZE = [
    # 「級距」系列 — 前綴匹配合併（Breeze 最高頻）
    ("機具", "級距", ["市值", "關卡"], []),
    ("幾句", "級距", ["市值", "關卡"], []),
    ("結局", "級距", ["市值", "重要"], ["結局也", "結局是"]),
    # 「市值」系列 — 前綴匹配（事實→市值，無合法用法在金融語境）
    ("事實管理", "市值管理", [], []),
    ("事實級距", "市值級距", [], []),
    ("事實關卡", "市值關卡", [], []),
    # 同音字
    ("互稱合", "護城河", [], []),
    ("風球", "逢九", [], []),
    ("遊車", "油車", ["傳統"], []),
    ("郵車", "油車", ["傳統"], []),
    ("輕能原車", "氫能源車", [], []),
    ("輕能源車", "氫能源車", [], []),
    ("椅盤", "以盤", ["代跌", "帶跌"], []),
    ("紙碟", "止跌", [], ["光碟"]),
    ("土耳朵", "兔耳朵", [], []),
    ("經濟商", "經紀商", [], []),
    ("道雄", "道瓊", [], []),
    ("賣非", "賣飛", [], []),
    ("強款彈", "搶反彈", [], []),
    # 金融校正補充：Breeze 特有
    ("合融合", "核融合", [], []),
    ("參加金額", "成交金額", [], []),
    ("參加量", "成交量", [], []),
    ("附和", "符合", ["條件", "標準", "規定", "要求"], ["附和他", "附和你"]),
    ("活力", "獲利", ["能力", "狀況", "表現", "成長"], ["有活力", "充滿活力"]),
    ("泡霧縣市", "拋物線式", [], []),
    ("玉京鄉", "鬱金香", [], []),
    # 5/7 技術分析-4月-09 學習
    ("君子回歸", "均值回歸", [], []),
    ("泥流區", "彌留區", [], []),
    ("甲衰", "假摔", [], []),
    ("三人均", "30均", [], []),
    ("中醫學", "中陰", ["長度", "區", "期"], []),
    ("三角區", "30均", ["附近", "還是"], []),
    ("健康", "建倉", ["角度", "策略"], ["身體健康", "健康狀況", "健康檢查", "心理健康"]),
    ("30 軍", "30均", [], []),
    ("300 軍", "300均", [], []),
    # 5/10 技術分析-4月-10 學習
    ("基隆海嘯", "金融海嘯", [], []),
    ("上市方向感", "喪失方向感", [], []),
    ("富凡", "複盤", [], []),
    ("繳獲式", "攪禍式", [], []),
    ("doc", "大可", [], []),
]

# 前綴匹配規則：(錯誤前綴, 正確前綴) — 自動替換前綴，保留後綴
PREFIX_REPLACE = [
    ("四指", "市值"),  # 四指極具→市值級距、四指關卡→市值關卡...
]

# 獨立語氣詞：整條只有這些字時刪除
STANDALONE_FILLERS = {"好", "嗯", "哎", "OK", "ok", "Ok", "對", "在", "我", "嗯嗯", "啊", "欸"}

# 拆句用的連接詞（在這些詞前面拆）
SPLIT_CONNECTORS = ["然後", "所以", "但是", "可是", "而且", "不過", "因為", "如果", "或者", "甚至"]

# ============================================================
# SRT 解析
# ============================================================

@dataclass
class Sub:
    idx: int
    start_ms: int
    end_ms: int
    text: str
    deleted: bool = False

    @property
    def char_len(self):
        return len(self.text)

    def ts_str(self):
        return f"{ms_to_ts(self.start_ms)} --> {ms_to_ts(self.end_ms)}"


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
    s = ms // 1000; frac = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{frac:03d}"


def parse_srt(path):
    with open(path, 'r', encoding='utf-8-sig') as f:
        content = f.read()
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    subs = []
    for block in re.split(r'\n\s*\n', content.strip()):
        lines = block.strip().split('\n')
        if len(lines) < 2: continue
        idx_m = re.match(r'^(\d+)\s*$', lines[0].strip())
        if not idx_m: continue
        ts_m = re.match(
            r'(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})',
            lines[1].strip())
        if not ts_m: continue
        text = '\n'.join(l.strip() for l in lines[2:] if l.strip())
        subs.append(Sub(int(idx_m.group(1)), ts_to_ms(ts_m.group(1)),
                        ts_to_ms(ts_m.group(2)), text))
    return subs


def write_srt(subs, path):
    with open(path, 'w', encoding='utf-8') as f:
        idx = 1
        for s in subs:
            if s.deleted: continue
            f.write(f"{idx}\n{s.ts_str()}\n{s.text}\n\n")
            idx += 1


# ============================================================
# 1. 自動拆句
# ============================================================

def split_subtitle(sub):
    """把超過 MAX_CHAR_LEN 的字幕拆成多條"""
    text = sub.text
    if len(text) <= MAX_CHAR_LEN:
        return [sub]

    # 找所有可拆的位置
    candidates = []

    # 逗號
    for m in re.finditer(r'，', text):
        pos = m.end()  # 逗號後面拆
        if pos < len(text):
            candidates.append(pos)

    # 連接詞前面拆
    for conn in SPLIT_CONNECTORS:
        for m in re.finditer(re.escape(conn), text):
            pos = m.start()
            if pos > 0:
                candidates.append(pos)

    if not candidates:
        return [sub]

    # 過濾掉會切斷英文單字的拆點
    candidates = [pos for pos in candidates
                  if not (0 < pos < len(text)
                          and text[pos - 1].isascii() and text[pos - 1].isalpha()
                          and text[pos].isascii() and text[pos].isalpha())]

    if not candidates:
        return [sub]

    candidates = sorted(set(candidates))

    # 只在必要時拆：從 start 開始累積，超過 MAX_CHAR_LEN 時才在最近的候選點切
    parts = []
    start = 0
    while start < len(text):
        remaining_len = len(text) - start
        if remaining_len <= MAX_CHAR_LEN:
            break  # 剩餘不超長，不用再拆

        # 找 start 之後、在 MAX_CHAR_LEN 範圍內的最後一個候選點
        best_pos = None
        for pos in candidates:
            if pos <= start:
                continue
            if pos - start > MAX_CHAR_LEN:
                break  # 超出範圍
            if pos - start >= 3 and len(text) - pos >= 3:
                best_pos = pos

        if best_pos is None:
            # 範圍內沒有候選點，擴大搜索找最近的候選點
            for pos in candidates:
                if pos > start + 3 and len(text) - pos >= 3:
                    best_pos = pos
                    break

        if best_pos is None:
            break  # 完全沒有可拆的點

        segment = text[start:best_pos].strip()
        if segment:
            parts.append(segment)
        start = best_pos

    # 最後一段
    remaining = text[start:].strip()
    if remaining:
        parts.append(remaining)

    if len(parts) <= 1:
        return [sub]

    # 二次檢查：如果拆出來還有超長的，嘗試在中點附近拆
    final_parts = []
    for part in parts:
        if len(part) > MAX_CHAR_LEN * 1.5:
            mid = len(part) // 2
            best_pos = None
            best_dist = len(part)
            for m in re.finditer(r'，', part):
                dist = abs(m.end() - mid)
                if dist < best_dist:
                    best_dist = dist
                    best_pos = m.end()
            for conn in SPLIT_CONNECTORS:
                for m in re.finditer(re.escape(conn), part):
                    dist = abs(m.start() - mid)
                    if dist < best_dist:
                        best_dist = dist
                        best_pos = m.start()
            if best_pos and 3 <= best_pos <= len(part) - 3:
                final_parts.append(part[:best_pos].strip())
                final_parts.append(part[best_pos:].strip())
            else:
                final_parts.append(part)
        else:
            final_parts.append(part)

    # 去除每段結尾的逗號（拆句產生的殘留，在字幕結尾很突兀）
    final_parts = [p.rstrip('，,') for p in final_parts]
    final_parts = [p for p in final_parts if p]  # 防止整段只有逗號的邊緣情況

    if len(final_parts) <= 1:
        return [sub]

    # 產生拆句後的 Sub 物件，時間軸按文字長度比例分配
    total_ms = sub.end_ms - sub.start_ms
    char_lens = [max(len(p), 1) for p in final_parts]
    total_chars = sum(char_lens)
    result = []
    cumulative = 0
    for part, cl in zip(final_parts, char_lens):
        seg_start = sub.start_ms + (total_ms * cumulative // total_chars)
        cumulative += cl
        seg_end = sub.start_ms + (total_ms * cumulative // total_chars)
        result.append(Sub(idx=sub.idx, start_ms=seg_start, end_ms=seg_end, text=part))
    return result


# ============================================================
# 2. 高信度同音字自動替換
# ============================================================

def auto_replace_homophones(sub, stats, use_breeze=False):
    """對高信度的同音字做自動替換"""
    text = sub.text

    # 前綴匹配規則
    for wrong_prefix, correct_prefix in PREFIX_REPLACE:
        if wrong_prefix in text:
            count = text.count(wrong_prefix)
            text = text.replace(wrong_prefix, correct_prefix)
            stats["auto_replace"] += count

    # 精確匹配規則
    rules = AUTO_REPLACE_COMMON[:]
    if use_breeze:
        rules.extend(AUTO_REPLACE_BREEZE)

    for wrong, correct, contexts, excludes in rules:
        if wrong not in text:
            continue
        if any(ex in text for ex in excludes):
            continue
        if contexts:
            pos = 0
            while True:
                pos = text.find(wrong, pos)
                if pos == -1: break
                window_start = max(0, pos - 5)
                window_end = min(len(text), pos + len(wrong) + 5)
                window = text[window_start:window_end]
                if any(c in window for c in contexts):
                    text = text[:pos] + correct + text[pos + len(wrong):]
                    stats["auto_replace"] += 1
                    pos += len(correct)
                else:
                    pos += 1
        else:
            count = text.count(wrong)
            text = text.replace(wrong, correct)
            stats["auto_replace"] += count
    sub.text = text


# ============================================================
# 3. 獨立語氣詞刪除
# ============================================================

def delete_standalone_fillers(sub, stats):
    """整條只有語氣詞時標記刪除"""
    stripped = sub.text.strip()
    if stripped in STANDALONE_FILLERS:
        sub.deleted = True
        stats["deleted_fillers"] += 1
        return True
    return False


# ============================================================
# 4. Whisper 重複偵測
# ============================================================

def detect_duplicates(subs, stats):
    """偵測相鄰的重複字幕"""
    for i in range(len(subs) - 1):
        if subs[i].deleted: continue
        if subs[i].text == subs[i + 1].text:
            # 時間軸相鄰（間隔 < 2 秒）
            gap = abs(subs[i + 1].start_ms - subs[i].end_ms)
            if gap < 2000:
                subs[i + 1].deleted = True
                stats["deleted_duplicates"] += 1


# ============================================================
# 5. 高信度贅詞刪除
# ============================================================

def delete_fillers_in_text(sub, stats):
    """刪除高信度的填充贅詞"""
    text = sub.text
    original = text

    # 句首的「好」（後面接逗號或直接接其他內容）
    text = re.sub(r'^好[，,]\s*', '', text)
    text = re.sub(r'^好(?=[我你他她它們大家那這所以])', '', text)

    # 句首的「那」（過渡用法，非指示代詞）
    # 「那這個」「那我們」（後面接「這」或代詞）
    text = re.sub(r'^那(?=這個[^人事物])', '', text)
    text = re.sub(r'^那(?=這個$)', '', text)

    # 句尾「啦」
    text = re.sub(r'啦$', '', text)

    if text != original:
        sub.text = text.strip()
        stats["deleted_fillers_inline"] += 1


# ============================================================
# Main
# ============================================================

def main():
    args = sys.argv[1:]
    show_stats = "--stats" in args
    use_breeze = "--breeze" in args
    args = [a for a in args if not a.startswith("--")]

    if not args:
        print("用法: python3 srt_preprocess.py input.srt [output.srt] [--stats] [--breeze]", file=sys.stderr)
        sys.exit(1)

    input_path = args[0]
    if len(args) > 1:
        output_path = args[1]
    else:
        base = input_path.rsplit('.', 1)[0]
        output_path = f"{base}_preprocessed.srt"

    stats = {
        "total_input": 0,
        "split_operations": 0,
        "auto_replace": 0,
        "deleted_fillers": 0,
        "deleted_duplicates": 0,
        "deleted_fillers_inline": 0,
        "total_output": 0,
        "lines_over_limit": 0,
    }

    # 解析
    subs = parse_srt(input_path)
    stats["total_input"] = len(subs)

    # Pass 1: 高信度同音字替換
    for sub in subs:
        auto_replace_homophones(sub, stats, use_breeze=use_breeze)

    # Pass 2: 獨立語氣詞刪除
    for sub in subs:
        delete_standalone_fillers(sub, stats)

    # Pass 3: 重複偵測
    detect_duplicates(subs, stats)

    # Pass 4: 高信度贅詞
    for sub in subs:
        if not sub.deleted:
            delete_fillers_in_text(sub, stats)

    # Pass 4.5: 修復 ASR 標點恢復的詞中斷逗號（如「辦，法」→「辦法」）
    # 策略：逗號兩側的字組成高頻詞，且前面的字不是其他多字詞的結尾
    _mid_comma_re = re.compile(r'([\u4e00-\u9fff])，([\u4e00-\u9fff])')
    mid_comma_fixes = 0
    for sub in subs:
        if sub.deleted:
            continue
        def _fix_mid_comma(m):
            pair = m.group(1) + m.group(2)
            cn = _t2s.convert(pair)
            if jieba.dt.FREQ.get(cn, 0) < 500:
                return m.group(0)
            # 逗號前的字如果是某個多字詞的結尾，逗號就是正確的
            # 例：「比較，大家」→「較大」是詞但「比較」也是，不該修
            idx = sub.text.find(m.group(0))
            if idx >= 1:
                prev2 = sub.text[idx-1] + m.group(1)
                prev2_cn = _t2s.convert(prev2)
                if jieba.dt.FREQ.get(prev2_cn, 0) >= 500:
                    return m.group(0)
            return pair
        fixed = _mid_comma_re.sub(_fix_mid_comma, sub.text)
        if fixed != sub.text:
            mid_comma_fixes += 1
            sub.text = fixed

    # Pass 5: 拆句（對存活的字幕）
    new_subs = []
    for sub in subs:
        if sub.deleted:
            new_subs.append(sub)
            continue
        parts = split_subtitle(sub)
        if len(parts) > 1:
            stats["split_operations"] += 1
        new_subs.extend(parts)

    # 統計
    alive = [s for s in new_subs if not s.deleted]
    stats["total_output"] = len(alive)
    stats["lines_over_limit"] = sum(1 for s in alive if s.char_len > MAX_CHAR_LEN)

    # 輸出
    write_srt(new_subs, output_path)

    if show_stats:
        print(f"\n{'='*50}", file=sys.stderr)
        print(f"  預處理統計", file=sys.stderr)
        print(f"{'='*50}", file=sys.stderr)
        print(f"  輸入: {stats['total_input']} 條", file=sys.stderr)
        print(f"  同音字自動替換: {stats['auto_replace']} 處", file=sys.stderr)
        print(f"  獨立語氣詞刪除: {stats['deleted_fillers']} 條", file=sys.stderr)
        print(f"  重複字幕刪除: {stats['deleted_duplicates']} 條", file=sys.stderr)
        print(f"  行內贅詞刪除: {stats['deleted_fillers_inline']} 處", file=sys.stderr)
        if mid_comma_fixes:
            print(f"  詞中斷逗號修復: {mid_comma_fixes} 處", file=sys.stderr)
        print(f"  拆句操作: {stats['split_operations']} 條被拆", file=sys.stderr)
        print(f"  輸出: {stats['total_output']} 條", file=sys.stderr)
        print(f"  仍超過{MAX_CHAR_LEN}字: {stats['lines_over_limit']} 條", file=sys.stderr)
        print(f"  輸出檔: {output_path}", file=sys.stderr)
        print(f"{'='*50}\n", file=sys.stderr)

    print(f"預處理完成: {stats['total_input']}→{stats['total_output']} 條, "
          f"替換{stats['auto_replace']}處, 刪除{stats['deleted_fillers']+stats['deleted_duplicates']}條, "
          f"拆句{stats['split_operations']}條 → {output_path}")


if __name__ == '__main__':
    main()
