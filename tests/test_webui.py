# -*- coding: utf-8 -*-
"""极简首页（api/webui.py）mock 自测（不花真钱、不外呼）：
- GET / 是照上游 Pages 样板复刻的极简本地归因页（加载态/三按钮/结果区），品牌「洞真 dongzhen」，
  且内藏 ai-instructions AI 提示词块；旧版全功能页标志（建库/采集/API 测试）一律不出现；
- 地址红线：对外可见产物（首页 HTML、/llms.txt、说明文案模块）只允许 https://dz.duojie.games，
  绝不出现 127.0.0.1 / localhost / 内网地址；
- GET /data/unified_bank.json 提供页面本地归因要拉的统一库；
- 暴露面最小：旧版 web 工具的 /api/*（challenges/analyze/test probe/banks/enroll）一律 404；
- 鉴权豁免面：首页/llms.txt/静态资源/统一库 JSON 公开，业务与外呼端点仍要 token。
"""
import json
import sys
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import config
from api.ai_instructions import AI_INSTRUCTIONS
from api.main import app

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_BASE_URL = "https://dz.duojie.games"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_DB", tmp_path / "history.sqlite3")
    monkeypatch.delenv("DZ_API_TOKEN", raising=False)
    with TestClient(app) as test_client:
        yield test_client


# ---- 极简首页 ----


def test_home_is_minimal_sample_page(client):
    """首页必须是极简样板页：品牌洞真 + 指纹库加载提示 + 三按钮 + 结果区，归因本地算。"""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    # 品牌：洞真 dongzhen + ModelTrace 血统小注
    assert "<title>洞真 dongzhen</title>" in body
    assert "洞真 dongzhen" in body
    assert "ModelTrace" in body
    # 样板三件套：加载态提示、三个按钮、结果展示区、本地计算徽标
    assert "正在加载指纹库" in body
    assert "三次挑战" in body
    assert "重新生成" in body
    assert "计算归因概率" in body
    assert 'id="result"' in body
    assert "本地计算" in body
    # 静态资源按 FastAPI 挂载路径引用
    assert 'href="/static/styles.css"' in body
    assert 'src="/static/pages-app.js"' in body


def test_home_has_no_legacy_full_page_features(client):
    """暴露面最小：旧版全功能页的标志（建库/采集/API 测试/bank 管理）一律不再出现在首页。"""
    body = client.get("/").text
    for legacy in (
        "指纹库管理",
        "手动测试",
        "API 自动测试",
        "API Key",
        "新建指纹库",
        "自动采集并建立指纹",
        "window.BANK_SUMMARIES",
        "/static/app.js",
    ):
        assert legacy not in body, legacy


def test_home_hides_ai_instructions_block(client):
    """AI 提示词内藏在 <script type="text/markdown" id="ai-instructions">：AI 可读、人不可见。"""
    body = client.get("/").text
    assert 'id="ai-instructions"' in body
    assert "text/markdown" in body
    # 两接口用法、判定口径、族语义都在块里（原始文本，未转义）
    assert "POST /challenge" in body
    assert "POST /submit" in body
    assert "族与候选列表" in body
    assert "candidates" in body
    assert "同族模型" in body
    assert "可能是 X / Y / Z" in body


def test_home_and_llms_txt_address_redline(client):
    """地址红线：首页 HTML 与 /llms.txt 只允许 https://dz.duojie.games，
    绝不出现 127.0.0.1 / localhost / 内网地址；说明模块本体同样断言。"""
    home = client.get("/").text
    llms = client.get("/llms.txt").text
    for text in (home, llms, AI_INSTRUCTIONS):
        assert "127.0.0.1" not in text
        assert "localhost" not in text
        assert "0.0.0.0" not in text
        assert "192.168." not in text
        assert "10.0." not in text
        assert PUBLIC_BASE_URL in text
    # 红线：说明里不放任何密钥
    assert "sk-" not in home
    assert "sk-" not in llms


