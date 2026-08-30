"""所有 shell 腳本都必須能在 macOS 內建的 bash 3.2 底下解析。

背景：macOS 內建的 /bin/bash 是 3.2.57（蘋果因授權問題二十年沒更新）。
腳本的 shebang 是 `#!/usr/bin/env bash`，在沒裝 homebrew bash 的機器上
就會指到它。2026-08-30 在 Mini CC（M1 8GB、macOS 15.4.1）實測，
cloud_asr.sh 的 `[[ -v INITIAL_PROMPT ]]` 讓整支腳本 exit 2，
雲端 ASR 完全跑不起來，而在有 brew bash 的開發機上完全看不出來。
"""

import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SHELL_SCRIPTS = sorted(SCRIPTS_DIR.glob("*.sh"))

# bash 3.2 之後才有的東西。分三類，因為三類的危險程度差很多。
#
# 最危險的是「靜默」那類：bash 3.2 解析得過、執行也不報錯，只是算出來的
# 東西是錯的。$EPOCHSECONDS 在 3.2 展開成空字串，算術把空字串當 0，
# 於是所有計時器都歸零而且完全沒有錯誤訊息。
#
# 這份清單由 Fable reviewer 在 2026-08-30 補齊（review-19）：我原本只掃了
# 「解析就炸」那五種，那是三類裡最不危險的一類——因為它至少會報錯。

# 第一類：靜默出錯（最危險，bash -n 抓不到，執行也不報錯）
SILENT_PATTERNS = [
    (re.compile(r"\$EPOCHSECONDS|\$\{EPOCHSECONDS"), "$EPOCHSECONDS 在 bash 3.2 展開成空字串，算術當成 0，計時器全錯且不報錯（需 5.0）"),
    (re.compile(r"\$EPOCHREALTIME|\$\{EPOCHREALTIME"), "$EPOCHREALTIME 同上（需 5.0）"),
    (re.compile(r"exec\s+\{[A-Za-z_]"), "exec {fd}> 在 bash 3.2 會真的建一個叫 {fd} 的檔案（需 4.1）"),
    (re.compile(r"shopt\s+-s\s+globstar"), "globstar 的 ** 在 bash 3.2 被當成普通的 *，會掃錯範圍（需 4.0）"),
    (re.compile(r"\$\{?BASH_ARGV0"), "$BASH_ARGV0 在 bash 3.2 是空的（需 5.0）"),
]

