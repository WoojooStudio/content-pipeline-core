"""metrics.sqlite initialisation and connection."""
from __future__ import annotations

import sqlite3
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    content_id      TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    date            TEXT NOT NULL,
    platform        TEXT,
    views           INTEGER,
    watch_time_sec  INTEGER,
    impressions     INTEGER,
    ctr             REAL,
    likes           INTEGER,
    comments        INTEGER,
    shares          INTEGER,
    retention_pct   REAL,
    avg_pct_viewed  REAL,
    utm_clicks      INTEGER,
    collected_at    TEXT,
    UNIQUE(content_id, date, platform)
);
"""


def get_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_DDL)
    conn.commit()
    return conn


def init_db(path: Path) -> None:
    conn = get_db(path)
    conn.close()
