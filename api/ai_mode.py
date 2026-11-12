# -*- coding: utf-8 -*-
"""AI 自助模式：洞真退化为「出题 + 判卷」，不碰目标模型的 key、不发外呼，完全无状态。
- POST /challenge：出题；题目信息 (模板号, N) 编进自包含的 challenge_id（base64url(payload) + "." + HMAC-SHA256），
  服务器不存任何题目状态；
- POST /submit：验签 -> 验过期 -> 由 (模板号, N) 重新渲染题面 -> 判卷归因，按 VERDICT_RULES 给 verdict；
- GET /llms.txt：给 AI 的纯文本使用说明（主页 GET / 在 api/webui.py，AI 提示词内藏进原版页面）。
判定结果仍写历史留底（probes 表，base_url 占位 ai-mode、key 占位 ****）；题目状态零存储。
签名密钥：DZ_CHALLENGE_SECRET > DZ_API_TOKEN > 开发默认（见 api/config.challenge_secret）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from api.ai_instructions import AI_INSTRUCTIONS, USAGE_HINT
from api.auth import require_token
from api.config import VERDICT_RULES, challenge_secret
from api.verdict import build_candidates, decide_verdict
from fingerprint import analyze_global_outputs, generate_challenges, parse_numbers, render_challenge

logger = logging.getLogger("dongzhen")

CHALLENGE_TTL = timedelta(hours=24)
PAYLOAD_VERSION = 1

router = APIRouter()

# 指纹库与历史留底由 main.py lifespan 注入（避免与 main 循环 import）
_bank_holder = None
_history_store = None


class ChallengeRequest(BaseModel):
    model_hint: str | None = Field(
        None, max_length=200,
        description="宣称的模型名，如 gpt-6.1-sol；只作展示与结果解读提示，不参与判定",
    )
    count: int = Field(3, ge=1, le=10, description="题数，默认 3（与指纹库校准最高档一致）")


class AnswerItem(BaseModel):
    index: int = Field(..., ge=0, description="题号，对应 /challenge 返回的 index（0 起）")
    text: str = Field(..., min_length=1, description="目标模型的原始文本回答，一字不改")


class SubmitRequest(BaseModel):
    challenge_id: str = Field(..., min_length=1, description="/challenge 返回的 challenge_id（自包含题目信息）")
    target_model: str | None = Field(
        None, max_length=200, description="目标模型名（你实际去问的模型），只作展示，不参与判定",
    )
    answers: list[AnswerItem] = Field(..., min_length=1, description="按题提交的回答")


def set_bank_holder(holder) -> None:
    """main.py lifespan 调用：注入已加载的指纹库（复用 BankHolder，不重复加载）。"""
    global _bank_holder
    _bank_holder = holder


def set_history_store(store) -> None:
    """main.py lifespan 调用：注入历史留底 store（判定结果写 probes 表，题目状态不写）。"""
    global _history_store
    _history_store = store


def reset_state() -> None:
    """测试夹具用：清掉注入的全局依赖。"""
    global _bank_holder, _history_store
    _bank_holder = None
    _history_store = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bank() -> dict:
    if _bank_holder is None:
        raise HTTPException(status_code=503, detail="服务未就绪：指纹库未加载")
    return _bank_holder.bank


# ---- 自包含 challenge_id：base64url(json payload) + "." + HMAC-SHA256（hex）----
# payload = {"v": 1, "t": 签发 unix 秒, "h": model_hint, "q": [[模板号, expected_count], ...]}
# 无状态关键：凭 id 就能重新渲染全部题面；服务器零存储，id 不可伪造（HMAC）、不可续期（过期只看 t）。


def _encode_challenge_id(pairs: list[tuple[int, int]], model_hint: str | None, issued_at: int) -> str:
    payload = {
        "v": PAYLOAD_VERSION,
        "t": issued_at,
        "h": model_hint,
        "q": [[template, n] for template, n in pairs],
    }
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=")
    signature = hmac.new(challenge_secret().encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return f"{raw.decode('ascii')}.{signature}"


def _decode_challenge_id(challenge_id: str) -> dict:
    """解出并校验 payload：格式/版本/签名不对 -> 422，过期 -> 404。"""
    parts = challenge_id.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise HTTPException(status_code=422, detail="challenge_id 格式无效（应为 base64url负载.签名）")
    raw = parts[0].encode("ascii")
    expected = hmac.new(challenge_secret().encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(parts[1], expected):
        raise HTTPException(status_code=422, detail="challenge_id 签名校验失败（被篡改或密钥不符）")
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4)).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=422, detail="challenge_id 负载无法解析")
    if not isinstance(payload, dict) or payload.get("v") != PAYLOAD_VERSION:
        raise HTTPException(status_code=422, detail=f"challenge_id 版本不支持（需要 v{PAYLOAD_VERSION}）")
    issued_at = payload.get("t")
    pairs = payload.get("q")
    if not isinstance(issued_at, (int, float)) or not isinstance(pairs, list) or not pairs:
        raise HTTPException(status_code=422, detail="challenge_id 负载缺字段（t/q）")
    if not all(isinstance(pair, list) and len(pair) == 2 for pair in pairs):
        raise HTTPException(status_code=422, detail="challenge_id 负载 q 字段格式无效")
    if _now().timestamp() > float(issued_at) + CHALLENGE_TTL.total_seconds():
        raise HTTPException(status_code=404, detail="challenge 已过期（自签发起 24 小时有效）")
    return payload


@router.post("/challenge", dependencies=[Depends(require_token)])
def create_challenge(request: ChallengeRequest):
    """出题：生成 count 道整数挑战，(模板号, N) 编进自包含 challenge_id 返回；服务器零存储。"""
    raw = generate_challenges(request.count)
    items = [
        {"index": i, "prompt": c["prompt"], "expected_count": int(c["expected_count"])}
        for i, c in enumerate(raw)
    ]
    model_hint = (request.model_hint or "").strip() or None
    now = _now()
    challenge_id = _encode_challenge_id(
        [(int(c["template"]), int(c["expected_count"])) for c in raw],
        model_hint,
        int(now.timestamp()),
    )
    return {
        "challenge_id": challenge_id,
        "expires_at": (now + CHALLENGE_TTL).isoformat(),
        "model_hint": model_hint,
        "challenges": items,
        "usage_hint": USAGE_HINT,
    }


@router.post("/submit", dependencies=[Depends(require_token)])
def submit_answers(request: SubmitRequest):
    """判卷：验签验过期 -> 重渲染题面 -> 解析数字 -> 归因 -> 按 VERDICT_RULES 给 verdict。
    target_model 只展示不参与判定；题目状态零存储，凭 challenge_id 即可判卷。
    """
    payload = _decode_challenge_id(request.challenge_id)
    bank = _bank()
    pairs = [(int(template), int(n)) for template, n in payload["q"]]
    challenges = [
        {"index": i, "prompt": render_challenge(template, n), "expected_count": n}
        for i, (template, n) in enumerate(pairs)
    ]
    count = len(challenges)
    expected_by_index = {item["index"]: item["expected_count"] for item in challenges}

    # 同一 index 交多份时取最后一份（以最后一次回答为准）；越界 index 直接 422
    by_index: dict[int, str] = {}
    for answer in request.answers:
        if answer.index not in expected_by_index:
            raise HTTPException(
                status_code=422,
                detail=f"answers 里有越界 index={answer.index}（有效范围 0~{count - 1}）",
            )
        by_index[answer.index] = answer.text

    # 每题解析与有效性判定（口径与 /probe 的 run_one_challenge 一致）
    per_challenge: list[dict] = []
    outputs: list[dict] = []
    for item in challenges:
        index = item["index"]
        text = by_index.get(index, "")
        parsed = parse_numbers(text)
        minimum = max(80, math.ceil(expected_by_index[index] * 0.55))
        valid = len(parsed) >= minimum
        per_challenge.append({"index": index, "parsed_count": len(parsed), "valid": valid})
        if valid:
            outputs.append({"text": text, "expected_count": expected_by_index[index]})

    if not outputs:
        raise HTTPException(
            status_code=422,
            detail=(
                "没有可用回答：每题至少需要 "
                f"{max(80, math.ceil(min(expected_by_index.values()) * 0.55))}"
                " 个左右的有效数字（视题目而定），拒答或严重截断的回答无法归因；"
                "请确认把题面原样发给了目标模型、并原样回传了它的纯文本回答。"
            ),
        )

    try:
        analysis = analyze_global_outputs(outputs, bank)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))

    verdict, reason = decide_verdict(analysis, VERDICT_RULES)
    candidates, candidates_tied = build_candidates(analysis, VERDICT_RULES)
    results = analysis.get("results") or []
    top_gap = None
    if len(results) > 1:
        top_gap = float(results[0].get("score") or 0.0) - float(results[1].get("score") or 0.0)

    expires_at = datetime.fromtimestamp(float(payload["t"]), timezone.utc) + CHALLENGE_TTL
    _record_history(
        request, payload, verdict, reason, analysis, len(outputs),
        expires_at.isoformat(), candidates,
    )

    return {
        "verdict": verdict,
        "verdict_reason": reason,
        "family": analysis.get("family_prediction"),
        "family_name": analysis.get("family_prediction_name"),
        "family_p": analysis.get("family_probability"),
        "candidates": candidates,
        "candidates_tied": candidates_tied,
        "top_model": analysis.get("prediction"),
        "top_p": analysis.get("probability"),
        "top_gap": top_gap,
        "valid_samples": len(outputs),
        "total_answers": len(request.answers),
        "per_challenge": per_challenge,
        "target_model": (request.target_model or "").strip() or None,
        "model_hint": payload.get("h"),
        "expires_at": expires_at.isoformat(),
    }


def _record_history(
    request: SubmitRequest,
    payload: dict,
    verdict: str,
    reason: str,
    analysis: dict,
    valid_samples: int,
    expires_at: str,
    candidates: list[dict] | None = None,
) -> None:
    """判定结果写历史留底（probes 表）；失败不影响判定返回。
    AI 模式没有 base_url 与 key：base_url 占位 ai-mode，key 占位 ****；model 取 target_model 优先。
    """
    if _history_store is None:
        return
    try:
        results = analysis.get("results") or []
        top_gap = None
        if len(results) > 1:
            top_gap = float(results[0].get("score") or 0.0) - float(results[1].get("score") or 0.0)
        _history_store.insert_probe(
            ts=_now().isoformat(),
            base_url="ai-mode",
            model=(request.target_model or "").strip() or payload.get("h") or "unknown",
            api_key_masked="****",
            verdict=verdict,
            verdict_reason=reason,
            family=analysis.get("family_prediction"),
            family_p=analysis.get("family_probability"),
            top_model=analysis.get("prediction"),
            top_p=analysis.get("probability"),
            top_gap=top_gap,
            valid_samples=valid_samples,
            rounds=1,
            questions=len(payload["q"]),
            latency_ms=None,
            error=None,
            candidates=candidates,
        )
    except Exception:
        logger.exception("AI 模式判定留底失败（challenge_id 前 16 字符=%s…）", request.challenge_id[:16])


@router.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt():
    """给 AI 的纯文本版使用说明，内容与主页 ai-instructions 块同源。"""
    return PlainTextResponse(AI_INSTRUCTIONS, media_type="text/plain; charset=utf-8")
