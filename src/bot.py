"""Telegram polling loop.

    python -m src.bot            # run until interrupted
    python -m src.bot --once     # drain pending updates and exit

GitHub Actions cannot host a long-running poller within the free minute
budget (a 5-minute cadence alone would need ~8600 runs/month against a
2000-minute allowance). So this serves two roles:

  * local use — run it while working, get instant replies
  * CI use — `--once` at the start of each scheduled scan, so commands
    sent between runs are still answered

Instant always-on replies need a webhook; see README for the Cloudflare
Worker setup, which is free and needs no card.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

from .commands import handle, resume_for

API = "https://api.telegram.org/bot{token}/{method}"
OFFSET_FILE = ".bot_offset"


def _token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not t:
        print("TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    return t


def _allowed_chat() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "")


def _load_offset() -> int:
    try:
        return int(open(OFFSET_FILE).read().strip())
    except Exception:
        return 0


def _save_offset(v: int) -> None:
    try:
        open(OFFSET_FILE, "w").write(str(v))
    except Exception:
        pass


def poll_once(token: str, offset: int, timeout: int = 0) -> int:
    """Fetch and answer pending updates. Returns the new offset."""
    try:
        r = httpx.get(
            API.format(token=token, method="getUpdates"),
            params={"offset": offset, "timeout": timeout},
            timeout=timeout + 15,
        )
        data = r.json()
    except Exception as e:
        print(f"poll error: {type(e).__name__}", file=sys.stderr)
        return offset

    if not data.get("ok"):
        return offset

    allowed = _allowed_chat()
    for upd in data.get("result", []):
        offset = max(offset, upd["update_id"] + 1)
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        chat_id = str((msg.get("chat") or {}).get("id", ""))

        # Only PJ's own chat is served. The bot username is guessable, so
        # without this anyone could query his job pipeline.
        if allowed and chat_id != allowed:
            print(f"ignored message from unauthorised chat {chat_id}")
            continue

        text = msg.get("text", "")
        print(f"<- {text!r}")

        # Free text gets translated to a canonical slash command first —
        # same contract as command_runner.py: nlu.resolve() only
        # produces text, so the /resume file-handling check right below
        # still applies to whatever it returns.
        if not text.startswith("/"):
            from . import nlu
            text, direct = nlu.resolve(text)
            if direct:
                httpx.post(
                    API.format(token=token, method="sendMessage"),
                    json={"chat_id": chat_id, "text": direct,
                          "parse_mode": "HTML",
                          "disable_web_page_preview": True},
                    timeout=25.0,
                )
                continue

        # /resume returns a file rather than text.
        if text.strip().lower().startswith("/resume"):
            arg = text.split(maxsplit=1)[1].strip() if " " in text else ""
            if arg:
                path, caption = resume_for(arg)
                if path:
                    from .notify import Telegram
                    Telegram(token, chat_id).send_document(path, caption)
                    continue
                reply = caption
            else:
                reply = "Usage: <code>/resume &lt;company&gt;</code>"
        else:
            reply = handle(text)
        try:
            httpx.post(
                API.format(token=token, method="sendMessage"),
                json={
                    "chat_id": chat_id,
                    "text": reply,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=25.0,
            )
        except Exception as e:
            print(f"reply failed: {type(e).__name__}", file=sys.stderr)
    return offset


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true",
                   help="drain pending updates and exit")
    args = p.parse_args()

    token = _token()
    offset = _load_offset()

    if args.once:
        new = poll_once(token, offset, timeout=0)
        _save_offset(new)
        return 0

    print("polling — Ctrl+C to stop")
    try:
        while True:
            new = poll_once(token, offset, timeout=25)
            if new != offset:
                offset = new
                _save_offset(offset)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
