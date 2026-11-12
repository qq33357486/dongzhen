# -*- coding: utf-8 -*-
"""原版 web 工具页（上游作者 app.py 的 Flask 页面）挂到 FastAPI：
- GET  /                渲染 templates/index.html（手动粘贴答案 -> 归因，页面功能保持原样）；
- GET  /static/*        静态资源（styles.css / app.js 等，main.py 挂载）；
- GET  /api/challenges  出题（原 app.py 同名接口）；
- POST /api/analyze     归因（对着统一库，原 app.py 同名接口）；
- POST /api/test/probe  单题代调（API 自动测试模式，原 app.py 同名接口）；
- GET  /api/bank(s)     指纹库摘要（部署里只有统一库）；
- POST /api/banks 与 /api/enroll/auto：采集/建库需要本地 data 文件，部署版返回 400 说明。

AI 提示词内藏：模板里 <script type="text/markdown" id="ai-instructions">（内容=AI_INSTRUCTIONS，
与 /llms.txt 同源），对 AI 抓取可读、对人不可见。
"""
from __future__ import annotations

import logging
import math
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from enrollment import request_completion
from fingerprint import FAMILY_DISPLAY_NAMES, analyze_global_outputs, generate_challenges, parse_numbers

from api.ai_instructions import AI_INSTRUCTIONS
from api.config import TEMPLATES_DIR

logger = logging.getLogger("dongzhen")

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# 部署形态：只带统一库（data/unified_bank.json），库管理写入类操作不开放
UNIFIED_BANK_ID = "unified"
UNIFIED_BANK_LABEL = "全部指纹"
_READONLY_MESSAGE = (
    "服务部署只带统一指纹库，不支持在线创建指纹库/自动采集；"
    "如需采集新指纹，请在本仓库本地运行 python app.py 操作后重建统一库。"
)

_bank_holder = None


def set_bank_holder(holder) -> None:
    """main.py lifespan 调用：注入已加载的统一指纹库（复用 BankHolder）。"""
    global _bank_holder
    _bank_holder = holder


def reset_state() -> None:
    """测试夹具用。"""
    global _bank_holder
    _bank_holder = None


def _bank() -> dict:
    if _bank_holder is None:
        raise RuntimeError("指纹库未加载")
    return _bank_holder.bank


def unified_summary() -> dict:
    """统一库摘要：形状对齐原 app.py 的 bank_summary/summarized_unified_bank，模板无需改动。"""
    bank = _bank()
    models = bank.get("models", [])
    family_counts: dict[str, int] = {}
    for model in models:
        family_id = model.get("family") or "models"
        family_counts[family_id] = family_counts.get(family_id, 0) + 1
    return {
        "id": UNIFIED_BANK_ID,
        "label": UNIFIED_BANK_LABEL,
        "model_count": len(models),
        "response_count": sum(int(model.get("response_count") or 0) for model in models),
        "number_count": sum(int(model.get("valid_number_count") or 0) for model in models),
        "family_count": len(family_counts),
        "families": [
            {
                "id": family_id,
                "label": FAMILY_DISPLAY_NAMES.get(family_id, family_id),
                "model_count": count,
            }
            for family_id, count in family_counts.items()
        ],
        "models": [
            {
                "id": model["id"],
                "display_name": model["display_name"],
                "responses": int(model.get("response_count") or 0),
                "valid_numbers": int(model.get("valid_number_count") or 0),
                "family": model.get("family") or "models",
            }
            for model in models
        ],
    }


def _optional_temperature(payload: dict) -> float | None:
    value = payload.get("temperature")
    return None if value in (None, "") else float(value)


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    """主页 = 上游原版 web 工具页；AI 提示词内藏在 ai-instructions 块（对人不可见）。"""
    summary = unified_summary()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "banks": {UNIFIED_BANK_ID: summary},
            "bank": summary,
            "unified": summary,
            "default_bank_id": UNIFIED_BANK_ID,
            "ai_instructions": AI_INSTRUCTIONS,
        },
    )


@router.get("/api/challenges")
def api_challenges():
    """出题：与原 app.py 相同（3 道整数挑战）。"""
    return {"challenges": generate_challenges(3)}


@router.post("/api/analyze")
def api_analyze(payload: dict[str, Any]):
    """归因：outputs 对着统一库做全局稳健指纹归因（原 app.py 同名接口）。"""
    try:
        outputs = payload["outputs"]
        result = analyze_global_outputs(outputs, _bank())
    except (KeyError, ValueError, TypeError) as error:
        message = str(error) if isinstance(error, ValueError) else "请求缺少 outputs 字段或格式无效"
        return JSONResponse({"error": message}, status_code=400)
    result["bank"] = unified_summary()
    return result


@router.post("/api/test/probe")
def api_test_probe(payload: dict[str, Any]):
    """API 自动测试模式单题：代调目标模型一次并按有效性阈值判定（原 app.py 同名接口）。"""
    try:
        text = request_completion(
            base_url=str(payload["base_url"]).strip(),
            api_key=payload["api_key"],
            api_model=str(payload["api_model"]).strip(),
            prompt=payload["prompt"],
            temperature=_optional_temperature(payload),
            api_format="auto",
        )
        expected_count = int(payload["expected_count"])
        parsed_numbers = len(parse_numbers(text))
        minimum_numbers = max(80, math.ceil(expected_count * 0.55))
        return {
            "text": text,
            "parsed_numbers": parsed_numbers,
            "minimum_numbers": minimum_numbers,
            "accepted": parsed_numbers >= minimum_numbers,
        }
    except KeyError as error:
        return JSONResponse({"error": f"请求缺少字段：{error}"}, status_code=400)
    except Exception as error:
        return JSONResponse({"error": str(error)}, status_code=502)


@router.get("/api/bank")
def api_bank(bank_id: str = UNIFIED_BANK_ID):
    if bank_id != UNIFIED_BANK_ID:
        return JSONResponse({"error": f"未知指纹库：{bank_id}（部署只有统一库）"}, status_code=400)
    return unified_summary()


@router.get("/api/banks")
def api_banks():
    return {UNIFIED_BANK_ID: unified_summary()}


@router.post("/api/banks")
def api_create_bank():
    return JSONResponse({"error": _READONLY_MESSAGE}, status_code=400)


@router.post("/api/enroll/auto")
def api_enroll_auto():
    return JSONResponse({"error": _READONLY_MESSAGE}, status_code=400)
