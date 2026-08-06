"""Instahyre — free JSON API, no browser, no credits.

Startup-heavy Indian board with good AI/ML density and less competition
than Naukri.

⚠️ NO POSTING DATE. The API exposes none — not in search, not in the
detail endpoint, not on the employer object. The pipeline's rule is never
to invent a timestamp, so `posted_at` stays None and the scorer applies a
per-source exemption instead of silently assuming a date. Telegram cards
show "date unknown", and the tracker's first_seen is the only temporal
claim ever made about these rows.
"""

from __future__ import annotations

import time

import httpx

from .sources import Job

API = "https://www.instahyre.com/api/v1/job_search"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Referer": "https://www.instahyre.com/search-jobs/",
}

# Instahyre job_function ids. 9 is Data Science / Machine Learning, which
# is where the AI roles live; broader functions add mostly noise that the
# title filter would discard anyway.
JOB_FUNCTIONS = [9]

PAGE_SIZE = 35
MAX_PAGES = 4


def _to_job(item: dict) -> Job | None:
    title = str(item.get("title") or item.get("candidate_title") or "").strip()
    url = str(item.get("public_url") or "").strip()
    if not title or not url:
        return None

    employer = item.get("employer") or {}
    company = str(employer.get("company_name") or "").strip()
    if not company:
        return None

    location = str(item.get("locations") or "").strip()
    if not location:
        return None

    keywords = item.get("keywords") or []
    blob = " ".join([
        " ".join(str(k) for k in keywords),
        str(employer.get("company_tagline") or ""),
    ]).strip()

    return Job(
        source="instahyre",
        company=company,
        title=title,
        location=location,
        url=url,
        # Deliberately None — see module docstring. Never guessed.
        posted_at=None,
        job_id=str(item.get("id") or ""),
        description=blob[:20000],
    )


def fetch(max_pages: int = MAX_PAGES) -> tuple[list[Job], list[str]]:
    """-> (jobs, errors). Never raises."""
    jobs: list[Job] = []
    errors: list[str] = []

    with httpx.Client(headers=HEADERS, timeout=30.0) as client:
        for fn in JOB_FUNCTIONS:
            for page in range(max_pages):
                try:
                    r = client.get(API, params={
                        "job_functions": fn,
                        "offset": page * PAGE_SIZE,
                    })
                    if r.status_code != 200:
                        errors.append(
                            f"instahyre[fn={fn}]: HTTP {r.status_code}")
                        break
                    objs = (r.json() or {}).get("objects") or []
                    if not objs:
                        break
                    for o in objs:
                        job = _to_job(o)
                        if job:
                            jobs.append(job)
                    if len(objs) < PAGE_SIZE:
                        break
                except Exception as e:
                    errors.append(f"instahyre[fn={fn}]: {type(e).__name__}")
                    break
                time.sleep(0.5)

    unique: dict[str, Job] = {}
    for j in jobs:
        unique.setdefault(j.job_id or j.url, j)
    return list(unique.values()), errors
