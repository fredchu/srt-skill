#!/usr/bin/env python3
"""
srt_hallucination_fix.py — ASR 幻覺偵測 + 自動修復

掃描 SRT 找兩種異常：
  1. 重複型幻覺：單條字幕內同一字/詞重複 ≥ 10 次且佔比 ≥ 80%
  2. 時間軸空白：相鄰字幕間隔 > GAP_THRESHOLD 秒

偵測到後自動截取音檔片段、用 Breeze ASR 重跑、patch 回 SRT。

用法:
    python3 srt_hallucination_fix.py <SRT> <音檔或影片> [--breeze] [--output <path>] [--gap-threshold 10]

不加 --output 就直接覆寫原 SRT。
"""

import re
import sys
import os
import subprocess
from collections import Counter

# ============================================================
# 設定
# ============================================================

GAP_THRESHOLD_S = 10  # 空白超過幾秒算異常
REPEAT_MIN_COUNT = 10  # 同一 token 至少重複幾次
REPEAT_MIN_RATIO = 0.8  # 同一 token 佔比門檻
BUFFER_S = 10  # 截取音檔前後多抓幾秒（ASR 需要足夠上下文）

# ============================================================
# SRT 解析（與 srt_preprocess.py 相同結構）
# ============================================================

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
        if len(lines) < 2:
            continue
        idx_m = re.match(r'^(\d+)\s*$', lines[0].strip())
        if not idx_m:
            continue
        ts_m = re.match(
            r'(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})',
            lines[1].strip())
        if not ts_m:
            continue
        text = '\n'.join(l.strip() for l in lines[2:] if l.strip())
        entries.append({
            'idx': int(idx_m.group(1)),
            'start_ms': ts_to_ms(ts_m.group(1)),
            'end_ms': ts_to_ms(ts_m.group(2)),
            'text': text,
        })
    return entries


def write_srt(entries, path):
    with open(path, 'w', encoding='utf-8') as f:
        for i, e in enumerate(entries, 1):
            f.write(f"{i}\n{ms_to_ts(e['start_ms'])} --> {ms_to_ts(e['end_ms'])}\n{e['text']}\n\n")


# ============================================================
# 偵測
# ============================================================

def detect_repetition(entry):
    """偵測單條字幕內的重複幻覺。回傳 (token, count, ratio) 或 None。"""
    text = re.sub(r'[，,。！？、\s]', '', entry['text'])
    if len(text) < 10:
        return None
    for token_len in [1, 2, 3]:
        if len(text) < token_len:
            continue
        tokens = [text[i:i+token_len] for i in range(len(text) - token_len + 1)]
        counts = Counter(tokens)
        char, count = counts.most_common(1)[0]
        ratio = count / max(len(tokens), 1)
        if ratio >= REPEAT_MIN_RATIO and count >= REPEAT_MIN_COUNT:
            return (char, count, ratio)
    return None


def find_anomalies(entries, gap_threshold_s=None):
    """掃描所有條目，回傳異常區間列表 [{type, start_ms, end_ms, indices, detail}]。"""
    if gap_threshold_s is None:
        gap_threshold_s = GAP_THRESHOLD_S
    anomalies = []

    # 1. 重複型
    for i, e in enumerate(entries):
        result = detect_repetition(e)
        if result:
            char, count, ratio = result
            anomalies.append({
                'type': 'repetition',
                'start_ms': e['start_ms'],
                'end_ms': e['end_ms'],
                'indices': [i],
                'detail': f"'{char}' × {count} ({ratio:.0%})",
            })

    # 2. 空白型
    for i in range(len(entries) - 1):
        gap_ms = entries[i + 1]['start_ms'] - entries[i]['end_ms']
        if gap_ms > gap_threshold_s * 1000:
            anomalies.append({
                'type': 'gap',
                'start_ms': entries[i]['end_ms'],
                'end_ms': entries[i + 1]['start_ms'],
                'indices': [],  # 空白區沒有條目
                'detail': f"{gap_ms / 1000:.1f}s gap",
            })

    # 合併重疊/相鄰的異常區間（例如重複型後面緊接空白型）
    if not anomalies:
        return []

    anomalies.sort(key=lambda a: a['start_ms'])
    merged = [anomalies[0]]
    for a in anomalies[1:]:
        prev = merged[-1]
        # 兩個異常區間距離 < 5 秒，合併
        if a['start_ms'] - prev['end_ms'] < 5000:
            prev['end_ms'] = max(prev['end_ms'], a['end_ms'])
            prev['indices'] = sorted(set(prev['indices'] + a['indices']))
            prev['type'] = 'merged'
            prev['detail'] += ' + ' + a['detail']
        else:
            merged.append(a)

    return merged


