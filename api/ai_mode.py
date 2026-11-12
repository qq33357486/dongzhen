# -*- coding: utf-8 -*-
"""AI 自助模式：洞真退化为「出题 + 判卷」，不碰目标模型的 key、不发外呼。
- POST /challenge：出题；challenge 存内存字典 + sqlite 留底（表 ai_challenges），TTL 24 小时；
- POST /submit：收答案，parse_numbers + analyze_global_outputs 归因，按 VERDICT_RULES 给判定；
- GET / 与 GET /llms.txt：给 AI 的使用说明（HTML 内嵌 ai-instructions 块 / 纯文本）。
target_model 只作展示不参与判定；每题有效性口径与 /probe 一致（max(80, ceil(expected*0.55))）。
"""
from __future__ import annotations

import json
import logging
import math
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from api.ai_instructions import AI_INSTRUCTIONS, USAGE_HINT, build_home_html
from api.auth import require_token
from api.config import VERDICT_RULES
from api.verdict import decide_verdict
from fingerprint import analyze_global_outputs, generate_challenges, parse_numbers

logger = logging.getLogger("dongzhen")

CHALLENGE_TTL = timedelta(hours=24)

router = APIRouter()

# 指纹库由 main.py lifespan 注入（避免与 main 循环 import）
_bank_holder = None
# challenge 内存字典：challenge_id -> 条目；sqlite 只作留底与服务重启后的兜底读取
_challenges: dict[str, dict] = {}
_store_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_challenges (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    model_hint TEXT,
    question_count INTEGER NOT NULL,
    challenges_json TEXT NOT NULL,
    submitted_at TEXT,
    target_model TEXT,
    verdict TEXT,
    valid_samples INTEGER
);
"""


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
    challenge_id: str = Field(..., min_length=1, description="/challenge 返回的 challenge_id")
    target_model: str | None = Field(
        None, max_length=200, description="目标模型名（你实际去问的模型），只作展示，不参与判定",
    )
    answers: list[AnswerItem] = Field(..., min_length=1, description="按题提交的回答")


def init_challenge_store(db_path) -> None:
    """main.py lifespan 调用；幂等（重复调用重建连接）。"""
    global _conn
    with _store_lock:
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(_SCHEMA)
        _conn.commit()


def set_bank_holder(holder) -> None:
    """main.py lifespan 调用：注入已加载的指纹库（复用 BankHolder，不重复加载）。"""
    global _bank_holder
    _bank_holder = holder


def reset_challenge_state() -> None:
    """测试夹具用：清空内存挑战（sqlite 留底不动）。"""
    with _store_lock:
        _challenges.clear()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cleanup_expired() -> None:
    """清掉内存里过期的 challenge；sqlite 留底不清（对账用），读取时另行校验过期。"""
    now_iso = _now().isoformat()
    for cid in [cid for cid, item in _challenges.items() if item["expires_at"] <= now_iso]:
        _challenges.pop(cid, None)


def _save_challenge(entry: dict) -> None:
    """留底到 sqlite；留底失败不影响出题（内存字典才是主存储）。"""
    if _conn is None:
        return
    try:
        with _store_lock:
            _conn.execute(
                "INSERT OR REPLACE INTO ai_challenges"
                " (id, created_at, expires_at, model_hint, question_count, challenges_json)"
                " VALUES (?,?,?,?,?,?)",
                (
                    entry["challenge_id"], entry["created_at"], entry["expires_at"],
                    entry["model_hint"], len(entry["challenges"]),
                    json.dumps(entry["challenges"], ensure_ascii=False),
                ),
            )
            _conn.commit()
    except Exception:
        logger.exception("AI 模式 challenge 留底失败（challenge_id=%s）", entry["challenge_id"])


def _load_challenge_from_db(challenge_id: str) -> dict | None:
    """内存 miss 时从 sqlite 兜底读取（服务重启后 AI 还能交卷）；过期或损坏返回 None。"""
    if _conn is None:
        return None
    try:
        with _store_lock:
            row = _conn.execute(
                "SELECT id, created_at, expires_at, model_hint, challenges_json"
                " FROM ai_challenges WHERE id = ?",
                (challenge_id,),
            ).fetchone()
    except Exception:
        logger.exception("AI 模式 challenge 兜底读取失败（challenge_id=%s）", challenge_id)
        return None
    if row is None:
        return None
    if row["expires_at"] <= _now().isoformat():
        return None
    items = json.loads(row["challenges_json"])
    if not isinstance(items, list) or not items:
        return None
    return {
        "challenge_id": row["id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "model_hint": row["model_hint"],
        "challenges": items,
    }


def _get_challenge(challenge_id: str) -> dict:
    with _store_lock:
        _cleanup_expired()
        entry = _challenges.get(challenge_id)
    if entry is not None:
        return entry
    entry = _load_challenge_from_db(challenge_id)  # 重启兜底
    if entry is None:
        raise HTTPException(status_code=404, detail="challenge 不存在或已过期")
    with _store_lock:
        _challenges[challenge_id] = entry
    return entry


def _bank() -> dict:
    if _bank_holder is None:
        raise HTTPException(status_code=503, detail="服务未就绪：指纹库未加载")
    return _bank_holder.bank


@router.post("/challenge", dependencies=[Depends(require_token)])
def create_challenge(request: ChallengeRequest):
    """出题：生成 count 道整数挑战，存内存 + sqlite 留底，24 小时内可交卷。"""
    if _conn is None:
        raise HTTPException(status_code=503, detail="服务未就绪：挑战存储未初始化")
    raw = generate_challenges(request.count)
    items = [
        {"index": i, "prompt": c["prompt"], "expected_count": int(c["expected_count"])}
        for i, c in enumerate(raw)
    ]
    now = _now()
    entry = {
        "challenge_id": secrets.token_hex(8),
        "created_at": now.isoformat(),
        "expires_at": (now + CHALLENGE_TTL).isoformat(),
        "model_hint": (request.model_hint or "").strip() or None,
        "challenges": items,
    }
    with _store_lock:
        _cleanup_expired()
        _challenges[entry["challenge_id"]] = entry
    _save_challenge(entry)
    return {
        "challenge_id": entry["challenge_id"],
        "expires_at": entry["expires_at"],
        "model_hint": entry["model_hint"],
        "challenges": items,
        "usage_hint": USAGE_HINT,
    }


@router.post("/submit", dependencies=[Depends(require_token)])
def submit_answers(request: SubmitRequest):
    """判卷：解析数字 -> 归因 -> 按 VERDICT_RULES 给 verdict。target_model 只展示不参与判定。"""
    entry = _get_challenge(request.challenge_id)
    bank = _bank()
    count = len(entry["challenges"])
    expected_by_index = {item["index"]: int(item["expected_count"]) for item in entry["challenges"]}

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
    for item in entry["challenges"]:
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
    results = analysis.get("results") or []
    top_gap = None
    if len(results) > 1:
        top_gap = float(results[0].get("score") or 0.0) - float(results[1].get("score") or 0.0)

    _record_submission(request, verdict, len(outputs))

    return {
        "verdict": verdict,
        "verdict_reason": reason,
        "family": analysis.get("family_prediction"),
        "family_name": analysis.get("family_prediction_name"),
        "family_p": analysis.get("family_probability"),
        "top_model": analysis.get("prediction"),
        "top_p": analysis.get("probability"),
        "top_gap": top_gap,
        "valid_samples": len(outputs),
        "total_answers": len(request.answers),
        "per_challenge": per_challenge,
        "target_model": (request.target_model or "").strip() or None,
        "model_hint": entry["model_hint"],
        "expires_at": entry["expires_at"],
    }


def _record_submission(request: SubmitRequest, verdict: str, valid_samples: int) -> None:
    """提交留底（审计/对账用），失败不影响判定返回。"""
    if _conn is None:
        return
    try:
        with _store_lock:
            _conn.execute(
                "UPDATE ai_challenges SET submitted_at=?, target_model=?, verdict=?, valid_samples=?"
                " WHERE id=?",
                (
                    _now().isoformat(), (request.target_model or "").strip() or None,
                    verdict, valid_samples, request.challenge_id,
                ),
            )
            _conn.commit()
    except Exception:
        logger.exception("AI 模式提交留底失败（challenge_id=%s）", request.challenge_id)


@router.get("/", response_class=HTMLResponse)
def home():
    """主页：人类可读版面 + 内嵌 <pre id="ai-instructions"> 机器可读说明块。"""
    return HTMLResponse(build_home_html())


@router.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt():
    """给 AI 的纯文本版使用说明，内容与主页 ai-instructions 块同源。"""
    return PlainTextResponse(AI_INSTRUCTIONS, media_type="text/plain; charset=utf-8")
