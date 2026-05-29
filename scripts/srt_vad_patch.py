#!/usr/bin/env python3
"""
srt_vad_patch.py — 用 Silero VAD 偵測 Whisper 漏轉錄的段落並自動補救

流程：
  1. Silero VAD 掃描音訊，找出所有有語音的時間段
  2. 與 SRT 字幕比對，找出「有語音但沒字幕」的缺口（gaps）
  3. 用 ffmpeg 切出缺口段落（前後各加 buffer）
  4. 用 mlx_whisper 補跑缺口段落
  5. 把補跑結果插入原始 SRT，按時間排序、重新編號
  6. 第二次比對確認，若仍有缺口只產出報告不再重跑

用法：
    python3 srt_vad_patch.py audio.wav input.srt [--output patched.srt]

需求：
    pip install silero-vad torch torchaudio
    brew install ffmpeg
    pip install mlx-whisper    （你應該已經有了）

作者：Fred's subtitle pipeline
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


# ============================================================
# 資料結構
# ============================================================

@dataclass
class TimeSegment:
    """一個時間段"""
    start: float  # 秒
    end: float    # 秒

    @property
    def duration(self) -> float:
        return self.end - self.start

    def overlaps(self, other: 'TimeSegment') -> bool:
        return self.start < other.end and other.start < self.end

    def overlap_duration(self, other: 'TimeSegment') -> float:
        if not self.overlaps(other):
            return 0.0
        return min(self.end, other.end) - max(self.start, other.start)

    def __repr__(self):
        return f"[{format_time(self.start)} → {format_time(self.end)}] ({self.duration:.1f}s)"


@dataclass
class SubtitleEntry:
    """一條 SRT 字幕"""
    index: int
    start: float   # 秒
    end: float      # 秒
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start

    def as_segment(self) -> TimeSegment:
        return TimeSegment(self.start, self.end)

    def to_srt(self, new_index: int = None) -> str:
        idx = new_index if new_index is not None else self.index
        start_ts = seconds_to_srt_timestamp(self.start)
        end_ts = seconds_to_srt_timestamp(self.end)
        return f"{idx}\n{start_ts} --> {end_ts}\n{self.text}\n"


@dataclass
class GapInfo:
    """一個偵測到的缺口"""
    segment: TimeSegment
    status: str = "detected"  # detected, patched, unresolved


# ============================================================
# 時間格式工具
# ============================================================

def format_time(seconds: float) -> str:
    """格式化為 HH:MM:SS.s"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"


