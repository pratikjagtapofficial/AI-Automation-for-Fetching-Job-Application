"""Drafts LinkedIn posts from PJ's raw context via Gemini.

Mirrors tailor.py's philosophy exactly: the model does the writing, plain
Python does the gating. An LLM told "don't mention you're job hunting"
will comply most of the time; the failure mode here is a public,
permanent post that outs a quiet search — worse than a resume line a
human catches before hitting submit. So drafting (this file's job) and
validation (scan_guardrails, deterministic) are separate steps, same
split as validator.py makes for the resume side.

This module only reads config/ — it never writes to data/jobs.db or any
file the job-scanning pipeline owns.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]

# Same candidate list as tailor.py, for the same reason: the models
# endpoint cannot be trusted to reflect what a given key can actually
# call, so probe in order and use the first that answers.
MODEL_CANDIDATES = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-flash-latest",
    "gemini-3.5-flash-lite",
]
ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/"
            "models/{model}:generateContent")

IDEAS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "ideas": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "number": {"type": "INTEGER"},
                    "hook": {"type": "STRING"},
                    "angle": {"type": "STRING"},
                    "format": {"type": "STRING"},
                },
                "required": ["number", "hook", "angle", "format"],
            },
        },
    },
    "required": ["ideas"],
}

POST_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "post": {"type": "STRING"},
        "visual_idea": {"type": "STRING"},
        "first_comment": {"type": "STRING"},
        "alt_hooks": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["post"],
}


@dataclass
class IdeaSet:
    ok: bool
    ideas: list[dict] = field(default_factory=list)
    error: str = ""


@dataclass
class Draft:
    ok: bool
    text: str = ""
    flags: list[str] = field(default_factory=list)
    visual_idea: str = ""
    first_comment: str = ""
    alt_hooks: list[str] = field(default_factory=list)
    error: str = ""


def _voice() -> dict:
    return yaml.safe_load(
        (ROOT / "config" / "social.yml").read_text(encoding="utf-8"))


def _profile() -> dict:
    return yaml.safe_load(
        (ROOT / "config" / "profile.yml").read_text(encoding="utf-8"))


def _strategy() -> str:
    return (ROOT / "config" / "social_strategy.md").read_text(encoding="utf-8")


def _hard_rules(max_chars: int) -> str:
    """Facts override the strategy doc, not the other way round — the
    strategy doc is about persuasion, this is about what's actually
    true. See social_strategy.md's "Hard override" section for why this
    split exists (it lists Docker/Azure/AWS in places; profile.yml says
    Pratik doesn't have them, and profile.yml is the project's single
    source of truth). Shared by both the ideas prompt and the full-post
    prompt — a forbidden skill must never appear even at the hook/angle
    stage, not just filtered out of the final text."""
    profile = _profile()
    lacks = (profile.get("skills", {}) or {}).get("lacks") or []
    months = (profile.get("experience", {}) or {}).get("total_months", 0)

    return f"""# HARD RULES — these override the strategy document above if they ever conflict
- Do not reference any of these skills anywhere, even as an SEO keyword,
  a passing mention, or part of a hook — Pratik does not have them:
  {', '.join(lacks) if lacks else '(none)'}
- Pratik has ~{months} months of professional experience. Do not imply more.
- Do not name Pratik's current employer.
- Do not invent projects, metrics, companies, or achievements not present
  in the context below. If the context is thin, stay general rather than
  filling gaps with invented specifics.
- Do not imply Pratik is job hunting, open to work, or dissatisfied with
  his current role. This is a technical share, not a status update.
