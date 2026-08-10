"""Telegram delivery for the LinkedIn drafting bot.

Deliberately independent of notify.py rather than importing it: this bot
needs inline keyboards and callback-query handling that the job bot's
sender was never built for, and a bug in one send path must not be able
to reach the other. The ~80 lines of duplication here is the price of
that isolation, paid on purpose — see SPEC.md's Phase 6 note.
"""

from __future__ import annotations

import html
import os
import time

import httpx

API = "https://api.telegram.org/bot{token}/{method}"


class Telegram:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or os.environ.get("SOCIAL_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        if not self.token or not self.chat_id:
            raise RuntimeError(
                "SOCIAL_BOT_TOKEN and TELEGRAM_CHAT_ID must be set "
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
                if 400 <= r.status_code < 500:
                    return data
            except Exception as e:
                last = {"ok": False, "error": str(e)}
            time.sleep(2 ** attempt)
        return last

    def _keyboard(self, buttons):
        return {
            "inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row]
                for row in buttons
            ]
        } if buttons else {"inline_keyboard": []}

    def send(self, text: str, buttons=None) -> dict:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = self._keyboard(buttons)
        return self._post("sendMessage", payload)

    def edit(self, message_id: int, text: str, buttons=None) -> dict:
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons is not None:
            payload["reply_markup"] = self._keyboard(buttons)
        return self._post("editMessageText", payload)

    def answer_callback(self, callback_id: str, text: str = "") -> dict:
        return self._post("answerCallbackQuery", {
            "callback_query_id": callback_id, "text": text[:200]})


def ideas_card(ideas: list[dict]) -> str:
    """Strategy doc section 27 — proposed angles, shown before any full
    post is written. Each idea's hook/angle/format are already
    guardrail-scanned (see social_draft.generate_ideas); flags surface
    here so a bad idea can be skipped before spending a second Gemini
    call writing it out in full."""
    e = html.escape
    lines = [f"💡 <b>Ideas</b> — pick one to write the full post", ""]
    for idea in ideas:
        n = idea.get("number", "?")
        lines += [
            f"<b>{n}. {e(idea.get('hook', ''))}</b>",
            f"<i>{e(idea.get('angle', ''))}</i>",
            f"Format: {e(idea.get('format', ''))}",
        ]
        flags = idea.get("flags") or []
        if flags:
            lines.append("⚠️ " + "; ".join(e(f) for f in flags))
        lines.append("")
    return "\n".join(lines).rstrip()


def idea_buttons(idea_set_id: int, ideas: list[dict]):
    return [[(f"Idea {idea.get('number', i + 1)}",
              f"idea:{idea_set_id}:{idea.get('number', i + 1)}")]
            for i, idea in enumerate(ideas)]


def draft_card(draft_id: int, text: str, flags: list[str],
              visual_idea: str = "", first_comment: str = "",
              alt_hooks: list[str] | None = None) -> str:
    """Built to be judged in a few seconds on a phone, same spirit as
    notify.py's strong_fit_card — and built to be copied cleanly.

    The post text sits inside a <pre> block. Telegram renders that as
    monospace with a one-tap copy action (desktop: hover, click the copy
    icon; mobile: tap the block, or long-press -> Copy) and hands back
    the exact text with no HTML entities or markdown symbols mixed in.
    Nothing outside the <pre> block is meant to be copied.
    """
    e = html.escape
    lines = [
        f"📝 <b>Draft #{draft_id}</b> — tap the block below to copy",
        "",
        f"<pre>{e(text)}</pre>",
    ]
    if flags:
        lines += ["", "⚠️ <b>Flags — check before pasting this anywhere</b>"]
        lines += [f"• {e(f)}" for f in flags]
    if visual_idea:
        lines += ["", "🎨 <b>Visual idea</b>", e(visual_idea)]
    if first_comment:
        lines += ["", "💬 <b>First comment</b> (tap to copy)",
                  f"<pre>{e(first_comment)}</pre>"]
    if alt_hooks:
        lines += ["", "<b>Alt hooks</b>"]
        lines += [f"• {e(h)}" for h in alt_hooks]
    lines += ["", f"<i>{len(text)} characters</i>"]
    return "\n".join(lines)
