# -*- coding: utf-8 -*-
"""verdict 判定：把归因结果映射成 clean / degraded / unknown。
规则在 api/config.VERDICT_RULES，判定口径依据：
  - GPT 线定谳：判出 astra = 没降智；判出 luna 族 = 真降智；库外强归 = 不可信。
  - 量化依据（docs/family-branch-research.md）：库内自匹配 top1-top2 分差 p5=0.78，
    库外强归分差中位 ~0.1；分差低于阈值报 unknown 而不是硬判（诚实协议）。
"""
from __future__ import annotations

from api.config import VERDICT_RULES


def mask_key(api_key: str) -> str:
    """key 脱敏：只留前后 4 位，短 key 全掩。日志与 sqlite 一律用这个。"""
    key = (api_key or "").strip()
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}***{key[-4:]}"


def parse_pin(pin: str | None) -> tuple[str | None, str | None]:
    """pin 形如 gpt-6.1-sol@s141c592 -> ("gpt-6.1-sol", "s141c592")；无 @ 则整体是宣称模型。"""
    if not pin:
        return None, None
    if "@" in pin:
        claimed, tag = pin.split("@", 1)
        return claimed.strip(), tag.strip()
    return pin.strip(), None


def decide_verdict(
    analysis: dict,
    rules: dict | None = None,
) -> tuple[str, str]:
    """依据 analyze_global_outputs 的结果给 (verdict, reason)。
    analysis 需要字段：prediction/probability/results（含 score）， valid 样本数为 0 时调用方自行处理。
    """
    rules = rules or VERDICT_RULES
    results = analysis.get("results") or []
    if not results:
        return "unknown", "无归因结果"
    top = results[0]
    top_model = str(top.get("model") or "")
    top_p = float(top.get("probability") or 0.0)
    top_score = float(top.get("score") or 0.0)
    second_score = float(results[1].get("score") or 0.0) if len(results) > 1 else 0.0
    gap = top_score - second_score

    lowered = top_model.lower()
    for sub in rules.get("degraded_substrings", []):
        if sub in lowered:
            return "degraded", f"top1 命中降智分支（{top_model} 含 '{sub}'）"

    for sub in rules.get("clean_substrings", []):
        if sub in lowered:
            return "clean", f"top1 命中正常分支（{top_model} 含 '{sub}'）"

    if gap < float(rules.get("ambiguous_gap_score", 0.40)):
        second_model = results[1].get("model") if len(results) > 1 else "?"
        return "unknown", f"top1({top_model}) 与 top2({second_model}) 分差 {gap:.3f} 过小，疑似库外强归，不硬判"

    if top_p >= float(rules.get("self_match_p", 0.85)):
        return "clean", f"自匹配成立（top1={top_model} p={top_p:.3f}）"

    return "unknown", f"top1={top_model} p={top_p:.3f} 不满足任何判定规则"


def build_pinned_info(pin: str | None, analysis: dict, bank_models: list[str]) -> dict | None:
    """构造 pin 的定向核对信息。库内模型给排名与 p；库外模型只给家族提示，不硬归类。"""
    claimed, tag = parse_pin(pin)
    if not claimed:
        return None
    results = analysis.get("results") or []
    in_bank = claimed in bank_models
    info = {
        "claimed": claimed,
        "tag": tag,
        "in_bank": in_bank,
        "claimed_rank": None,
        "claimed_p": None,
        "top1_is_claimed": None,
        "note": None,
    }
    if in_bank:
        for rank, item in enumerate(results, start=1):
            if item.get("model") == claimed:
                info["claimed_rank"] = rank
                info["claimed_p"] = float(item.get("probability") or 0.0)
                break
        info["top1_is_claimed"] = (info["claimed_rank"] == 1)
        if not info["top1_is_claimed"]:
            info["note"] = f"宣称 {claimed}，但指纹 top1 是 {results[0].get('model')}，宣称不符"
    else:
        # 库外模型：只看家族。claimed 的家族按名字粗判（gpt-*/claude-*），不强行归类
        claimed_family = None
        lowered = claimed.lower()
        if lowered.startswith(("gpt", "o3", "o4", "chatgpt")):
            claimed_family = "gpt"
        elif lowered.startswith(("claude",)):
            claimed_family = "claude"
        family_prediction = analysis.get("family_prediction")
        if claimed_family and family_prediction == claimed_family:
            info["note"] = f"宣称模型不在库内；家族判定 {family_prediction} 与宣称一致，具体版本无法核对（库外不硬判）"
        else:
            info["note"] = (
                f"宣称模型不在库内；家族判定 {family_prediction} 与宣称 {claimed_family or '?'} 不一致，"
                "宣称可疑"
            )
    return info
