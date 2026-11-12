# -*- coding: utf-8 -*-
"""AI 自助模式 mock 自测（不花真钱、不外呼）：
- /challenge → /submit happy path：用库内真实参考回答当模型输出，断言 clean / degraded；
- challenge 过期 / 不存在：404；
- 样本不足 / 越界 index：422；
- 鉴权依赖开关两态：DZ_API_TOKEN 设置后无 token 401、带 token 放行，未设置放行；
- GET / 与 GET /llms.txt：说明块可抓取、无密钥。
"""
import json
import sqlite3
import sys
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import ai_mode, config
from api.main import app

ROOT = Path(__file__).resolve().parents[1]


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
    monkeypatch.delenv("DZ_API_TOKEN", raising=False)  # 默认关鉴权，开关两态在各自测试里控制
    ai_mode.reset_challenge_state()
    with TestClient(app) as test_client:
        yield test_client, tmp_path
    ai_mode.reset_challenge_state()


def take_challenge(client: TestClient, count: int = 3, model_hint: str | None = None) -> dict:
    payload = {"count": count}
    if model_hint is not None:
        payload["model_hint"] = model_hint
    response = client.post("/challenge", json=payload)
    assert response.status_code == 200
    return response.json()


def test_challenge_then_submit_astra_clean(client):
    test_client, _ = client
    body = take_challenge(test_client, count=3, model_hint="gpt-6.1-sol")
    assert len(body["challenges"]) == 3
    assert body["challenges"][0]["index"] == 0
    assert body["challenges"][0]["expected_count"] > 0
    assert body["model_hint"] == "gpt-6.1-sol"
    assert "原样" in body["usage_hint"]

    text = load_reference_text("gpt-6-astra")
    response = test_client.post("/submit", json={
        "challenge_id": body["challenge_id"],
        "target_model": "some-proxy-model",  # 只作展示，不影响判定
        "answers": [{"index": i, "text": text} for i in range(3)],
    })
    assert response.status_code == 200
    result = response.json()
    assert result["verdict"] == "clean"
    assert result["top_model"] == "gpt-6-astra"
    assert result["family"] == "gpt"
    assert result["valid_samples"] == 3
    assert result["target_model"] == "some-proxy-model"
    assert len(result["per_challenge"]) == 3
    assert all(item["valid"] for item in result["per_challenge"])
    assert all(item["parsed_count"] >= 80 for item in result["per_challenge"])


def test_challenge_then_submit_luna_degraded(client):
    test_client, _ = client
    body = take_challenge(test_client)
    text = load_reference_text("gpt-6-luna")
    response = test_client.post("/submit", json={
        "challenge_id": body["challenge_id"],
        "answers": [{"index": i, "text": text} for i in range(3)],
    })
    assert response.status_code == 200
    assert response.json()["verdict"] == "degraded"


def test_submit_partial_valid_samples(client):
    test_client, _ = client
    body = take_challenge(test_client)
    text = load_reference_text("gpt-6-astra")
    response = test_client.post("/submit", json={
        "challenge_id": body["challenge_id"],
        "answers": [{"index": 0, "text": text}, {"index": 1, "text": "抱歉，我无法完成该任务。"}],
    })
    assert response.status_code == 200
    result = response.json()
    assert result["valid_samples"] == 1
    assert result["per_challenge"][0]["valid"] is True
    assert result["per_challenge"][1]["valid"] is False
    # 没交答案的题也在 per_challenge 里，标 invalid
    assert result["per_challenge"][2] == {"index": 2, "parsed_count": 0, "valid": False}


def test_submit_no_valid_answer_422(client):
    test_client, _ = client
    body = take_challenge(test_client)
    response = test_client.post("/submit", json={
        "challenge_id": body["challenge_id"],
        "answers": [{"index": i, "text": "抱歉，我无法完成该任务。"} for i in range(3)],
    })
    assert response.status_code == 422
    assert "没有可用回答" in response.json()["detail"]


