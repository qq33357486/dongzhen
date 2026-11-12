# -*- coding: utf-8 -*-
"""verdict 判定规则与 key 脱敏的单测（零网络、零成本）。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.config import VERDICT_RULES
from api.verdict import build_pinned_info, decide_verdict, mask_key, parse_pin


def analysis_of(results: list[dict]) -> dict:
    """构造 analyze_global_outputs 形状的最小结果。score 为 18 维融合分的均值分。"""
    return {"results": results, "family_prediction": "gpt", "family_probability": 0.9}


def test_astra_top_is_clean():
    result = decide_verdict(analysis_of([
        {"model": "gpt-6-astra", "probability": 0.42, "score": 1.2},
        {"model": "gpt-6-sol", "probability": 0.31, "score": 0.7},
    ]))
    assert result == ("clean", "top1 命中正常分支（gpt-6-astra 含 'astra'）")


def test_luna_top_is_degraded():
    result = decide_verdict(analysis_of([
        {"model": "gpt-6-luna", "probability": 0.55, "score": 1.0},
        {"model": "gpt-6-astra", "probability": 0.20, "score": 0.3},
    ]))
    assert result[0] == "degraded"


def test_self_match_clean():
    # 非 astra/luna 模型靠高 p 自匹配判 clean
    result = decide_verdict(analysis_of([
        {"model": "gpt-6-sol", "probability": 0.97, "score": 1.5},
        {"model": "gpt-5.5", "probability": 0.02, "score": 0.1},
    ]))
    assert result[0] == "clean"
    assert "自匹配" in result[1]


def test_low_gap_is_unknown():
    # 分差过小：疑似库外强归，不硬判（诚实协议）
    result = decide_verdict(analysis_of([
        {"model": "gpt-6-sol", "probability": 0.5, "score": 0.30},
        {"model": "gpt-5.6-sol", "probability": 0.45, "score": 0.25},
    ]))
    assert result[0] == "unknown"
    assert "分差" in result[1]


def test_mid_p_big_gap_unknown():
    # 分差够大但 p 不够自匹配、名字也不含 astra/luna -> unknown
    result = decide_verdict(analysis_of([
        {"model": "gpt-6-sol", "probability": 0.60, "score": 1.2},
        {"model": "gpt-5.5", "probability": 0.10, "score": 0.2},
    ]))
    assert result == ("unknown", "top1=gpt-6-sol p=0.600 不满足任何判定规则")


def test_verdict_rules_gpt_line_defaults():
    # 口径锚点：默认规则必须是 GPT 线
    assert VERDICT_RULES["degraded_substrings"] == ["luna"]
    assert VERDICT_RULES["clean_substrings"] == ["astra"]


def test_mask_key():
    assert mask_key("sk-abcdef1234567890") == "sk-a***7890"
    assert mask_key("short") == "****"
    assert mask_key("") == "****"
    # 前后 4 位保留、中间不可见
    masked = mask_key("0123456789abcdef")
    assert masked == "0123***cdef"


def test_parse_pin():
    assert parse_pin("gpt-6.1-sol@s141c592") == ("gpt-6.1-sol", "s141c592")
    assert parse_pin("gpt-6.1-sol") == ("gpt-6.1-sol", None)
    assert parse_pin(None) == (None, None)


def test_pinned_in_bank():
    info = build_pinned_info("gpt-6-astra@s1", analysis_of([
        {"model": "gpt-6-astra", "probability": 0.9, "score": 1.0},
        {"model": "gpt-6-sol", "probability": 0.05, "score": 0.2},
    ]), ["gpt-6-astra", "gpt-6-sol"])
    assert info["in_bank"] is True
    assert info["claimed_rank"] == 1
    assert info["top1_is_claimed"] is True


def test_pinned_out_of_bank():
    # 库外宣称：只判家族，不硬归类
    info = build_pinned_info("gpt-6.1-sol@s141c592", analysis_of([
        {"model": "gpt-6-astra", "probability": 0.4, "score": 1.0},
    ]), ["gpt-6-astra"])
    assert info["in_bank"] is False
    assert info["claimed_rank"] is None
    assert "库外" in info["note"]
