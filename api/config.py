# -*- coding: utf-8 -*-
"""服务配置：全部可用环境变量覆盖，代码里只放默认值。
判定规则（verdict）集中在本文件，改口径只动这里。
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 指纹库：显式指到统一库（fingerprint.DEFAULT_BANK 默认是 gpt_bank.json，是坑，绝不省略）
BANK_PATH = Path(os.environ.get("DZ_BANK_PATH", ROOT / "data" / "unified_bank.json"))

# 监听（容器内 0.0.0.0:8100）
HOST = os.environ.get("DZ_HOST", "0.0.0.0")
PORT = int(os.environ.get("DZ_PORT", "8100"))

# 历史留底 sqlite（docker-compose 挂卷时指向卷内路径）
HISTORY_DB = Path(os.environ.get("DZ_HISTORY_DB", ROOT / "data" / "dongzhen_history.sqlite3"))

# 单次 /probe 内的题间并发（继承 mt_cli：轮内一次发完不补题）
PROBE_CONCURRENCY = int(os.environ.get("DZ_PROBE_CONCURRENCY", "3"))
# /probe_batch 的目标间并发
BATCH_CONCURRENCY = int(os.environ.get("DZ_BATCH_CONCURRENCY", "4"))

# ---- verdict 判定规则（GPT 线口径，2026-11 定谳，勿随口改）----
# 依据 tools/confusion_analysis.py 的量化分析（docs/family-branch-research.md）：
#   库内自匹配 top1-top2 分差（z 分数）k=3 时 p5=0.78；库外强归分差中位 ~0.1、p5<=0.11。
#   分差阈值取 0.4 可以把库外强归几乎全部拦成 unknown，库内误拦 <5%。
VERDICT_RULES = {
    # 1) top1-top2 融合分差 < 此值 -> unknown（疑似库外强归，不硬判）
    "ambiguous_gap_score": 0.40,
    # 2) top_p >= 此值 -> 自匹配成立 -> clean
    "self_match_p": 0.85,
    # 3) top 模型名含这些子串 -> 直接按口径判（先于 p 值）
    "degraded_substrings": ["luna"],   # 判出 luna 族 = 真降智
    "clean_substrings": ["astra"],     # 判出 astra = 正常没降智
    # 4) 都不命中 -> unknown
}

SERVICE_NAME = "dongzhen"
SERVICE_TITLE = "洞真（dongzhen）模型指纹探针服务"
SERVICE_VERSION = "0.1.0"