def test_home_static_urls_are_relative(client):
    """静态资源用根相对路径：无论访客用什么 Host（含 127.0.0.1/IP）访问，
    页面都不会把请求方地址回写进 HTML（url_for 绝对 URL 会泄漏，故禁用）。"""
    home = client.get("/", headers={"host": "127.0.0.1:8199"}).text
    assert "127.0.0.1" not in home
    assert 'href="/static/styles.css"' in home
    assert 'src="/static/pages-app.js"' in home


def test_llms_txt_explains_family_semantics(client):
    """/llms.txt 保留且解释族口径：族 + 候选列表 + 同族不可分如实并列。"""
    response = client.get("/llms.txt")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "AI 自助模式使用说明" in response.text
    assert "POST /challenge" in response.text and "POST /submit" in response.text
    assert "candidates" in response.text and "candidates_tied" in response.text
    assert "Authorization: Bearer <token>" in response.text
    assert "不存" in response.text  # 无状态口径


# ---- 极简页的数据来源 ----


def test_unified_bank_json_served_for_local_analysis(client):
    """/data/unified_bank.json：页面本地归因拉的统一库（与服务器判定同一份文件）。"""
    response = client.get("/data/unified_bank.json")
    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]
    bank = response.json()
    assert bank["schema"] == "robust-number-fingerprint-bank"
    assert len(bank["models"]) == 18
    # 页面 JS 按相对路径拉取：./data/unified_bank.json（首页挂在 / 下）
    assert 'fetch("./data/unified_bank.json"' in (
        (ROOT / "static" / "pages-app.js").read_text(encoding="utf-8")
    )


def test_static_assets_served(client):
    """极简页所需静态资源可用；旧版全功能页的 app.js 不再提供。"""
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/repository-link.css").status_code == 200
    assert client.get("/static/pages-app.js").status_code == 200
    assert client.get("/static/fingerprint-core.js").status_code == 200
    assert client.get("/static/challenge-browser.js").status_code == 200
    assert client.get("/static/app.js").status_code == 404


# ---- 暴露面最小：旧版 web 工具接口一律下线 ----


def test_legacy_webui_endpoints_removed(client):
    """旧版全功能页的 /api/* 全部 404（不是 400 占位，是彻底删除）。"""
    assert client.get("/api/challenges").status_code == 404
    assert client.post("/api/analyze", json={"outputs": []}).status_code == 404
    assert client.post("/api/test/probe", json={}).status_code == 404
    assert client.get("/api/bank").status_code == 404
    assert client.get("/api/banks").status_code == 404
    assert client.post("/api/banks", json={"label": "Llama"}).status_code == 404
    assert client.post("/api/enroll/auto", json={}).status_code == 404


# ---- 鉴权豁免面 ----


def test_public_surface_exempt_from_auth(client, monkeypatch):
    """设置 token 后：首页/llms.txt/静态资源/统一库 JSON 公开（页面要能完整加载）；
    业务与外呼端点仍要 token；旧 webui 端点是没了（404）而不是豁免。"""
    monkeypatch.setenv("DZ_API_TOKEN", "secret-token-0123456789abcdef")
    assert client.get("/").status_code == 200
    assert client.get("/llms.txt").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/pages-app.js").status_code == 200
    assert client.get("/data/unified_bank.json").status_code == 200
    # 业务/外呼端点仍要 token
    assert client.post("/probe", json={}).status_code == 401
    assert client.get("/history").status_code == 401
    assert client.post("/challenge", json={}).status_code == 401
    assert client.post("/submit", json={}).status_code == 401
    # 旧 webui 端点已删除（鉴权中间件先于路由拦截，未豁免路径一律 401，
    # 无 token 时的 404 由 test_legacy_webui_endpoints_removed 证明）
    assert client.get("/api/challenges").status_code == 401
    assert client.get("/api/banks").status_code == 401
