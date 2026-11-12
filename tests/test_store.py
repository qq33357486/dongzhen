# -*- coding: utf-8 -*-
"""历史留底 sqlite 单测：脱敏落库、查询过滤、库里绝无完整 key。"""
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.store import HistoryStore
from api.verdict import mask_key


def make_store(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "history.sqlite3")


def test_insert_and_query(tmp_path):
    store = make_store(tmp_path)
    row_id = store.insert_probe(
        ts="2026-11-13T00:00:00+00:00",
        base_url="https://example.com/v1",
        model="gpt-6-astra",
        api_key_masked=mask_key("sk-abcdef1234567890"),
        verdict="clean",
        verdict_reason="top1 命中正常分支",
        family="gpt",
        family_p=0.9,
        top_model="gpt-6-astra",
        top_p=0.42,
        top_gap=0.5,
        valid_samples=3,
        rounds=1,
        questions=3,
        latency_ms=12345,
        error=None,
    )
    assert row_id == 1
    rows = store.query(model="gpt-6-astra")
    assert len(rows) == 1
    row = rows[0]
    assert row["api_key_masked"] == "sk-a***7890"
    assert row["verdict"] == "clean"
    assert row["top_p"] == 0.42


def test_query_filters(tmp_path):
    from datetime import datetime, timedelta, timezone
    store = make_store(tmp_path)
    now = datetime.now(timezone.utc)
    stamps = [
        (now - timedelta(days=10)).isoformat(),
        (now - timedelta(days=5)).isoformat(),
        (now - timedelta(hours=1)).isoformat(),
    ]
    for ts, model in zip(stamps, ["gpt-6-astra", "gpt-6-sol", "gpt-6-astra"]):
        store.insert_probe(
            ts=ts,
            base_url="https://example.com/v1",
            model=model,
            api_key_masked="****",
            verdict="unknown",
            verdict_reason=None,
            family=None,
            family_p=None,
            top_model=None,
            top_p=None,
            top_gap=None,
            valid_samples=0,
            rounds=1,
            questions=3,
            latency_ms=None,
            error=None,
        )
    assert len(store.query(model="gpt-6-astra")) == 2
    assert len(store.query(days=1)) == 1
    assert len(store.query(days=30)) == 3
    assert len(store.query(limit=2)) == 2
    stats = store.stats()
    assert stats["total"] == 3


def test_candidates_roundtrip(tmp_path):
    """族候选列表：insert 存 JSON、query 解析回 list；不传则为 None（旧记录语义）。"""
    store = make_store(tmp_path)
    candidates = [
        {"model": "gpt-6-sol", "p": 0.5},
        {"model": "gpt-6-astra", "p": 0.45},
    ]
    store.insert_probe(
        ts="2026-11-13T00:00:00+00:00",
        base_url="ai-mode",
        model="proxy-x",
        api_key_masked="****",
        verdict="unknown",
        verdict_reason="族内多候选并列（可能是 gpt-6-sol / gpt-6-astra）",
        family="gpt",
        family_p=0.95,
        top_model="gpt-6-sol",
        top_p=0.5,
        top_gap=0.05,
        valid_samples=3,
        rounds=1,
        questions=3,
        latency_ms=None,
        error=None,
        candidates=candidates,
    )
    row = store.query(model="proxy-x")[0]
    assert row["candidates"] == candidates
    # 不带 candidates 的旧式写入 -> None
    store.insert_probe(
        ts="2026-11-13T01:00:00+00:00",
        base_url="https://example.com/v1",
        model="gpt-6-astra",
        api_key_masked="****",
        verdict="clean",
        verdict_reason=None,
        family=None,
        family_p=None,
        top_model=None,
        top_p=None,
        top_gap=None,
        valid_samples=1,
        rounds=1,
        questions=3,
        latency_ms=None,
        error=None,
    )
    assert store.query(model="gpt-6-astra")[0]["candidates"] is None


def test_migration_adds_candidates_column_to_legacy_db(tmp_path):
    """旧库（无 candidates 列）打开时自动补列，旧记录读出 candidates=None。"""
    import sqlite3

    db_path = tmp_path / "history.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE probes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
        " base_url TEXT NOT NULL, model TEXT NOT NULL, api_key_masked TEXT NOT NULL,"
        " verdict TEXT, verdict_reason TEXT, family TEXT, family_p REAL, top_model TEXT,"
        " top_p REAL, top_gap REAL, valid_samples INTEGER, rounds INTEGER, questions INTEGER,"
        " latency_ms INTEGER, error TEXT)"
    )
    conn.execute(
        "INSERT INTO probes (ts, base_url, model, api_key_masked, verdict, valid_samples)"
        " VALUES ('2026-11-01T00:00:00+00:00', 'https://old.example/v1', 'gpt-6-astra', '****', 'clean', 3)"
    )
    conn.commit()
    conn.close()

    store = HistoryStore(db_path)  # 打开即迁移
    row = store.query(model="gpt-6-astra")[0]
    assert row["candidates"] is None
    assert row["verdict"] == "clean"
    # 迁移后的库还能正常写 candidates
    store.insert_probe(
        ts="2026-11-13T02:00:00+00:00",
        base_url="ai-mode",
        model="m",
        api_key_masked="****",
        verdict=None,
        verdict_reason=None,
        family=None,
        family_p=None,
        top_model=None,
        top_p=None,
        top_gap=None,
        valid_samples=0,
        rounds=1,
        questions=3,
        latency_ms=None,
        error=None,
        candidates=[{"model": "gpt-6-sol", "p": 0.9}],
    )
    assert store.query(model="m")[0]["candidates"] == [{"model": "gpt-6-sol", "p": 0.9}]
    store.close()


def test_no_full_key_in_db_file(tmp_path):
    """红线断言：完整 api_key 绝不能出现在 sqlite 文件字节里。"""
    secret = "sk-super-secret-key-abcdefghijklmnop"
    store = make_store(tmp_path)
    store.insert_probe(
        ts="2026-11-13T00:00:00+00:00",
        base_url="https://example.com/v1",
        model="gpt-6-astra",
        api_key_masked=mask_key(secret),
        verdict="clean",
        verdict_reason=None,
        family=None,
        family_p=None,
        top_model=None,
        top_p=None,
        top_gap=None,
        valid_samples=3,
        rounds=1,
        questions=3,
        latency_ms=None,
        error=None,
    )
    store.close()
    raw = (tmp_path / "history.sqlite3").read_bytes()
    assert secret.encode() not in raw
    assert mask_key(secret).encode() in raw
