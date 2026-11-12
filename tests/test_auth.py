# -*- coding: utf-8 -*-
"""鉴权 middleware mock 自测（不花真钱）：
- 设置 DZ_API_TOKEN 后：无/错 token 打业务端点 -> 401；/health 与首页豁免；
- 带 token 正常放行进业务；
- 未设置 DZ_API_TOKEN：行为与从前一致（全放行）。
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.auth import generate_token, get_token
from api.main import app

TOKEN = "a" * 48  # 48 位十六进制样例（测试值，非真实密钥）


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_generate_token_hex_and_length():
    token = generate_token()
    assert len(token) >= 32
    assert all(c in "0123456789abcdef" for c in token)


def test_no_token_env_allows_all(client, monkeypatch):
    """未设置 DZ_API_TOKEN：业务端点直接放行（本地开发行为不变）。"""
    monkeypatch.delenv("DZ_API_TOKEN", raising=False)
    assert get_token() == ""
    response = client.get("/history", params={"limit": 1})
    assert response.status_code == 200
    response = client.get("/health")
    assert response.status_code == 200


def test_missing_token_returns_401(client, monkeypatch):
    """设置 token 后：无 Authorization 头打业务端点 -> 401。"""
    monkeypatch.setenv("DZ_API_TOKEN", TOKEN)
    response = client.post("/probe", json={})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    response = client.get("/history")
    assert response.status_code == 401


def test_wrong_token_returns_401(client, monkeypatch):
    """错 token -> 401，且 detail 不泄露预期值。"""
    monkeypatch.setenv("DZ_API_TOKEN", TOKEN)
    response = client.get("/history", headers={"Authorization": f"Bearer {'b' * 48}"})
    assert response.status_code == 401
    assert TOKEN not in response.text


def test_health_and_home_exempt(client, monkeypatch):
    """设置 token 后：/health 与首页豁免鉴权。"""
    monkeypatch.setenv("DZ_API_TOKEN", TOKEN)
    assert client.get("/health").status_code == 200
    response = client.get("/")
    # 首页是静态页（模板重定向或 200），关键是不因鉴权被拦 401
    assert response.status_code != 401


def test_valid_token_passes_through(client, monkeypatch):
    """带对 token：正常进业务（/history 200）。"""
    monkeypatch.setenv("DZ_API_TOKEN", TOKEN)
    response = client.get("/history", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
