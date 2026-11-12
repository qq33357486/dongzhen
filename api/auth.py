# -*- coding: utf-8 -*-
"""独立鉴权模块：读环境变量 DZ_API_TOKEN，以 FastAPI 依赖函数方式供端点复用。
- 未设置（或为空）：放行，本地/内网行为不变；
- 设置了：要求请求头 Authorization: Bearer <token>，不匹配返回 401。
只在请求处理时读环境变量（不缓存），方便测试与运行期变更。
"""
from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException


def _expected_token() -> str:
    return (os.environ.get("DZ_API_TOKEN") or "").strip()


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
