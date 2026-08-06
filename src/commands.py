"""Telegram command handling.

Parses a message and returns reply text. Kept transport-agnostic so the
same logic serves both polling (now) and a webhook (later) without a
rewrite.

Anything that costs real work — running a scan, applying — is dispatched
to GitHub Actions rather than executed inline, since the handler may be
running somewhere with a few seconds of budget.
"""

from __future__ import annotations

import html
import os
from datetime import datetime, timezone

import httpx

from . import store

HELP = """<b>Commands</b>

/scan — run a job scan now
/status — pipeline counts + last run
/recent [n] — latest matches (default 5)
/pending — matches awaiting your decision
/brief &lt;company&gt; — what did I send them?
/help — this message

You can also just type a company name to look it up."""


def _age(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return iso
    h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    if h < 1:
        return f"{int(h * 60)}m ago"
    if h < 24:
        return f"{int(h)}h ago"
    return f"{int(h // 24)}d ago"


def dispatch_workflow(event: str = "run-scout") -> tuple[bool, str]:
    """Trigger the Actions workflow via repository_dispatch.

    Needs a token with `repo` scope in GH_DISPATCH_TOKEN. The default
    GITHUB_TOKEN cannot trigger workflows from outside a run.
    """
    token = os.environ.get("GH_DISPATCH_TOKEN", "")
    repo = os.environ.get("GH_REPO", "")
    if not token or not repo:
        return False, "GH_DISPATCH_TOKEN / GH_REPO not configured"
    try:
        r = httpx.post(
            f"https://api.github.com/repos/{repo}/dispatches",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json={"event_type": event},
            timeout=20.0,
        )
        if r.status_code == 204:
            return True, "dispatched"
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, type(e).__name__


def handle(text: str) -> str:
    """Return HTML reply for a message. Never raises."""
    e = html.escape
    text = (text or "").strip()
    if not text:
        return HELP

    parts = text.split()
    cmd = parts[0].lower().lstrip("/")
    arg = " ".join(parts[1:]).strip()

    try:
        with store.connect() as conn:

            if cmd in ("start", "help"):
                return HELP

            if cmd == "scan":
                ok, msg = dispatch_workflow()
                if ok:
                    return ("🔄 Scan triggered. Results land here in a few "
                            "minutes.")
                return (f"⚠️ Couldn't trigger the scan: {e(msg)}\n\n"
                        "<i>The scheduled 2 AM / 2 PM runs are unaffected.</i>")

            if cmd == "status":
                counts = store.pipeline_counts(conn)
                run = store.last_run(conn)
                lines = ["📊 <b>Pipeline</b>"]
                if counts:
                    for k, v in sorted(counts.items()):
                        lines.append(f"  {e(k)}: <b>{v}</b>")
                else:
                    lines.append("  <i>empty — no jobs stored yet</i>")
                if run:
                    lines += [
                        "",
                        f"🕒 <b>Last run</b> {_age(run['started_at'])}",
                        f"  fetched {run['fetched']} · relevant "
                        f"{run['relevant']} · notified {run['notified']}",
                    ]
                    errs = run["errors"] or "[]"
                    if errs not in ("[]", "", None):
                        lines.append(f"  ⚠️ had source errors")
                else:
                    lines += ["", "🕒 <i>No run recorded yet.</i>"]
                return "\n".join(lines)

            if cmd == "recent":
                n = 5
                if arg.isdigit():
                    n = max(1, min(int(arg), 20))
                rows = store.recent(conn, n)
                if not rows:
                    return "Nothing stored yet."
                out = [f"📋 <b>Last {len(rows)}</b>", ""]
                for r in rows:
                    out.append(
                        f"<b>{r['score']}</b> · "
                        f"<a href=\"{e(r['url'])}\">{e(r['title'])}</a>\n"
                        f"     {e(r['company'])} · {e(r['location'] or '')} · "
                        f"{_age(r['posted_at'])} · <i>{e(r['status'])}</i>"
                    )
                return "\n".join(out)

            if cmd == "pending":
                rows = store.recent(conn, 15, status="new")
                if not rows:
                    return "✅ Nothing pending."
                out = [f"⏳ <b>{len(rows)} awaiting decision</b>", ""]
                for r in rows:
                    out.append(
                        f"<b>{r['score']}</b> · "
                        f"<a href=\"{e(r['url'])}\">{e(r['title'])}</a>\n"
                        f"     {e(r['company'])} · {_age(r['posted_at'])}"
                    )
                return "\n".join(out)

            if cmd == "brief" or not text.startswith("/"):
                name = arg if cmd == "brief" else text
                if not name:
                    return "Usage: <code>/brief &lt;company&gt;</code>"
                rows = store.find_by_company(conn, name)
                if not rows:
                    return (f"No record for <b>{e(name)}</b>.\n"
                            "<i>Either you haven't applied there, or it came "
                            "in before tracking started.</i>")
                out = [f"📞 <b>{e(rows[0]['company'])}</b>", ""]
                for r in rows[:3]:
                    out.append(f"<b>{e(r['title'])}</b> · {e(r['location'] or '')}")
                    out.append(f"  status: <i>{e(r['status'])}</i> · "
                               f"seen {_age(r['first_seen'])}")
                    if r["ctc_quoted"]:
                        out.append(f"  💰 you quoted: <b>{e(r['ctc_quoted'])}</b>")
                    if r["applied_at"]:
                        out.append(f"  applied {_age(r['applied_at'])}")
                    out.append(f"  <a href=\"{e(r['url'])}\">posting →</a>")
                    out.append("")
                # Phase 5 fills resume version + submitted answers here.
                return "\n".join(out)

            return (f"Unknown command: <code>{e(cmd)}</code>\n\n{HELP}")

    except Exception as ex:
        return f"⚠️ Error handling that: {e(type(ex).__name__)}"
