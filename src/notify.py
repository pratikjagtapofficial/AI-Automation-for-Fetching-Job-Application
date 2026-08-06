"""Telegram delivery.

Phase 1 is send-only: job cards and digests go out, nothing comes back.
Inbound approval buttons arrive in Phase 3 via a Cloudflare Worker, since
GitHub Actions cannot hold a long-running poller cheaply.
"""

from __future__ import annotations

import html
import os
import time
from datetime import datetime, timezone

import httpx

from .sources import Job

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4096  # Telegram hard limit per message


class Telegram:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        if not self.token or not self.chat_id:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set "
                "(local: .env, CI: repository secrets)"
            )

    def _post(self, method: str, payload: dict) -> dict:
        """POST with retry. Honours Telegram's 429 retry_after."""
        url = API.format(token=self.token, method=method)
        last = {}
        for attempt in range(4):
            try:
                r = httpx.post(url, json=payload, timeout=25.0)
                data = r.json()
                if data.get("ok"):
                    return data
                last = data
                if r.status_code == 429:
                    wait = data.get("parameters", {}).get("retry_after", 5)
                    time.sleep(min(wait, 30))
                    continue
                # 4xx other than rate-limit won't fix themselves.
                if 400 <= r.status_code < 500:
                    return data
            except Exception as e:  # network flake
                last = {"ok": False, "error": str(e)}
            time.sleep(2 ** attempt)
        return last

    def send(self, text: str, preview: bool = False) -> dict:
        """Send one message, splitting on newlines if over the limit."""
        chunks = _split(text, MAX_LEN)
        result = {}
        for chunk in chunks:
            result = self._post("sendMessage", {
                "chat_id": self.chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": not preview,
            })
        return result


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            out.append(cur.rstrip())
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur.rstrip())
    return out


def _age(posted: datetime | None) -> str:
    if not posted:
        return "unknown"
    delta = datetime.now(timezone.utc) - posted
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)}m ago"
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{delta.days}d ago"


def _bar(score: int) -> str:
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)


def strong_fit_card(job: Job) -> str:
    """One job, sent immediately. Built to be read on a phone in seconds."""
    e = html.escape
    gaps = [r for r in job.reasons if r.startswith("⚠")]
    good = [r for r in job.reasons if not r.startswith("⚠")]

    lines = [
        f"🎯 <b>{e(job.title)}</b>",
        f"<b>{e(job.company)}</b> · {e(job.location)}",
        "",
        f"<code>{_bar(job.score)}</code> {job.score}/100",
        f"📅 {_age(job.posted_at)} · via {e(job.source)}",
    ]
    if good:
        lines += ["", "<b>Why it fits</b>"]
        lines += [f"• {e(r)}" for r in good[:4]]
    if gaps:
        lines += ["", "<b>Heads up</b>"]
        lines += [f"• {e(r.lstrip('⚠ '))}" for r in gaps]
    lines += ["", f'<a href="{e(job.url)}">Open posting →</a>']
    return "\n".join(lines)


def digest(jobs: list[Job], strong_threshold: int) -> str:
    """Everything below the live-ping bar, batched into one message."""
    e = html.escape
    if not jobs:
        return ("🔍 <b>Job scan complete</b>\n\n"
                "No new matches this run.\n"
                "<i>Nothing is being hidden — the filters found nothing "
                "genuinely relevant, which is normal on a quiet day.</i>")

    head = f"📋 <b>{len(jobs)} new match{'es' if len(jobs) != 1 else ''}</b>"
    lines = [head, ""]
    for j in jobs:
        gaps = "  ⚠️" if any(r.startswith("⚠") for r in j.reasons) else ""
        lines.append(
            f"<b>{j.score}</b> · <a href=\"{e(j.url)}\">{e(j.title)}</a>{gaps}\n"
            f"     {e(j.company)} · {e(j.location)} · {_age(j.posted_at)}"
        )
    return "\n".join(lines)


def run_summary(fetched: int, scored: int, sent: int, errors: list[str]) -> str:
    lines = [
        "✅ <b>Scan finished</b>",
        f"fetched <b>{fetched}</b> · relevant <b>{scored}</b> · notified <b>{sent}</b>",
    ]
    if errors:
        lines += ["", "⚠️ <b>Source problems</b>"]
        lines += [f"• {html.escape(x)}" for x in errors[:6]]
    return "\n".join(lines)
