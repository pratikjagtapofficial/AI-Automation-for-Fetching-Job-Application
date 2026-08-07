"""Execute one Telegram command and reply.

Invoked by the command workflow when the Cloudflare Worker dispatches a
message:

    python -m src.command_runner "/status"

The Worker cannot read the tracker, so it acknowledges and hands the real
work here. Commands that produce a file (/resume) upload it; everything
else replies as text.
"""

from __future__ import annotations

import os
import sys

from . import nlu
from .commands import handle, resume_for
from .notify import Telegram


def run(text: str) -> int:
    text = (text or "").strip()
    if not text:
        return 0

    tg = Telegram()

    # Free text (no leading "/") gets translated to a canonical slash
    # command before anything else runs, so /scan and /resume below see
    # the SAME string whether PJ typed a command or plain English. This
    # is purely translation — nlu.resolve() never executes anything, so
    # every safety check below (scan-as-separate-job, resume-as-file)
    # still applies to whatever it produces.
    direct = None
    if not text.startswith("/"):
        text, direct = nlu.resolve(text)
        if direct:
            tg.send(direct)
            return 0

    # /scan is dispatched by the Worker as a command, but the actual scan
    # is a separate workflow job — running it inline would block this one
    # for minutes and burn the command budget.
    if text.lower().startswith("/scan"):
        tg.send("🔄 <b>Scan started.</b> Results arrive here shortly.")
        return 0

    if text.lower().startswith("/resume"):
        arg = text.split(maxsplit=1)[1].strip() if " " in text else ""
        if not arg:
            tg.send("Usage: <code>/resume &lt;company&gt;</code>")
            return 0
        path, caption = resume_for(arg)
        if path:
            tg.send_document(path, caption)
        else:
            tg.send(caption)
        return 0

    try:
        tg.send(handle(text))
    except Exception as e:
        # A crash here would leave PJ with no reply and no explanation.
        tg.send(f"⚠️ That command failed: <code>{type(e).__name__}</code>")
        return 1
    return 0


if __name__ == "__main__":
    cmd = " ".join(sys.argv[1:]) or os.environ.get("TG_COMMAND", "")
    sys.exit(run(cmd))
