# -*- coding: utf-8 -*-
"""AI 自助模式（无状态版）mock 自测（不花真钱、不外呼）：
- /challenge → /submit happy path：库内真实参考回答当模型输出，断言 clean / degraded；
- 自包含凭证：篡改签名 / 坏格式 422、过期 404、跨重启同一 id 可交卷；
- 判定结果仍写 probes 历史留底（题目状态零存储）；
- 鉴权依赖开关两态；GET / 与 GET /llms.txt 说明块可抓取、无密钥。
"""
import json
import sqlite3
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import ai_mode, config
from api.main import app
from fingerprint import encode_challenge_template, generate_challenges, render_challenge

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
    monkeypatch.delenv("DZ_API_TOKEN", raising=False)         # 默认关鉴权，开关两态在各自测试里控制
    monkeypatch.delenv("DZ_CHALLENGE_SECRET", raising=False)  # 默认用回退链，密钥口径在专门测试里覆盖
    with TestClient(app) as test_client:
        yield test_client, tmp_path


def take_challenge(client: TestClient, count: int = 3, model_hint: str | None = None) -> dict:
    payload = {"count": count}
    if model_hint is not None:
        payload["model_hint"] = model_hint
    response = client.post("/challenge", json=payload)
    assert response.status_code == 200
    return response.json()


def test_render_challenge_deterministic():
    """(模板号, N) 确定性重建：同参数同题面、generate_challenges 可 round-trip、越界报错。"""
    template = encode_challenge_template(2, 1, 3, 2)
    assert render_challenge(template, 317) == render_challenge(template, 317)
    assert f" 317 个 1 到 355" in render_challenge(template, 317)
    assert render_challenge(template, 317) != render_challenge(template, 292)
    # generate_challenges 产出的 (template, expected_count) 渲染结果与 prompt 逐字一致
    for item in generate_challenges(5):
        assert render_challenge(item["template"], item["expected_count"]) == item["prompt"]
    with pytest.raises(ValueError):
        render_challenge(5 * 5 * 5 * 4, 300)  # 模板号越界


def test_challenge_then_submit_astra_clean(client):
    test_client, _ = client
    body = take_challenge(test_client, count=3, model_hint="gpt-6.1-sol")
    # 自包含凭证形态：负载.签名 两段；响应带过期时间
    assert body["challenge_id"].count(".") == 1
    assert len(body["challenge_id"]) > 100
    assert body["expires_at"]
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
    assert result["model_hint"] == "gpt-6.1-sol"  # 从凭证里解出回显
    assert len(result["per_challenge"]) == 3
    assert all(item["valid"] for item in result["per_challenge"])
    # 族口径新字段：candidates 非空、按概率降序、top1 与 top_model 一致；旧字段仍在（向后兼容）
    candidates = result["candidates"]
    assert candidates and candidates[0]["model"] == "gpt-6-astra"
    assert all({"model", "p"} <= set(item) and 0 <= item["p"] <= 1 for item in candidates)
    assert all(candidates[i]["p"] >= candidates[i + 1]["p"] for i in range(len(candidates) - 1))
    assert isinstance(result["candidates_tied"], bool)
    for legacy in ("top_model", "top_p", "family", "family_p", "verdict", "verdict_reason"):
        assert legacy in result


def test_challenge_then_submit_luna_degraded(client):
    test_client, _ = client
    body = take_challenge(test_client)
    text = load_reference_text("gpt-6-luna")
    response = test_client.post("/submit", json={
        "challenge_id": body["challenge_id"],
        "answers": [{"index": i, "text": text} for i in range(3)],
    })
    assert response.status_code == 200
    result = response.json()
    assert result["verdict"] == "degraded"
    # 族口径：候选列表 top 也是 luna，与 verdict 口径一致
    assert result["candidates"][0]["model"] == "gpt-6-luna"
    assert result["top_model"] == "gpt-6-luna"  # 旧字段兼容


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


