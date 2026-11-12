# -*- coding: utf-8 -*-
"""洞真（dongzhen）模型指纹探针 FastAPI 服务。
路由：
  POST /probe        单目标探针
  POST /probe_batch  多目标并发探针
  GET  /history      历史查询（?model=&days=&limit=）
  GET  /health       健康检查
AI 自助模式（api/ai_mode.py，洞真只出题判卷、不碰 key 不外呼）：
  POST /challenge    得到题目（TTL 24 小时）
  POST /submit       提交答案拿判定
  GET  /             主页（内嵌 ai-instructions 给 AI 的说明块）
  GET  /llms.txt     同内容纯文本版
安全红线：api_key 只进内存；日志与 sqlite 一律脱敏（前4后4）；响应体绝不回显 key。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query

from api import config
from api.ai_mode import init_challenge_store, router as ai_router, set_bank_holder
from api.probe import BankHolder, run_probe
from api.schemas import (
    HistoryRow,
    ProbeBatchRequest,
    ProbeRequest,
    ProbeResponse,
)
from api.store import HistoryStore
from api.verdict import mask_key

logger = logging.getLogger("dongzhen")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

holder: BankHolder | None = None
store: HistoryStore | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global holder, store
    holder = BankHolder(config.BANK_PATH)
    store = HistoryStore(config.HISTORY_DB)
    # AI 自助模式：注入指纹库（复用同一份，不重复加载）+ 初始化挑战留底表
    set_bank_holder(holder)
    init_challenge_store(config.HISTORY_DB)
    logger.info(
        "洞真服务启动：库 %s（%d 模型，built %s），历史库 %s",
        config.BANK_PATH, len(holder.model_ids),
        holder.bank.get("built_at", "?")[:10], config.HISTORY_DB,
    )
    yield
    if store is not None:
        store.close()


app = FastAPI(
    title=config.SERVICE_TITLE,
    version=config.SERVICE_VERSION,
    description="对中转站/上游市场的模型做真身判定：长整数生成挑战 + 数字分布指纹归因。",
    lifespan=lifespan,
)

# AI 自助模式：POST /challenge（出题）、POST /submit（判卷）、GET /（含 AI 说明）、GET /llms.txt
app.include_router(ai_router)


def _persist(target_request: ProbeRequest, result: dict | None, error: str | None, started: str) -> None:
    """结果或错误都留底；key 只存掩码。"""
    if store is None:
        return
    try:
        if result is not None:
            store.insert_probe(
                ts=started,
                base_url=target_request.base_url,
                model=target_request.model,
                api_key_masked=mask_key(target_request.api_key),
                verdict=result.get("verdict"),
                verdict_reason=result.get("verdict_reason"),
                family=result.get("family"),
                family_p=result.get("family_p"),
                top_model=result.get("top_model"),
                top_p=result.get("top_p"),
                top_gap=result.get("top_gap"),
                valid_samples=result.get("valid_samples"),
                rounds=target_request.rounds,
                questions=target_request.questions,
                latency_ms=result.get("latency_ms"),
                error="; ".join(result.get("errors") or [])[:500] or None,
            )
        else:
            store.insert_probe(
                ts=started,
                base_url=target_request.base_url,
                model=target_request.model,
                api_key_masked=mask_key(target_request.api_key),
                verdict=None,
                verdict_reason=None,
                family=None,
                family_p=None,
                top_model=None,
                top_p=None,
                top_gap=None,
                valid_samples=0,
                rounds=target_request.rounds,
                questions=target_request.questions,
                latency_ms=None,
                error=error,
            )
    except Exception:  # 留底失败不影响主流程
        logger.exception("历史留底失败（目标已脱敏：%s）", mask_key(target_request.api_key))


@app.post("/probe", response_model=ProbeResponse)
def probe(request: ProbeRequest):
    if holder is None:
        raise HTTPException(status_code=503, detail="服务未就绪：指纹库未加载")
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        result = run_probe(
            holder,
            base_url=request.base_url.strip(),
            api_key=request.api_key,
            model=request.model.strip(),
            pin=request.pin,
            rounds=request.rounds,
            questions=request.questions,
            timeout_s=request.timeout_s,
            temperature=request.temperature,
        )
    except RuntimeError as error:
        _persist(request, None, str(error), started_at)
        raise HTTPException(status_code=502, detail=str(error))
    _persist(request, result, None, started_at)
    return result


@app.post("/probe_batch")
def probe_batch(batch: ProbeBatchRequest):
    if holder is None:
        raise HTTPException(status_code=503, detail="服务未就绪：指纹库未加载")
    if any(target.rounds * target.questions <= 0 for target in batch.targets):
        raise HTTPException(status_code=422, detail="rounds/questions 必须为正")

    results: list[dict] = []

    def run_one(target: ProbeRequest) -> dict:
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            result = run_probe(
                holder,
                base_url=target.base_url.strip(),
                api_key=target.api_key,
                model=target.model.strip(),
                pin=target.pin,
                rounds=target.rounds,
                questions=target.questions,
                timeout_s=target.timeout_s,
                temperature=target.temperature,
            )
            _persist(target, result, None, started_at)
            return result
        except RuntimeError as error:
            _persist(target, None, str(error), started_at)
            return {
                "target": f"{target.base_url.rstrip('/')}::{target.model}",
                "verdict": "unknown",
                "verdict_reason": "探针失败",
                "error": str(error),
                "valid_samples": 0,
            }

    with ThreadPoolExecutor(max_workers=config.BATCH_CONCURRENCY) as pool:
        futures = [pool.submit(run_one, target) for target in batch.targets]
        for future in as_completed(futures):
            results.append(future.result())
    # 保持与请求一致的顺序输出
    order = {f"{t.base_url.rstrip('/')}::{t.model}": i for i, t in enumerate(batch.targets)}
    results.sort(key=lambda item: order.get(item.get("target", ""), 999))
    return {"count": len(results), "results": results}


@app.get("/history", response_model=list[HistoryRow])
def history(
    model: str | None = Query(None, description="按目标模型名过滤"),
    days: int | None = Query(None, ge=1, le=365, description="只看最近 N 天"),
    limit: int = Query(100, ge=1, le=1000),
):
    if store is None:
        raise HTTPException(status_code=503, detail="服务未就绪")
    return store.query(model=model, days=days, limit=limit)


@app.get("/health")
def health():
    return {
        "status": "ok" if holder is not None else "starting",
        "service": config.SERVICE_TITLE,
        "version": config.SERVICE_VERSION,
        "bank": {
            "path": str(config.BANK_PATH),
            "models": len(holder.model_ids) if holder else 0,
            "built_at": (holder.bank.get("built_at") if holder else None),
        },
        "history": store.stats() if store else {"total": 0, "by_verdict": {}},
    }
