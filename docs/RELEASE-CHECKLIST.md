# 發版檢查清單

> 這份存在的原因：v1.8.0 發出去之後才發現**發布的那個版本從來沒有在目標機器上跑過**。
> 實測用的是中途的 commit（82dd30f），之後又改了 5 次才打標籤。
> **測的東西不等於發的東西。**
>
> 補跑之後立刻抓到一個會誤導使用者的 bug：沒裝 MLX 的機器上印「原因：記憶體不足」，
> 真因是「套件沒裝」——照那個訊息，使用者會去買記憶體。

## 順序（最後一步不可以省略也不可以提前）

1. **README** 有沒有需要更新（新旗標、新環境變數、行為改變）
2. **版本號** — `SKILL.md` 的 `version:` 欄位
3. **CHANGELOG** — 含「Known issues」，把**沒做到的**寫進去，不要只寫做到的
4. **全套測試** — `python3 -m pytest -q`
5. **commit + push**
6. **打標籤 + `gh release create`**
7. ⚠️ **在目標機器上跑發布的那個標籤** ← **最後一步，不可省**

## 第 7 步的具體做法

「目標機器」指的是**跟開發機不一樣的那種機器**。本專案目前是
Mac mini M1、8 GB、macOS 15.4.1，**沒有 MLX、沒有 coreutils、只有 bash 3.2**。
開發機是 M1 Max 32GB，有 homebrew bash、有 coreutils、有 MLX——
**開發機跑得過的東西，那台不一定跑得過**。v1.8.0 修的六個 bug 全部屬於這一類。

```bash
ssh <目標機器> '
  cd ~/dev/srt-skill
  git fetch --tags origin
  git checkout <tag>
  # ⚠️ 一定要印版本確認切成功了。
  # checkout 可能因為本機有改動而 abort，那時後面所有測試都是跑在舊版上。
  echo "版本: $(git describe --tags) ($(git rev-parse --short HEAD))"
  [ "$(git describe --tags)" = "<tag>" ] || { echo "❌ 切版本失敗，停"; exit 1; }

  # 免費的四項
  for f in scripts/*.sh; do /bin/bash -n "$f" || exit 1; done   # bash 3.2 能解析
  bash scripts/runpod_reap.sh                                    # 新工具存在且能跑
  python scripts/asr_capacity_check --no-cache                   # 探針能跑
  env -u SRT_ASR_ENGINE bash scripts/subtitle.sh <某個檔>        # 沒指定引擎時的訊息對不對

  # 花錢的一項（約 US$0.02）
  bash scripts/subtitle.sh <60秒的clip> --breeze --engine=runpod
  bash scripts/runpod_reap.sh                                    # 確認機器砍乾淨
'
```

**那個 `echo 版本` 不是裝飾。** v1.8.0 補跑時 `git checkout` 因為本機有未提交的改動
而 abort，我差點拿舊版的測試結果回報「發布版驗過了」。是因為印了版本號才發現。

## 兩種介面測試都要留

發布前後都會遇到「兩支程式各自正確、但它們之間的約定沒有主人」這類 bug。
兩種測法互補，不要只做一種：

| 測法 | 做什麼 | 抓得到什麼 |
|---|---|---|
| **靜態** | 掃消費端實際會取哪些鍵，斷言生產端每個都輸出且非空 | 鍵名打錯、少輸出一個鍵 |
| **動態** | **讓真正的消費者實際吃一次產物**，看它回報的數字對不對 | 格式對但語意錯、下游其實讀不動 |

動態那種比較誠實——把 VV 的 JSON 直接餵 `srt_prepare_segments.py`，
看 `vv_segments` 是不是 170、每段有沒有 `_vv_ref_N.txt`，
比逐一比對鍵名可信。但靜態那種跑得快、能進 CI，兩個都留。
