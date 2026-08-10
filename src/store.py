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

    -- Tailoring output, kept so the approval card can be re-shown and so
    -- call-prep can answer "which version of my resume do they have?"
    tailored_json TEXT,
    tailored_at   TEXT,

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

-- Jobs awaiting a real-submit confirmation. Keyed by job_key rather than
-- a singleton row: a scan can proactively stage several apply-ready jobs
-- at once, and PJ confirms them one at a time by name ("CONFIRM gitlab"),
-- or with a bare "CONFIRM" when exactly one is waiting. Each Telegram
-- message runs as a fresh, memory-less Actions process, so this table is
-- the only place the gap between staging and confirmation can live.
-- Time-boxed via expires_at so a stray "confirm" sent hours or days
-- later -- to a message PJ has long forgotten -- can never fire a
-- submission against it.
CREATE TABLE IF NOT EXISTS pending_submit (
    job_key      TEXT PRIMARY KEY,
    company      TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    resume_path  TEXT,
    requested_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL
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


# Columns added after the first release. CREATE TABLE IF NOT EXISTS is a
# no-op on an existing table, so new columns must be added explicitly or
# the committed DB from an earlier run would break every query.
_MIGRATIONS = {
    "jobs": {
        "tailored_json": "TEXT",
        "tailored_at": "TEXT",
        "prepared_at": "TEXT",
        "prep_filled": "INTEGER",
        "prep_unanswered": "TEXT",
        "prep_screenshot": "TEXT",
        "prep_supported": "INTEGER",
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _MIGRATIONS.items():
        existing = {
            r["name"] for r in conn.execute(f"PRAGMA table_info({table})")
        }
        for col, decl in columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    # pending_submit used to be a singleton (id INTEGER PRIMARY KEY CHECK
    # (id=1)); a committed jobs.db from before the multi-pending redesign
    # still has that shape, and CREATE TABLE IF NOT EXISTS above is a
    # no-op against it. Detect and rebuild -- rows are time-boxed to 10
    # minutes, so nothing of real value is ever lost by dropping it.
    pending_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(pending_submit)")
    }
    if "id" in pending_cols:
        conn.execute("DROP TABLE pending_submit")
        conn.execute("""
            CREATE TABLE pending_submit (
                job_key      TEXT PRIMARY KEY,
                company      TEXT NOT NULL,
                title        TEXT NOT NULL,
                url          TEXT NOT NULL,
                resume_path  TEXT,
                requested_at TEXT NOT NULL,
                expires_at   TEXT NOT NULL
            )
        """)


@contextmanager
def connect(path: Path | None = None):
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
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


def save_tailoring(conn: sqlite3.Connection, key: str, payload: dict) -> bool:
    """Returns False if no row matched.

    An UPDATE against a missing key is a silent no-op in SQLite, which
    previously meant tailoring could be computed, sent to Telegram, and
    then lost with nothing indicating a problem. Callers must check.
    """
    cur = conn.execute(
        "UPDATE jobs SET tailored_json=?, tailored_at=? WHERE key=?",
        (json.dumps(payload, ensure_ascii=False), _now(), key),
    )
    return cur.rowcount > 0


def get_tailoring(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute(
        "SELECT tailored_json FROM jobs WHERE key=?", (key,)
    ).fetchone()
    if not row or not row["tailored_json"]:
        return None
    try:
        return json.loads(row["tailored_json"])
    except Exception:
        return None


def save_preparation(conn: sqlite3.Connection, key: str, *, filled: int,
                     unanswered: list[str], screenshot: str | None,
                     supported: bool) -> bool:
    cur = conn.execute(
        "UPDATE jobs SET prepared_at=?, prep_filled=?, prep_unanswered=?, "
        "prep_screenshot=?, prep_supported=? WHERE key=?",
        (_now(), filled, json.dumps(unanswered, ensure_ascii=False),
         screenshot, 1 if supported else 0, key),
    )
    return cur.rowcount > 0


def open_questions(conn: sqlite3.Connection, limit: int = 20) -> list[tuple]:
    """Unanswered form questions across prepared applications.

    Backs /questions and /answer, so a question PJ answers once is stored
    in the Answer Bank and never asked again.
    """
    rows = conn.execute(
        "SELECT company, title, prep_unanswered FROM jobs "
        "WHERE prep_unanswered IS NOT NULL AND prep_unanswered != '[]' "
        "ORDER BY prepared_at DESC LIMIT ?", (limit,)).fetchall()
    out, seen = [], set()
    for r in rows:
        try:
            qs = json.loads(r["prep_unanswered"]) or []
        except Exception:
            continue
        for q in qs:
            k = q.strip().lower()
            if k and k not in seen:
                seen.add(k)
                out.append((q, r["company"], r["title"]))
    return out


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


def distinct_companies(conn: sqlite3.Connection, limit: int = 40) -> list[str]:
    """Distinct company names, most recently active first.

    Used as fuzzy-match context for the natural-language router. Raw
    `recent()` rows collapse to very few distinct companies whenever a
    handful of employers post several listings each (Deutsche Telekom
    posted 8+ in one scan) — that starved the NLU context window down to
    single digits of real variety even at limit=15 rows.
    """
    rows = conn.execute(
        "SELECT company, MAX(COALESCE(applied_at, prepared_at, first_seen)) "
        "AS last_activity FROM jobs GROUP BY company "
        "ORDER BY last_activity DESC LIMIT ?", (limit,)
    ).fetchall()
    return [r["company"] for r in rows if r["company"]]


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


# ── pending real-submit confirmations (multi-row, time-boxed) ────────

def set_pending_submit(conn: sqlite3.Connection, job_key: str, company: str,
                       title: str, url: str, resume_path: str | None,
                       ttl_minutes: int = 10) -> None:
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT OR REPLACE INTO pending_submit "
        "(job_key, company, title, url, resume_path, requested_at, expires_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_key, company, title, url, resume_path,
         now.isoformat(timespec="seconds"),
         (now + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")),
    )


def list_pending_submit(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every not-yet-expired pending confirmation, oldest first. Clears
    expired rows first so no caller has to separately check freshness."""
    conn.execute("DELETE FROM pending_submit WHERE expires_at < ?", (_now(),))
    return conn.execute(
        "SELECT * FROM pending_submit ORDER BY requested_at"
    ).fetchall()


def get_pending_submit(
    conn: sqlite3.Connection, company: str | None = None
) -> tuple[sqlite3.Row | None, str | None]:
    """Resolve which pending confirmation a "CONFIRM" (optionally
    "CONFIRM <company>") reply refers to.

    -> (row, error). Exactly one of the two is set. `error` is
    plain-text, ready to show PJ as-is: "nothing pending", "no match for
    X", or -- when more than one job is waiting and no company was named
    to disambiguate -- the list of companies to choose from.
    """
    rows = list_pending_submit(conn)

    if company:
        low = company.strip().lower()
        matches = [r for r in rows if low in r["company"].lower()]
        if not matches:
            return None, f"No pending confirmation for '{company}'."
        if len(matches) > 1:
            names = ", ".join(r["company"] for r in matches)
            return None, (f"Multiple pending jobs match '{company}': "
                          f"{names}. Be more specific.")
        return matches[0], None

    if not rows:
        return None, ("Nothing is pending confirmation right now. It may "
                      "have expired (10 minute window) — send "
                      "/apply <company> again.")
    if len(rows) > 1:
        names = ", ".join(r["company"] for r in rows)
        return None, (f"{len(rows)} jobs are pending confirmation: "
                      f"{names}. Reply CONFIRM <company>.")
    return rows[0], None


def clear_pending_submit(conn: sqlite3.Connection, job_key: str) -> None:
    conn.execute("DELETE FROM pending_submit WHERE job_key=?", (job_key,))


def set_status(conn: sqlite3.Connection, key: str, status: str) -> bool:
    cur = conn.execute(
        "UPDATE jobs SET status=?, decided_at=? WHERE key=?",
        (status, _now(), key),
    )
    return cur.rowcount > 0
