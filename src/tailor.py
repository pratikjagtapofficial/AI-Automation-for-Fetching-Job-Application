"""Per-job resume tailoring via Gemini, gated by the Validator.

The model is given a deliberately narrow job: reorder and reword content
that already exists. It cannot invent bullets (every output must carry a
source id), cannot touch locked fields, and cannot introduce a skill
outside the whitelist.

That narrowness is the point. A model asked to "tailor a resume" will
happily write that PJ has Kubernetes because the JD wants Kubernetes.
Constraining it to selection-and-rephrasing removes the opportunity, and
the Validator catches whatever slips through anyway.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

import httpx

from .validator import Verdict, validate

# The models endpoint advertises gemini-2.5-flash but it 404s for keys
# created after its deprecation, so the listing cannot be trusted. These
# were probed against PJ's actual key; the first that answers is used.
MODEL_CANDIDATES = [
    "gemini-3.5-flash",       # current default: quality/cost balance
    "gemini-3.6-flash",
    "gemini-flash-latest",    # moving alias — last resort, may drift
    "gemini-3.5-flash-lite",
]
ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/"
            "models/{model}:generateContent")

# Structured output. Without this the model free-writes JSON and, on a
# heavily-constrained prompt, degenerates near the end -- observed looping
# on the constraint text and emitting unparseable output. The schema makes
# malformed JSON impossible rather than something to retry around.
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "headline": {"type": "STRING"},
        "summary": {"type": "STRING"},
        "skill_order": {"type": "ARRAY", "items": {"type": "STRING"}},
        "bullets": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "text": {"type": "STRING"},
                    "changed": {"type": "BOOLEAN"},
                },
                "required": ["id", "text", "changed"],
            },
        },
        "rationale": {"type": "STRING"},
    },
    "required": ["headline", "summary", "skill_order", "bullets"],
}


@dataclass
class TailorResult:
    ok: bool
    headline: str = ""
    summary: str = ""
    skill_order: list[str] = field(default_factory=list)
    bullets: list[dict] = field(default_factory=list)   # {id, text, changed}
    rationale: str = ""
    verdict: Verdict | None = None
    error: str = ""
    used_fallback: bool = False


def _all_bullets(resume: dict) -> dict[str, str]:
    """id -> original text, across experience and projects."""
    out = {}
    for section in ("experience", "projects"):
        for entry in resume.get(section) or []:
            if not isinstance(entry, dict):
                continue
            for b in entry.get("bullets") or []:
                if b.get("id"):
                    out[b["id"]] = " ".join(str(b.get("text", "")).split())
    return out


def _allowed_skills(profile: dict) -> list[str]:
    out = []
    for group in (profile.get("skills", {}).get("has") or {}).values():
        out.extend(group)
    return out


def build_prompt(job, resume: dict, profile: dict) -> str:
    bullets = _all_bullets(resume)
    allowed = _allowed_skills(profile)
    forbidden = profile.get("skills", {}).get("lacks") or []
    groups = [g["name"] for g in resume["skills"]["groups"]]

    # Word count is given alongside each bullet so the model has a
    # concrete target rather than a vague "keep it similar" -- without
    # this, tailored bullets ran visibly longer than the source resume's
    # and PJ's resume no longer fit one page cleanly.
    bullet_block = "\n".join(
        f"  {bid} [{len(txt.split())} words]: {txt}" for bid, txt in bullets.items())
    jd = (job.description or "")[:6000]

    return f"""You are tailoring a resume for ONE specific job application.

# ABSOLUTE RULES — violating any of these fails the output entirely
1. NEVER claim any skill the candidate does not have. These are FORBIDDEN
   and must not appear anywhere in your output, in any form:
   {', '.join(forbidden)}
2. NEVER invent a bullet. Every bullet you return MUST use one of the
   exact source ids listed below. You may reword; you may not fabricate.
3. NEVER change facts: employers, job titles, dates, numbers, metrics.
4. NEVER inflate experience. The candidate has ~11 months professional
   experience. Do not imply more.
5. Only these skills may be referenced: {', '.join(allowed)}
6. NEVER make a bullet longer than its source. Each source bullet below
   is tagged with its word count -- your reworded version must be AT OR
   UNDER that count, never over. The candidate's real resume fits one
   page with bullets at this length; longer ones push it past a page and
   read as padded, not stronger. If you cannot mirror the job's
   vocabulary within the same length, keep more of the original wording
   rather than exceed it.

