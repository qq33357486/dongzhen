# -*- coding: utf-8 -*-
"""API mock 自测（不花真钱）：
- /probe happy path：mock 上游返回库内真实参考回答，断言判定与 key 脱敏；
- /probe 上游全挂：502；
- /probe_batch：多目标并发；
- /health、/history。
红线断言：完整 api_key 绝不出现在任何响应、任何 sqlite 文件里。
"""
import json
import sys
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import config
from api.main import app
from api.verdict import mask_key

ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = "sk-mock-abcd1234wxyz"


def load_reference_text(source: str) -> tuple[str, int]:
    """从 gpt_reference.jsonl 取某模型第一条有效回答全文与期望数字数（真实指纹数据，不花钱）。"""
    with (ROOT / "data" / "gpt_reference.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row["source"] == source and row.get("strict_valid"):
                return row["text"], int(row["requested_count"] or 0)
    raise RuntimeError(f"参考数据里找不到 {source}")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_DB", tmp_path / "history.sqlite3")
    with TestClient(app) as test_client:
        yield test_client


def mock_completion_factory(text: str):
    def fake_request_completion(base_url, api_key, api_model, prompt, temperature, api_format="openai", system_prompt="", timeout=240):
        return text
    return fake_request_completion


def test_probe_happy_path_astra_clean(client, monkeypatch):
    text, _ = load_reference_text("gpt-6-astra")
    monkeypatch.setattr("api.probe.request_completion", mock_completion_factory(text))
    response = client.post("/probe", json={
        "base_url": "https://mock.example/v1",
        "api_key": FAKE_KEY,
        "model": "gpt-6-astra",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "clean"
    assert body["top_model"] == "gpt-6-astra"
    assert body["family"] == "gpt"
    assert body["valid_samples"] == 3
    assert len(body["per_round"]) == 1
    assert body["cost_estimate"]["output_tokens_est"] > 0
    # 红线：响应里绝不回显 key
    assert FAKE_KEY not in response.text
    assert "abcd1234" not in response.text


def test_probe_luna_degraded(client, monkeypatch):
    text, _ = load_reference_text("gpt-6-luna")
    monkeypatch.setattr("api.probe.request_completion", mock_completion_factory(text))
    response = client.post("/probe", json={
        "base_url": "https://mock.example/v1",
        "api_key": FAKE_KEY,
        "model": "some-proxy-model",
    })
    assert response.status_code == 200
    assert response.json()["verdict"] == "degraded"


def test_probe_pin_out_of_bank(client, monkeypatch):
    text, _ = load_reference_text("gpt-6-astra")
    monkeypatch.setattr("api.probe.request_completion", mock_completion_factory(text))
    response = client.post("/probe", json={
        "base_url": "https://mock.example/v1",
        "api_key": FAKE_KEY,
        "model": "gpt-6.1-sol",
        "pin": "gpt-6.1-sol@s141c592",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["pinned"]["claimed"] == "gpt-6.1-sol"
    assert body["pinned"]["tag"] == "s141c592"
    assert body["pinned"]["in_bank"] is False
    assert "库外" in body["pinned"]["note"]


def test_probe_all_failed_is_502(client, monkeypatch):
    def fake_fail(*args, **kwargs):
        raise RuntimeError("HTTP 401: 无效密钥")
    monkeypatch.setattr("api.probe.request_completion", fake_fail)
    response = client.post("/probe", json={
        "base_url": "https://mock.example/v1",
        "api_key": FAKE_KEY,
        "model": "gpt-6-astra",
    })
    assert response.status_code == 502
    assert "401" in response.json()["detail"]
    assert FAKE_KEY not in response.text


def test_probe_batch(client, monkeypatch):
    text_astra, _ = load_reference_text("gpt-6-astra")
    text_luna, _ = load_reference_text("gpt-6-luna")
    texts = {"gpt-6-astra": text_astra, "gpt-6-luna": text_luna}

    def fake_request_completion(base_url, api_key, api_model, prompt, temperature, api_format="openai", system_prompt="", timeout=240):
        for name, text in texts.items():
            if name in api_model:
                return text
        return text_astra
    monkeypatch.setattr("api.probe.request_completion", fake_request_completion)
    response = client.post("/probe_batch", json={
        "targets": [
            {"base_url": "https://a.example/v1", "api_key": FAKE_KEY, "model": "gpt-6-astra"},
            {"base_url": "https://b.example/v1", "api_key": FAKE_KEY, "model": "gpt-6-luna"},
        ],
    })
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 2
    verdicts = {item["target"]: item["verdict"] for item in results}
    assert verdicts["https://a.example/v1::gpt-6-astra"] == "clean"
    assert verdicts["https://b.example/v1::gpt-6-luna"] == "degraded"
    assert FAKE_KEY not in response.text


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["bank"]["models"] == 18  # 统一库 18 模型
    assert body["service"].startswith("洞真")


def test_history_masked(client, monkeypatch, tmp_path):
    text, _ = load_reference_text("gpt-6-astra")
    monkeypatch.setattr("api.probe.request_completion", mock_completion_factory(text))
    client.post("/probe", json={
        "base_url": "https://mock.example/v1",
        "api_key": FAKE_KEY,
        "model": "gpt-6-astra",
    })
    response = client.get("/history", params={"model": "gpt-6-astra"})
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) >= 1
    latest = rows[0]
    assert latest["api_key_masked"] == mask_key(FAKE_KEY)
    assert FAKE_KEY not in response.text
    # 留底字段齐：时间/verdict/概率/样本数/延迟
    for key in ("ts", "verdict", "top_p", "valid_samples", "latency_ms", "base_url", "model"):
        assert key in latest
    # 红线：sqlite 文件字节里没有完整 key
    raw = (tmp_path / "history.sqlite3").read_bytes()
    assert FAKE_KEY.encode() not in raw
