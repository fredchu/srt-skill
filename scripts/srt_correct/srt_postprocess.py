#!/usr/bin/env python3
"""
srt_postprocess.py — LLM 校正後的驗證和修復

在 Sonnet 校正完成後，自動檢查並修復：
  0. 時間軸還原：用 preprocessed SRT 還原被 LLM 捏造的時間軸
  1. 術語保護：偵測被切到相鄰字幕的術語，合回正確的那條
  2. 序號重新編號
  3. 格式驗證（時間軸合法性）
  4. 產出品質報告（含仍超長條目數，供人工檢視）

用法:
    python3 srt_postprocess.py input.srt [output.srt] [--stats] [--ref preprocessed.srt] [--terms terms.txt]
"""

import re
import sys
import os
from difflib import SequenceMatcher

import jieba
jieba.initialize()

MAX_CHAR_LEN = 20

SPLIT_CONNECTORS = ["然後", "所以", "但是", "可是", "而且", "不過", "因為", "如果", "或者", "甚至"]


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
    entries = []
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
        entries.append({
            "start_ms": ts_to_ms(ts_m.group(1)),
            "end_ms": ts_to_ms(ts_m.group(2)),
            "text": text,
        })
    return entries


def _is_in_english_word(text, pos):
    """檢查 pos 是否落在英文單字中間"""
    if pos <= 0 or pos >= len(text):
        return False
    return text[pos - 1].isascii() and text[pos - 1].isalpha() and \
           text[pos].isascii() and text[pos].isalpha()