# ============================================================
# 修復
# ============================================================

def run_asr(wav_path, output_dir, use_breeze=True):
    """執行 ASR，回傳產出的 SRT 路徑。"""
    script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subtitle_sh = os.path.join(script_dir, 'subtitle.sh')

    cmd = [subtitle_sh, wav_path]
    if use_breeze:
        cmd.append('--breeze')

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        print(f"  ❌ ASR 失敗: {result.stderr[-200:]}", file=sys.stderr)
        return None

    # 找產出的 SRT
    basename = os.path.splitext(os.path.basename(wav_path))[0]
    srt_path = os.path.join(output_dir, f'{basename}.srt')
    if os.path.exists(srt_path):
        return srt_path
    # fallback: 找最近的 .srt
    for f in sorted(os.listdir(output_dir), key=lambda x: os.path.getmtime(os.path.join(output_dir, x)), reverse=True):
        if f.endswith('.srt') and basename in f:
            return os.path.join(output_dir, f)
    return None


def patch_entries(original, anomaly, new_entries, offset_ms):
    """把重跑的 ASR 結果 patch 回原始條目列表。"""
    # 計算新條目的時間偏移
    patched = []
    for e in new_entries:
        patched.append({
            'idx': 0,
            'start_ms': e['start_ms'] + offset_ms,
            'end_ms': e['end_ms'] + offset_ms,
            'text': e['text'],
        })

    # 過濾掉落在異常區間內的新條目（如果超出邊界）
    patched = [e for e in patched
               if e['start_ms'] >= anomaly['start_ms'] - 1000
               and e['end_ms'] <= anomaly['end_ms'] + 1000]

    # 從原始列表中移除異常區間的條目，插入新條目
    result = []
    insert_done = False
    for i, e in enumerate(original):
        # 條目完全在異常區間內 → 跳過
        if e['start_ms'] >= anomaly['start_ms'] - 500 and e['end_ms'] <= anomaly['end_ms'] + 500:
            if i in anomaly.get('indices', []):
                if not insert_done:
                    result.extend(patched)
                    insert_done = True
                continue
            # 空白型：這條不在 indices 裡但時間在範圍內，也跳過
            if anomaly['type'] in ('gap', 'merged'):
                if not insert_done:
                    result.extend(patched)
                    insert_done = True
                continue

        # 空白型：插入點在兩條之間
        if (anomaly['type'] in ('gap', 'merged') and not insert_done
                and e['start_ms'] >= anomaly['end_ms'] - 500):
            result.extend(patched)
            insert_done = True

        result.append(e)

    # 如果還沒插入（異常在最後面）
    if not insert_done:
        result.extend(patched)

    return result


# ============================================================
# Main
# ============================================================

