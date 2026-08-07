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


def draft_card(draft_id: int, text: str, flags: list[str]) -> str:
    """Built to be judged in a few seconds on a phone, same spirit as
    notify.py's strong_fit_card."""
    e = html.escape
    lines = [f"📝 <b>Draft #{draft_id}</b>", "", e(text)]
    if flags:
        lines += ["", "⚠️ <b>Flags — check before posting</b>"]
        lines += [f"• {e(f)}" for f in flags]
    lines += ["", f"<i>{len(text)} characters</i>"]
    return "\n".join(lines)
