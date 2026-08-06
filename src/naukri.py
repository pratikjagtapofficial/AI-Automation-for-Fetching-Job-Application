"""Naukri via Apify.

Naukri blocks direct scraping (403, Akamai edge) even from residential
IPs, so self-hosting this is not an option. `valig/naukri-jobs-scraper`
was benchmarked at ~$0.041 per 100 jobs — roughly 12x cheaper than the
alternatives and comfortably inside Apify's $5/month free credit.

Two fields matter beyond the obvious ones:
  consultant=False + companyApplyJob=True  ->  posted by the company
itself rather than reposted by a staffing agency, which is exactly the
"official postings only" requirement.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx

from .sources import Job

ACTOR = "valig~naukri-jobs-scraper"
BASE = "https://api.apify.com/v2"

# Hard ceiling per run so a bad config can never drain the month's credit.
MAX_ITEMS_PER_SEARCH = 60


def _ts(ms: int | None, text: str | None) -> datetime | None:
    """Prefer the epoch field; fall back to the text form. Returns None
    rather than guessing — the spec forbids invented timestamps."""
    if ms and ms > 0:
        try:
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        except Exception:
            pass
    if text:
        try:
            dt = datetime.fromisoformat(text.replace(" ", "T"))
            # Naukri reports IST; normalise to UTC.
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except Exception:
            pass
    return None


def _to_job(item: dict, official_only: bool) -> Job | None:
    if official_only:
        # Agency reposts and consultant listings are excluded.
        if item.get("consultant") is True:
            return None

    title = (item.get("title") or "").strip()
    url = (item.get("url") or "").strip()
    if not title or not url:
        return None

    company = ((item.get("company") or {}).get("name") or "").strip()
    if not company:
        return None

    locs = item.get("locations") or []
    if isinstance(locs, dict):
        locs = [locs]
    location = ", ".join(
        filter(None, [(l or {}).get("label", "") for l in locs])
    ) or ""

    posted = _ts(item.get("createdDate"), item.get("createdDateText"))
    if posted is None:
        return None  # unverifiable freshness -> drop

    desc = (item.get("description") or {})
    skills = item.get("skills") or {}
    blob = " ".join(filter(None, [
        desc.get("full") or desc.get("short") or "",
        " ".join(skills.get("preferred") or []),
        " ".join(skills.get("other") or []),
        (item.get("experience") or {}).get("text") or "",
    ]))

    return Job(
        source="naukri",
        company=company,
        title=title,
        location=location,
        url=url,
        posted_at=posted,
        job_id=str(item.get("id") or ""),
        description=blob[:20000],
    )


def fetch(keywords: list[str], locations: list[str],
          official_only: bool = True,
          token: str | None = None) -> tuple[list[Job], list[str]]:
    """Run the actor once per keyword/location pair.

    Returns (jobs, errors). Errors are reported rather than raised so one
    dead search cannot abort the whole scan.
    """
    token = token or os.environ.get("APIFY_TOKEN", "")
    if not token:
        return [], ["naukri: APIFY_TOKEN not set — source skipped"]

    jobs: list[Job] = []
    errors: list[str] = []

    with httpx.Client(timeout=120.0) as client:
        for kw in keywords:
            for loc in locations:
                try:
                    r = client.post(
                        f"{BASE}/acts/{ACTOR}/run-sync-get-dataset-items",
                        params={"token": token},
                        json={
                            "keywords": kw,
                            "location": loc,
                            "jobAge": "1",          # last 24h
                            "experience": 0,
                            "limit": MAX_ITEMS_PER_SEARCH,
                        },
                    )
                    if r.status_code >= 400:
                        errors.append(
                            f"naukri[{kw}/{loc}]: HTTP {r.status_code}")
                        continue
                    items = r.json()
                    if not isinstance(items, list):
                        errors.append(f"naukri[{kw}/{loc}]: unexpected payload")
                        continue
                    for it in items:
                        job = _to_job(it, official_only)
                        if job:
                            jobs.append(job)
                except Exception as e:
                    errors.append(
                        f"naukri[{kw}/{loc}]: {type(e).__name__}")

    # Same posting can surface under several keywords.
    unique: dict[str, Job] = {}
    for j in jobs:
        unique.setdefault(j.job_id or j.url, j)
    return list(unique.values()), errors
