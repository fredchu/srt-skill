import os
import sys

# srt skill 的腳本在 scripts/，tests 在 tests/；把 scripts/ 注入 sys.path
# 讓 test 能 import terminology_rules / postprocess_srt 等模組。
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))
