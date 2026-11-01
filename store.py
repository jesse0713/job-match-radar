# -*- coding: utf-8 -*-
"""去重库：记录已推送岗位，保证跨天跨次不重复推送。"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("JOB_DB_PATH", BASE / "jobs.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS pushed (
    job_id     TEXT PRIMARY KEY,
    source     TEXT,
    title      TEXT,
    company    TEXT,
    city       TEXT,
    salary     TEXT,
    url        TEXT,
    score      REAL,
    pushed_at  REAL,
    run_tag    TEXT
);
CREATE INDEX IF NOT EXISTS idx_company_title ON pushed(company, title);
CREATE INDEX IF NOT EXISTS idx_pushed_at ON pushed(pushed_at);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def make_job_id(source: str, raw_id: str, company: str, title: str, city: str) -> str:
    if raw_id:
        return f"{source}:{raw_id}"
    digest = hashlib.md5(f"{source}|{company}|{title}|{city}".encode("utf-8")).hexdigest()[:16]
    return f"{source}:h:{digest}"


def filter_new(jobs: list[dict], within_days: int = 30) -> list[dict]:
    """去掉已推送过的岗位（同一岗位 ID，或 30 天内同公司同岗位名）。"""
    conn = get_conn()
    cur = conn.cursor()
    fresh = []
    for j in jobs:
        if cur.execute("SELECT 1 FROM pushed WHERE job_id=?", (j["job_id"],)).fetchone():
            continue
        dup = cur.execute(
            "SELECT 1 FROM pushed WHERE company=? AND title=? AND pushed_at > ?",
            (j["company"], j["title"], time.time() - within_days * 86400),
        ).fetchone()
        if dup:
            continue
        fresh.append(j)
    conn.close()
    return fresh


def remember(jobs: list[dict], run_tag: str) -> None:
    conn = get_conn()
    now = time.time()
    conn.executemany(
        "INSERT OR REPLACE INTO pushed (job_id, source, title, company, city, salary, url, score, pushed_at, run_tag)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                j["job_id"], j.get("source"), j.get("title"), j.get("company"), j.get("city"),
                j.get("salary_text"), j.get("url"), j.get("score"), now, run_tag,
            )
            for j in jobs
        ],
    )
    conn.commit()
    conn.close()


def stats() -> dict:
    conn = get_conn()
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) FROM pushed").fetchone()[0]
    last = cur.execute("SELECT MAX(pushed_at) FROM pushed").fetchone()[0]
    conn.close()
    return {"total_pushed": total, "last_pushed_at": last}
