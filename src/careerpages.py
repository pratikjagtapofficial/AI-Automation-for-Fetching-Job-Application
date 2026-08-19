"""Company career pages / ATS aggregator via Apify.

The static verified board list in targets.yml only covers 28 hand-picked
companies, mostly US ones (measured: 4,352 raw postings -> 1-3 relevant
per scan). It structurally cannot discover a new employer's career page
on its own.

`fantastic-jobs/career-site-job-listing-api` aggregates real postings
from 175k+ company career sites across 54 ATS platforms -- Workday,
SuccessFactors, Zoho Recruit, Oracle Cloud, JazzHR and more that the
static list never reaches. It exposes an AI-derived `ai_experience_level`
band computed from the actual JD, so the server does most of the
JD-verification work the scorer would otherwise have to do blind.

Cost is controlled by filtering server-side rather than paying per raw
result: aiExperienceLevelFilter=["0-2"] cuts a ~$1/search bill down to
single-digit cents, since only genuinely entry-level postings are
returned at all. Verified: 79 unfiltered results for one keyword vs 5
with the experience filter applied, for the same search.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from . import apify_status
from .sources import Job

RUN_SYNC ="https://api.apify.com/v2/acts/fantastic-jobs~career-site-job-listing-api/run-sync-get-dataset-items"


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _to_job(item: dict) -> Job | None:
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    company = str(item.get("organization") or "").strip()
    if not title or not url or not company:
        return None

    locs = item.get("locations_derived") or []
    location = ", ".join(str(l) for l in locs if l) if locs else ""
    if item.get("ai_work_arrangement") in ("Remote OK", "Remote Solely") and \
       "remote" not in location.lower():
        location = f"{location}, Remote" if location else "Remote"
    if not location:
        return None

    posted = _parse_date(item.get("date_posted") or item.get("date_created"))
    if posted is None:
        return None  # unverifiable freshness -> drop, never guessed

    desc = str(item.get("description_text") or "")
    exp = str(item.get("ai_experience_level") or "")
    blob = f"{exp} years experience. {desc}" if exp else desc

    return Job(
        source="careerpages",
        company=company,
        title=title,
        location=location,
        url=url,
        posted_at=posted,
        job_id=str(item.get("id") or ""),
        description=blob[:20000],
    )


def fetch(titles: list[str], token: str | None = None,
         time_range: str = "7d") -> tuple[list[Job], list[str]]:
    """-> (jobs, errors). Never raises.

    Experience and agency filtering happen server-side so the response
    (and the bill) is already close to what the scorer would keep -- this
    is the one source where the vendor's own AI does first-pass JD
    verification, rather than the scorer working from raw text alone.
    """
    import os
    token = token or os.environ.get("APIFY_TOKEN", "")
    if not token:
        return [], ["careerpages: APIFY_TOKEN not set — source skipped"]

    try:
        with httpx.Client(timeout=60.0) as client:
            r = client.post(
                RUN_SYNC,
                params={"token": token},
                json={
                    "titleSearch": titles,
                    "locationSearch": ["India"],
                    "aiExperienceLevelFilter": ["0-2"],
                    "timeRange": time_range,
                    "removeAgency": True,
                    "limit": 100,
                    "descriptionType": "text",
                },
            )
            if r.status_code >= 400:
                return [], [apify_status.explain(r, "careerpages")]
            items = r.json()
            if not isinstance(items, list):
                return [], ["careerpages: unexpected payload"]
    except Exception as e:
        return [], [f"careerpages: {type(e).__name__}"]

    jobs = [j for it in items if (j := _to_job(it))]
    unique: dict[str, Job] = {}
    for j in jobs:
        unique.setdefault(j.job_id or j.url, j)
    return list(unique.values()), []
