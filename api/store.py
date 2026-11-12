# -*- coding: utf-8 -*-
"""历史留底 sqlite。硬要求：api_key 一律脱敏后才允许进库（用 verdict.mask_key）。
存的内容：时间 / 目标（base_url+model）/ verdict / 概率 / 样本数 / 延迟 / 错误摘要。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    api_key_masked TEXT NOT NULL,
    verdict TEXT,
    verdict_reason TEXT,
    family TEXT,
    family_p REAL,
    top_model TEXT,
    top_p REAL,
    top_gap REAL,
    valid_samples INTEGER,
    rounds INTEGER,
    questions INTEGER,
    latency_ms INTEGER,
    error TEXT,
    candidates TEXT
);
CREATE INDEX IF NOT EXISTS idx_probes_model_ts ON probes(model, ts);
"""


class HistoryStore:
    """sqlite 轻封装：单连接 + 线程锁（FastAPI 线程池下够用）。"""

    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # 旧库迁移：candidates 列（族内候选 JSON）是后加的，已存在的库补列
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(probes)")}
            if "candidates" not in columns:
                self._conn.execute("ALTER TABLE probes ADD COLUMN candidates TEXT")
            self._conn.commit()

    def insert_probe(
        self,
        ts: str,
        base_url: str,
        model: str,
        api_key_masked: str,
        verdict: str | None,
        verdict_reason: str | None,
        family: str | None,
        family_p: float | None,
        top_model: str | None,
        top_p: float | None,
        top_gap: float | None,
        valid_samples: int | None,
        rounds: int | None,
        questions: int | None,
        latency_ms: int | None,
        error: str | None,
        candidates: list[dict] | None = None,
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO probes (ts, base_url, model, api_key_masked, verdict, verdict_reason,"
                " family, family_p, top_model, top_p, top_gap, valid_samples, rounds, questions,"
                " latency_ms, error, candidates) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, base_url, model, api_key_masked, verdict, verdict_reason,
                    family, family_p, top_model, top_p, top_gap, valid_samples,
                    rounds, questions, latency_ms,
                    (error or "")[:500] or None,
                    json.dumps(candidates, ensure_ascii=False) if candidates else None,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def query(self, model: str | None = None, days: int | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM probes WHERE 1=1"
        params: list = []
        if model:
            sql += " AND model = ?"
            params.append(model)
        if days is not None:
            sql += " AND ts >= datetime('now', ?)"
            params.append(f"-{int(days)} days")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            raw_candidates = item.get("candidates")
            if raw_candidates:
                try:
                    item["candidates"] = json.loads(raw_candidates)
                except ValueError:
                    item["candidates"] = None
            else:
                item["candidates"] = None
            results.append(item)
        return results

    def stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS n FROM probes").fetchone()["n"]
            by_verdict = {
                row["verdict"] or "error": row["n"]
                for row in self._conn.execute(
                    "SELECT verdict, COUNT(*) AS n FROM probes GROUP BY verdict"
                ).fetchall()
            }
        return {"total": total, "by_verdict": by_verdict}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
