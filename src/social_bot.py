"""Telegram poller for the LinkedIn drafting bot.

A separate process from bot.py, with its own token (SOCIAL_BOT_TOKEN),
its own offset file (.bot_offset.social), and its own database
(data/social.db). Nothing here imports scout, store, tailor, bot,
commands, or notify — and nothing there imports this. A crash or bug in
one bot cannot reach the other; that is the entire design goal, not an
accident of file layout.

    python -m src.social_bot                       # run until interrupted (local use)
    python -m src.social_bot --once                # drain pending updates and exit
    python -m src.social_bot --handle-message TEXT  # one-shot, used by GitHub Actions
    python -m src.social_bot --handle-callback JSON # one-shot, used by GitHub Actions

The last two modes exist for 24/7 operation without a laptop: the same
Cloudflare Worker that already serves the job bot (see worker/worker.js)
takes a second route for this bot's webhook, dispatches to GitHub
Actions, and Actions runs one of these one-shot modes and exits — no
polling loop involved. The polling loop above still works for local
testing, but must not run at the same time as the webhook (Telegram
will not deliver to both — see worker/README.md).

Flow: send any text that isn't a command and it's treated as raw context
-> a draft comes back in a tap-to-copy block, plus any guardrail flags.
Paste it into LinkedIn yourself and post it there.

This bot never calls any LinkedIn API to publish. LinkedIn's API Terms
of Use (Section 3.1.26) prohibit using the API "to automate posting on
the LinkedIn Services" — full stop, regardless of a human approving the
content first. So there is no post_text() call anywhere in this file;
"Mark posted" below is pure bookkeeping (for /status history), not a
publish action. See SPEC.md "Phase 6" for the finding.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time

import httpx

from . import social_draft, social_store
from .social_notify import Telegram, draft_card, idea_buttons, ideas_card

API = "https://api.telegram.org/bot{token}/{method}"
OFFSET_FILE = ".bot_offset.social"

HELP = """<b>LinkedIn draft bot</b>

Send any text — a topic, a bullet, a raw idea — and I'll come back with
3-5 different angles to pick from (strategy doc section 27). Reply with
the idea's number, or tap its button, and I'll write the full post for
it, plus a visual idea, a first comment, and 2 alt hooks. Paste the post
into LinkedIn yourself; this bot never posts anything on its own.

/drafts — pending drafts
/status — recent activity
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


def _draft_buttons(draft_id: int):
    # Both actions are bookkeeping only — neither calls any LinkedIn API.
    # Posting happens when PJ pastes the copied text into LinkedIn himself.
    return [[("✅ Mark posted", f"posted:{draft_id}"),
             ("❌ Discard", f"discard:{draft_id}")]]


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
                    buttons=_draft_buttons(d["id"]))
        return

    if stripped.startswith("/"):
        tg.send(f"Unknown command: <code>{cmd}</code>\n\n{HELP}")
        return

    if not stripped:
        tg.send(HELP)
        return

    # A bare number replying to a pending idea set = selection, not new
    # context. Anything else (including a number with more text) is
    # treated as a fresh topic — see _handle_callback for the button path,
    # which does the same thing without relying on typing a digit.
    if stripped.isdigit():
        with social_store.connect() as conn:
            idea_set = social_store.latest_pending_ideas(conn)
        if idea_set:
            _write_selected_idea(tg, idea_set, int(stripped))
            return
        tg.send("No pending ideas to pick from — send a topic first.")
        return

    # Freeform text = raw context for a new set of ideas (strategy doc
    # section 27). The full post is only written after a selection, in
    # _write_selected_idea below — this is deliberately two Gemini calls,
    # not one, so PJ picks the angle instead of getting whatever the
    # model reached for first.
    result = social_draft.generate_ideas(stripped)
    if not result.ok:
        tg.send(f"⚠️ Could not generate ideas: {result.error}")
        return

    with social_store.connect() as conn:
        idea_set_id = social_store.create_ideas(conn, stripped, result.ideas)

    tg.send(ideas_card(result.ideas),
            buttons=idea_buttons(idea_set_id, result.ideas))


