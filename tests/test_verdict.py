# -*- coding: utf-8 -*-
"""verdict 判定规则、族候选（candidates）与 key 脱敏的单测（零网络、零成本）。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.config import VERDICT_RULES
from api.verdict import build_candidates, build_pinned_info, decide_verdict, mask_key, parse_pin


def analysis_of(results: list[dict], family: str = "gpt") -> dict:
    """构造 analyze_global_outputs 形状的最小结果。score 为 18 维融合分的均值分。
    每条 result 都补 family 字段（analyze_global_outputs 会给每条结果标族）。"""
    filled = [{**item, "family": family, "family_name": family.upper()} for item in results]
    return {
        "results": filled,
        "family_prediction": family,
        "family_probability": 0.9,
    }


def test_astra_top_is_clean():
    result = decide_verdict(analysis_of([
        {"model": "gpt-6-astra", "probability": 0.42, "score": 1.2},
        {"model": "gpt-6-sol", "probability": 0.31, "score": 0.7},
    ]))
    # 族口径：判定不变，reason 附族内候选
    assert result == (
        "clean",
        "top1 命中正常分支（gpt-6-astra 含 'astra'）；族内可能是 gpt-6-astra / gpt-6-sol",
    )


def test_luna_top_is_degraded():
    result = decide_verdict(analysis_of([
        {"model": "gpt-6-luna", "probability": 0.55, "score": 1.0},
        {"model": "gpt-6-astra", "probability": 0.20, "score": 0.3},
    ]))
    assert result[0] == "degraded"
    assert "可能是 gpt-6-luna / gpt-6-astra" in result[1]


def test_self_match_clean():
    # 非 astra/luna 模型靠高 p 自匹配判 clean；第二候选 p<0.05 不进候选列表，reason 无附注
    result = decide_verdict(analysis_of([
        {"model": "gpt-6-sol", "probability": 0.97, "score": 1.5},
        {"model": "gpt-5.5", "probability": 0.02, "score": 0.1},
    ]))
    assert result[0] == "clean"
    assert "自匹配" in result[1]
    assert "可能是" not in result[1]


def test_low_gap_is_unknown():
    # 分差过小：疑似库外强归，不硬判（诚实协议）；同族近对 -> 多候选并列
    result = decide_verdict(analysis_of([
        {"model": "gpt-6-sol", "probability": 0.5, "score": 0.30},
        {"model": "gpt-5.6-sol", "probability": 0.45, "score": 0.25},
    ]))
    assert result[0] == "unknown"
    assert "分差" in result[1]
    assert "族内多候选并列（可能是 gpt-6-sol / gpt-5.6-sol）" in result[1]


def test_mid_p_big_gap_unknown():
    # 分差够大但 p 不够自匹配、名字也不含 astra/luna -> unknown
    result = decide_verdict(analysis_of([
        {"model": "gpt-6-sol", "probability": 0.60, "score": 1.2},
        {"model": "gpt-5.5", "probability": 0.10, "score": 0.2},
    ]))
    assert result == (
        "unknown",
        "top1=gpt-6-sol p=0.600 不满足任何判定规则；族内可能是 gpt-6-sol / gpt-5.5",
    )


# ---- 族候选 build_candidates ----


def test_candidates_filters_by_family_and_p():
    """候选只收族内成员且 p>0.05；claude 族的成绩不掺进 gpt 族候选。"""
    analysis = analysis_of([
        {"model": "gpt-6-astra", "probability": 0.42, "score": 1.2},
        {"model": "gpt-6-sol", "probability": 0.31, "score": 0.7},
        {"model": "gpt-5.5", "probability": 0.03, "score": 0.1},
    ])
    # 塞一条别族的（analyze_global_outputs 的 results 是全模型的）
    analysis["results"].append({"model": "claude-opus-5", "probability": 0.20, "score": 0.9, "family": "claude"})
    candidates, tied = build_candidates(analysis)
    assert [item["model"] for item in candidates] == ["gpt-6-astra", "gpt-6-sol"]
    assert candidates[0]["p"] == 0.42 and candidates[1]["p"] == 0.31
    assert tied is False  # 族内 top1-top2 分差 1.2-0.7=0.5 >= 0.40


def test_candidates_tied_when_family_gap_small():
    """族内 top1-top2 分差 < ambiguous_gap_score(0.40)：多候选并列。"""
    analysis = analysis_of([
        {"model": "gpt-6-sol", "probability": 0.50, "score": 0.30},
        {"model": "gpt-6-astra", "probability": 0.45, "score": 0.25},
    ])
    candidates, tied = build_candidates(analysis)
    assert tied is True
    assert [item["model"] for item in candidates] == ["gpt-6-sol", "gpt-6-astra"]


def test_candidates_never_empty_and_no_results_safe():
    """族内全部 p<=0.05 时兜底给 top1；无结果时返回空列表。"""
    analysis = analysis_of([
        {"model": "gpt-6-sol", "probability": 0.04, "score": 0.1},
    ])
    candidates, tied = build_candidates(analysis)
    assert candidates == [{"model": "gpt-6-sol", "p": 0.04}]
    assert tied is False
    assert build_candidates({}) == ([], False)


def test_verdict_rules_gpt_line_defaults():
    # 口径锚点：默认规则必须是 GPT 线
    assert VERDICT_RULES["degraded_substrings"] == ["luna"]
    assert VERDICT_RULES["clean_substrings"] == ["astra"]


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
