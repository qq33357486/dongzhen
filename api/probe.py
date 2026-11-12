# -*- coding: utf-8 -*-
"""探针管线封装：挑战生成 -> 轮内并发采集（一次发完不补题，继承 mt_cli 口径）-> 归因 -> verdict。
api_key 只在本模块内存中流转，绝不写日志/落盘（sqlite 只收 mask 后的 key，见 store.py）。
"""
from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from fingerprint import (
    analyze_global_outputs,
    generate_challenges,
    load_bank,
    parse_numbers,
)
from enrollment import request_completion

from api.config import PROBE_CONCURRENCY, VERDICT_RULES
from api.verdict import build_pinned_info, decide_verdict

# token 估算口径（与 enroll_cli 一致）：输出 ~3 token/整数，输入 ~0.4K/题
TOKEN_PER_NUMBER = 3
INPUT_TOKEN_PER_TASK = 400


class BankHolder:
    """库只加载一次；换库文件需重启服务（改库本来就要求显式重建+重启）。"""

    def __init__(self, bank_path):
        self.path = bank_path
        self.bank = load_bank(self.path)
        self.model_ids = [model["id"] for model in self.bank["models"]]


def run_one_challenge(
    base_url: str,
    api_key: str,
    model: str,
    challenge: dict,
    temperature: float | None,
    timeout_s: int,
) -> tuple[dict | None, str | None]:
    """发一题并按有效性阈值过滤。返回 (output, error)，二选一非空。"""
    text = request_completion(
        base_url, api_key, model, challenge["prompt"], temperature, "openai", "", timeout_s
    )
    minimum = max(80, math.ceil(challenge["expected_count"] * 0.55))
    parsed_count = len(parse_numbers(text))
    if parsed_count >= minimum:
        return {"text": text, "expected_count": challenge["expected_count"]}, None
    return None, f"{challenge['id']} 有效数字不足：{parsed_count}/{minimum}"


def run_probe(
    holder: BankHolder,
    base_url: str,
    api_key: str,
    model: str,
    pin: str | None = None,
    rounds: int = 1,
    questions: int = 3,
    timeout_s: int = 240,
    temperature: float | None = None,
    rules: dict | None = None,
) -> dict:
    """跑一次完整探针。全部题目失败时抛 RuntimeError（调用方决定 HTTP 语义）。"""
    started = time.time()
    errors: list[str] = []
    outputs: list[dict] = []          # 全部有效输出（总体归因用）
    round_chunks: list[list[dict]] = []  # 每轮各自的有效输出（轮级归因用）
    total_questions = rounds * questions

    for round_index in range(1, rounds + 1):
        challenges = generate_challenges(questions)
        round_outputs: list[dict] = []
        with ThreadPoolExecutor(max_workers=min(PROBE_CONCURRENCY, questions)) as pool:
            futures = [
                pool.submit(run_one_challenge, base_url, api_key, model, challenge, temperature, timeout_s)
                for challenge in challenges
            ]
            for future in as_completed(futures):
                try:
                    output, error = future.result()
                except Exception as exc:
                    errors.append(f"r{round_index}: {exc}")
                    continue
                if output is not None:
                    round_outputs.append(output)
                else:
                    errors.append(f"r{round_index}: {error}")
        outputs.extend(round_outputs)
        round_chunks.append(round_outputs)

    valid_samples = len(outputs)
    latency_ms = int((time.time() - started) * 1000)
    cost_estimate = {
        "input_tokens_est": total_questions * INPUT_TOKEN_PER_TASK,
        "output_tokens_est": sum(len(parse_numbers(item["text"])) for item in outputs) * TOKEN_PER_NUMBER,
        "note": "粗估：输出 3 token/整数，输入 0.4K/题；只含已完成的题",
    }
    per_round = [
        {"round": index + 1, "valid_samples": len(chunk), "top_model": None, "top_p": None, "family": None, "family_p": None}
        for index, chunk in enumerate(round_chunks)
    ]

    base_result = {
        "target": f"{base_url.rstrip('/')}::{model}",
        "valid_samples": valid_samples,
        "total_questions": total_questions,
        "per_round": per_round,
        "errors": errors,
        "latency_ms": latency_ms,
        "cost_estimate": cost_estimate,
    }

    if valid_samples == 0:
        message = errors[-1] if errors else "无有效回答"
        raise RuntimeError(f"全部 {total_questions} 题失败，最后错误：{message}")

    analysis = analyze_global_outputs(outputs, holder.bank)
    verdict, reason = decide_verdict(analysis, rules or VERDICT_RULES)
    results = [
        {
            key: item.get(key)
            for key in ("model", "display_name", "family", "family_name", "probability", "score", "profile_similarity")
        }
        for item in analysis.get("results", [])[:8]
    ]
    full_results = analysis.get("results", [])
    top_gap = None
    if len(full_results) > 1:
        top_gap = float(full_results[0].get("score") or 0.0) - float(full_results[1].get("score") or 0.0)

    # 轮级摘要：每轮独立归因（题目数不足 1 时跳过）
    for index, chunk in enumerate(round_chunks):
        if not chunk:
            continue
        try:
            round_analysis = analyze_global_outputs(chunk, holder.bank)
            per_round[index]["top_model"] = round_analysis.get("prediction")
            per_round[index]["top_p"] = round_analysis.get("probability")
            per_round[index]["family"] = round_analysis.get("family_prediction")
            per_round[index]["family_p"] = round_analysis.get("family_probability")
        except ValueError:
            pass

    base_result.update(
        {
            "verdict": verdict,
            "verdict_reason": reason,
            "family": analysis.get("family_prediction"),
            "family_name": analysis.get("family_prediction_name"),
            "family_p": analysis.get("family_probability"),
            "top_model": analysis.get("prediction"),
            "top_p": analysis.get("probability"),
            "top_gap": top_gap,
            "results": results,
            "pinned": build_pinned_info(pin, analysis, holder.model_ids),
        }
    )
    return base_result