def _write_selected_idea(tg: Telegram, idea_set, number: int) -> None:
    """Second Gemini call of the two-step flow — writes the full post
    for whichever idea PJ picked, from either the number-reply path
    above or the button-tap path in _handle_callback."""
    ideas = json.loads(idea_set["ideas_json"])
    idea = next((i for i in ideas if int(i.get("number", -1)) == number), None)
    if not idea:
        tg.send(f"No idea numbered {number} in that set.")
        return

    result = social_draft.draft_from_idea(idea_set["context"], idea)
    if not result.ok:
        tg.send(f"⚠️ Could not write that post: {result.error}")
        return

    with social_store.connect() as conn:
        social_store.mark_ideas_resolved(conn, idea_set["id"])
        draft_id = social_store.create_from_idea(
            conn, idea_set["context"], idea, result.text, result.flags,
            result.visual_idea, result.first_comment, result.alt_hooks)

    resp = tg.send(
        draft_card(draft_id, result.text, result.flags,
                   result.visual_idea, result.first_comment,
                   result.alt_hooks),
        buttons=_draft_buttons(draft_id))
    msg = resp.get("result") or {}
    if msg.get("message_id"):
        with social_store.connect() as conn:
            social_store.set_message_id(conn, draft_id, msg["message_id"])


def _handle_callback(tg: Telegram, cq: dict) -> None:
    data = cq.get("data", "")
    cq_id = cq.get("id", "")
    message_id = (cq.get("message") or {}).get("message_id")
    action, _, rest = data.partition(":")

    if action == "idea":
        # callback_data shape: "idea:{idea_set_id}:{number}"
        id_str, _, number_str = rest.partition(":")
        if not (id_str.isdigit() and number_str.isdigit()):
            tg.answer_callback(cq_id, "bad request")
            return
        with social_store.connect() as conn:
            idea_set = conn.execute(
                "SELECT * FROM idea_sets WHERE id=?", (int(id_str),)
            ).fetchone()
        if not idea_set:
            tg.answer_callback(cq_id, "idea set not found")
            return
        if idea_set["status"] != "pending":
            tg.answer_callback(cq_id, "already picked")
            return
        tg.answer_callback(cq_id, "writing the full post…")
        if message_id:
            tg.edit(message_id, f"✍️ Writing idea {number_str}…")
        _write_selected_idea(tg, idea_set, int(number_str))
        return

    id_str = rest
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

        if action == "posted":
            # Bookkeeping only — PJ already pasted this into LinkedIn
            # himself. No API call happens here.
            social_store.mark_posted(conn, draft_id, "")
            tg.answer_callback(cq_id, "marked posted")
            if message_id:
                tg.edit(message_id,
                        f"✅ <b>Marked posted</b> — draft #{draft_id}\n\n"
                        f"<pre>{html.escape(row['text'])}</pre>")
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
    p.add_argument("--handle-message",
                   help="process one message's text and exit (used by "
                        "the Worker -> Actions dispatch, not polling)")
    p.add_argument("--handle-callback",
                   help="process one callback_query, given as a JSON "
                        "string with id/data/message.message_id, and exit")
    args = p.parse_args()

    token = _token()

    # One-shot modes for the webhook path: no offset file, no getUpdates
    # call — the Worker already received the update, this just does the
    # work Telegram itself cannot (drafting needs a Gemini call, and the
    # Worker's execution budget is too short to wait on that reliably).
    if args.handle_message is not None:
        tg = Telegram(token, _allowed_chat())
        _handle_text(tg, args.handle_message)
        return 0

    if args.handle_callback is not None:
        tg = Telegram(token, _allowed_chat())
        try:
            cq = json.loads(args.handle_callback)
        except Exception as e:
            print(f"bad --handle-callback JSON: {type(e).__name__}",
                  file=sys.stderr)
            return 1
        _handle_callback(tg, cq)
        return 0

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