- Hard character limit on the final post: {max_chars} (LinkedIn's own cap)
  — the strategy document's 150-300 word target is the real goal, this
  is just a ceiling."""


def build_ideas_prompt(context: str) -> str:
    """Strategy doc section 27: propose 3-5 genuinely different angles
    before writing anything. This is the first of the two Gemini calls
    in the flow — see draft_from_idea() for the second."""
    max_chars = (_voice().get("voice", {}) or {}).get("max_chars", 3000)

    return f"""{_strategy()}

# CONTEXT FROM PRATIK — the raw material for today's post
{context.strip()}

{_hard_rules(max_chars)}

# TASK
Follow section 27 of the strategy above: generate 3-5 genuinely
different content ideas from this context — not variations of the same
angle. Each needs an exact Hook (the opening 1-2 lines), a distinct
Angle (the unique perspective), and a recommended Format.

# OUTPUT
Return ONLY valid JSON, no markdown fence:
{{"ideas": [{{"number": 1, "hook": "...", "angle": "...", "format": "..."}}, ...]}}"""


def build_post_prompt(context: str, idea: dict) -> str:
    """Strategy doc section 28: write the full post for the idea PJ
    picked, plus a visual suggestion, an optional first comment, and 2
    alt hooks. This is the second Gemini call, only run after selection
    — see draft_from_idea()."""
    max_chars = (_voice().get("voice", {}) or {}).get("max_chars", 3000)

    return f"""{_strategy()}

# CONTEXT FROM PRATIK — the raw material for today's post
{context.strip()}

# SELECTED IDEA — write the post for exactly this angle, not a different one
Hook: {idea.get('hook', '')}
Angle: {idea.get('angle', '')}
Format: {idea.get('format', '')}

{_hard_rules(max_chars)}

# TASK
Follow section 28 of the strategy above: write the complete LinkedIn
post for the selected idea, silently running the quality checks in
section 29 before finalizing. Then provide a Visual idea, an optional
First comment, and 2 Alt hooks (in case the original hook could be
improved).

# OUTPUT
Return ONLY valid JSON, no markdown fence:
{{"post": "the full post text", "visual_idea": "...", "first_comment": "...", "alt_hooks": ["...", "..."]}}"""


def _extract_json(raw: str) -> dict | None:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def call_gemini(prompt: str, schema: dict, api_key: str | None = None,
                timeout: float = 60.0) -> tuple[str, str]:
    """-> (text, error). Falls through the candidate models on 404/429."""
    key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return "", "GEMINI_API_KEY not set"

    models = [os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL") \
        else MODEL_CANDIDATES
    last = ""

    for model in models:
        try:
            r = httpx.post(
                ENDPOINT.format(model=model),
                params={"key": key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.6,
                        "maxOutputTokens": 2048,
                        "responseMimeType": "application/json",
                        "responseSchema": schema,
                    },
                },
                timeout=timeout,
            )
            if r.status_code in (404, 429, 503):
                last = f"{model}: HTTP {r.status_code}"
                continue
            if r.status_code != 200:
                return "", f"{model}: HTTP {r.status_code}: {r.text[:200]}"

            data = r.json()
            cands = data.get("candidates") or []
            if not cands:
                last = f"{model}: no candidates"
                continue
            parts = (cands[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
            if not text.strip():
                last = f"{model}: empty response"
                continue
            return text, ""
        except Exception as e:
            last = f"{model}: {type(e).__name__}"
            continue

    return "", last or "all models failed"


def scan_guardrails(text: str) -> list[str]:
    """Deterministic check — the part that must not depend on the model
    having listened. Same job validator.py does for the resume, applied
    to a different failure mode: a public post outing a quiet search."""
    voice = _voice()
    profile = _profile()
    flags: list[str] = []

    for phrase in voice.get("avoid_phrases") or []:
        if re.search(re.escape(phrase), text, re.I):
            flags.append(f"job-search signal: '{phrase}'")

    for name in voice.get("never_mention") or []:
        if re.search(re.escape(name), text, re.I):
            flags.append(f"never mention '{name}' — quiet search")

    lacks = (profile.get("skills", {}) or {}).get("lacks") or []
    for skill in lacks:
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(skill)}(?![A-Za-z0-9])", re.I)
        if pattern.search(text):
            flags.append(f"claims '{skill}' — not an actual skill")

    max_chars = (voice.get("voice", {}) or {}).get("max_chars", 3000)
    if len(text) > max_chars:
        flags.append(f"{len(text)} chars — over LinkedIn's {max_chars} limit")

    return flags


def generate_ideas(context: str, api_key: str | None = None) -> IdeaSet:
    """First of the two-step flow (strategy doc section 27). Never
    raises — on any failure returns ok=False with a reason."""
    if not context.strip():
        return IdeaSet(ok=False, error="empty context")

    raw, err = call_gemini(build_ideas_prompt(context), IDEAS_SCHEMA, api_key)
    if err:
        return IdeaSet(ok=False, error=err)

    parsed = _extract_json(raw)
    ideas = (parsed or {}).get("ideas") or []
    if not ideas:
        return IdeaSet(ok=False, error="model returned no ideas")

    # Guardrail-scan every hook/angle up front — a forbidden skill or a
    # job-search signal in the hook itself is worth catching before PJ
    # even picks an idea, not just in the final post.
    for idea in ideas:
        combined = f"{idea.get('hook', '')} {idea.get('angle', '')}"
        idea["flags"] = scan_guardrails(combined)

    return IdeaSet(ok=True, ideas=ideas)


def draft_from_idea(context: str, idea: dict,
                    api_key: str | None = None) -> Draft:
    """Second of the two-step flow (strategy doc section 28), run only
    after PJ picks an idea from generate_ideas(). Never raises."""
    if not context.strip():
        return Draft(ok=False, error="empty context")

    raw, err = call_gemini(build_post_prompt(context, idea), POST_SCHEMA,
                           api_key)
    if err:
        return Draft(ok=False, error=err)

    parsed = _extract_json(raw)
    if not parsed or not str(parsed.get("post", "")).strip():
        return Draft(ok=False, error="model returned no usable post")

    text = str(parsed["post"]).strip()
    alt_hooks = [str(h) for h in (parsed.get("alt_hooks") or []) if h]
    return Draft(
        ok=True,
        text=text,
        flags=scan_guardrails(text),
        visual_idea=str(parsed.get("visual_idea", "")).strip(),
        first_comment=str(parsed.get("first_comment", "")).strip(),
        alt_hooks=alt_hooks,
    )
