# -*- coding: utf-8 -*-
"""洞真（dongzhen）调试模式：逐题调用、保存全文、逐题单独归因（定位污染来源）。
用法: python mt_debug.py <n_challenges>
环境变量: MT_BASE_URL, MT_API_KEY
输出: debug_out/answer_N.txt 全文 + 控制台逐题归因
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path

from fingerprint import load_bank, analyze_global_outputs, generate_challenges, parse_numbers
from enrollment import request_completion

BASE_URL = os.environ.get("MT_BASE_URL", "http://127.0.0.1:8425/v1")
API_KEY = os.environ.get("MT_API_KEY", "")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3

bank = load_bank(Path(__file__).with_name("data") / "unified_bank.json")
challenges = generate_challenges(N)
os.makedirs("debug_out", exist_ok=True)

outputs = []
for i, ch in enumerate(challenges, 1):
    print(f"\n--- 挑战 {i}: 期望 {ch['expected_count']} 个数字 ---", flush=True)
    with open(f"debug_out/challenge_{i}.txt", "w", encoding="utf-8") as f:
        f.write(ch["prompt"])
    try:
        text = request_completion(BASE_URL, API_KEY, os.environ.get("MT_MODEL", "claude/claude-sonnet-5-5"),
                                  ch["prompt"], None, "openai")
    except Exception as e:
        print(f"失败: {e}")
        continue
    with open(f"debug_out/answer_{i}.txt", "w", encoding="utf-8") as f:
        f.write(text)
    nums = parse_numbers(text)
    print(f"全文 {len(text)} 字符，解析出 {len(nums)} 个数字")
    print(f"数字样本(前10): {nums[:10]}")
    tiny = [n for n in nums if abs(n) < 1000]
    print(f"小于1000的数字个数: {len(tiny)}  样本: {tiny[:15]}")
    print(f"开头200字符: {text[:200]!r}")
    outputs.append({"text": text, "expected_count": ch["expected_count"]})
    # 逐题单独归因
    try:
        single = analyze_global_outputs([outputs[-1]], bank)
        top = [(r["model"], round(r["probability"], 4)) for r in single["results"][:3]]
        print(f"单题归因: {top}")
    except Exception as e:
        print(f"单题归因失败: {e}")

if len(outputs) >= 2:
    print("\n=== 合并归因 ===")
    result = analyze_global_outputs(outputs, bank)
    for r in result["results"][:4]:
        print(f"  {r['model']}: p={r['probability']:.4f}")
