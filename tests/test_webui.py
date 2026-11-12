# -*- coding: utf-8 -*-
"""原版 web 工具页（api/webui.py）mock 自测（不花真钱、不外呼）：
- GET / 是上游原版页面（模型测试/指纹库管理），且内藏 ai-instructions AI 提示词块；
- 地址红线：对外可见产物（首页 HTML、/llms.txt、说明文案模块）只允许 https://dz.duojie.games，
  绝不出现 127.0.0.1 / localhost / 内网地址；
- /api/challenges、/api/analyze 与原 app.py 行为一致；/api/banks 摘要来自统一库；
- 部署形态：在线建库/自动采集返回 400；/api/test/probe 可 mock 单题代调；
- 鉴权豁免面：/api/challenges、/api/analyze 与首页/llms.txt 一样公开（纯计算，不碰 key 不外呼）。
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


def load_reference_text(source: str) -> str:
    """从 gpt_reference.jsonl 取某模型第一条有效回答全文（真实指纹数据，不花钱）。"""
    with (ROOT / "data" / "gpt_reference.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row["source"] == source and row.get("strict_valid"):
                return row["text"]
    raise RuntimeError(f"参考数据里找不到 {source}")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_DB", tmp_path / "history.sqlite3")
    monkeypatch.delenv("DZ_API_TOKEN", raising=False)
    with TestClient(app) as test_client:
        yield test_client


# ---- 原版首页恢复 ----


def test_home_is_original_tool_page(client):
    """首页必须是上游原版 web 工具页（模型测试/指纹库管理），不再是 AI 说明书页。"""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    # 原版页面标志：ModelTrace 顶栏、两个工作区、手动/自动测试 Tab、统一库摘要注入
    assert "ModelTrace" in body
    assert "模型测试" in body and "指纹库管理" in body
    assert "手动测试" in body and "API 自动测试" in body
    assert "window.BANK_SUMMARIES" in body and "window.DEFAULT_BANK_ID" in body
    # 库摘要来自统一库（18 模型），datalist 有库内模型
    assert "gpt-6-astra" in body
    # 静态资源按 FastAPI 挂载路径引用
    assert "/static/styles.css" in body and "/static/app.js" in body
    # 旧的"AI 说明书整页"标志不应再是页面主体（页面标题回归原版）
    assert "<title>ModelTrace</title>" in body


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
    """静态资源用相对路径：无论访客用什么 Host（含 127.0.0.1/IP）访问，
    页面都不会把请求方地址回写进 HTML（url_for 绝对 URL 会泄漏，故禁用）。"""
    home = client.get("/", headers={"host": "127.0.0.1:8199"}).text
    assert "127.0.0.1" not in home
    assert 'href="/static/styles.css"' in home
    assert 'src="/static/app.js"' in home


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


# ---- web 工具接口（原 app.py 行为）----


def test_api_challenges(client):
    response = client.get("/api/challenges")
    assert response.status_code == 200
    challenges = response.json()["challenges"]
    assert len(challenges) == 3
    for item in challenges:
        assert item["prompt"] and item["expected_count"] > 0


def test_api_analyze_with_reference_answers(client):
    outputs = [
        {"text": load_reference_text("gpt-6-astra"), "expected_count": 300},
        {"text": load_reference_text("gpt-6-astra"), "expected_count": 310},
        {"text": load_reference_text("gpt-6-astra"), "expected_count": 320},
    ]
    response = client.post("/api/analyze", json={"outputs": outputs})
    assert response.status_code == 200
    body = response.json()
    assert body["prediction"] == "gpt-6-astra"
    assert body["family_prediction"] == "gpt"
    assert 0 < body["probability"] <= 1
    assert body["bank"]["model_count"] == 18


def test_api_analyze_rejects_bad_payload(client):
    response = client.post("/api/analyze", json={"outputs": []})
    assert response.status_code == 400
    assert "error" in response.json()
    response = client.post("/api/analyze", json={})
    assert response.status_code == 400


def test_api_banks_summary_from_unified(client):
    response = client.get("/api/banks")
    assert response.status_code == 200
    banks = response.json()
    assert list(banks.keys()) == ["unified"]
    summary = banks["unified"]
    assert summary["model_count"] == 18
    assert {item["id"] for item in summary["families"]} == {"gpt", "claude"}
    # 单库查询：未知 id 报 400
    assert client.get("/api/bank", params={"bank_id": "nope"}).status_code == 400
    assert client.get("/api/bank").status_code == 200


def test_create_bank_and_enroll_readonly_400(client):
    """部署版只带统一库：在线建库/自动采集不开放，返回 400 + 说明。"""
    response = client.post("/api/banks", json={"label": "Llama"})
    assert response.status_code == 400
    assert "统一" in response.json()["error"]
    response = client.post("/api/enroll/auto", json={"base_url": "https://x/v1", "api_key": "k", "api_model": "m"})
    assert response.status_code == 400


def test_api_test_probe_mocked(client, monkeypatch):
    text = load_reference_text("gpt-6-astra")

    def fake_request_completion(base_url, api_key, api_model, prompt, temperature, api_format="auto", system_prompt="", timeout=240):
        return text

    monkeypatch.setattr("api.webui.request_completion", fake_request_completion)
    response = client.post("/api/test/probe", json={
        "base_url": "https://mock.example/v1",
        "api_key": "sk-mock-webui",
        "api_model": "proxy-model",
        "prompt": "题面",
        "expected_count": 300,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True and body["parsed_numbers"] >= body["minimum_numbers"]
    # 缺字段 400
    assert client.post("/api/test/probe", json={"base_url": "x"}).status_code == 400


def test_static_assets_served(client):
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


# ---- 鉴权豁免面 ----


def test_public_compute_endpoints_exempt_from_auth(client, monkeypatch):
    """设置 token 后：纯计算端点（/api/challenges、/api/analyze）与首页/llms.txt 一样公开。"""
    monkeypatch.setenv("DZ_API_TOKEN", "secret-token-0123456789abcdef")
    assert client.get("/").status_code == 200
    assert client.get("/llms.txt").status_code == 200
    assert client.get("/api/challenges").status_code == 200
    assert client.post("/api/analyze", json={"outputs": []}).status_code == 400  # 进了业务逻辑（非 401）
    # 业务/外呼端点仍要 token
    assert client.post("/api/test/probe", json={}).status_code == 401
    assert client.get("/api/banks").status_code == 401
