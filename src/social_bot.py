"""Telegram poller for the LinkedIn drafting bot.

A separate process from bot.py, with its own token (SOCIAL_BOT_TOKEN),
its own offset file (.bot_offset.social), and its own database
(data/social.db). Nothing here imports scout, store, tailor, bot,
commands, or notify — and nothing there imports this. A crash or bug in
one bot cannot reach the other; that is the entire design goal, not an
accident of file layout.

    python -m src.social_bot            # run until interrupted
    python -m src.social_bot --once     # drain pending updates and exit

Flow: send any text that isn't a command and it's treated as raw context
-> a draft comes back with Post / Discard buttons -> tapping Post
publishes immediately via the official Share on LinkedIn API. Nothing
goes out without that tap.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

from . import linkedin, social_draft, social_store
from .social_notify import Telegram, draft_card

API = "https://api.telegram.org/bot{token}/{method}"
OFFSET_FILE = ".bot_offset.social"

HELP = """<b>LinkedIn draft bot</b>

Send any text — a topic, a bullet, a raw idea — and I'll draft a post
from it and send it back for approval. Nothing posts without you tapping
Post.

/drafts — pending drafts awaiting a decision
/status — token health + recent posts
/help — this message"""


def _token() -> str:
    t = os.environ.get("SOCIAL_BOT_TOKEN", "")
    if not t:
        print("SOCIAL_BOT_TOKEN not set", file=sys.stderr)
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


def _approval_buttons(draft_id: int):
    return [[("✅ Post", f"post:{draft_id}"), ("❌ Discard", f"discard:{draft_id}")]]


def _handle_text(tg: Telegram, text: str) -> None:
    stripped = text.strip()
    cmd = stripped.split()[0].lower() if stripped else ""

    if cmd in ("/start", "/help"):
        tg.send(HELP)
        return

    if cmd == "/status":
        with social_store.connect() as conn:
            pend = social_store.pending(conn)
            recent = social_store.recent_posted(conn, 5)
        lines = [
            "📊 <b>Social bot</b>",
            f"LinkedIn token: {linkedin.token_status()}",
            f"pending drafts: <b>{len(pend)}</b>",
        ]
        if recent:
            lines += ["", "<b>Recently posted</b>"]
            for r in recent:
                lines.append(f"  #{r['id']} · {r['posted_at']}")
        tg.send("\n".join(lines))
        return

    if cmd == "/drafts":
        with social_store.connect() as conn:
            pend = social_store.pending(conn)
        if not pend:
            tg.send("✅ Nothing pending.")
            return
        for d in pend:
            flags = json.loads(d["flags"] or "[]")
            tg.send(draft_card(d["id"], d["text"], flags),
                    buttons=_approval_buttons(d["id"]))
        return

    if stripped.startswith("/"):
        tg.send(f"Unknown command: <code>{cmd}</code>\n\n{HELP}")
        return

    # Freeform text = raw context for a new draft.
    if not stripped:
        tg.send(HELP)
        return

    result = social_draft.draft(stripped)
    if not result.ok:
        tg.send(f"⚠️ Could not draft: {result.error}")
        return

    with social_store.connect() as conn:
        draft_id = social_store.create(conn, stripped, result.text, result.flags)

    resp = tg.send(draft_card(draft_id, result.text, result.flags),
                   buttons=_approval_buttons(draft_id))
    msg = resp.get("result") or {}
    if msg.get("message_id"):
        with social_store.connect() as conn:
            social_store.set_message_id(conn, draft_id, msg["message_id"])


def _handle_callback(tg: Telegram, cq: dict) -> None:
    data = cq.get("data", "")
    cq_id = cq.get("id", "")
    message_id = (cq.get("message") or {}).get("message_id")
    action, _, id_str = data.partition(":")
    if not id_str.isdigit():
        tg.answer_callback(cq_id, "bad request")
        return
    draft_id = int(id_str)

    with social_store.connect() as conn:
        row = social_store.get(conn, draft_id)
        if not row:
            tg.answer_callback(cq_id, "not found")
            return
        if row["status"] != "pending":
            tg.answer_callback(cq_id, f"already {row['status']}")
            return

        if action == "discard":
            social_store.mark_discarded(conn, draft_id)
            tg.answer_callback(cq_id, "discarded")
            if message_id:
                tg.edit(message_id, f"❌ <b>Discarded</b> — draft #{draft_id}")
            return

        if action == "post":
            tg.answer_callback(cq_id, "posting…")
            result = linkedin.post_text(row["text"])
            if not result.ok:
                if message_id:
                    tg.edit(
                        message_id,
                        f"⚠️ <b>Post failed</b> — draft #{draft_id}\n"
                        f"{result.error}\n\n<i>Still pending — fix the "
                        f"issue and tap Post again.</i>",
                        buttons=_approval_buttons(draft_id),
                    )
                return
            social_store.mark_posted(conn, draft_id, result.post_id)
            if message_id:
                tg.edit(message_id,
                        f"✅ <b>Posted</b> — draft #{draft_id}\n\n"
                        f"{row['text']}")
            return

    tg.answer_callback(cq_id, "unknown action")


def poll_once(token: str, offset: int, timeout: int = 0) -> int:
    try:
        r = httpx.get(
            API.format(token=token, method="getUpdates"),
            params={
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
            timeout=timeout + 15,
        )
        data = r.json()
    except Exception as e:
        print(f"poll error: {type(e).__name__}", file=sys.stderr)
        return offset

    if not data.get("ok"):
        return offset

    allowed = _allowed_chat()
    tg = Telegram(token, allowed)

    for upd in data.get("result", []):
        offset = max(offset, upd["update_id"] + 1)

        if "callback_query" in upd:
            cq = upd["callback_query"]
            chat_id = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
            if allowed and chat_id != allowed:
                print(f"ignored callback from unauthorised chat {chat_id}")
                continue
            try:
                _handle_callback(tg, cq)
            except Exception as e:
                print(f"callback error: {type(e).__name__}", file=sys.stderr)
            continue

        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        chat_id = str((msg.get("chat") or {}).get("id", ""))

        # Only PJ's own chat is served — same rule as bot.py, applied
        # independently here rather than shared, on purpose.
        if allowed and chat_id != allowed:
            print(f"ignored message from unauthorised chat {chat_id}")
            continue

        text = msg.get("text", "")
        print(f"<- {text!r}")
        try:
            _handle_text(tg, text)
        except Exception as e:
            print(f"handler error: {type(e).__name__}", file=sys.stderr)
            try:
                tg.send(f"⚠️ Error handling that: {type(e).__name__}")
            except Exception:
                pass

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

    print("social bot polling — Ctrl+C to stop")
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