def test_submit_tampered_challenge_id_rejected(client):
    test_client, _ = client
    body = take_challenge(test_client)
    challenge_id = body["challenge_id"]
    # 篡改签名段：改最后一个字符
    tampered = challenge_id[:-1] + ("0" if challenge_id[-1] != "0" else "1")
    assert tampered != challenge_id
    response = test_client.post("/submit", json={
        "challenge_id": tampered, "answers": [{"index": 0, "text": "123"}],
    })
    assert response.status_code == 422
    assert "签名" in response.json()["detail"]
    # 篡改负载段（base64 部分中间字符）同样过不了验签
    payload_part, sig = challenge_id.split(".")
    mid = len(payload_part) // 2
    swapped = ("A" if payload_part[mid] != "A" else "B")
    tampered_payload = payload_part[:mid] + swapped + payload_part[mid + 1:]
    response = test_client.post("/submit", json={
        "challenge_id": f"{tampered_payload}.{sig}", "answers": [{"index": 0, "text": "123"}],
    })
    assert response.status_code == 422
    # 坏格式：没有签名段
    response = test_client.post("/submit", json={
        "challenge_id": "not-a-valid-id", "answers": [{"index": 0, "text": "123"}],
    })
    assert response.status_code == 422


def test_submit_expired_challenge_404(client, monkeypatch):
    test_client, _ = client
    # 出题时把服务时间拨回 2 天前：id 里的签发时间即过期，交卷（真实时间）返回 404
    past = datetime.now(timezone.utc) - timedelta(days=2)
    monkeypatch.setattr(ai_mode, "_now", lambda: past)
    body = take_challenge(test_client)
    monkeypatch.undo()
    response = test_client.post("/submit", json={
        "challenge_id": body["challenge_id"],
        "answers": [{"index": 0, "text": load_reference_text("gpt-6-astra")}],
    })
    assert response.status_code == 404
    assert "过期" in response.json()["detail"]


def test_submit_works_across_restart(client, tmp_path):
    """跨重启语义：无状态下天然成立——重启（lifespan 重跑）后凭同一 id 交卷成功。"""
    with TestClient(app) as test_client:
        body = take_challenge(test_client, count=2)
        challenge_id = body["challenge_id"]
    # with 退出即 lifespan shutdown；重新进入 = 模拟服务重启（库与 store 全部重建）
    with TestClient(app) as restarted_client:
        text = load_reference_text("gpt-6-astra")
        response = restarted_client.post("/submit", json={
            "challenge_id": challenge_id,
            "target_model": "proxy-after-restart",
            "answers": [{"index": i, "text": text} for i in range(2)],
        })
    assert response.status_code == 200
    assert response.json()["verdict"] == "clean"


def test_submit_verdict_recorded_to_history(client, tmp_path):
    """判定结果写 probes 历史留底（base_url=ai-mode、key 掩 ****）；题目状态零存储。"""
    test_client, tmp_path = client
    body = take_challenge(test_client)
    text = load_reference_text("gpt-6-astra")
    test_client.post("/submit", json={
        "challenge_id": body["challenge_id"],
        "target_model": "proxy-x",
        "answers": [{"index": i, "text": text} for i in range(3)],
    })
    conn = sqlite3.connect(str(tmp_path / "history.sqlite3"))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = conn.execute(
        "SELECT base_url, model, api_key_masked, verdict, valid_samples FROM probes"
        " WHERE base_url = 'ai-mode'"
    ).fetchall()
    conn.close()
    assert "ai_challenges" not in tables          # 题目状态表已删
    assert rows == [("ai-mode", "proxy-x", "****", "clean", 3)]


def test_challenge_id_bound_to_secret(client, monkeypatch):
    """换签名密钥后旧 id 立即失效（验签不过），防伪造。"""
    test_client, _ = client
    body = take_challenge(test_client)
    monkeypatch.setenv("DZ_CHALLENGE_SECRET", "another-secret-value")
    response = test_client.post("/submit", json={
        "challenge_id": body["challenge_id"], "answers": [{"index": 0, "text": "123"}],
    })
    assert response.status_code == 422
    assert "签名" in response.json()["detail"]


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
    assert "不存" in home.text  # 无状态口径

    llms = test_client.get("/llms.txt")
    assert llms.status_code == 200
    assert "text/plain" in llms.headers["content-type"]
    assert "AI 自助模式使用说明" in llms.text
    assert "Authorization: Bearer <token>" in llms.text
    assert "不存" in llms.text
    # 红线：说明里不放任何密钥
    assert "sk-" not in home.text
    assert "sk-" not in llms.text