# 第二類：執行才炸（bash -n 抓不到，跑到那行才死）
RUNTIME_PATTERNS = [
    (re.compile(r"^\s*(declare|local|typeset)\s+-[Agn]", re.M), "declare -A / -g / local -n 需要 bash 4.0-4.3"),
    (re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(\^\^|\^|,,|,)\}"), "${VAR^^} / ${VAR,,} 大小寫展開需要 bash 4.0"),
    (re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:\s*-?\d*\s*:\s*-\d"), "${VAR:0:-1} 負數長度需要 bash 4.2"),
    (re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\[-\d"), "arr[-1] 負索引需要 bash 4.3"),
    (re.compile(r"\b(mapfile|readarray)\b"), "mapfile / readarray 需要 bash 4.0"),
    (re.compile(r"\bwait\s+-n\b"), "wait -n 需要 bash 4.3"),
    (re.compile(r"printf\s+.{0,20}%\(.*\)T"), "printf %(fmt)T 需要 bash 4.2"),
    (re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*@[QEPAa]\}"), "${VAR@Q} 之類的參數轉換需要 bash 4.4"),
]

# 第三類：解析就炸（最不危險，因為會直接報錯）
PARSE_PATTERNS = [
    (re.compile(r"\[\[\s*-v\s"), "[[ -v VAR ]] 需要 bash 4.2；請改用 [[ -n \"${VAR+x}\" ]]"),
    (re.compile(r"&>>"), "&>> 需要 bash 4.0；請改用 >>file 2>&1"),
]

# 第四類：GNU 專屬指令。macOS 用的是 BSD 版，旗標不一樣。
# 這跟 bash 版本無關，但同樣是「開發機能跑、目標機不能跑」，一起掃。
GNU_ONLY_PATTERNS = [
    (re.compile(r"\bdate\s+.{0,30}-d\s"), "date -d 是 GNU 專屬；BSD date 用 -v 或 -j -f"),
    (re.compile(r"\bsed\s+-[a-zA-Z]*r\b"), "sed -r 是 GNU 專屬；兩邊都吃的是 sed -E"),
    (re.compile(r"\bgrep\s+-[a-zA-Z]*P\b"), "grep -P 是 GNU 專屬；BSD grep 沒有 PCRE"),
    (re.compile(r"\bsort\s+-[a-zA-Z]*V\b"), "sort -V 是 GNU 專屬"),
    (re.compile(r"\b(sha256sum|md5sum|sha1sum)\b"), "sha256sum 是 GNU 專屬；macOS 用 shasum -a 256"),
    (re.compile(r"\breadlink\s+-f\b"), "readlink -f 在舊版 macOS 不支援"),
    (re.compile(r"\bsed\s+-i\s+''"), "sed -i '' 是 BSD 專屬；GNU sed 會把 '' 當檔名。兩邊都吃的是 sed -i.bak"),
]

BASH4_PATTERNS = SILENT_PATTERNS + RUNTIME_PATTERNS + PARSE_PATTERNS + GNU_ONLY_PATTERNS


def _bash32() -> str:
    """回傳 macOS 內建 bash 3.2 的路徑，沒有就跳過測試。"""
    out = subprocess.run(
        ["/bin/bash", "--version"], capture_output=True, text=True, check=False
    ).stdout
    if not out.startswith("GNU bash, version 3."):
        pytest.skip(f"/bin/bash 不是 3.x，跳過：{out.splitlines()[0] if out else '無輸出'}")
    return "/bin/bash"


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_parses_under_bash32(script: Path) -> None:
    """bash 3.2 要能解析（-n 只解析不執行）。"""
    bash = _bash32()
    r = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"{script.name} 在 bash 3.2 下解析失敗：\n{r.stderr}"


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_no_bash4_only_syntax(script: Path) -> None:
    """光靠 bash -n 抓不到全部——有些 bash 4 語法在 3.2 底下解析得過、
    執行才炸。所以再做一次靜態掃描。"""
    text = script.read_text(encoding="utf-8")
    # 去掉註解行，避免說明文字裡出現的範例被誤判
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    found = [msg for pat, msg in BASH4_PATTERNS if pat.search(body)]
    assert not found, f"{script.name} 在 bash 3.2 / BSD 工具下會出問題：\n  " + "\n  ".join(found)


# ============================================================
# 規則本身的體檢
# ============================================================
# 沒有紅過的規則可能只是正規表示式寫錯了。下面兩條測試確保每條規則
# 真的抓得到它宣稱要抓的東西，而且不會誤傷正常寫法。
# 加規則時同時往這兩份清單加樣本。

SHOULD_MATCH = [
    ("EPOCHSECONDS", "now=$EPOCHSECONDS"),
    ("EPOCHREALTIME", "t=$EPOCHREALTIME"),
    ("exec {fd}", "exec {fd}>/tmp/x"),
    ("globstar", "shopt -s globstar"),
    ("BASH_ARGV0", 'echo "$BASH_ARGV0"'),
    ("declare -A", "declare -A map"),
    ("local -n", "local -n ref=x"),
    ("declare -g", "declare -g GLOBAL=1"),
    ("${VAR^^}", 'echo "${name^^}"'),
    ("${VAR:0:-1}", 'echo "${s:0:-1}"'),
    ("arr[-1]", 'echo "${arr[-1]}"'),
    ("mapfile", "mapfile -t lines < f"),
    ("readarray", "readarray -t lines < f"),
    ("wait -n", "wait -n"),
    ("printf %()T", 'printf "%(%F)T\\n" -1'),
    ("${VAR@Q}", 'echo "${v@Q}"'),
    ("[[ -v ]]", "if [[ -v FOO ]]; then :; fi"),
    ("&>>", "cmd &>> log.txt"),
    ("date -d", 'date -d "2 days ago"'),
    ("sed -r", 'sed -r "s/a/b/" f'),
    ("grep -P", 'grep -P "\\d+" f'),
    ("sort -V", "sort -V versions.txt"),
    ("sha256sum", "sha256sum f"),
    ("readlink -f", 'readlink -f "$0"'),
    ("sed -i ''", "sed -i '' 's/a/b/' f"),
]

# 這些是 bash 3.2 與 BSD 工具都吃的正常寫法，一條都不可以被標紅
SHOULD_NOT_MATCH = [
    'sed -i.bak "s/a/b/" f',
    'grep -E "a|b" f',
    "sort -n nums.txt",
    "shasum -a 256 f",
    'local var="x"',
    "declare -a arr",
    'echo "${VAR:-default}"',
    'echo "${VAR#prefix}"',
    'wait "$pid"',
    'date "+%H:%M:%S"',
    'printf "%s\\n" "$x"',
]


@pytest.mark.parametrize("label,snippet", SHOULD_MATCH, ids=[c[0] for c in SHOULD_MATCH])
def test_rule_actually_fires(label: str, snippet: str) -> None:
    """每條規則都要抓得到它宣稱要抓的東西。"""
    assert any(pat.search(snippet) for pat, _ in BASH4_PATTERNS), (
        f"沒有任何規則抓到 {label}——規則的正規表示式可能寫錯了。樣本：{snippet}"
    )


@pytest.mark.parametrize("snippet", SHOULD_NOT_MATCH)
def test_no_false_positive(snippet: str) -> None:
    """bash 3.2 與 BSD 都吃的正常寫法不可以被標紅。"""
    hits = [msg for pat, msg in BASH4_PATTERNS if pat.search(snippet)]
    assert not hits, f"誤判正常寫法 {snippet!r}：{hits}"
