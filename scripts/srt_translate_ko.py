#!/usr/bin/env python3
"""繁中 SRT → 韓文 SRT（給 YouTube 獨立字幕軌）。

流程：整批帶前後文送 `claude -p`（訂閱額度，不走 API 計費）→ 逐批驗格式 → 組回原時間軸 → QC 報告。
時間軸不動；韓文語序不同，允許模型在相鄰條之間搬內容。

用法：
  srt_translate_ko.py run   <in.srt> --out-dir DIR [--glossary terms_ko.txt] [--batch 80] [--ctx 6] [--jobs 4] [--model opus]
術語表：`--glossary` 或環境變數 `SRT_KO_TERMS`，格式每行「中文<TAB>韓文<TAB>備註」；沒給就不帶術語段。
  srt_translate_ko.py assemble <in.srt> --out-dir DIR --out <out_ko.srt>
run 會跳過 DIR 裡已有合法輸出的批次（可續跑）。
"""
import argparse, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

GLOSSARY = None  # 由 --glossary 或 SRT_KO_TERMS 指定；術語檔因人而異，不隨 repo 發佈
CJK = re.compile(r"[一-鿿㐀-䶿]")
FULLWIDTH = re.compile(r"[，。？！、：；「」『』（）]")
MAX_LINE = 16
MAX_CPS = 12.0


def parse_srt(path):
    cues = []
    for block in re.split(r"\n\s*\n", Path(path).read_text(encoding="utf-8").strip()):
        lines = block.split("\n")
        if len(lines) < 3:
            continue
        a, b = lines[1].split(" --> ")
        cues.append({"i": len(cues) + 1, "start": a.strip(), "end": b.strip(),
                     "dur": ts(b) - ts(a), "zh": " ".join(l.strip() for l in lines[2:])})
    return cues


def ts(s):
    h, m, r = s.strip().split(":")
    sec, ms = r.split(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000


def glossary_text():
    if not GLOSSARY:
        return "（本次沒有術語表）"
    rows = []
    for line in Path(GLOSSARY).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        row = f"{parts[0]} → {parts[1]}"
        if len(parts) > 2 and parts[2].strip():
            row += f"（{parts[2].strip()}）"
        rows.append(row)
    return "\n".join(rows)


def build_prompt(before, batch, after):
    fmt = lambda c: f"[{c['i']}] ({c['dur']:.1f}s) {c['zh']}"
    return f"""你是韓文字幕譯者。把台灣財經 YouTube 講者 Austin 的繁體中文字幕翻成韓文字幕。這是給 YouTube 獨立字幕軌，觀眾是韓國散戶投資人。

規則：
1. 只輸出一個 JSON 陣列，每個元素是 {{"i": 索引, "ko": "韓文"}}。索引必須和「待翻譯」區段完全一樣：一個不多、一個不少、順序相同。不要任何說明文字，不要 markdown 圍欄。
2. 韓文動詞在句尾，先看整句再翻。允許把內容在相鄰字幕之間搬動，讓每條的韓文長度對得上該條秒數（每秒最多 {MAX_CPS:.0f} 個韓文字，含空格）。每條都要有內容，不可以空字串。
3. 敬語一律「해요체」，全片一致。不用「-습니다」，不用半語。
4. 中文口頭禪（那個、就是、喔、來、對不對、好、齁）不翻、直接省略，語意不變。重複的詞只翻一次。
5. 標點用半形（, ? !），句尾不加句號。不可出現任何漢字或中文字。
6. 每條最多兩行、每行最多 {MAX_LINE} 個字（含空格）。超過就在空格處換行，換行用 \\n。
7. 股票代號和英文縮寫（AAPL、S&P、FOMC）保留英文。數字用阿拉伯數字，單位用 억/만。
8. 韓國慣例是紅漲藍跌：黑K＝음봉、紅K＝양봉，不可直譯顏色。
9. 術語對照表必須照用：
{glossary_text()}

「上文」「下文」只給你理解語境，不要翻譯、不要輸出。

=== 上文 ===
{chr(10).join(map(fmt, before)) or '（無）'}

=== 待翻譯 ===
{chr(10).join(map(fmt, batch))}

=== 下文 ===
{chr(10).join(map(fmt, after)) or '（無）'}
"""


def validate(raw, batch):
    txt = raw.strip()
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        return None, "找不到 JSON 陣列"
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失敗: {e}"
    want = [c["i"] for c in batch]
    got = [d.get("i") for d in data]
    if got != want:
        return None, f"索引不符 want={want[0]}..{want[-1]} n={len(want)} got n={len(got)} first={got[:3]} last={got[-3:]}"
    for d in data:
        ko = d.get("ko", "")
        if not isinstance(ko, str) or not ko.strip():
            return None, f"[{d['i']}] 空字串"
        if CJK.search(ko):
            return None, f"[{d['i']}] 漏出漢字: {ko}"
        if FULLWIDTH.search(ko):
            return None, f"[{d['i']}] 全形標點: {ko}"
    return data, None


def call_claude(prompt, model, workdir, timeout=900):
    cmd = ["claude", "-p", "--model", model, "--tools", "", "--strict-mcp-config",
           "--disable-slash-commands", "--no-session-persistence", "--output-format", "text"]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=workdir)
    return r.returncode, r.stdout, r.stderr