def force_split(entry):
    """強制拆分超長字幕，保護英文單字邊界"""
    text = entry["text"]
    if len(text) <= MAX_CHAR_LEN:
        return [entry]

    candidates = []
    for m in re.finditer(r'，', text):
        candidates.append(m.end())
    for conn in SPLIT_CONNECTORS:
        for m in re.finditer(re.escape(conn), text):
            if m.start() > 0:
                candidates.append(m.start())

    if not candidates:
        # 沒有逗號或連接詞可拆，只有遠超上限才硬拆
        if len(text) <= MAX_CHAR_LEN * 1.5:
            return [entry]  # 微超就放過，不要硬切
        # 在中間硬拆，用 jieba 找最近的詞邊界避免斷詞
        mid = len(text) // 2
        words = list(jieba.cut(text))
        pos = 0
        word_boundaries = []
        for w in words:
            pos += len(w)
            if 2 < pos < len(text) - 2:
                word_boundaries.append(pos)
        if word_boundaries:
            best = min(word_boundaries, key=lambda b: abs(b - mid))
        else:
            best = mid
        # 避免切斷英文單字
        if _is_in_english_word(text, best):
            # 往前找到英文單字開頭
            while best > 0 and text[best - 1].isascii() and text[best - 1].isalpha():
                best -= 1
            if best == 0:
                # 整段都是英文，往後找單字結尾
                best = mid
                while best < len(text) and text[best].isascii() and text[best].isalpha():
                    best += 1
        candidates = [best]

    # 過濾掉會切斷英文單字的拆點
    candidates = [pos for pos in candidates if not _is_in_english_word(text, pos)]
    candidates = sorted(set(candidates))

    if not candidates:
        return [entry]

    # 只在必要時拆：累積超過 MAX_CHAR_LEN 時才在最近的候選點切
    parts = []
    start = 0
    while start < len(text):
        remaining_len = len(text) - start
        if remaining_len <= MAX_CHAR_LEN:
            break

        best_pos = None
        for pos in candidates:
            if pos <= start:
                continue
            if pos - start > MAX_CHAR_LEN:
                break
            if pos - start >= 2 and len(text) - pos >= 2:
                best_pos = pos

        if best_pos is None:
            for pos in candidates:
                if pos > start + 2 and len(text) - pos >= 2:
                    best_pos = pos
                    break

        if best_pos is None:
            break

        seg = text[start:best_pos].strip()
        if seg:
            parts.append(seg)
        start = best_pos

    remaining = text[start:].strip()
    if remaining:
        parts.append(remaining)

    if len(parts) <= 1:
        return [entry]

    # 去除每段結尾的逗號（拆句殘留）
    parts = [p.rstrip('，,') for p in parts]
    parts = [p for p in parts if p]

    if len(parts) <= 1:
        return [entry]

    total_ms = entry["end_ms"] - entry["start_ms"]
    n = len(parts)
    result = []
    for i, part in enumerate(parts):
        result.append({
            "start_ms": entry["start_ms"] + (total_ms * i // n),
            "end_ms": entry["start_ms"] + (total_ms * (i + 1) // n),
            "text": part,
        })
    return result


def _strip_punct(text):
    """去除標點，用於文字比對"""
    return re.sub(r'[，。、？！,.\s]', '', text)


def _find_anchor(cor_texts, ref_texts, ci_start, ri_start, ci_max, ri_max, threshold=0.75):
    """
    在 corrected[ci_start:ci_max] × ref[ri_start:ri_max] 中找最佳 anchor。
    回傳 (ci, ri, ratio) 或 None。
    優先找靠近起點的高信度匹配。
    """
    best = None
    # 限制搜尋範圍避免 O(n²) 爆炸
    ci_end = min(ci_start + 80, ci_max)
    ri_end = min(ri_start + 80, ri_max)
    for dist in range(1, max(ci_end - ci_start, ri_end - ri_start)):
        # 先搜距離為 dist 的所有 (ci, ri) 組合（曼哈頓距離）
        found_at_dist = False
        for dc in range(min(dist + 1, ci_end - ci_start)):
            dr = dist - dc
            if dr < 0 or dr >= ri_end - ri_start:
                continue
            c = ci_start + dc
            r = ri_start + dr
            if c >= ci_end or r >= ri_end:
                continue
            ct = cor_texts[c]
            rt = ref_texts[r]
            if not ct or not rt:
                continue
            ratio = SequenceMatcher(None, ct, rt).ratio()
            if ratio >= threshold:
                if best is None or ratio > best[2]:
                    best = (c, r, ratio)
                    found_at_dist = True
        # 如果在這個距離找到了高信度 anchor，就不用看更遠的了
        if found_at_dist and best[2] >= 0.85:
            break
    return best


def restore_timecodes(corrected, ref_entries):
    """
    用 preprocessed SRT 還原被 LLM 捏造的時間軸。

    三階段策略：
    1. 順序掃描：corrected 和 ref 各維護一個指標，嘗試 1:1、N:1（合併）、1:N（拆句）匹配
    2. 失配時用 anchor 搜尋重新同步：在前方大範圍搜尋下一個高信度匹配點
    3. 孤兒修復：未匹配的 entry 按比例插值（而非全部共用同一段時間）
    """
    restored = 0
    ref_texts = [_strip_punct(e["text"]) for e in ref_entries]
    cor_texts = [_strip_punct(e["text"]) for e in corrected]

    # matched[ci] = (ref_start_ms, ref_end_ms) or None
    matched = [None] * len(corrected)

    ri = 0  # ref index
    ci = 0  # corrected index

    while ci < len(corrected) and ri < len(ref_entries):
        ct = cor_texts[ci]
        if not ct:
            ci += 1
            continue

        rt = ref_texts[ri]

        # Case 1: 1:1 匹配（文字相同或高度相似）
        ratio = SequenceMatcher(None, ct, rt).ratio()
        if ratio >= 0.6:
            matched[ci] = (ref_entries[ri]["start_ms"], ref_entries[ri]["end_ms"])
            ci += 1
            ri += 1
            restored += 1
            continue

        # Case 2: N:1 合併 — corrected 一條 = ref 多條合併
        best_merge_span = 0
        best_merge_ratio = 0.0
        for span in range(2, 8):
            end_ri = ri + span
            if end_ri > len(ref_entries):
                break
            merged = ''.join(ref_texts[ri:end_ri])
            r = SequenceMatcher(None, ct, merged).ratio()
            if r > best_merge_ratio:
                best_merge_ratio = r
                best_merge_span = span

        # Case 3: 1:N 拆句 — ref 一條被 LLM 拆成多條 corrected
        best_split_span = 0
        best_split_ratio = 0.0
        for span in range(2, 6):
            end_ci = ci + span
            if end_ci > len(corrected):
                break
            merged_cor = ''.join(cor_texts[ci:end_ci])
            r = SequenceMatcher(None, merged_cor, rt).ratio()
            if r > best_split_ratio:
                best_split_ratio = r
                best_split_span = span

        # 選最好的匹配
        if best_merge_ratio >= 0.6 and best_merge_ratio >= best_split_ratio:
            # N:1 合併匹配
            end_ri = ri + best_merge_span
            matched[ci] = (ref_entries[ri]["start_ms"], ref_entries[end_ri - 1]["end_ms"])
            ci += 1
            ri = end_ri
            restored += 1
        elif best_split_ratio >= 0.6:
            # 1:N 拆句匹配 — 把 ref 的時間軸按文字長度比例分配給多條 corrected
            end_ci = ci + best_split_span
            total_ms = ref_entries[ri]["end_ms"] - ref_entries[ri]["start_ms"]
            char_lens = [max(len(cor_texts[ci + k]), 1) for k in range(best_split_span)]
            total_chars = sum(char_lens)
            cumulative = 0
            for k in range(best_split_span):
                seg_start = ref_entries[ri]["start_ms"] + (total_ms * cumulative // total_chars)
                cumulative += char_lens[k]
                seg_end = ref_entries[ri]["start_ms"] + (total_ms * cumulative // total_chars)
                matched[ci + k] = (seg_start, seg_end)
                restored += 1
            ci = end_ci
            ri += 1
        else:
            # 都沒匹配上 — 用 anchor 搜尋重新同步
            anchor = _find_anchor(cor_texts, ref_texts,
                                  ci, ri, len(corrected), len(ref_entries))
            if anchor:
                a_ci, a_ri, _ = anchor
                # 跳到 anchor 點，中間的 entries 留給孤兒修復
                ci = a_ci
                ri = a_ri
                # 不 advance — 下一輪迴圈會在 Case 1 匹配這個 anchor
            else:
                # 完全找不到 anchor，放棄剩餘部分
                break

    # 套用匹配結果
    for ci, m in enumerate(matched):
        if m:
            corrected[ci]["start_ms"] = m[0]
            corrected[ci]["end_ms"] = m[1]

    # 孤兒修復：未匹配的 entry 按比例插值到前後鄰居之間
    for ci in range(len(corrected)):
        if matched[ci] is not None:
            continue
        # 找前一個和後一個已匹配的 entry
        prev_ci = None
        for j in range(ci - 1, -1, -1):
            if matched[j] is not None:
                prev_ci = j
                break
        next_ci = None
        for j in range(ci + 1, len(corrected)):
            if matched[j] is not None:
                next_ci = j
                break

        if prev_ci is not None and next_ci is not None:
            # 按比例插值：把 prev_end ~ next_start 均分給中間所有孤兒
            prev_end = corrected[prev_ci]["end_ms"]
            next_start = corrected[next_ci]["start_ms"]
            orphan_count = next_ci - prev_ci - 1  # 中間孤兒數
            idx_in_gap = ci - prev_ci  # 1-based
            gap = next_start - prev_end
            seg_start = prev_end + gap * (idx_in_gap - 1) // orphan_count
            seg_end = prev_end + gap * idx_in_gap // orphan_count
            # 安全檢查：不讓時間倒退
            if seg_end <= seg_start:
                seg_end = seg_start + 500
            corrected[ci]["start_ms"] = seg_start
            corrected[ci]["end_ms"] = seg_end
        elif prev_ci is not None:
            corrected[ci]["start_ms"] = corrected[prev_ci]["end_ms"]
            corrected[ci]["end_ms"] = corrected[prev_ci]["end_ms"] + 2000

    return restored


def main():
    args = sys.argv[1:]
    show_stats = "--stats" in args
    ref_path = None
    terms_path = None
    clean_args = []
    i = 0
    while i < len(args):
        if args[i] == "--stats":
            i += 1
            continue
        if args[i] == "--ref" and i + 1 < len(args):
            ref_path = args[i + 1]
            i += 2
            continue
        if args[i] == "--terms" and i + 1 < len(args):
            terms_path = args[i + 1]
            i += 2
            continue
        clean_args.append(args[i])
        i += 1
    args = clean_args

    if not args:
        print("用法: python3 srt_postprocess.py input.srt [output.srt] [--stats] [--ref preprocessed.srt] [--terms terms.txt]", file=sys.stderr)
        sys.exit(1)

    input_path = args[0]
    if len(args) > 1:
        output_path = args[1]
    else:
        output_path = input_path  # 就地覆蓋

    entries = parse_srt(input_path)
    original_count = len(entries)

    # Pass 0: 時間軸還原（如果提供了 ref）
    tc_restored = 0
    if ref_path:
        ref_entries = parse_srt(ref_path)
        tc_restored = restore_timecodes(entries, ref_entries)

    # Pass 0.5: 移除行尾標點（字幕慣例不加句號、逗號結尾）
    period_count = 0
    for e in entries:
        orig = e["text"]
        stripped = orig.rstrip("。，.,")
        if stripped != orig:
            period_count += 1
            e["text"] = stripped

    new_entries = list(entries)

    # Pass 1: 術語保護 — 偵測術語被切到相鄰字幕，合回正確的那條
    term_merge_count = 0
    terms_file = terms_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "terms_austin_v2.txt")
    if os.path.exists(terms_file):
        terms = set()
        for line in open(terms_file, encoding='utf-8'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # 取術語本體（去掉括號註解）
            term = re.split(r'[（(]', line)[0].strip()
            if len(term) >= 2:
                terms.add(term)

        i = 0
        while i < len(new_entries) - 1:
            cur = new_entries[i]
            nxt = new_entries[i + 1]
            ct = cur["text"]
            nt = nxt["text"]

            merged = False
            for term in terms:
                # 檢查：前一條結尾 + 後一條開頭 = 術語
                for split_pos in range(1, len(term)):
                    prefix = term[:split_pos]
                    suffix = term[split_pos:]
                    if ct.endswith(prefix) and nt.startswith(suffix):
                        # 把 suffix 從下一條搬到這一條
                        cur["text"] = ct + suffix
                        nxt["text"] = nt[len(suffix):].lstrip('，,、 ')
                        # 調整時間軸：按字數比例從下一條借時間
                        if nxt["text"]:
                            total_nxt_ms = nxt["end_ms"] - nxt["start_ms"]
                            orig_nxt_len = len(suffix) + len(nxt["text"])
                            borrow_ms = total_nxt_ms * len(suffix) // max(orig_nxt_len, 1)
                            cur["end_ms"] = cur["end_ms"] + borrow_ms
                            nxt["start_ms"] = cur["end_ms"]
                        else:
                            # 下一條變空了，整條合併
                            cur["end_ms"] = nxt["end_ms"]
                            new_entries.pop(i + 1)
                        term_merge_count += 1
                        merged = True
                        break
                if merged:
                    break
            if not merged:
                i += 1

        # 清除空條目
        new_entries = [e for e in new_entries if e["text"].strip()]
    else:
        print(f"WARNING: terms file not found, skipping terminology protection: {terms_file}", file=sys.stderr)

    # Pass 2: 去除時間軸重疊的重複條目（時間軸+文字都相似才去重）
    dedup_count = 0
    deduped = []
    for e in new_entries:
        if deduped and abs(e["start_ms"] - deduped[-1]["start_ms"]) < 300 \
                and abs(e["end_ms"] - deduped[-1]["end_ms"]) < 300:
            # 時間軸相似 — 再檢查文字是否也相似
            text_ratio = SequenceMatcher(None,
                _strip_punct(e["text"]), _strip_punct(deduped[-1]["text"])).ratio()
            if text_ratio >= 0.6:
                dedup_count += 1
                if len(e["text"]) > len(deduped[-1]["text"]):
                    deduped[-1] = e
                continue
        deduped.append(e)
    new_entries = deduped

    # Pass 3: 驗證時間軸（修復 invalid + 消除 overlap）
    invalid_ts = 0
    for i, e in enumerate(new_entries):
        if e["end_ms"] <= e["start_ms"]:
            invalid_ts += 1
            # 修復：end 設為 min(start+1000, 下一條 start)，避免產生 overlap
            next_start = new_entries[i + 1]["start_ms"] if i + 1 < len(new_entries) else e["start_ms"] + 1000
            e["end_ms"] = min(e["start_ms"] + 1000, next_start)
    # Clamp：確保所有條目的 end 不超過下一條的 start
    for i in range(len(new_entries) - 1):
        if new_entries[i]["end_ms"] > new_entries[i + 1]["start_ms"]:
            new_entries[i]["end_ms"] = new_entries[i + 1]["start_ms"]

    # Pass 4: 拆分超長字幕（Sonnet 校正後可能產生合併導致的超長條目）
    split_count = 0
    split_entries = []
    for e in new_entries:
        parts = force_split(e)
        if len(parts) > 1:
            split_count += 1
        split_entries.extend(parts)
    new_entries = split_entries

    # 輸出
    still_over = sum(1 for e in new_entries if len(e["text"]) > MAX_CHAR_LEN)

    with open(output_path, 'w', encoding='utf-8') as f:
        for i, e in enumerate(new_entries, 1):
            f.write(f"{i}\n")
            f.write(f"{ms_to_ts(e['start_ms'])} --> {ms_to_ts(e['end_ms'])}\n")
            f.write(f"{e['text']}\n\n")

    if show_stats:
        parts = [f"後處理: {original_count}→{len(new_entries)}條"]
        if tc_restored:
            parts.append(f"時間軸還原{tc_restored}條")
        parts.extend([
            f"移除句點{period_count}條",
            f"術語保護{term_merge_count}處",
            f"去重{dedup_count}條",
            f"拆超長{split_count}條",
            f"仍超長{still_over}條",
            f"時間軸修復{invalid_ts}處",
        ])
        print(f"\n{', '.join(parts)}", file=sys.stderr)

    summary = f"後處理完成: {len(new_entries)}條"
    if tc_restored:
        summary += f", 時間軸還原{tc_restored}條"
    summary += f" → {output_path}"
    print(summary)


if __name__ == '__main__':
    main()