def seconds_to_srt_timestamp(seconds: float) -> str:
    """轉成 SRT 時間戳格式 HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt_timestamp_to_seconds(ts: str) -> float:
    """SRT 時間戳轉秒數"""
    ts = ts.strip().replace(',', '.')
    match = re.match(r'(\d+):(\d+):(\d+)\.(\d+)', ts)
    if not match:
        raise ValueError(f"無法解析時間戳: {ts}")
    h, m, s, frac = match.groups()
    frac = frac.ljust(3, '0')[:3]
    return int(h) * 3600 + int(m) * 60 + int(s) + int(frac) / 1000


# ============================================================
# SRT 解析
# ============================================================

def parse_srt(filepath: str) -> list[SubtitleEntry]:
    """解析 SRT 檔案"""
    entries = []
    content = Path(filepath).read_text(encoding='utf-8-sig')
    # 用空行分割每個條目
    blocks = re.split(r'\n\s*\n', content.strip())

    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue

        # 找序號行和時間碼行
        try:
            index = int(lines[0].strip())
        except ValueError:
            continue

        time_match = re.match(
            r'(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})',
            lines[1].strip()
        )
        if not time_match:
            continue

        start = srt_timestamp_to_seconds(time_match.group(1))
        end = srt_timestamp_to_seconds(time_match.group(2))
        text = '\n'.join(lines[2:]).strip()

        entries.append(SubtitleEntry(index=index, start=start, end=end, text=text))

    return entries


def write_srt(entries: list[SubtitleEntry], filepath: str):
    """寫出 SRT 檔案，自動重新編號"""
    with open(filepath, 'w', encoding='utf-8') as f:
        for i, entry in enumerate(entries, 1):
            f.write(entry.to_srt(new_index=i))
            f.write('\n')


# ============================================================
# Silero VAD 偵測
# ============================================================

def run_vad(audio_path: str, merge_gap: float = 2.0, min_duration: float = 1.0) -> list[TimeSegment]:
    """
    用 Silero VAD 偵測音訊中的語音段落。

    Args:
        audio_path: 音訊檔案路徑
        merge_gap: 相鄰語音段間隔小於此秒數則合併
        min_duration: 最短語音段秒數，短於此的丟棄
    """
    print(f"🎙️  正在用 Silero VAD 掃描音訊...")

    try:
        import torch
        from silero_vad import load_silero_vad, read_audio, get_speech_timestamps
    except ImportError:
        print("❌ 缺少 silero-vad。請安裝：")
        print("   pip install silero-vad torch torchaudio")
        sys.exit(1)

    torch.set_num_threads(1)  # VAD 很輕量，單執行緒即可

    model = load_silero_vad()

    # 讀取音訊（Silero 需要 16kHz）
    # 如果音訊不是 wav/16kHz，先用 ffmpeg 轉換
    wav_16k_path = _ensure_wav_16k(audio_path)

    wav = read_audio(wav_16k_path, sampling_rate=16000)

    # 取得語音時間戳（以秒為單位）
    speech_timestamps = get_speech_timestamps(
        wav, model,
        sampling_rate=16000,
        return_seconds=True,
        threshold=0.5,             # 語音偵測閾值（預設 0.5）
        min_speech_duration_ms=250, # 最短語音段 250ms
        min_silence_duration_ms=300 # 最短靜音段 300ms
    )

    # 清理臨時檔案
    if wav_16k_path != audio_path and wav_16k_path.startswith(tempfile.gettempdir()):
        os.remove(wav_16k_path)

    # 轉換格式
    segments = [TimeSegment(start=ts['start'], end=ts['end']) for ts in speech_timestamps]

    print(f"   VAD 偵測到 {len(segments)} 個原始語音段")

    # 合併相鄰段落（間隔小於 merge_gap 秒）
    segments = _merge_segments(segments, merge_gap)
    print(f"   合併後（gap < {merge_gap}s）：{len(segments)} 個語音段")

    # 過濾太短的段落
    segments = [s for s in segments if s.duration >= min_duration]
    print(f"   過濾後（duration >= {min_duration}s）：{len(segments)} 個語音段")

    if segments:
        total_speech = sum(s.duration for s in segments)
        print(f"   語音總時長：{format_time(total_speech)}")

    return segments


def _ensure_wav_16k(audio_path: str) -> str:
    """確保音訊是 16kHz WAV 格式，必要時用 ffmpeg 轉換"""
    # 檢查是否已經是 16kHz WAV
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json',
             '-show_streams', audio_path],
            capture_output=True, text=True
        )
        info = json.loads(result.stdout)
        for stream in info.get('streams', []):
            if stream.get('codec_type') == 'audio':
                sr = int(stream.get('sample_rate', 0))
                codec = stream.get('codec_name', '')
                if sr == 16000 and codec in ('pcm_s16le', 'pcm_f32le'):
                    return audio_path
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        pass

    # 需要轉換
    tmp_wav = os.path.join(tempfile.gettempdir(), 'vad_input_16k.wav')
    print(f"   正在轉換音訊為 16kHz WAV...")
    subprocess.run(
        ['ffmpeg', '-y', '-i', audio_path, '-ar', '16000', '-ac', '1',
         '-sample_fmt', 's16', tmp_wav],
        capture_output=True, check=True
    )
    return tmp_wav


def _merge_segments(segments: list[TimeSegment], max_gap: float) -> list[TimeSegment]:
    """合併相鄰的語音段（間隔小於 max_gap 秒）"""
    if not segments:
        return []

    merged = [TimeSegment(start=segments[0].start, end=segments[0].end)]
    for seg in segments[1:]:
        if seg.start - merged[-1].end <= max_gap:
            merged[-1].end = max(merged[-1].end, seg.end)
        else:
            merged.append(TimeSegment(start=seg.start, end=seg.end))
    return merged


# ============================================================
# 缺口偵測
# ============================================================

def find_gaps(vad_segments: list[TimeSegment],
              srt_entries: list[SubtitleEntry],
              min_coverage: float = 0.5,
              min_gap_duration: float = 2.0) -> list[GapInfo]:
    """
    找出「VAD 偵測到語音但 SRT 沒有覆蓋」的缺口。

    Args:
        vad_segments: VAD 偵測到的語音段落
        srt_entries: SRT 字幕條目
        min_coverage: VAD 語音段被 SRT 覆蓋的最低比例（低於此算缺口）
        min_gap_duration: 最短缺口秒數（短於此不報告）
    """
    srt_segments = [e.as_segment() for e in srt_entries]
    gaps = []

    for vad_seg in vad_segments:
        # 計算這個 VAD 語音段被 SRT 覆蓋了多少
        total_overlap = 0.0
        for srt_seg in srt_segments:
            total_overlap += vad_seg.overlap_duration(srt_seg)

        coverage = total_overlap / vad_seg.duration if vad_seg.duration > 0 else 1.0

        if coverage < min_coverage:
            # 找出具體哪些子區間沒被覆蓋
            uncovered = _find_uncovered_intervals(vad_seg, srt_segments)
            for interval in uncovered:
                if interval.duration >= min_gap_duration:
                    gaps.append(GapInfo(segment=interval))

    # 合併相鄰缺口
    if gaps:
        gaps.sort(key=lambda g: g.segment.start)
        merged_gaps = [gaps[0]]
        for gap in gaps[1:]:
            if gap.segment.start - merged_gaps[-1].segment.end <= 1.0:
                merged_gaps[-1].segment.end = max(merged_gaps[-1].segment.end, gap.segment.end)
            else:
                merged_gaps.append(gap)
        gaps = merged_gaps

    return gaps


def _find_uncovered_intervals(target: TimeSegment,
                               covers: list[TimeSegment]) -> list[TimeSegment]:
    """找出 target 中沒被 covers 覆蓋的子區間"""
    # 收集所有與 target 重疊的覆蓋段
    overlapping = []
    for c in covers:
        if target.overlaps(c):
            clip_start = max(target.start, c.start)
            clip_end = min(target.end, c.end)
            overlapping.append((clip_start, clip_end))

    if not overlapping:
        return [TimeSegment(start=target.start, end=target.end)]

    # 排序並合併覆蓋段
    overlapping.sort()
    merged_covers = [overlapping[0]]
    for start, end in overlapping[1:]:
        if start <= merged_covers[-1][1]:
            merged_covers[-1] = (merged_covers[-1][0], max(merged_covers[-1][1], end))
        else:
            merged_covers.append((start, end))

    # 找出未覆蓋的區間
    uncovered = []
    current = target.start
    for cover_start, cover_end in merged_covers:
        if current < cover_start:
            uncovered.append(TimeSegment(start=current, end=cover_start))
        current = max(current, cover_end)
    if current < target.end:
        uncovered.append(TimeSegment(start=current, end=target.end))

    return uncovered


# ============================================================
# 缺口補救：ffmpeg 切段 + mlx_whisper 重跑
# ============================================================

def patch_gaps(audio_path: str,
               srt_entries: list[SubtitleEntry],
               gaps: list[GapInfo],
               buffer_seconds: float = 5.0,
               whisper_model: str = "large-v3",
               no_speech_threshold: float = 0.3,
               language: str = "zh") -> list[SubtitleEntry]:
    """
    對每個缺口：切出音訊 → mlx_whisper 補跑 → 收集新字幕條目

    Args:
        audio_path: 原始音訊路徑
        srt_entries: 現有的 SRT 條目
        gaps: 要補救的缺口列表
        buffer_seconds: 切段時前後多加的秒數
        whisper_model: mlx_whisper 模型名
        no_speech_threshold: 降低此值讓 Whisper 更敏感
        language: 語言代碼
    """
    if not gaps:
        return srt_entries

    new_entries = list(srt_entries)  # 複製一份
    patched_count = 0

    with tempfile.TemporaryDirectory(prefix='vad_patch_') as tmpdir:
        for i, gap in enumerate(gaps):
            seg = gap.segment
            print(f"\n🔧 補救缺口 {i+1}/{len(gaps)}: {seg}")

            # 計算切段範圍（加 buffer，但不超出音訊頭尾）
            cut_start = max(0, seg.start - buffer_seconds)
            cut_end = seg.end + buffer_seconds

            # ffmpeg 切出片段
            segment_path = os.path.join(tmpdir, f"gap_{i:03d}.wav")
            try:
                subprocess.run(
                    ['ffmpeg', '-y', '-i', audio_path,
                     '-ss', str(cut_start), '-to', str(cut_end),
                     '-ar', '16000', '-ac', '1', segment_path],
                    capture_output=True, check=True
                )
            except subprocess.CalledProcessError as e:
                print(f"   ❌ ffmpeg 切段失敗: {e.stderr.decode()[:200]}")
                gap.status = "unresolved"
                continue

            # mlx_whisper 補跑（降低 no_speech_threshold）
            srt_output_path = os.path.join(tmpdir, f"gap_{i:03d}.srt")
            try:
                result = subprocess.run(
                    ['mlx_whisper', segment_path,
                     '--language', language,
                     '--model', whisper_model,
                     '--no-speech-threshold', str(no_speech_threshold),
                     '--compression-ratio-threshold', '3.0',
                     '--condition-on-previous-text', 'False',
                     '--output-format', 'srt',
                     '--output-dir', tmpdir],
                    capture_output=True, text=True, timeout=300
                )
            except subprocess.TimeoutExpired:
                print(f"   ❌ mlx_whisper 超時（5分鐘）")
                gap.status = "unresolved"
                continue
            except FileNotFoundError:
                print(f"   ❌ 找不到 mlx_whisper 指令，請確認已安裝")
                gap.status = "unresolved"
                break

            # 解析補跑結果
            # mlx_whisper 會在 output-dir 產生 gap_XXX.srt
            if not os.path.exists(srt_output_path):
                # 有些版本會加不同的副檔名
                possible = [f for f in os.listdir(tmpdir) if f.startswith(f"gap_{i:03d}") and f.endswith('.srt')]
                if possible:
                    srt_output_path = os.path.join(tmpdir, possible[0])
                else:
                    print(f"   ❌ mlx_whisper 未產出 SRT 檔案")
                    gap.status = "unresolved"
                    continue

            patch_entries = parse_srt(srt_output_path)

            if not patch_entries:
                print(f"   ⚠️  mlx_whisper 補跑後仍然沒有輸出")
                gap.status = "unresolved"
                continue

            # 調整時間偏移：patch 裡的時間是相對於 cut_start 的，要加回去
            for entry in patch_entries:
                entry.start += cut_start
                entry.end += cut_start

            # 只保留落在原始缺口範圍內的條目（buffer 區域的丟棄，避免重複）
            filtered = []
            for entry in patch_entries:
                entry_seg = entry.as_segment()
                # 至少有 50% 落在缺口範圍內
                overlap = seg.overlap_duration(entry_seg)
                if overlap > 0 and overlap / entry.duration >= 0.3:
                    filtered.append(entry)

            if filtered:
                new_entries.extend(filtered)
                gap.status = "patched"
                patched_count += 1
                print(f"   ✅ 補回 {len(filtered)} 條字幕")
            else:
                print(f"   ⚠️  補跑有結果但不在缺口範圍內")
                gap.status = "unresolved"

    # 按時間排序、重新編號
    new_entries.sort(key=lambda e: (e.start, e.end))

    print(f"\n📊 補救結果：{patched_count}/{len(gaps)} 個缺口已填補")

    return new_entries


# ============================================================
# 報告產出
# ============================================================

def generate_report(gaps: list[GapInfo], round_num: int) -> str:
    """產出缺口偵測報告"""
    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"VAD 缺口偵測報告（第 {round_num} 輪）")
    lines.append(f"{'='*60}")
    lines.append(f"偵測到 {len(gaps)} 個缺口：")
    lines.append("")

    for i, gap in enumerate(gaps, 1):
        seg = gap.segment
        status_icon = {"detected": "🔍", "patched": "✅", "unresolved": "⚠️"}
        icon = status_icon.get(gap.status, "?")
        lines.append(f"  {icon} 缺口 {i}: {seg} — {gap.status}")

    lines.append("")

    resolved = sum(1 for g in gaps if g.status == "patched")
    unresolved = sum(1 for g in gaps if g.status == "unresolved")
    lines.append(f"  已填補：{resolved}")
    lines.append(f"  未解決：{unresolved}")

    if unresolved > 0:
        lines.append("")
        lines.append("⚠️  以下缺口需要人工處理：")
        for i, gap in enumerate(gaps, 1):
            if gap.status == "unresolved":
                seg = gap.segment
                lines.append(f"  - {seg}")
                lines.append(f"    建議：用影片播放器跳到 {format_time(seg.start)}，聽一下講者是否真的有說話")

    return '\n'.join(lines)


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='用 Silero VAD 偵測 Whisper 漏轉錄並自動補救',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例：
  # 基本用法
  python3 srt_vad_patch.py audio.wav raw.srt

  # 指定輸出和 Whisper 模型
  python3 srt_vad_patch.py audio.wav raw.srt --output patched.srt --model large-v3

  # 只偵測不補救（快速檢查）
  python3 srt_vad_patch.py audio.wav raw.srt --detect-only

  # 調整 VAD 參數
  python3 srt_vad_patch.py audio.wav raw.srt --merge-gap 3.0 --min-gap 3.0
        """
    )
    parser.add_argument('audio', help='音訊檔案路徑（wav/mp3/m4a/...）')
    parser.add_argument('srt', help='SRT 字幕檔案路徑')
    parser.add_argument('--output', '-o', help='輸出 SRT 路徑（預設：原檔名_vad_patched.srt）')
    parser.add_argument('--report', '-r', help='報告輸出路徑（預設：原檔名_vad_report.txt）')
    parser.add_argument('--detect-only', action='store_true', help='只偵測缺口，不自動補救')
    parser.add_argument('--model', default='large-v3', help='mlx_whisper 模型（預設：large-v3）')
    parser.add_argument('--language', default='zh', help='語言代碼（預設：zh）')
    parser.add_argument('--merge-gap', type=float, default=2.0,
                        help='VAD 語音段合併間距秒數（預設：2.0）')
    parser.add_argument('--min-gap', type=float, default=2.0,
                        help='最短缺口秒數，短於此不報告（預設：2.0）')
    parser.add_argument('--min-coverage', type=float, default=0.5,
                        help='VAD 段被 SRT 覆蓋的最低比例（預設：0.5）')
    parser.add_argument('--buffer', type=float, default=5.0,
                        help='切段時前後多加的秒數（預設：5.0）')
    parser.add_argument('--no-speech-threshold', type=float, default=0.3,
                        help='補跑 Whisper 的 no-speech-threshold（預設：0.3，比原始的 0.6 更敏感）')

    args = parser.parse_args()

    # 設定輸出路徑
    srt_base = Path(args.srt).stem
    srt_dir = Path(args.srt).parent
    output_path = args.output or str(srt_dir / f"{srt_base}_vad_patched.srt")
    report_path = args.report or str(srt_dir / f"{srt_base}_vad_report.txt")

    # 檢查輸入檔案
    if not os.path.exists(args.audio):
        print(f"❌ 找不到音訊檔案: {args.audio}")
        sys.exit(1)
    if not os.path.exists(args.srt):
        print(f"❌ 找不到 SRT 檔案: {args.srt}")
        sys.exit(1)

    # 檢查 ffmpeg
    if not _command_exists('ffmpeg'):
        print("❌ 找不到 ffmpeg。請安裝：brew install ffmpeg")
        sys.exit(1)

    print(f"📂 音訊：{args.audio}")
    print(f"📂 字幕：{args.srt}")
    print()

    # ========== 步驟 1：解析 SRT ==========
    srt_entries = parse_srt(args.srt)
    print(f"📝 SRT 條目數：{len(srt_entries)}")
    if srt_entries:
        srt_total = srt_entries[-1].end - srt_entries[0].start
        print(f"   SRT 時間範圍：{format_time(srt_entries[0].start)} → {format_time(srt_entries[-1].end)} ({srt_total:.0f}s)")
    print()

    # ========== 步驟 2：VAD 偵測 ==========
    vad_segments = run_vad(args.audio, merge_gap=args.merge_gap)
    print()

    # ========== 步驟 3：比對找缺口 ==========
    print("🔍 正在比對 VAD 語音段 vs SRT 字幕...")
    gaps = find_gaps(
        vad_segments, srt_entries,
        min_coverage=args.min_coverage,
        min_gap_duration=args.min_gap
    )

    if not gaps:
        print("✅ 沒有偵測到漏轉錄的缺口！")
        report = generate_report(gaps, round_num=1)
        Path(report_path).write_text(report, encoding='utf-8')
        print(f"\n📄 報告已存到：{report_path}")
        return

    print(f"⚠️  偵測到 {len(gaps)} 個缺口：")
    for i, gap in enumerate(gaps, 1):
        print(f"   {i}. {gap.segment}")

    # ========== 步驟 4：偵測模式 or 補救模式 ==========
    if args.detect_only:
        print("\n（--detect-only 模式，不自動補救）")
        report = generate_report(gaps, round_num=1)
        Path(report_path).write_text(report, encoding='utf-8')
        print(f"\n📄 報告已存到：{report_path}")
        return

    # ========== 步驟 5：自動補救（第一輪）==========
    print(f"\n{'='*60}")
    print("🔄 第一輪自動補救")
    print(f"{'='*60}")

    # 檢查 mlx_whisper
    if not _command_exists('mlx_whisper'):
        print("❌ 找不到 mlx_whisper。請確認已安裝：pip install mlx-whisper")
        print("   改為只產出偵測報告。")
        report = generate_report(gaps, round_num=1)
        Path(report_path).write_text(report, encoding='utf-8')
        print(f"\n📄 報告已存到：{report_path}")
        return

    patched_entries = patch_gaps(
        audio_path=args.audio,
        srt_entries=srt_entries,
        gaps=gaps,
        buffer_seconds=args.buffer,
        whisper_model=args.model,
        no_speech_threshold=args.no_speech_threshold,
        language=args.language
    )

    # ========== 步驟 6：第二次比對確認 ==========
    print(f"\n{'='*60}")
    print("🔍 第二輪比對確認")
    print(f"{'='*60}")

    remaining_gaps = find_gaps(
        vad_segments, patched_entries,
        min_coverage=args.min_coverage,
        min_gap_duration=args.min_gap
    )

    if not remaining_gaps:
        print("✅ 所有缺口已填補！")
    else:
        print(f"⚠️  仍有 {len(remaining_gaps)} 個缺口未解決：")
        for i, gap in enumerate(remaining_gaps, 1):
            gap.status = "unresolved"
            print(f"   {i}. {gap.segment}")
        print("\n⛔ 已達最大自動補救次數（1 輪），不再重跑。")
        print("   請人工檢查上述時間段。")

    # ========== 步驟 7：寫出結果 ==========
    write_srt(patched_entries, output_path)
    print(f"\n📄 校正後 SRT：{output_path}")
    print(f"   條目數：{len(srt_entries)} → {len(patched_entries)}")

    # 合併兩輪的報告
    all_gaps = gaps + [g for g in remaining_gaps if g.status == "unresolved"]
    report = generate_report(all_gaps, round_num=2 if remaining_gaps else 1)
    Path(report_path).write_text(report, encoding='utf-8')
    print(f"📄 報告：{report_path}")

    # 總結
    print(f"\n{'='*60}")
    print("📊 總結")
    print(f"{'='*60}")
    round1_patched = sum(1 for g in gaps if g.status == "patched")
    round1_unresolved = sum(1 for g in gaps if g.status == "unresolved")
    round2_unresolved = len(remaining_gaps) if remaining_gaps else 0
    print(f"   第一輪偵測：{len(gaps)} 個缺口")
    print(f"   第一輪補救：{round1_patched} 個成功，{round1_unresolved} 個失敗")
    if remaining_gaps:
        print(f"   第二輪確認：仍有 {round2_unresolved} 個缺口（需人工處理）")
    else:
        print(f"   第二輪確認：全部通過 ✅")


def _command_exists(cmd: str) -> bool:
    """檢查系統指令是否存在"""
    try:
        subprocess.run(['which', cmd], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


if __name__ == '__main__':
    main()
