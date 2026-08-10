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

-- Idea-generation stage (strategy doc section 27): before a full post is
-- written, PJ picks one of 3-5 proposed angles. This table holds that
-- proposal set while it's awaiting a reply; once picked, the chosen idea
-- becomes a row in `drafts` via create_from_idea() and this row is
-- marked resolved.
CREATE TABLE IF NOT EXISTS idea_sets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    context      TEXT NOT NULL,
    ideas_json   TEXT NOT NULL,   -- list of {number, hook, angle, format}
    status       TEXT DEFAULT 'pending'  -- pending|resolved
);

CREATE INDEX IF NOT EXISTS idx_idea_sets_status ON idea_sets(status);
"""

# Columns added after the original `drafts` table shipped. ALTER TABLE
# ADD COLUMN has no "IF NOT EXISTS" in SQLite, so each is wrapped below
# and duplicate-column errors are swallowed — this keeps both a fresh DB
# and an already-deployed one (data/social.db, committed by Actions)
# working without a separate migration step to remember to run.
_MIGRATIONS = [
    "ALTER TABLE drafts ADD COLUMN visual_idea TEXT",
    "ALTER TABLE drafts ADD COLUMN first_comment TEXT",
    "ALTER TABLE drafts ADD COLUMN alt_hooks TEXT",       # json list
    "ALTER TABLE drafts ADD COLUMN source_hook TEXT",
    "ALTER TABLE drafts ADD COLUMN source_angle TEXT",
    "ALTER TABLE drafts ADD COLUMN source_format TEXT",
]


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
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


def create_ideas(conn, context: str, ideas: list[dict]) -> int:
    cur = conn.execute(
        "INSERT INTO idea_sets (created_at, context, ideas_json) "
        "VALUES (?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), context, json.dumps(ideas)),
    )
    return cur.lastrowid


def latest_pending_ideas(conn) -> sqlite3.Row | None:
    """Single-user bot: at most one idea set is ever meaningfully
    'pending' at a time, so the most recent one is the right one."""
    return conn.execute(
        "SELECT * FROM idea_sets WHERE status='pending' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()


def mark_ideas_resolved(conn, idea_set_id: int) -> None:
    conn.execute(
        "UPDATE idea_sets SET status='resolved' WHERE id=?", (idea_set_id,))


def create_from_idea(conn, context: str, idea: dict, text: str,
                     flags: list[str], visual_idea: str,
                     first_comment: str, alt_hooks: list[str]) -> int:
    cur = conn.execute(
        "INSERT INTO drafts (created_at, context, text, flags, "
        "visual_idea, first_comment, alt_hooks, "
        "source_hook, source_angle, source_format) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), context, text,
         json.dumps(flags), visual_idea, first_comment,
         json.dumps(alt_hooks), idea.get("hook", ""),
         idea.get("angle", ""), idea.get("format", "")),
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