def main():
    args = sys.argv[1:]

    use_breeze = '--breeze' in args
    args = [a for a in args if a != '--breeze']

    output_path = None
    gap_threshold = GAP_THRESHOLD_S
    positional = []
    i = 0
    while i < len(args):
        if args[i] == '--output' and i + 1 < len(args):
            output_path = args[i + 1]
            i += 2
        elif args[i] == '--gap-threshold' and i + 1 < len(args):
            gap_threshold = float(args[i + 1])
            i += 2
        else:
            positional.append(args[i])
            i += 1

    if len(positional) < 2:
        print("用法: python3 srt_hallucination_fix.py <SRT> <音檔或影片> [--breeze] [--output <path>] [--gap-threshold 10]",
              file=sys.stderr)
        sys.exit(1)

    srt_path = positional[0]
    media_path = positional[1]

    if not os.path.exists(srt_path):
        print(f"❌ 找不到 SRT: {srt_path}", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(media_path):
        print(f"❌ 找不到媒體檔: {media_path}", file=sys.stderr)
        sys.exit(1)

    if output_path is None:
        output_path = srt_path  # 覆寫原檔

    # 解析 SRT
    entries = parse_srt(srt_path)
    print(f"掃描 {len(entries)} 條字幕...", file=sys.stderr)

    # 偵測異常
    anomalies = find_anomalies(entries, gap_threshold_s=gap_threshold)

    if not anomalies:
        print("✅ 未偵測到幻覺或異常空白", file=sys.stderr)
        if output_path != srt_path:
            write_srt(entries, output_path)
        sys.exit(0)

    print(f"⚠️  偵測到 {len(anomalies)} 個異常區間:", file=sys.stderr)
    for a in anomalies:
        print(f"  [{a['type']}] {ms_to_ts(a['start_ms'])} → {ms_to_ts(a['end_ms'])} — {a['detail']}",
              file=sys.stderr)

    # 逐一修復
    fixed = 0
    work_dir = os.path.dirname(srt_path)

    for ai, anomaly in enumerate(anomalies):
        print(f"\n修復 {ai+1}/{len(anomalies)}: {ms_to_ts(anomaly['start_ms'])} → {ms_to_ts(anomaly['end_ms'])}",
              file=sys.stderr)

        success = False
        # 嘗試不同大小的 buffer（10s → 20s → 30s）
        for attempt, buf_s in enumerate([BUFFER_S, BUFFER_S * 2, BUFFER_S * 3]):
            if attempt > 0:
                print(f"  重試 (buffer={buf_s}s)...", file=sys.stderr)

            # 截取音檔
            seg_wav = os.path.join(work_dir, f'_hallucination_seg_{ai}.wav')
            if not _extract_with_buffer(media_path, anomaly, buf_s, seg_wav):
                print(f"  跳過（音檔截取失敗）", file=sys.stderr)
                break

            # 重跑 ASR
            srt_result_path = run_asr(seg_wav, work_dir, use_breeze=use_breeze)
            if not srt_result_path:
                print(f"  跳過（ASR 失敗）", file=sys.stderr)
                _cleanup(work_dir, ai)
                break

            new_entries = parse_srt(srt_result_path)

            # 檢查重跑結果是否也是幻覺
            if len(new_entries) <= 1:
                print(f"  ⚠️  ASR 只產出 {len(new_entries)} 條", file=sys.stderr)
                _cleanup(work_dir, ai)
                continue  # 重試更大 buffer

            new_anomalies = find_anomalies(new_entries)
            if new_anomalies:
                print(f"  ⚠️  ASR 仍有幻覺", file=sys.stderr)
                _cleanup(work_dir, ai)
                continue  # 重試更大 buffer

            # 成功！計算時間偏移
            actual_start_s = max(0, anomaly['start_ms'] / 1000 - buf_s)
            offset_ms = int(actual_start_s * 1000)

            # Patch
            entries = patch_entries(entries, anomaly, new_entries, offset_ms)
            fixed += 1
            success = True
            print(f"  ✅ 已修復（插入 {len(new_entries)} 條字幕）", file=sys.stderr)
            _cleanup(work_dir, ai)
            break

        if not success:
            print(f"  ⚠️  多次重試仍失敗，可能為重複語音/靜音，需人工確認", file=sys.stderr)

    # 寫入
    write_srt(entries, output_path)
    print(f"\n{'='*50}", file=sys.stderr)
    print(f"完成: 偵測 {len(anomalies)} 個異常, 修復 {fixed} 個", file=sys.stderr)
    print(f"輸出: {output_path}", file=sys.stderr)
    print(f"{'='*50}", file=sys.stderr)


def _extract_with_buffer(media_path, anomaly, buf_s, output_path):
    """用指定的 buffer 截取音檔。"""
    start_s = max(0, anomaly['start_ms'] / 1000 - buf_s)
    end_s = anomaly['end_ms'] / 1000 + buf_s
    cmd = [
        'ffmpeg', '-y',
        '-i', media_path,
        '-ss', f'{start_s:.3f}',
        '-to', f'{end_s:.3f}',
        '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ❌ ffmpeg 失敗: {result.stderr[-200:]}", file=sys.stderr)
        return False
    return True


def _cleanup(work_dir, seg_idx):
    """清理單一片段的暫存檔。"""
    for pattern in [f'_hallucination_seg_{seg_idx}.wav',
                    f'_hallucination_seg_{seg_idx}.srt']:
        path = os.path.join(work_dir, pattern)
        if os.path.exists(path):
            os.remove(path)


if __name__ == '__main__':
    main()
