# -*- coding: utf-8 -*-
"""洞真 API Bearer Token 鉴权（双形态合一）。

用法：设置环境变量 DZ_API_TOKEN（>=32 位十六进制）后，
除豁免路径（PUBLIC_PATHS 精确匹配 + PUBLIC_PREFIXES 前缀匹配：
/health、首页 /、/llms.txt、统一库 JSON、/static/* 静态资源）外，
所有端点要求
    Authorization: Bearer <token>
无 / 错 token 一律 401；未设置变量时全部放行（本地开发行为不变）。

两种形态：
1) TokenAuthMiddleware —— 纯 ASGI middleware 全局兜底（main.py 两行接线）；
2) require_token —— FastAPI 依赖函数，供个别端点显式声明用
   （全局中间件在场时冗余但无害，语义一致，均每请求动态读环境变量）。
"""
from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

# 鉴权豁免路径：健康检查 + 极简首页 + 给 AI 的说明书 + 首页渲染所需的公开静态资源
# （/static/* 的 CSS/JS 与统一指纹库 JSON 不含敏感数据，页面要在未登录时能完整加载；
#   旧版 /api/challenges、/api/analyze 已随全功能页下线，豁免面相应收缩）
PUBLIC_PATHS = {"/health", "/", "/llms.txt", "/data/unified_bank.json"}
PUBLIC_PREFIXES = ("/static/",)


# 随机生成一把 48 位十六进制 token 的便捷函数（运维手动调用）
def generate_token() -> str:
    return secrets.token_hex(24)  # 48 位十六进制


def get_token() -> str:
    """每请求动态读，方便测试与运行期变更；空串视为未设置。"""
    return (os.environ.get("DZ_API_TOKEN") or "").strip()


class TokenAuthMiddleware:
    """设置 DZ_API_TOKEN 后强制 Bearer 鉴权，未设置则放行。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = get_token()
        path = scope.get("path", "")
        if not token or path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return
        auth_header = Headers(scope=scope).get("Authorization", "")
        if secrets.compare_digest(auth_header, f"Bearer {token}"):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            {"detail": "无效或缺失的 Bearer token"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


def _expected_token() -> str:
    return get_token()


def require_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI 依赖：DZ_API_TOKEN 设置则校验 Bearer，未设置放行。

    用法：@router.post("/xxx", dependencies=[Depends(require_token)])
    """
    token = _expected_token()
    if not token:
        return
    if not secrets.compare_digest(authorization or "", f"Bearer {token}"):
        raise HTTPException(
            status_code=401,
            detail="无效或缺失的 Bearer token（需要 Authorization: Bearer <token>）",
            headers={"WWW-Authenticate": "Bearer"},
        )
