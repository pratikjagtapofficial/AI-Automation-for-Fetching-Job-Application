"""Instahyre — free JSON API, no browser, no credits.

Startup-heavy Indian board with good AI/ML density and less competition
than Naukri.

⚠️ NO POSTING DATE. The API exposes none — not in search, not in the
detail endpoint, not on the employer object. The pipeline's rule is never
to invent a timestamp, so `posted_at` stays None and the scorer applies a
per-source exemption instead of silently assuming a date. Telegram cards
show "date unknown", and the tracker's first_seen is the only temporal
claim ever made about these rows.

⚠️ NO JD TEXT EITHER. The search and detail API endpoints return only a
keyword list and a company tagline — never a job description body, and
never an experience-years figure. Measured directly: 0 of 60 sampled
descriptions carried any years figure. Until `enrich()` is called, the
experience filter is a silent no-op for this source — a listing needing
8 years and a listing needing 0 look identical to the scorer. This is
exactly the "trusted the tag, not the JD" failure class the rest of the
pipeline is built to avoid, so nothing may be scored on Instahyre
description text alone; `enrich()` must run first.
"""

from __future__ import annotations

import re
import time

import httpx

from .sources import Job

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

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


async def _detail_text(job: Job) -> str | None:
    """Fetch the public job page and pull the experience band + JD body.

    No login required — verified directly. The page shows the band as
    plain text next to the location, e.g. "Chennai5-9 Years", so it is
    prefixed onto the body in a form the shared _min_years_required
    regex can parse, matching how hirist.py surfaces its own min/max.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(user_agent=UA, locale="en-IN")
        page = await ctx.new_page()
        try:
            await page.goto(job.url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(1500)
            body = await page.inner_text("body")
        except Exception:
            body = None
        finally:
            await browser.close()
    if not body:
        return None

    m = re.search(r"([A-Za-z ,]+?)(\d+\s*-\s*\d+|\d+\+)\s*Years?", body)
    band = f"{m.group(2)} years experience. " if m else ""
    return band + body[:6000]


async def enrich(jobs: list[Job], max_enrich: int = 20) -> tuple[list[Job], list[str]]:
    """Replace the thin keyword blob with real JD text for a shortlist.

    Call this AFTER cheap filtering (title/location/blacklist) and BEFORE
    scoring — enriching all ~140 jobs from a single search would mean
    ~140 browser launches every run. Only the survivors of the free
    checks are worth the cost; `jobs` should already be that shortlist.
    """
    errors: list[str] = []
    for j in jobs[:max_enrich]:
        try:
            text = await _detail_text(j)
            if text:
                j.description = text
            else:
                errors.append(f"instahyre-detail[{j.company}]: empty page")
        except Exception as e:
            errors.append(f"instahyre-detail[{j.company}]: {type(e).__name__}")
    if len(jobs) > max_enrich:
        errors.append(
            f"instahyre-detail: {len(jobs) - max_enrich} shortlisted job(s) "
            f"skipped (max_enrich={max_enrich}) — their experience band is "
            f"unverified, so they should be treated as unscreened, not clean")
    return jobs, errors
