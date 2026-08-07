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
from pathlib import Path

import httpx
import yaml

from . import callprep, store

HELP = """<b>Commands</b>

/scan — run a job scan now
/status — pipeline counts + last run
/recent [n] — latest matches (default 5)
/pending — matches awaiting your decision
/brief &lt;company&gt; — what did I send them?
/diff &lt;company&gt; — full resume changes for that job
/resume &lt;company&gt; — send the tailored PDF for that job
/jd &lt;company&gt; — the job description as it was when you applied
/applied &lt;company&gt; — mark that you actually submitted it
/note &lt;company&gt; &lt;text&gt; — save a note after a call
/questions — form questions waiting on you
/answer &lt;n&gt; &lt;your answer&gt; — answer question n
/help — this message

You can also just talk normally — "what's pending", "gitlab called",
"send me the meraki resume" all work, or type a company name to look it
up directly.

<i>Nothing gets bulk-approved or auto-applied from a plain-English
request, even if you ask for it — every application still needs a real,
individual review.</i>"""


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


def resume_for(company: str):
    """-> (Path, caption) for a tailored PDF, or (None, error text).

    Re-rendered from the stored tailoring rather than kept on disk: the
    JSON is the durable artefact, runners are ephemeral, and a PDF can
    always be rebuilt from it.
    """
    import re as _re
    from pathlib import Path as _P
    import yaml as _yaml
    from .render import render as _render

    root = _P(__file__).resolve().parents[1]
    try:
        with store.connect() as conn:
            rows = store.find_by_company(conn, company)
            row = next((r for r in rows if r["tailored_json"]), None)
            if not row:
                return None, (f"No tailored resume stored for "
                              f"<b>{html.escape(company)}</b>.")
            t = store.get_tailoring(conn, row["key"]) or {}
            title, comp = row["title"], row["company"]
        resume = _yaml.safe_load(
            (root / "config" / "resume.yml").read_text(encoding="utf-8"))
        safe = _re.sub(r"[^A-Za-z0-9]+", "_", f"{comp}_{title}")[:60].strip("_")
        pdf, _pages = _render(resume, root / "output" / f"Pratik_Jagtap_{safe}.pdf",
                              tailored=t)
        return pdf, f"Tailored for <b>{html.escape(comp)}</b> — {html.escape(title)}"
    except Exception as ex:
        return None, f"Could not render: {html.escape(type(ex).__name__)}"


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

            if cmd == "resume":
                if not arg:
                    return "Usage: <code>/resume &lt;company&gt;</code>"
                # bot.py intercepts this and uploads the file; reaching
                # here means the transport could not send documents.
                return ("<i>Preparing the PDF…</i> if nothing arrives, the "
                        "sender does not support file upload.")

            if cmd == "questions":
                qs = store.open_questions(conn)
                if not qs:
                    return ("✅ No open questions.\n"
                            "<i>Every field the forms asked for was already "
                            "in your Answer Bank.</i>")
                out = [f"❓ <b>{len(qs)} question(s) need you</b>",
                       "<i>Answer once and it is saved forever.</i>", ""]
                for i, (q, comp, _t) in enumerate(qs[:12], 1):
                    out.append(f"<b>{i}.</b> {e(q[:150])}\n"
                               f"     <i>from {e(comp)}</i>")
                out += ["", "Reply: <code>/answer 1 Yes</code>"]
                return "\n".join(out)

            if cmd == "answer":
                from .answers import remember
                bits = arg.split(maxsplit=1)
                if len(bits) < 2 or not bits[0].isdigit():
                    return ("Usage: <code>/answer &lt;number&gt; &lt;your "
                            "answer&gt;</code>\nSee <code>/questions</code>.")
                idx, value = int(bits[0]), bits[1].strip()
                qs = store.open_questions(conn)
                if not (1 <= idx <= len(qs)):
                    return f"No question {idx}. See <code>/questions</code>."
                question = qs[idx - 1][0]
                try:
                    remember(question, value)
                except Exception as ex:
                    return f"⚠️ Could not save: {e(type(ex).__name__)}"
                return (f"✅ Saved.\n\n<b>Q:</b> {e(question[:160])}\n"
                        f"<b>A:</b> {e(value[:160])}\n\n"
                        "<i>Filled automatically from now on.</i>")

            if cmd == "diff":
                if not arg:
                    return "Usage: <code>/diff &lt;company&gt;</code>"
                rows = store.find_by_company(conn, arg)
                if not rows:
                    return f"No record for <b>{e(arg)}</b>."
                row = next((r for r in rows if r["tailored_json"]), None)
                if not row:
                    return (f"No tailoring stored for <b>{e(arg)}</b>.\n"
                            "<i>Only strong-fit roles get tailored — this one "
                            "scored below the threshold, or tailoring failed.</i>")
                t = store.get_tailoring(conn, row["key"]) or {}
                out = [f"📄 <b>{e(row['company'])}</b> — {e(row['title'])}", ""]
                out += [f"<b>Headline</b>\n  {e(t.get('headline',''))}", ""]
                out += [f"<b>Summary</b>\n  {e(t.get('summary','')[:600])}", ""]
                changed = [b for b in (t.get("bullets") or [])
                           if b.get("changed")]
                if changed:
                    out.append(f"<b>Reworded bullets ({len(changed)})</b>")
                    for b in changed:
                        out.append(
                            f"  <b>{e(b.get('id',''))}</b> "
                            f"{e(' '.join(str(b.get('text','')).split()))}")
                for w in t.get("warnings") or []:
                    out += ["", f"⚠️ {e(w)}"]
                return "\n".join(out)

            if cmd == "applied":
                if not arg:
                    return "Usage: <code>/applied &lt;company&gt;</code>"
                row = callprep.mark_applied(conn, arg)
                if not row:
                    return f"No record for <b>{e(arg)}</b>."
                return (f"✅ Marked <b>{e(row['company'])}</b> — "
                        f"{e(row['title'])} as applied.\n\n"
                        f"<i>If they call, send me their name and you'll get "
                        f"the brief.</i>")

            if cmd == "note":
                bits = arg.split(maxsplit=1)
                if len(bits) < 2:
                    return ("Usage: <code>/note &lt;company&gt; &lt;your "
                            "note&gt;</code>")
                if callprep.add_note(conn, bits[0], bits[1]):
                    return (f"📝 Noted against <b>{e(bits[0])}</b>.\n"
                            f"<i>It appears on the brief next time.</i>")
                return f"No record for <b>{e(bits[0])}</b>."

            if cmd == "jd":
                if not arg:
                    return "Usage: <code>/jd &lt;company&gt;</code>"
                row = callprep.lookup(conn, arg)
                if not row:
                    return f"No record for <b>{e(arg)}</b>."
                jd = callprep.jd_to_text(
                    row["jd_snapshot"] or row["description"] or "")
                if not jd.strip():
                    return (f"No JD stored for <b>{e(row['company'])}</b>.\n"
                            f'<a href="{e(row["url"])}">Try the posting →</a>')
                head = (f"📋 <b>{e(row['company'])}</b> — {e(row['title'])}\n"
                        f"<i>Snapshot taken when you applied; the live "
                        f"posting may be gone.</i>\n\n")
                return head + e(jd[:3200])

            if cmd == "brief" or not text.startswith("/"):
                name = arg if cmd == "brief" else text
                if not name:
                    return "Usage: <code>/brief &lt;company&gt;</code>"
                row = callprep.lookup(conn, name)
                if not row:
                    return (f"No record for <b>{e(name)}</b>.\n"
                            "<i>Either you haven't applied there, or it came "
                            "in before tracking started.</i>")
                tailored = store.get_tailoring(conn, row["key"])
                profile = yaml.safe_load(
                    (Path(__file__).resolve().parents[1] / "config" /
                     "profile.yml").read_text(encoding="utf-8"))
                return callprep.build(row, profile, tailored)

            return (f"Unknown command: <code>{e(cmd)}</code>\n\n{HELP}")

    except Exception as ex:
        return f"⚠️ Error handling that: {e(type(ex).__name__)}"
