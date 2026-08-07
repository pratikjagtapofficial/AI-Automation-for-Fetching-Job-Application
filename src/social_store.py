"""SQLite tracker for drafted LinkedIn posts.

Its own file, data/social.db — never data/jobs.db. That separation is the
whole point of building this as an isolated bot: a bug in draft storage
must be structurally unable to touch the job pipeline's tracker.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "social.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    context      TEXT NOT NULL,   -- what PJ sent
    text         TEXT NOT NULL,   -- drafted post text
    flags        TEXT,            -- json list, guardrail warnings
    status       TEXT DEFAULT 'pending',  -- pending|posted|discarded
    message_id   INTEGER,         -- telegram message id, for editing later
    posted_at    TEXT,
    li_post_id   TEXT
);

CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
"""


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create(conn, context: str, text: str, flags: list[str]) -> int:
    cur = conn.execute(
        "INSERT INTO drafts (created_at, context, text, flags) "
        "VALUES (?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), context, text,
         json.dumps(flags)),
    )
    return cur.lastrowid


def set_message_id(conn, draft_id: int, message_id: int) -> None:
    conn.execute("UPDATE drafts SET message_id=? WHERE id=?",
                 (message_id, draft_id))


def get(conn, draft_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()


def mark_posted(conn, draft_id: int, li_post_id: str) -> None:
    conn.execute(
        "UPDATE drafts SET status='posted', posted_at=?, li_post_id=? "
        "WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), li_post_id, draft_id),
    )


def mark_discarded(conn, draft_id: int) -> None:
    conn.execute(
        "UPDATE drafts SET status='discarded' WHERE id=?", (draft_id,))


def pending(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM drafts WHERE status='pending' ORDER BY id DESC"
    ).fetchall()


def recent_posted(conn, n: int = 5) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM drafts WHERE status='posted' "
        "ORDER BY posted_at DESC LIMIT ?", (n,)
    ).fetchall()
