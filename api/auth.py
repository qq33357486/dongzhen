# -*- coding: utf-8 -*-
"""洞真 API Bearer Token 鉴权（最小实现）。

用法：设置环境变量 DZ_API_TOKEN（>=32 位十六进制）后，
除豁免路径（/health 与首页 /）外，所有端点要求
    Authorization: Bearer <token>
无 / 错 token 一律 401；未设置变量时全部放行（本地开发行为不变）。

以纯 ASGI middleware 实现，main.py 只需两行接线：
    from api.auth import TokenAuthMiddleware
    app.add_middleware(TokenAuthMiddleware)
"""
from __future__ import annotations

import os
import secrets

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

# 鉴权豁免路径：健康检查 + 首页
PUBLIC_PATHS = {"/health", "/"}

# 随机生成一把 >=32 位十六进制 token 的便捷函数（运维手动调用）
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
        if not token or scope.get("path", "") in PUBLIC_PATHS:
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