# WHAT YOU MAY DO
- Choose a headline that fits the job (from the variants, or compose one
  using ONLY allowed skills).
- Rewrite the summary to lead with what this job cares about (max 90 words).
- Reorder the skill groups so the most relevant appear first.
- Reword bullets to mirror the job's vocabulary, keeping the same facts
  AND the same or shorter length (see rule 6 -- this is not optional).
- Reorder bullets so the most relevant lead.

# THE JOB
Title: {job.title}
Company: {job.company}
Location: {job.location}

Description:
{jd}

# CANDIDATE'S CURRENT SUMMARY
{' '.join(str(resume['summary']['default']).split())}

# HEADLINE VARIANTS AVAILABLE
{json.dumps(resume['headline'], indent=2)}

# SKILL GROUPS (reorder only — do not add or remove)
{json.dumps(groups, indent=2)}

# SOURCE BULLETS (use these ids exactly)
{bullet_block}

# OUTPUT
Return ONLY valid JSON, no markdown fence:
{{
  "headline": "...",
  "summary": "...",
  "skill_order": ["group name", ...],
  "bullets": [
    {{"id": "ht1", "text": "reworded text", "changed": true}},
    ...
  ],
  "rationale": "one sentence on what you emphasised and why"
}}

Include every source bullet id. Set "changed": false and return the text
unmodified where rewording adds nothing."""


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


def call_gemini(prompt: str, api_key: str | None = None,
                timeout: float = 120.0) -> tuple[str, str]:
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
                        "temperature": 0.3,   # editing, not writing
                        "maxOutputTokens": 8192,
                        "responseMimeType": "application/json",
                        "responseSchema": RESPONSE_SCHEMA,
                    },
                },
                timeout=timeout,
            )
            if r.status_code in (404, 429, 503):
                # Model gone or rate-limited — try the next one rather
                # than dropping the whole tailoring for this job.
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


def tailor(job, resume: dict, profile: dict,
           api_key: str | None = None) -> TailorResult:
    """Tailor for one job. Never raises; on any failure returns ok=False
    and the caller falls back to the untailored resume."""
    source = _all_bullets(resume)

    raw, err = call_gemini(build_prompt(job, resume, profile), api_key)
    if err:
        return TailorResult(ok=False, error=err)

    parsed = _extract_json(raw)
    if not parsed:
        return TailorResult(ok=False, error="model returned unparseable JSON")

    headline = str(parsed.get("headline", "")).strip()
    summary = " ".join(str(parsed.get("summary", "")).split())
    bullets = parsed.get("bullets") or []
    order = parsed.get("skill_order") or []

    if not headline or not summary or not bullets:
        return TailorResult(ok=False, error="model output missing fields")

    # Everything the model produced, checked as one blob.
    blob = " ".join([headline, summary] + [str(b.get("text", "")) for b in bullets])

    verdict = validate(
        blob, profile,
        source_ids=set(source),
        tailored_bullets=bullets,
    )

    if not verdict.ok:
        return TailorResult(ok=False, verdict=verdict,
                            error="failed validation", used_fallback=True)

    # Guard the summary length — an over-long summary pushes a fresher
    # resume onto a second page, which reads badly.
    limit = resume["summary"].get("max_words", 90)
    if len(summary.split()) > limit * 1.3:
        summary = " ".join(summary.split()[:limit])

    return TailorResult(
        ok=True, headline=headline, summary=summary,
        skill_order=[str(s) for s in order], bullets=bullets,
        rationale=str(parsed.get("rationale", "")).strip(),
        verdict=verdict,
    )


def diff_lines(resume: dict, result: TailorResult) -> list[str]:
    """Human-readable before/after for the Telegram approval card."""
    source = _all_bullets(resume)
    out: list[str] = []

    old_head = resume["headline"]["default"]
    if result.headline and result.headline != old_head:
        out.append(f"HEADLINE\n  - {old_head}\n  + {result.headline}")

    for b in result.bullets:
        if not b.get("changed"):
            continue
        old = source.get(b.get("id", ""), "")
        new = " ".join(str(b.get("text", "")).split())
        if old and new != old:
            out.append(f"{b['id']}\n  - {old}\n  + {new}")
    return out
