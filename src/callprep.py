"""Call-prep briefs.

A recruiter rings unexpectedly. PJ has about ten seconds to remember
which of several tailored resumes they are holding and what salary he
quoted six weeks ago. This builds that card.

Two things make it work, and both must happen at preparation time rather
than on demand:

  * The JD is SNAPSHOTTED, not linked. Postings are taken down routinely,
    and the interview happens weeks after the application — a dead link
    is worthless at exactly the moment it is needed.
  * The CTC quoted is recorded verbatim. Contradicting your own
    application on a call is the single most damaging thing here, and it
    is entirely avoidable.

Gaps are surfaced with honest framing from profile.skills.gap_framing.
The card never suggests claiming something PJ does not have — it prepares
him to answer the question he is about to be asked.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import datetime, timezone

from . import store


def jd_to_text(raw: str) -> str:
    """Turn a stored JD into readable plain text.

    Greenhouse returns its description as HTML-escaped HTML, so the stored
    value is doubly encoded. Escaping that again for Telegram produced
    "&amp;lt;div class=&amp;quot;" instead of readable text. Unescape
    twice, strip tags, then let the caller escape once.
    """
    if not raw:
        return ""
    txt = html.unescape(html.unescape(raw))
    txt = re.sub(r"(?i)<\s*br\s*/?>", "\n", txt)
    txt = re.sub(r"(?i)</\s*(p|div|li|h[1-6]|tr)\s*>", "\n", txt)
    txt = re.sub(r"(?i)<\s*li[^>]*>", "• ", txt)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n\s*\n\s*\n+", "\n\n", txt)
    return txt.strip()


def _age(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return iso
    days = (datetime.now(timezone.utc) - dt).days
    stamp = dt.strftime("%d %b")
    if days == 0:
        return f"today ({stamp})"
    if days == 1:
        return f"yesterday ({stamp})"
    return f"{days}d ago ({stamp})"


def find_gaps(jd_text: str, profile: dict) -> list[tuple[str, str]]:
    """-> [(skill, honest framing)] for things the JD wants and PJ lacks."""
    text = (jd_text or "").lower()
    framing = (profile.get("skills", {}).get("gap_framing") or {})
    default = framing.get("default", "")
    out = []
    for skill in profile.get("skills", {}).get("lacks") or []:
        if re.search(rf"(?<![a-z0-9]){re.escape(skill.lower())}(?![a-z0-9])",
                     text):
            out.append((skill, framing.get(skill, default)))
    return out


def find_shallow(jd_text: str, profile: dict) -> list[tuple[str, str]]:
    """Skills PJ has only lightly that the JD emphasises."""
    text = (jd_text or "").lower()
    out = []
    for skill, note in (profile.get("skills", {}).get("shallow") or {}).items():
        if re.search(rf"(?<![a-z0-9]){re.escape(skill.lower())}(?![a-z0-9])",
                     text):
            out.append((skill, note))
    return out


def top_claims(tailored: dict | None, limit: int = 3) -> list[str]:
    """The bullets the tailored resume led with — what they actually read."""
    if not tailored:
        return []
    bullets = tailored.get("bullets") or []
    changed = [b for b in bullets if b.get("changed")] or bullets
    out = []
    for b in changed[:limit]:
        t = " ".join(str(b.get("text", "")).split())
        if t:
            out.append(t if len(t) <= 120 else t[:117].rstrip() + "…")
    return out


def build(row: sqlite3.Row, profile: dict,
          tailored: dict | None) -> str:
    """The card. Built to be read during a live call, not studied."""
    e = html.escape
    lines: list[str] = []

    status = row["status"] or "new"
    badge = {"applied": "✅ applied", "approved": "👍 approved",
             "skipped": "⏭ skipped"}.get(status, "🔎 not applied yet")

    lines.append(f"📞 <b>{e(row['company'])}</b>")
    lines.append(f"{e(row['title'])} · {e(row['location'] or '—')}")
    lines.append("")

    when = row["applied_at"] or row["prepared_at"] or row["first_seen"]
    label = "Applied" if row["applied_at"] else (
        "Prepared" if row["prepared_at"] else "Found")
    lines.append(f"{label}: <b>{_age(when)}</b> · {badge}")
    lines.append(f"Source: {e(row['source'])}")
    lines.append("")

    # The row that saves the call.
    ctc = row["ctc_quoted"]
    notice = (profile.get("compensation", {}) or {}).get("notice_period_days")
    if ctc:
        lines.append(f"💰 <b>You quoted: {e(str(ctc))}</b>")
    else:
        exp_min = (profile.get("compensation", {}) or {}).get("expected_min_lpa")
        exp_max = (profile.get("compensation", {}) or {}).get("expected_max_lpa")
        if exp_min and exp_max:
            lines.append(f"💰 Standard ask: <b>{exp_min}-{exp_max} LPA</b> "
                         f"<i>(not recorded for this one)</i>")
    if notice:
        lines.append(f"📅 Notice stated: <b>{notice} days</b>")
    lines.append("")

    if tailored and tailored.get("headline"):
        lines.append(f"📄 <b>Resume they have</b>")
        lines.append(f"<i>{e(tailored['headline'])}</i>")
        claims = top_claims(tailored)
        if claims:
            lines.append("")
            lines.append("<b>What you claimed</b>")
            lines += [f"• {e(c)}" for c in claims]
        lines.append("")
    else:
        lines.append("📄 <b>Resume:</b> base version (not tailored)")
        lines.append("")

    jd = row["jd_snapshot"] or row["description"] or ""
    gaps = find_gaps(jd, profile)
    shallow = find_shallow(jd, profile)

    if gaps:
        lines.append("⚠️ <b>They asked for, you don't have</b>")
        for skill, framing in gaps[:3]:
            lines.append(f"<b>{e(skill)}</b>")
            lines.append(f"  ↳ <i>{e(framing)}</i>")
        lines.append("")

    if shallow:
        lines.append("🟡 <b>Go carefully on</b>")
        for skill, note in shallow[:2]:
            lines.append(f"<b>{e(skill)}</b> — <i>{e(note)}</i>")
        lines.append("")

    if row["notes"]:
        lines.append("📝 <b>Your notes</b>")
        for n in str(row["notes"]).split("\n")[-4:]:
            if n.strip():
                lines.append(f"• {e(n.strip())}")
        lines.append("")

    lines.append(f'<a href="{e(row["url"])}">Original posting →</a>')
    lines.append("")
    cmds = [f"/resume {row['company']}", f"/jd {row['company']}",
            f"/note {row['company']} &lt;text&gt;"]
    lines.append("<i>" + "  ·  ".join(e(c) if "&" not in c else c
                                      for c in cmds) + "</i>")
    return "\n".join(lines)


def snapshot(conn: sqlite3.Connection, key: str, *, jd_text: str,
             answers: list[tuple[str, str]], ctc_quoted: str | None,
             resume_path: str | None) -> bool:
    """Freeze what was actually sent. Called at preparation time.

    Doing this later is not possible: the posting may be gone and the
    tailored PDF regenerated, so the only reliable moment to record what
    an employer received is when it is handed over.
    """
    cur = conn.execute(
        "UPDATE jobs SET jd_snapshot=?, answers_json=?, ctc_quoted=?, "
        "resume_path=? WHERE key=?",
        ((jd_text or "")[:40000],
         json.dumps([{"q": q, "a": a} for q, a in answers], ensure_ascii=False),
         ctc_quoted, resume_path, key),
    )
    return cur.rowcount > 0


def add_note(conn: sqlite3.Connection, company: str, text: str) -> bool:
    row = conn.execute(
        "SELECT key, notes FROM jobs WHERE company LIKE ? "
        "ORDER BY COALESCE(applied_at, prepared_at, first_seen) DESC LIMIT 1",
        (f"%{company}%",)).fetchone()
    if not row:
        return False
    stamp = datetime.now(timezone.utc).strftime("%d %b")
    existing = row["notes"] or ""
    updated = f"{existing}\n{stamp}: {text}".strip()
    conn.execute("UPDATE jobs SET notes=? WHERE key=?", (updated, row["key"]))
    return True


def mark_applied(conn: sqlite3.Connection, company: str) -> sqlite3.Row | None:
    """PJ confirms he actually submitted it.

    The system never submits, so 'applied' can only ever be asserted by
    him — it is not inferred from a prepared form.
    """
    row = conn.execute(
        "SELECT key, company, title FROM jobs WHERE company LIKE ? "
        "ORDER BY COALESCE(prepared_at, first_seen) DESC LIMIT 1",
        (f"%{company}%",)).fetchone()
    if not row:
        return None
    conn.execute(
        "UPDATE jobs SET status='applied', applied_at=?, decided_at=? "
        "WHERE key=?",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),
         datetime.now(timezone.utc).isoformat(timespec="seconds"),
         row["key"]))
    return row


def lookup(conn: sqlite3.Connection, company: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE company LIKE ? "
        "ORDER BY COALESCE(applied_at, prepared_at, first_seen) DESC LIMIT 1",
        (f"%{company}%",)).fetchone()
