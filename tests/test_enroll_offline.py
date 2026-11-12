# -*- coding: utf-8 -*-
"""enroll_cli 离线管线自测（零网络零成本）：
用库里两个模型的参考数据造假候选，走 build_temp_bank -> neighbor_report -> self_match_report 全链路。
正例：把 gpt-6-astra 从"现库"剔除、它的数据当新模型候选 -> 自匹配应命中自身标签。
负例：把 gpt-6-sol 的数据冒充 astra 候选 -> 自匹配应命中真身而非候选标签（撞库防护）。
注：候选与现库行都取自不同环境（每环境 3 行），保证建库的完整环境/校准路径与真实录入一致。
"""
import json
import sys
import os
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import enroll_cli
from enroll_cli import (
    build_temp_bank,
    distribution_summary,
    enrich_rows,
    neighbor_report,
    select_tasks,
    self_match_report,
    tagged_candidate_rows,
    token_estimate,
)

ROOT = Path(__file__).resolve().parents[1]
PER_SOURCE = 12  # 4 环境 × 3 行，接近真实录入密度


def load_rows(source: str, n: int) -> list[dict]:
    rows = []
    with (ROOT / "data" / "gpt_reference.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row["source"] == source and row.get("strict_valid"):
                rows.append(row)
                if len(rows) == n:
                    break
    assert len(rows) == n, f"{source} 参考数据不足 {n} 条"
    enrich_rows(rows)
    return rows


def fake_existing_rows(keep_sources: list[str], per_source: int = PER_SOURCE) -> list[dict]:
    rows = []
    for source in keep_sources:
        rows.extend(load_rows(source, per_source))
    return rows


def test_self_match_hits_own_label(monkeypatch):
    candidate = load_rows("gpt-6-astra", PER_SOURCE)
    monkeypatch.setattr(enroll_cli, "load_existing_rows", lambda: fake_existing_rows(["gpt-6-sol", "gpt-5.5"]))
    fit = tagged_candidate_rows(candidate[: PER_SOURCE - 3], "test-astra-new", "gpt")
    holdout = candidate[PER_SOURCE - 3 :]
    bank = build_temp_bank(fit)
    report = self_match_report(bank, holdout, "test-astra-new")
    assert report["total"] == 1
    assert report["hits"] == 1
    group = report["groups"][0]
    assert group["top1"] == "test-astra-new"
    assert group["p"] > 0.5
    assert group["gap"] > 0


def test_candidate_colliding_with_bank_twin(monkeypatch):
    """负例：gpt-6-sol 数据冒充新模型入库 -> 自匹配应指到真身 gpt-6-sol，不许冒名通过。"""
    candidate = load_rows("gpt-6-sol", PER_SOURCE)
    monkeypatch.setattr(
        enroll_cli, "load_existing_rows",
        lambda: fake_existing_rows(["gpt-6-sol", "gpt-5.6-sol"]),
    )
    fit = tagged_candidate_rows(candidate[: PER_SOURCE - 3], "test-fake-label", "gpt")
    holdout = candidate[PER_SOURCE - 3 :]
    bank = build_temp_bank(fit)
    report = self_match_report(bank, holdout, "test-fake-label")
    assert report["total"] == 1
    assert report["hits"] == 0
    assert report["groups"][0]["top1"] in ("gpt-6-sol", "gpt-5.6-sol")


def test_neighbor_report_and_summary(monkeypatch):
    candidate = load_rows("gpt-6-astra", PER_SOURCE)
    monkeypatch.setattr(
        enroll_cli, "load_existing_rows",
        lambda: fake_existing_rows(["gpt-6-sol", "gpt-5.5", "gpt-6-luna"]),
    )
    fit = tagged_candidate_rows(candidate[: PER_SOURCE - 2], "test-astra-new", "gpt")
    bank = build_temp_bank(fit)
    neighbors = neighbor_report(bank, "test-astra-new")
    assert len(neighbors) == 3
    # astra 的最近邻应是 gpt-6-sol（真实库内关系，见 docs/family-branch-research.md）
    assert neighbors[0]["model"] == "gpt-6-sol"
    assert -1.0 <= neighbors[0]["centroid_cos"] <= 1.0
    summary = distribution_summary(candidate)
    assert summary["numbers"] == sum(len(row["numbers"]) for row in candidate)
    assert summary["numbers"] > 0
    assert 1 <= summary["top_value"] <= 355
    tokens = token_estimate(candidate, PER_SOURCE)
    assert tokens["output_tokens_est"] == summary["numbers"] * 3


def test_select_tasks_stride():
    tasks = select_tasks(36)
    assert len(tasks) == 36
    assert len({t["challenge_id"] for t in tasks}) == 36
    small = select_tasks(6)
    assert len(small) == 6
    assert len({t["condition"] for t in small}) >= 5  # 等距抽样应跨环境
