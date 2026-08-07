"""Natural-language front end for the Telegram bot.

PJ typed "apply to all" and the bot silently treated it as a company name
called "apply to all", found no match, and replied "No record found" —
technically correct, completely unhelpful, and gave no sign that bulk
auto-apply was never a real feature.

This module fixes that class of confusion, but on a narrow contract:
it TRANSLATES free text into one of the bot's existing slash commands,
then hands that canonical string back to the exact same execution path
PJ typing it directly would hit. It never executes anything itself, and
the model may only choose from a fixed intent enum that maps 1:1 onto
commands that already exist — plus one fixed "unsupported" bucket for
things like bulk-apply, auto-submit, or delete requests, which always
gets the same honest, canned explanation. So even if the classifier
misfires, the worst case is picking the wrong *existing* safe command —
it cannot invent a new capability like actually applying to a job.

If classification is unavailable or low-confidence, resolve() returns
the original text unchanged, which falls through to the same legacy
"treat it as a company name" behaviour that existed before this file —
so nothing regresses when Gemini is down or the message really is just
a company name.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from . import store
from .tailor import ENDPOINT, MODEL_CANDIDATES

INTENTS = (
    "status", "pending", "recent", "questions", "scan", "help",
    "brief", "diff", "resume", "jd", "applied", "note", "answer",
    "apply", "unsupported", "unknown",
)

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intent": {"type": "STRING", "enum": list(INTENTS)},
        "company": {"type": "STRING"},
        "count": {"type": "INTEGER"},
        "note_text": {"type": "STRING"},
        "answer_number": {"type": "INTEGER"},
        "answer_text": {"type": "STRING"},
    },
    "required": ["intent"],
}

# Same reply regardless of which unsupported thing was asked for
# (bulk-apply, auto-submit, delete, edit the resume by chat, etc.) —
# one honest sentence beats the model improvising a different excuse
# each time.
UNSUPPORTED_REPLY = (
    "🚫 I don't do that. Every application goes through a real review — "
    "nothing gets bulk-approved or auto-submitted, even from a plain-"
    "English request.\n\n"
    "Use <code>/pending</code> to see what's waiting, "
    "<code>/resume &lt;company&gt;</code> for a specific one, or "
    "<code>/diff &lt;company&gt;</code> to check the tailoring — then "
    "you apply it yourself."
)


def _prompt(text: str, companies: list[str]) -> str:
    known = ", ".join(companies) or "(none yet)"
    return f"""Classify this Telegram message sent to a job-application
tracking bot into exactly one intent.

# INTENTS
status      — pipeline counts, last run
pending     — jobs awaiting a decision
recent      — latest matches (optional count, e.g. "show me 10")
questions   — form questions needing an answer
scan        — run a job scan right now
help        — what can you do / how do I use this
brief       — call-prep summary for one company: "what did I send X",
              "X called", "X called me", or any short mention that a
              company is calling/contacting PJ with no explicit request
              to save/remember/note something — this is the single most
              important intent to get right, since it's what a real HR
              call needs answered in seconds
diff        — resume changes made for one company's job
resume      — send the tailored PDF for one company's job
jd          — the saved job description for one company's job
applied     — confirm PJ actually submitted an application himself
note        — save a freeform note against one company; requires an
              explicit save/remember/log-style instruction plus actual
              content to save (e.g. "note for X: ...", "log that X said
              ..."). A bare "X called" with nothing further to record is
              "brief", not "note".
answer      — answer a numbered open question (needs answer_number + answer_text)
apply       — start a real submission for exactly ONE explicitly named
              company's job ("apply to X", "go ahead and submit the X
              one", "send my application to X"). Requires a specific
              company to be named in THIS message — a pronoun referring
              back to an earlier message ("apply to that one", "yes do
              it") is NOT enough on its own, since there is nothing to
              fuzzy-match against; classify those as "unknown" instead
              of guessing which job was meant. This never submits by
              itself — it only starts a confirmation step PJ must still
              approve separately.
unsupported — bulk-apply / apply-to-all / apply-to-everything / auto-
              submit-all / delete data / anything that would act on more
              than one job at once, or that asks to skip the individual
              confirmation step entirely (e.g. "apply and don't ask me
              again", "just auto apply from now on")
unknown     — genuinely unclear, or none of the above fit

# COMPANIES RECENTLY IN THE TRACKER (for fuzzy matching only — pick the
# closest real name if the message references one loosely, e.g. "the
# gitlab one" or "meraki's backend role")
{known}

# MESSAGE
{text!r}

Return company as the best-matching name from the list above when the
intent needs one, or leave it empty if none is referenced or none match
well enough to be confident. Never invent a company not in that list."""


def _call(text: str, companies: list[str],
          api_key: str | None = None) -> dict | None:
    import os
    key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return None
    for model in MODEL_CANDIDATES:
        try:
            r = httpx.post(
                ENDPOINT.format(model=model),
                params={"key": key},
                json={
                    "contents": [{"parts": [{"text": _prompt(text, companies)}]}],
                    "generationConfig": {
                        "temperature": 0.1,
                        "maxOutputTokens": 512,
                        "responseMimeType": "application/json",
                        "responseSchema": SCHEMA,
                    },
                },
                timeout=25.0,
            )
            if r.status_code in (404, 429, 503):
                continue
            if r.status_code != 200:
                return None
            data = r.json()
            cands = data.get("candidates") or []
            if not cands:
                continue
            parts = (cands[0].get("content") or {}).get("parts") or []
            raw = "".join(p.get("text", "") for p in parts).strip()
            return json.loads(raw) if raw else None
        except Exception:
            continue
    return None


def _synthesize(parsed: dict) -> str | None:
    """Intent + params -> a canonical slash command string, or None if
    the intent needs no translation / can't be built safely."""
    intent = parsed.get("intent")
    company = (parsed.get("company") or "").strip()

    simple = {"status": "/status", "pending": "/pending",
              "questions": "/questions", "scan": "/scan", "help": "/help"}
    if intent in simple:
        return simple[intent]

    if intent == "recent":
        n = parsed.get("count")
        return f"/recent {int(n)}" if isinstance(n, int) and n > 0 else "/recent"

    if intent in ("brief", "diff", "resume", "jd", "applied", "apply") and company:
        return f"/{intent} {company}"

    if intent == "note" and company and parsed.get("note_text"):
        return f"/note {company} {parsed['note_text']}"

    if intent == "answer":
        n, a = parsed.get("answer_number"), parsed.get("answer_text")
        if isinstance(n, int) and a:
            return f"/answer {n} {a}"

    return None


def resolve(text: str) -> tuple[str, str | None]:
    """-> (canonical_command, direct_reply). Exactly one path applies:

    - direct_reply set: send this verbatim, do nothing else.
    - canonical_command set: run it through the SAME command dispatch
      used for typed slash commands (including any file-sending special
      case) — this function only classifies, it never executes.

    Falls back to the original text unchanged whenever classification
    fails, is low-confidence, or Gemini is unavailable, which reproduces
    the pre-existing "treat it as a company name" behaviour exactly.
    """
    text = (text or "").strip()
    if not text or text.startswith("/"):
        return text, None

    try:
        with store.connect() as conn:
            companies = store.distinct_companies(conn, 40)
    except Exception:
        companies = []

    parsed = _call(text, companies)
    if not parsed:
        return text, None  # Gemini unavailable — legacy fallback

    intent = parsed.get("intent")
    if intent == "unsupported":
        return text, UNSUPPORTED_REPLY
    if intent in (None, "unknown"):
        return text, None

    cmd = _synthesize(parsed)
    return (cmd, None) if cmd else (text, None)
