"""SQLite tracker — dedupe memory and application pipeline state.

The DB file is committed back to the repo after each run. GitHub Actions
runners are ephemeral, so without persisting it every posting would look
new on the next run and PJ would be re-notified about the same jobs twice
a day forever.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .sources import Job

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key           TEXT PRIMARY KEY,   -- stable hash, see job_key()
    source        TEXT NOT NULL,
    company       TEXT NOT NULL,
    title         TEXT NOT NULL,
    location      TEXT,
    url           TEXT NOT NULL,
    posted_at     TEXT,
    score         INTEGER DEFAULT 0,
    reasons       TEXT,
    description   TEXT,

    first_seen    TEXT NOT NULL,
    notified_at   TEXT,               -- NULL = never sent to Telegram

    -- Application pipeline (Phase 3+)
    status        TEXT DEFAULT 'new', -- new|approved|skipped|applied|failed
    decided_at    TEXT,
    applied_at    TEXT,

    -- Call-prep snapshot, populated at submit time (Phase 5)
    resume_path   TEXT,
    answers_json  TEXT,
    ctc_quoted    TEXT,
    jd_snapshot   TEXT,
    notes         TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status   ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_notified ON jobs(notified_at);
CREATE INDEX IF NOT EXISTS idx_jobs_company  ON jobs(company);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    fetched    INTEGER DEFAULT 0,
    relevant   INTEGER DEFAULT 0,
    notified   INTEGER DEFAULT 0,
    errors     TEXT
);

-- Answer Bank: screening answers PJ has authored. Never LLM-generated.
CREATE TABLE IF NOT EXISTS answers (
    question_hash TEXT PRIMARY KEY,
    question      TEXT NOT NULL,
    answer        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    used_count    INTEGER DEFAULT 0
);
"""


def job_key(job: Job) -> str:
    """Stable identity for a posting.

    Uses source+job_id when the source gives a real id, else falls back to
    a hash of company+title+url. Board ids can change between runs on some
    ATSes, so the fallback keeps dedupe working rather than silently
    re-notifying.
    """
    if job.job_id:
        raw = f"{job.source}:{job.job_id}"
    else:
        raw = f"{job.source}:{job.company}:{job.title}:{job.url}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


@contextmanager
def connect(path: Path | None = None):
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def filter_new(conn: sqlite3.Connection, jobs: list[Job]) -> list[Job]:
    """Return only jobs never stored before. Does not write."""
    if not jobs:
        return []
    keys = [job_key(j) for j in jobs]
    placeholders = ",".join("?" * len(keys))
    seen = {
        r["key"] for r in
        conn.execute(f"SELECT key FROM jobs WHERE key IN ({placeholders})", keys)
    }
    return [j for j, k in zip(jobs, keys) if k not in seen]


def save(conn: sqlite3.Connection, jobs: list[Job]) -> int:
    """Insert new jobs. Existing rows are left untouched so that manual
    status changes (approved/applied) are never clobbered by a re-scan."""
    n = 0
    for j in jobs:
        cur = conn.execute(
            """INSERT OR IGNORE INTO jobs
               (key, source, company, title, location, url, posted_at,
                score, reasons, description, first_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (job_key(j), j.source, j.company, j.title, j.location, j.url,
             j.posted_at.isoformat() if j.posted_at else None,
             j.score, "; ".join(j.reasons), (j.description or "")[:20000],
             _now()),
        )
        n += cur.rowcount
    return n


def mark_notified(conn: sqlite3.Connection, jobs: list[Job]) -> None:
    ts = _now()
    conn.executemany(
        "UPDATE jobs SET notified_at=? WHERE key=?",
        [(ts, job_key(j)) for j in jobs],
    )


def record_run(conn: sqlite3.Connection, fetched: int, relevant: int,
               notified: int, errors: list[str]) -> None:
    conn.execute(
        "INSERT INTO runs (started_at, fetched, relevant, notified, errors) "
        "VALUES (?,?,?,?,?)",
        (_now(), fetched, relevant, notified, json.dumps(errors)),
    )


# ── queries used by Telegram commands ────────────────────────────────

def pipeline_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM jobs GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def recent(conn: sqlite3.Connection, limit: int = 10,
           status: str | None = None) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            "SELECT * FROM jobs WHERE status=? "
            "ORDER BY COALESCE(posted_at, first_seen) DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM jobs ORDER BY COALESCE(posted_at, first_seen) DESC "
        "LIMIT ?", (limit,),
    ).fetchall()


def find_by_company(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    """Backs the call-prep lookup: 'Acme called' -> what did I send them?"""
    return conn.execute(
        "SELECT * FROM jobs WHERE company LIKE ? "
        "ORDER BY COALESCE(applied_at, first_seen) DESC LIMIT 10",
        (f"%{name}%",),
    ).fetchall()


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def set_status(conn: sqlite3.Connection, key: str, status: str) -> bool:
    cur = conn.execute(
        "UPDATE jobs SET status=?, decided_at=? WHERE key=?",
        (status, _now(), key),
    )
    return cur.rowcount > 0
