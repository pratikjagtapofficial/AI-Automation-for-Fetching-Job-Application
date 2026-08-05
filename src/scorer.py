"""Filter and score postings against PJ's profile.

Deliberately deterministic — no LLM. Scoring runs over thousands of rows
twice a day, and rules here are cheap, auditable, and free. The LLM is
reserved for resume tailoring in Phase 2, where judgement actually helps.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .sources import Job

# Indian locations plus remote; anything else is out of scope unless the
# posting is explicitly remote.
_INDIA_HINTS = (
    "india", "pune", "bengaluru", "bangalore", "hyderabad", "noida",
    "gurugram", "gurgaon", "mumbai", "delhi", "chennai", "kolkata",
    "ahmedabad", "maharashtra", "karnataka", "telangana",
)
_REMOTE_HINTS = ("remote", "anywhere", "work from home", "wfh", "distributed",
                 "worldwide", "global")

# Signals that a role is above entry level even when the title looks clean.
_SENIOR_PATTERNS = (
    r"\b(senior|sr\.?|staff|principal|lead|head|director|manager|architect)\b",
    r"\b(vp|vice president|chief|founding)\b",
    r"\b(ii|iii|iv|2|3)\b\s*$",
)
_EXP_PATTERN = re.compile(
    r"(\d+)\s*(?:\+|plus)?\s*(?:-|to|–)?\s*(\d+)?\s*\+?\s*(?:years?|yrs?)",
    re.I,
)


_REMOTE_RE = re.compile(
    r"\b(" + "|".join(re.escape(h) for h in _REMOTE_HINTS) + r")\b", re.I
)

# What may remain after stripping remote-words from a location string for
# it to still count as globally open. Anything else means a specific
# region was named, and the posting is not available from India.
_GLOBAL_OK = {"", "global", "worldwide", "any", "all", "n a", "flexible",
              "global remote", "any location", "no location"}


def _is_remote(text: str) -> bool:
    return any(h in text for h in _REMOTE_HINTS)


def _in_scope_location(loc: str) -> tuple[bool, bool]:
    """-> (in_scope, is_remote)

    In scope means: mentions an Indian location, OR is remote *without*
    naming a foreign region. A blank location is out of scope — the spec
    says drop rather than guess.
    """
    low = loc.lower().strip()
    if not low:
        return False, False

    remote = _is_remote(low)

    # India named anywhere -> in scope regardless of what else appears.
    if any(h in low for h in _INDIA_HINTS):
        return True, remote

    if not remote:
        return False, False

    # Remote, but no Indian location named. Accept ONLY if the string
    # carries no other geographic qualifier at all — "Remote", "Remote -
    # Global", "Anywhere". Enumerating foreign places instead would be an
    # endless blocklist that leaks on every city we forgot (Fukuoka,
    # Belarus, ...), so we allowlist the shape of a truly-global posting.
    residue = _REMOTE_RE.sub(" ", low)
    residue = re.sub(r"[^a-z]+", " ", residue).strip()
    return (residue in _GLOBAL_OK), True


def _title_matches(title: str, cfg: dict) -> bool:
    low = title.lower()
    wanted = [t.lower() for t in cfg["search"]["titles"]]
    return any(w in low for w in wanted)


def _title_too_senior(title: str) -> bool:
    low = title.lower()
    return any(re.search(p, low) for p in _SENIOR_PATTERNS)


def _min_years_required(text: str) -> int | None:
    """Lowest experience figure mentioned, or None if unstated."""
    if not text:
        return None
    hits = _EXP_PATTERN.findall(text[:6000])
    lows = [int(a) for a, _ in hits if a.isdigit() and int(a) <= 20]
    return min(lows) if lows else None


def _blacklisted(company: str, text: str, cfg: dict) -> str | None:
    bl = cfg["blacklist"]
    comp = company.lower()
    for c in (bl.get("companies") or []) + (bl.get("client_companies") or []):
        if c.lower() in comp:
            return f"blacklisted company: {c}"
    for kw in bl.get("keywords") or []:
        if kw.lower() in comp:
            return f"staffing/recruitment keyword: {kw}"
    return None


def score_job(job: Job, cfg: dict, now: datetime | None = None) -> Job:
    """Attach score + reasons. Score of 0 means rejected."""
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []

    # ── hard rejects ──────────────────────────────────────────────
    if not _title_matches(job.title, cfg):
        job.score, job.reasons = 0, ["title not in target list"]
        return job

    if _title_too_senior(job.title):
        job.score, job.reasons = 0, ["title signals above entry level"]
        return job

    if (reason := _blacklisted(job.company, job.description, cfg)):
        job.score, job.reasons = 0, [reason]
        return job

    in_scope, remote = _in_scope_location(job.location)
    if not in_scope:
        job.score, job.reasons = 0, [f"location out of scope: {job.location}"]
        return job

    # Freshness. A missing timestamp is a reject, never a guess.
    if job.posted_at is None:
        job.score, job.reasons = 0, ["no verifiable posting timestamp"]
        return job
    age = now - job.posted_at
    if age > timedelta(hours=cfg["search"]["freshness_hours"]):
        job.score, job.reasons = 0, [f"older than cutoff ({age.days}d)"]
        return job

    min_yrs = _min_years_required(job.description)
    if min_yrs is not None and min_yrs > cfg["search"]["experience_years_max"]:
        job.score, job.reasons = 0, [f"requires {min_yrs}+ yrs"]
        return job

    # ── scoring ───────────────────────────────────────────────────
    score = 50.0

    low_title = job.title.lower()
    if any(k in low_title for k in ("genai", "generative", "agentic", "llm")):
        score += 20
        reasons.append("title matches GenAI/agentic focus")
    elif "ai engineer" in low_title or "ai developer" in low_title:
        score += 15
        reasons.append("core AI title match")

    if any(k in low_title for k in ("associate", "junior", "graduate", "intern")):
        score += 10
        reasons.append("explicitly entry level")

    # Location weighting from profile.
    weight = 0.9
    low_loc = job.location.lower()
    for entry in cfg["search"]["locations"]:
        name = entry["name"].lower()
        if name == "remote" and remote:
            weight = max(weight, entry["weight"])
            reasons.append("remote — top preference")
        elif name != "remote" and name in low_loc:
            weight = max(weight, entry["weight"])
            reasons.append(f"preferred location: {entry['name']}")
    score *= weight

    # Skill overlap against what PJ actually has.
    have = [s.lower() for group in cfg["skills"]["has"].values() for s in group]
    blob = f"{job.title} {job.description}".lower()
    hits = sorted({s for s in have if s in blob})
    if hits:
        score += min(len(hits) * 4, 20)
        reasons.append(f"skill overlap: {', '.join(hits[:5])}")

    # Gaps are recorded honestly, never hidden — and cost only a little,
    # since posted requirements are usually wishlists.
    gaps = sorted({s for s in cfg["skills"]["lacks"] if s.lower() in blob})
    if gaps:
        score -= min(len(gaps) * 3, 12)
        reasons.append(f"⚠ JD wants: {', '.join(gaps)}")

    if min_yrs is not None:
        reasons.append(f"asks {min_yrs}+ yrs")

    # Fresher postings rank higher.
    if age < timedelta(hours=6):
        score += 5
        reasons.append("posted in last 6h")

    job.score = max(0, min(100, round(score)))
    job.reasons = reasons
    return job


def score_all(jobs: list[Job], cfg: dict) -> list[Job]:
    now = datetime.now(timezone.utc)
    scored = [score_job(j, cfg, now) for j in jobs]
    keep = [j for j in scored if j.score >= cfg["limits"]["min_score_to_show"]]
    return sorted(keep, key=lambda j: (-j.score, j.posted_at or datetime.min))