def test_submit_out_of_range_index_422(client):
    test_client, _ = client
    body = take_challenge(test_client, count=2)
    response = test_client.post("/submit", json={
        "challenge_id": body["challenge_id"],
        "answers": [{"index": 5, "text": "123"}],
    })
    assert response.status_code == 422
    assert "越界" in response.json()["detail"]


def test_submit_unknown_challenge_404(client):
    test_client, _ = client
    response = test_client.post("/submit", json={
        "challenge_id": "no-such-id",
        "answers": [{"index": 0, "text": "123"}],
    })
    assert response.status_code == 404


def test_submit_expired_challenge_404(client):
    test_client, tmp_path = client
    body = take_challenge(test_client)
    challenge_id = body["challenge_id"]
    # 模拟过期：清内存 + 把 sqlite 留底里的 expires_at 改成过去（重启兜底路径同样校验过期）
    ai_mode.reset_challenge_state()
    conn = sqlite3.connect(str(tmp_path / "history.sqlite3"))
    conn.execute("UPDATE ai_challenges SET expires_at = ?", ("2000-01-01T00:00:00+00:00",))
    conn.commit()
    conn.close()
    response = test_client.post("/submit", json={
        "challenge_id": challenge_id,
        "answers": [{"index": 0, "text": "123"}],
    })
    assert response.status_code == 404
    assert "不存在或已过期" in response.json()["detail"]


def test_challenge_persisted_to_sqlite(client):
    test_client, tmp_path = client
    body = take_challenge(test_client)
    conn = sqlite3.connect(str(tmp_path / "history.sqlite3"))
    row = conn.execute(
        "SELECT id, question_count, submitted_at FROM ai_challenges WHERE id = ?",
        (body["challenge_id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[1] == len(body["challenges"])
    assert row[2] is None  # 尚未提交


def test_submit_submission_recorded(client):
    test_client, tmp_path = client
    body = take_challenge(test_client)
    text = load_reference_text("gpt-6-astra")
    test_client.post("/submit", json={
        "challenge_id": body["challenge_id"],
        "target_model": "proxy-x",
        "answers": [{"index": i, "text": text} for i in range(3)],
    })
    conn = sqlite3.connect(str(tmp_path / "history.sqlite3"))
    row = conn.execute(
        "SELECT submitted_at, target_model, verdict, valid_samples FROM ai_challenges WHERE id = ?",
        (body["challenge_id"],),
    ).fetchone()
    conn.close()
    assert row[0] is not None
    assert row[1] == "proxy-x"
    assert row[2] == "clean"
    assert row[3] == 3


def test_auth_enabled_rejects_missing_and_wrong_token(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setenv("DZ_API_TOKEN", "secret-token-0123456789abcdef")
    # 无 token -> 401
    assert test_client.post("/challenge", json={"count": 1}).status_code == 401
    assert test_client.post("/submit", json={"challenge_id": "x", "answers": [{"index": 0, "text": "1"}]}).status_code == 401
    # 错 token -> 401
    assert test_client.post(
        "/challenge", json={"count": 1}, headers={"Authorization": "Bearer wrong"},
    ).status_code == 401
    # 对 token -> 放行
    assert test_client.post(
        "/challenge", json={"count": 1}, headers={"Authorization": "Bearer secret-token-0123456789abcdef"},
    ).status_code == 200


def test_auth_disabled_open(client, monkeypatch):
    test_client, _ = client
    monkeypatch.delenv("DZ_API_TOKEN", raising=False)
    assert test_client.post("/challenge", json={"count": 1}).status_code == 200


def test_home_and_llms_txt(client):
    test_client, _ = client
    home = test_client.get("/")
    assert home.status_code == 200
    assert "text/html" in home.headers["content-type"]
    assert 'id="ai-instructions"' in home.text
    assert "POST /challenge" in home.text
    assert "POST /submit" in home.text
    assert "luna" in home.text and "astra" in home.text

    llms = test_client.get("/llms.txt")
    assert llms.status_code == 200
    assert "text/plain" in llms.headers["content-type"]
    assert "AI 自助模式使用说明" in llms.text
    assert "Authorization: Bearer <token>" in llms.text
    # 红线：说明里不放任何密钥
    assert "sk-" not in home.text
    assert "sk-" not in llms.text
