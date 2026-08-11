"""Hard gate on tailored resume content.

Deliberately NOT an LLM. The whole point is to be the thing that cannot
be talked around: an LLM asked "don't add skills he lacks" will comply
most of the time, and the failure mode is PJ walking into an interview
holding a resume claiming Kubernetes experience he does not have.

So this is dumb, deterministic string matching, and it runs AFTER the
tailor. If it rejects, the tailored output is discarded and the original
is used.

Checks:
  1. No skill from profile.skills.lacks may appear anywhere.
  2. No claim of years of experience beyond PJ's real total.
  3. Every factual field marked `locked` must be byte-identical.
  4. Bullets may be reworded but not invented — each output bullet must
     trace back to a source bullet id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Verdict:
    ok: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def _word_re(term: str) -> re.Pattern:
    """Match a skill as a whole token.

    Substring matching would fire on 'GCP' inside unrelated text and miss
    'Docker.' with punctuation; both directions matter here.
    """
    escaped = re.escape(term).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.I)


# "5+ years", "3 yrs of experience", "over 4 years"
_YEARS_RE = re.compile(
    r"(?:over|more than|\b)(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", re.I
)


def check_forbidden_skills(text: str, lacks: list[str]) -> list[str]:
    hits = []
    for skill in lacks:
        if _word_re(skill).search(text):
            hits.append(f"claims '{skill}' — PJ does not have it")
    return hits


def check_experience_claims(text: str, real_months: int) -> list[str]:
    real_years = real_months / 12
    out = []
    for m in _YEARS_RE.finditer(text):
        claimed = int(m.group(1))
        # Allow figures that plausibly refer to something other than PJ's
        # tenure (a date range, a tool version) only when small.
        if claimed > max(1, round(real_years + 0.5)):
            out.append(
                f"claims '{m.group(0).strip()}' but PJ has "
                f"~{real_years:.1f} years"
            )
    return out


def check_locked_fields(original: dict, tailored: dict) -> list[str]:
    """Recursively verify anything under a `locked` flag is unchanged."""
    out: list[str] = []

    def walk(o, t, path="") -> None:
        if isinstance(o, dict):
            locked = o.get("locked") is True
            lf = set(o.get("locked_fields") or [])
            for k, v in o.items():
                if k in ("locked", "locked_fields"):
                    continue
                tv = (t or {}).get(k) if isinstance(t, dict) else None
                if (locked or k in lf) and not isinstance(v, (dict, list)):
                    if tv != v:
                        out.append(
                            f"locked field changed at {path}.{k}: "
                            f"{v!r} -> {tv!r}"
                        )
                else:
                    walk(v, tv, f"{path}.{k}")
        elif isinstance(o, list):
            for i, item in enumerate(o):
                tv = t[i] if isinstance(t, list) and i < len(t) else None
                walk(item, tv, f"{path}[{i}]")

    walk(original, tailored)
    return out


def check_bullet_provenance(source_ids: set[str],
                            tailored_bullets: list[dict]) -> list[str]:
    out = []
    for b in tailored_bullets:
        bid = b.get("id")
        if not bid:
            out.append(f"bullet with no id: {str(b.get('text',''))[:60]!r}")
        elif bid not in source_ids:
            out.append(f"invented bullet id '{bid}' — no source entry")
    return out


# Cliche phrases and formatting tells that read as AI-generated rather
# than human-written -- flagged here as a WARNING (not a hard reject like
# the checks above) because this is a style/quality issue, not a
# truthfulness one. The tailor prompt already forbids these (rules 7-8 in
# build_prompt); this is defense in depth for whatever slips through.
_AI_TELL_PHRASES = (
    "results-driven", "strategic thinker", "proven track record",
    "team player", "responsible for", "worked on", "assisted with",
)


def check_ai_tells(text: str) -> list[str]:
    out = []
    low = text.lower()
    for phrase in _AI_TELL_PHRASES:
        if phrase in low:
            out.append(f"AI-cliche phrase in output: '{phrase}'")
    if "—" in text:
        out.append("em dash (—) in output — reads as AI-generated formatting")
    return out


def validate(tailored_text: str, profile: dict,
             original: dict | None = None,
             tailored: dict | None = None,
             source_ids: set[str] | None = None,
             tailored_bullets: list[dict] | None = None) -> Verdict:
    """Run every check. Any violation fails the whole tailoring."""
    violations: list[str] = []
    warnings: list[str] = []

    lacks = profile.get("skills", {}).get("lacks", []) or []
    violations += check_forbidden_skills(tailored_text, lacks)

    months = (profile.get("experience", {}) or {}).get("total_months", 0)
    if months:
        violations += check_experience_claims(tailored_text, months)

    if original is not None and tailored is not None:
        violations += check_locked_fields(original, tailored)

    if source_ids is not None and tailored_bullets is not None:
        violations += check_bullet_provenance(source_ids, tailored_bullets)

    # Shallow skills are allowed but flagged, so the call-prep card can
    # warn PJ not to oversell them in an interview.
    for skill in (profile.get("skills", {}).get("shallow") or {}):
        if _word_re(skill).search(tailored_text):
            warnings.append(
                f"'{skill}' is on the resume but PJ has only light "
                f"experience — be ready to qualify it"
            )

    warnings += check_ai_tells(tailored_text)

    return Verdict(ok=not violations, violations=violations, warnings=warnings)
