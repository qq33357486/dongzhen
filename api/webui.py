# -*- coding: utf-8 -*-
"""极简首页（照上游 GitHub Pages 样板 static/index.html 复刻，品牌换成洞真）：
- GET /                      渲染 templates/index.html：指纹库加载提示 + 三次挑战/重新生成/
  计算归因概率 三件套 + 结果区；归因计算全部在浏览器本地完成
  （/static/pages-app.js + fingerprint-core.js + challenge-browser.js）；
- GET /data/unified_bank.json 页面异步加载的统一指纹库（与服务器判定同一份 config.BANK_PATH）；
- GET /static/*              静态资源（main.py 挂载）。

AI 提示词内藏：模板里 <script type="text/markdown" id="ai-instructions">（内容=AI_INSTRUCTIONS，
与 /llms.txt 同源），对 AI 抓取可读、对人不可见。

旧版全功能 web 工具页（在线建库/自动采集/API 测试/bank 管理）已整体下线：
对应 /api/challenges、/api/analyze、/api/test/probe、/api/bank(s)、/api/enroll/auto
全部删除，不再返回 400 占位（线上暴露面最小，见 tests/test_webui.py）。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from api.ai_instructions import AI_INSTRUCTIONS
from api.config import BANK_PATH, TEMPLATES_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    """主页 = 极简本地归因页；AI 提示词内藏在 ai-instructions 块（对人不可见）。"""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"ai_instructions": AI_INSTRUCTIONS},
    )


@router.get("/data/unified_bank.json")
def unified_bank_json():
    """页面本地归因要拉的统一指纹库（与服务器判定用同一份文件，无单独副本）。"""
    return FileResponse(BANK_PATH, media_type="application/json")