def run_batch(k, before, batch, after, args, out_dir):
    ok_path = out_dir / f"batch_{k:03d}.json"
    if ok_path.exists():
        data, err = validate(ok_path.read_text(encoding="utf-8"), batch)
        if data is not None:
            return k, "skip", 0.0
    prompt = build_prompt(before, batch, after)
    (out_dir / f"batch_{k:03d}.prompt.txt").write_text(prompt, encoding="utf-8")
    err = None
    for attempt in (1, 2):
        t0 = time.time()
        p = prompt if attempt == 1 else prompt + f"\n\n上一次輸出不合格：{err}。請重新輸出完整 JSON 陣列。"
        try:
            rc, out, se = call_claude(p, args.model, out_dir)
        except subprocess.TimeoutExpired:
            rc, out, se = 124, "", "timeout"
        dt = time.time() - t0
        (out_dir / f"batch_{k:03d}.raw{attempt}.txt").write_text(f"rc={rc}\n{se}\n---\n{out}", encoding="utf-8")
        data, err = validate(out, batch)
        if data is not None:
            ok_path.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
            print(f"batch {k:03d} ok  {dt:.0f}s attempt={attempt}", flush=True)
            return k, "ok", dt
        print(f"batch {k:03d} FAIL attempt={attempt} rc={rc}: {err}", flush=True)
    return k, f"fail: {err}", 0.0


def cmd_run(args):
    cues = parse_srt(args.srt)
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    batches = [cues[i:i + args.batch] for i in range(0, len(cues), args.batch)]
    jobs = []
    for k, b in enumerate(batches):
        s, e = b[0]["i"] - 1, b[-1]["i"]
        jobs.append((k, cues[max(0, s - args.ctx):s], b, cues[e:e + args.ctx]))
    print(f"{len(cues)} 條 → {len(batches)} 批（每批 {args.batch}，前後文 {args.ctx}），並行 {args.jobs}，model={args.model}", flush=True)
    with ThreadPoolExecutor(args.jobs) as ex:
        results = list(ex.map(lambda j: run_batch(j[0], j[1], j[2], j[3], args, out_dir), jobs))
    fails = [r for r in results if r[1].startswith("fail")]
    print(f"完成 {sum(r[1]=='ok' for r in results)} 跳過 {sum(r[1]=='skip' for r in results)} 失敗 {len(fails)}；"
          f"總耗時 {sum(r[2] for r in results):.0f}s（並行前累加）")
    for r in fails:
        print("  ", r)
    return 1 if fails else 0


def wrap(t):
    """單行超過 MAX_LINE 字時，在最接近中點的空格切成兩行。"""
    if "\n" in t or len(t) <= MAX_LINE or " " not in t:
        return t
    mid = len(t) // 2
    cut = min((i for i, ch in enumerate(t) if ch == " "), key=lambda i: abs(i - mid))
    return t[:cut] + "\n" + t[cut + 1:]


def cmd_assemble(args):
    cues = parse_srt(args.srt)
    out_dir = Path(args.out_dir)
    ko = {}
    for p in sorted(out_dir.glob("batch_*.json")):
        for d in json.loads(p.read_text(encoding="utf-8")):
            ko[d["i"]] = wrap(d["ko"].replace("\\n", "\n").strip())
    missing = [c["i"] for c in cues if c["i"] not in ko]
    if missing:
        print(f"缺 {len(missing)} 條: {missing[:10]}…", file=sys.stderr)
        return 1
    lines, qc = [], {"cps": [], "line_len": [], "lines3": [], "empty": [], "cjk": [], "fw": []}
    for n, c in enumerate(cues, 1):
        t = ko[c["i"]]
        lines += [str(n), f"{c['start']} --> {c['end']}", t, ""]
        if not t: qc["empty"].append(c["i"]); continue
        if CJK.search(t): qc["cjk"].append(c["i"])
        if FULLWIDTH.search(t): qc["fw"].append(c["i"])
        cps = len(t.replace("\n", "")) / c["dur"] if c["dur"] > 0 else 99
        if cps > MAX_CPS: qc["cps"].append((round(cps, 1), c["i"], t.replace("\n", "|")))
        ls = t.split("\n")
        if len(ls) > 2: qc["lines3"].append(c["i"])
        if max(len(l) for l in ls) > MAX_LINE: qc["line_len"].append((max(len(l) for l in ls), c["i"], t.replace("\n", "|")))
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    rep = [f"總條數 {len(cues)}",
           f"空白 {len(qc['empty'])}  漢字外漏 {len(qc['cjk'])}  全形標點 {len(qc['fw'])}  超過兩行 {len(qc['lines3'])}",
           f"語速 >{MAX_CPS:.0f} 字/秒：{len(qc['cps'])} 條", *[f"  {x}" for x in sorted(qc["cps"], reverse=True)[:15]],
           f"單行 >{MAX_LINE} 字：{len(qc['line_len'])} 條", *[f"  {x}" for x in sorted(qc["line_len"], reverse=True)[:15]]]
    Path(args.out).with_suffix(".qc.txt").write_text("\n".join(rep), encoding="utf-8")
    print("\n".join(rep))
    print(f"→ {args.out}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("srt"); r.add_argument("--out-dir", required=True)
    r.add_argument("--batch", type=int, default=80); r.add_argument("--ctx", type=int, default=6)
    r.add_argument("--jobs", type=int, default=4); r.add_argument("--model", default="opus")
    r.add_argument("--glossary", default=os.environ.get("SRT_KO_TERMS"))
    a = sub.add_parser("assemble"); a.add_argument("srt"); a.add_argument("--out-dir", required=True); a.add_argument("--out", required=True)
    args = ap.parse_args()
    global GLOSSARY
    GLOSSARY = getattr(args, "glossary", None)
    if GLOSSARY and not Path(GLOSSARY).exists():
        sys.exit(f"術語表不存在: {GLOSSARY}")
    sys.exit(cmd_run(args) if args.cmd == "run" else cmd_assemble(args))


if __name__ == "__main__":
    main()
