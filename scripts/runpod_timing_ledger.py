#!/usr/bin/env python3
"""累積「機器建立 → 直連埠就緒」的等待時間，供調整逾時上限用。

為什麼需要：RunPod 的直連 SSH 埠**大約一半機率不會生成**，這是已知的。
問題是逾時上限該設多久——設太短會把「還在生成中」的好機器砍掉，
設太長會在注定失敗的機器上白燒錢。

2026-08-30 靠人工撈 log 得到八筆，發現分佈是**雙峰**：
不是 40 秒內就緒（21/23/35/37/38 秒），就是永遠不會（431 秒、8-16 分鐘仍為 null）。
222 秒那筆是唯一離群值。**222 秒之後成功的案例：0 次。**

所以當時的結論是「420 秒維持，不要拉到 900」——拉長只會在注定失敗的機器上多燒錢。
但那個結論建立在 6 個成功樣本上，其中一個是離群值。**要降到 300 秒之前，
需要更多樣本。** 這支腳本讓樣本自己累積，不用每次重撈。

用法：
    runpod_timing_ledger.py <掃描目錄> [...]     掃描並更新帳本
    runpod_timing_ledger.py --stats              只看目前統計

帳本位置：~/.config/runpod/timing_ledger.tsv
"""

import json
import re
import statistics
import sys
from pathlib import Path

LEDGER = Path.home() / ".config/runpod/timing_ledger.tsv"
CREATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d\d:\d\d:\d\d).*pod_id=(\w+).*action=create result=success")
WAIT_RE = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d\d:\d\d:\d\d).*pod_id=(\w+).*action=direct_wait result=(\w+)")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _sec(hms: str) -> int:
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


# 觀察句轉秒數，例如 "appeared after 3.7 minutes" / "null after 16 minutes"
OBS_RE = re.compile(r"(?:appeared|null) after ([\d.]+)\s*(second|minute|hour)s?", re.I)


