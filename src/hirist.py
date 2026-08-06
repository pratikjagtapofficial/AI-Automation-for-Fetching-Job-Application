"""Hirist (hirist.tech) — free JSON API, no browser, no credits.

Hirist is the tech-focused board from the iimjobs family, which makes it
a far better fit for PJ than general boards: its listings are almost
entirely engineering roles, so the noise ratio is much lower than Naukri
("MOLDING OPERATOR III" surfaced in a Naukri AI-engineer search).

The search endpoint backing the site is public and unauthenticated. It
returns `min`/`max` experience as integers, so entry-level filtering is
exact rather than regex-guessed from prose, and `createdTime` as epoch
millis, so freshness never needs inferring.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from .sources import Job

API = "https://gladiator.hirist.tech/job/search"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Referer": "https://www.hirist.tech/",
}
PAGE_SIZE = 40
MAX_PAGES = 3

# Domains that are recruitment firms rather than employers. Hirist also
# exposes recruiter.designation, which catches most of them.
_AGENCY_HINTS = ("consult", "staffing", "recruit", "talent", "hr", "manpower",
                 "hiring", "placement", "career", "jobs")


def _company(item: dict) -> str:
    """Derive an employer name.

    `company`/`companyName` are null on the search endpoint, so fall back
    to the domain, and prefer a "Company - Role" title prefix when the
    poster used one since that is the employer's own wording.
    """
    for key in ("companyName", "company"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    title = str(item.get("title") or "")
    if " - " in title:
        head = title.split(" - ", 1)[0].strip()
        # Guard against titles that merely contain a dash, e.g.
        # "AI Engineer - LLM", where the head is the role not the company.
        if head and not any(
            w in head.lower()
            for w in ("engineer", "developer", "scientist", "analyst",
                      "intern", "lead", "manager", "architect")
        ):
            return head

    domain = str(item.get("creatorDomainName") or "").strip()
    if domain:
        name = domain.split(".")[0].replace("-", " ").replace("_", " ")
        return name.title()
    return ""


def _is_agency(item: dict) -> bool:
    rec = item.get("recruiter") or {}
    designation = str(rec.get("designation") or "").lower()
    if "consultant" in designation or "recruit" in designation:
        return True
    domain = str(item.get("creatorDomainName") or "").lower()
    return any(h in domain for h in _AGENCY_HINTS)


def _to_job(item: dict, official_only: bool) -> Job | None:
    if official_only and _is_agency(item):
        return None
    if item.get("confidential"):
        # Employer withheld — cannot be recorded honestly or applied to
        # with a tailored resume, so drop rather than guess.
        return None

    title = str(item.get("title") or "").strip()
    url = str(item.get("jobDetailUrl") or "").strip()
    if not title or not url:
        return None

    company = _company(item)
    if not company:
        return None

    ms = item.get("createdTimeMs") or item.get("createdTime")
    if not ms:
        return None
    try:
        posted = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except Exception:
        return None

    locs = item.get("locations") or item.get("location") or []
    if isinstance(locs, dict):
        locs = [locs]
    names = [str((l or {}).get("name", "")).strip() for l in locs]
    location = ", ".join(n for n in names if n)
    if item.get("workFromHome"):
        location = f"{location}, Remote" if location else "Remote"
    if not location:
        return None

    tags = [str((t or {}).get("name", "")) for t in (item.get("tags") or [])]
    mn, mx = item.get("min"), item.get("max")
    # The scorer reads years from the description; state the range in the
    # form it parses so exact API data drives the filter, not prose.
    exp = f"{mn}-{mx} years experience" if mn is not None else ""

    return Job(
        source="hirist",
        company=company,
        title=title,
        location=location,
        url=url,
        posted_at=posted,
        job_id=str(item.get("id") or ""),
        description=" ".join([exp, " ".join(tags)]).strip()[:20000],
    )


def fetch(keywords: list[str], official_only: bool = True,
          max_pages: int = MAX_PAGES) -> tuple[list[Job], list[str]]:
    """-> (jobs, errors). Never raises; a dead query is reported, not fatal."""
    jobs: list[Job] = []
    errors: list[str] = []

    with httpx.Client(headers=HEADERS, timeout=30.0) as client:
        for kw in keywords:
            for page in range(max_pages):
                try:
                    r = client.get(API, params={
                        "query": kw,
                        "page": page,
                        "posting": 0,
                        "industry": "",
                        "size": PAGE_SIZE,
                    })
                    if r.status_code != 200:
                        errors.append(f"hirist[{kw}]: HTTP {r.status_code}")
                        break
                    items = (r.json() or {}).get("data") or []
                    if not items:
                        break
                    for it in items:
                        job = _to_job(it, official_only)
                        if job:
                            jobs.append(job)
                    if len(items) < PAGE_SIZE:
                        break
                except Exception as e:
                    errors.append(f"hirist[{kw}]: {type(e).__name__}")
                    break
                time.sleep(0.5)   # courtesy pacing on a free public endpoint

    unique: dict[str, Job] = {}
    for j in jobs:
        unique.setdefault(j.job_id or j.url, j)
    return list(unique.values()), errors
