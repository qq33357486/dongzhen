# -*- coding: utf-8 -*-
"""请求/响应模型。注意：api_key 只出现在请求里，任何响应模型都不含它。"""
from typing import Literal

from pydantic import BaseModel, Field


class ProbeRequest(BaseModel):
    base_url: str = Field(..., description="目标 OpenAI 兼容端点，如 https://xxx/v1")
    api_key: str = Field(..., description="目标端点的 key，只进内存，不落盘不进日志")
    model: str = Field(..., description="目标模型名")
    pin: str | None = Field(
        None,
        description='可选，宣称身份，如 "gpt-6.1-sol@s141c592"。格式 <宣称模型>@<样本标记>；'
        "库内模型做定向自匹配，库外模型只判家族不硬归类",
    )
    rounds: int = Field(1, ge=1, le=10, description="轮数，每轮独立发 questions 题")
    questions: int = Field(3, ge=1, le=10, description="每轮题数，默认 3（与 mt_cli 口径一致）")
    timeout_s: int = Field(240, ge=5, le=600, description="单题请求超时")
    temperature: float | None = Field(None, description="留空=供应商默认（推荐，与指纹库同口径）")


class BatchTarget(ProbeRequest):
    """/probe_batch 的单个目标，字段同 ProbeRequest。"""


class ProbeBatchRequest(BaseModel):
    targets: list[BatchTarget] = Field(..., min_length=1, max_length=32)
    # rounds/questions/timeout_s 可在 BatchTarget 里逐个覆盖；这两个整体字段仅为便捷读取
    timeout_s: int = Field(240, ge=5, le=600)


class RoundSummary(BaseModel):
    round: int
    valid_samples: int
    top_model: str | None
    top_p: float | None
    family: str | None
    family_p: float | None


class PinnedInfo(BaseModel):
    claimed: str
    tag: str | None = None
    in_bank: bool
    claimed_rank: int | None = None
    claimed_p: float | None = None
    top1_is_claimed: bool | None = None
    note: str | None = None


class Candidate(BaseModel):
    """族内候选：按概率降序 [{model, p}]（p > 0.05 的族内成员）。"""
    model: str
    p: float


class CostEstimate(BaseModel):
    input_tokens_est: int
    output_tokens_est: int
    note: str


class ProbeResponse(BaseModel):
    target: str  # base_url + model 的可读标识（不含 key）
    verdict: Literal["clean", "degraded", "unknown"]
    verdict_reason: str
    family: str | None
    family_name: str | None
    family_p: float | None
    candidates: list[Candidate] = []  # 族口径：族内按概率排序的候选（p>0.05）
    candidates_tied: bool = False  # 族内 top1-top2 分差低于阈值 -> 多候选并列（指纹不可分，如实列出）
    top_model: str | None
    top_p: float | None
    top_gap: float | None  # top1-top2 融合分差（诚实协议指标）
    valid_samples: int
    total_questions: int
    per_round: list[RoundSummary]
    results: list[dict]  # 全模型概率明细（前 8）
    pinned: PinnedInfo | None = None
    cost_estimate: CostEstimate
    latency_ms: int
    errors: list[str]


class HistoryRow(BaseModel):
    id: int
    ts: str
    base_url: str
    model: str
    api_key_masked: str
    verdict: str | None
    family: str | None
    family_p: float | None
    candidates: list[dict] | None = None  # 族内候选 JSON（旧记录为 null）
    top_model: str | None
    top_p: float | None
    top_gap: float | None
    valid_samples: int | None
    latency_ms: int | None
    error: str | None
