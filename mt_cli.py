# -*- coding: utf-8 -*-
"""洞真（dongzhen）探针 CLI：直接调指纹管线对指定 base_url/model 做归因。
用法: python mt_cli.py [model_name ...]   # 不传参默认只测 gpt-6-astra
环境变量: MT_BASE_URL, MT_API_KEY, MT_TEMPERATURE(可选)
行为(11-10 定稿): 每模型并发发 3 题、一次发完不串行补题，按有效样本归因。
"""
import io
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path

from fingerprint import (
    load_bank,
    generate_challenges,
    parse_numbers,
    analyze_global_outputs,
)
from enrollment import request_completion

BASE_URL = os.environ.get("MT_BASE_URL", "https://api.duojie.games/v1")
API_KEY = os.environ.get("MT_API_KEY", "")
TEMPERATURE = float(os.environ["MT_TEMPERATURE"]) if os.environ.get("MT_TEMPERATURE") else None
DEFAULT_MODELS = ["gpt-6-astra"]
CHALLENGE_COUNT = 3

bank = load_bank(Path(__file__).with_name("data") / "unified_bank.json")

for model in sys.argv[1:] or DEFAULT_MODELS:
    print(f"\n===== {model} =====", flush=True)
    try:
        challenges = generate_challenges(CHALLENGE_COUNT)
        outputs = []
        errors = []

        def run(challenge):
            text = request_completion(
                BASE_URL, API_KEY, model, challenge["prompt"], TEMPERATURE, "openai"
            )
            minimum = max(80, math.ceil(challenge["expected_count"] * 0.55))
            parsed_count = len(parse_numbers(text))
            if parsed_count >= minimum:
                return {"text": text, "expected_count": challenge["expected_count"]}, None
            return None, f"有效数字不足：{parsed_count}/{minimum}"

        with ThreadPoolExecutor(max_workers=CHALLENGE_COUNT) as pool:
            futures = [pool.submit(run, challenge) for challenge in challenges]
            for future in as_completed(futures):
                try:
                    output, error = future.result()
                except Exception as exc:
                    errors.append(str(exc))
                    continue
                if output is not None:
                    outputs.append(output)
                else:
                    errors.append(error)

        result = analyze_global_outputs(outputs, bank)
    except Exception as error:
        print(f"失败: {error}")
        continue
    print(f"有效回答 {len(outputs)}/{CHALLENGE_COUNT}（并发一次发完，不补题）")
    if errors:
        for err in errors:
            print(f"  错误: {err}")
    print(f"家族判定: {result.get('family_prediction_name')} (p={result.get('family_probability'):.4f})")
    for fam in result.get("family_probabilities", []):
        if fam.get("probability", 0) > 0.005:
            print(f"  家族 {fam.get('family_name', fam.get('family'))}: {fam['probability']:.4f}")
    for item in result.get("results", [])[:6]:
        if item.get("probability", 0) > 0.005:
            print(f"  模型 {item['model']}: p={item['probability']:.4f}")
    note = result.get("reliability_note") or result.get("note")
    if note:
        print(f"备注: {note}")