def scan_json(path: Path) -> dict:
    """讀早期人工整理的 JSON 格式（runs/direct-provisioning-*.json）。

    ⚠️ 這個支援不可以省：那份檔裡有唯一一筆 222 秒（3.7 分鐘）的成功樣本，
    而它是整個分佈的離群值。只掃 log 行的話帳本會說「最慢成功 38 秒」，
    有人據此把逾時降到 100 秒，就會把那種機器砍掉。
    **一個會漏掉已知資料的工具，比沒有工具危險。**
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    # 這些 JSON 有兩種形狀：{"pods":[...]} 與直接就是一個陣列。
    # 只認一種會靜默漏掉另一種——就是這支腳本第一版漏掉 222 秒那筆的原因。
    if isinstance(data, dict):
        pods = data.get("pods", [])
    elif isinstance(data, list):
        pods = data
    else:
        return {}
    out = {}
    for pod in pods:
        if not isinstance(pod, dict):
            continue
        pid, res, obs = pod.get("id"), pod.get("direct_result"), pod.get("observation", "")
        if not pid or not res:
            continue
        m = OBS_RE.search(obs)
        if not m:
            continue
        n, unit = float(m.group(1)), m.group(2).lower()
        sec = int(n * {"second": 1, "minute": 60, "hour": 3600}[unit])
        out[pid] = (path.stem[-8:] if path.stem[-8:].isdigit() else "2026-08-29",
                    "ready" if res == "success" else res, sec)
    return out


def scan(paths) -> dict:
    """回 {pod_id: (日期, 結果, 等待秒數)}。跨午夜的樣本直接丟掉，不猜。"""
    created, waited = {}, {}
    from_json = {}
    for root in paths:
        root = Path(root)
        files = root.rglob("*") if root.is_dir() else [root]
        for f in files:
            if not f.is_file() or f.suffix not in {".txt", ".log", ".output", ".json", ""}:
                continue
            if f.suffix == ".json":
                from_json.update(scan_json(f))
                continue
            try:
                text = ANSI_RE.sub("", f.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
            for line in text.splitlines():
                if (m := CREATE_RE.search(line)):
                    created[m.group(3)] = (m.group(1), m.group(2))
                elif (m := WAIT_RE.search(line)):
                    waited[m.group(3)] = (m.group(1), m.group(2), m.group(4))

    out = dict(from_json)
    skipped_no_create = []
    for pid, (wd, wt, res) in waited.items():
        if pid in out:
            continue
        if pid not in created:
            skipped_no_create.append(pid)
            continue
        cd, ct = created[pid]
        if cd != wd:  # 跨午夜，秒數算不準，寧可丟掉也不要污染樣本
            continue
        out[pid] = (cd, res, _sec(wt) - _sec(ct))

    # mock 不是真機器，混進來會讓「最快成功 0 秒」這種假數字污染分佈
    mocks = [k for k in out if "mock" in k.lower()]
    for k in mocks:
        del out[k]

    # 掃不到的要出聲。安靜地產出一份不完整的帳本，比沒有帳本危險——
    # 有人會據此調整逾時。
    if skipped_no_create:
        print(f"⚠️ 有 {len(skipped_no_create)} 台只找到 direct_wait 沒找到 create，"
              f"算不出等待秒數，已跳過：{skipped_no_create}", file=sys.stderr)
    if mocks:
        print(f"（略過 {len(mocks)} 筆 mock：{mocks}）", file=sys.stderr)
    return out


def load() -> dict:
    if not LEDGER.exists():
        return {}
    rows = {}
    for line in LEDGER.read_text(encoding="utf-8").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 4:
            rows[parts[1]] = (parts[0], parts[2], int(parts[3]))
    return rows


def save(rows: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    lines = ["date\tpod_id\tresult\telapsed_sec"]
    for pid, (d, res, el) in sorted(rows.items(), key=lambda kv: (kv[1][0], kv[1][2])):
        lines.append(f"{d}\t{pid}\t{res}\t{el}")
    LEDGER.write_text("\n".join(lines) + "\n", encoding="utf-8")


# 等多久之後才算「真的等過了」。超過這個秒數仍沒就緒，才當成強證據；
# 低於它的逾時只是「我們在那裡放棄了」。目前的逾時上限是 420，
# 所以 480 以上才算我們真的多等過。
CENSOR_THRESHOLD = 480


def stats(rows: dict) -> None:
    ok = sorted(el for _, res, el in rows.values() if res == "ready")
    stopped = sorted(el for _, res, el in rows.values() if res != "ready" and el < CENSOR_THRESHOLD)
    waited = sorted(el for _, res, el in rows.values() if res != "ready" and el >= CENSOR_THRESHOLD)

    print(f"帳本：{LEDGER}")
    print(f"  成功 {len(ok)} 筆、沒就緒 {len(stopped) + len(waited)} 筆")
    if ok:
        print(f"  ✅ 成功的等待秒數：{ok}")
        print(f"     最慢 {max(ok)} 秒" + (f"、中位數 {statistics.median(ok):.0f} 秒" if len(ok) > 1 else ""))
    print()
    print("  ⚠️ 沒就緒的那些要分兩類讀，混在一起會推出錯的結論：")
    if stopped:
        print(f"     【我們放棄了】{stopped}")
        print(f"       這些**不是**「那台在該秒數確定失敗」，是「我們在該秒數不等了」。")
        print(f"       所以**不能**拿最小值去說「谷底在 X 與 Y 之間」——右設限樣本。")
    if waited:
        print(f"     【真的等很久仍沒有】{waited}")
        print(f"       這些才是強證據。")

    if ok:
        cur = 420
        print()
        print(f"  目前逾時 {cur} 秒 = 最慢成功（{max(ok)} 秒）的 {cur / max(ok):.1f} 倍")
        if len(ok) < 20:
            print(f"  ⚠️ 成功樣本只有 {len(ok)} 筆，**還不足以調整逾時**（門檻 20 筆）")
            print(f"     維持 420 的理由是「資料無法證明更短是安全的」，")
            print(f"     **不是**「已經證明更短不安全」——這兩句話不一樣。")
        else:
            print(f"  ✅ 樣本 {len(ok)} 筆已達門檻，可以重新評估逾時值")


def main() -> int:
    args = sys.argv[1:]
    rows = load()
    if args and args[0] != "--stats":
        found = scan(args)
        new = {k: v for k, v in found.items() if k not in rows}
        rows.update(found)
        save(rows)
        print(f"掃描完成：新增 {len(new)} 筆、帳本共 {len(rows)} 筆\n")
    stats(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
