"""Foundit (foundit.in, formerly Monster India) — free JSON API.

Added 2026-08-19 as a free stand-in for Naukri, after Apify's $5/month
credit was exhausted and took both Naukri and careerpages offline for
three days with no India-side coverage left.

Why this one, out of everything tested that day:
  * Remotive / Arbeitnow / RemoteOK / Himalayas / Jobicy are all free and
    keyless, but USA/Europe-dominated -- the location filter correctly
    rejects essentially all of it, so they add runtime and no matches.
  * Widening the ATS board list does not help either: nine large
    companies with live public boards produced 1,614 postings and ZERO
    that passed scoring, because their AI roles are not in India. Only
    1 of 46 Indian companies tried even has a public Greenhouse/Lever/
    Ashby board -- Indian hiring simply does not live on those.
  * iimjobs shares Hirist's `gladiator` API and returns cleanly, but is
    management/MBA-focused: 80 postings, 0 relevant.
  * Foundit returns 239 unique postings across PJ's four keywords, 14 of
    them genuinely entry-level in India (Qualcomm, Nvidia, Okta,
    Experian, Amazon), and exposes `minimumExperience`/`maximumExperience`
    as structured integers plus `createdAt` as epoch millis -- the same
    two things that make Hirist reliable, so experience and freshness are
    exact rather than regex-guessed from prose.

Access is a plain public JSON endpoint: `Accept: application/json` is the
only header that matters. NOTHING here spoofs credentials or evades a
bot check. Naukri's own equivalent endpoint was deliberately NOT used --
it demands an appId/systemId pair, and supplying its frontend's values to
get around that gate is exactly the kind of anti-bot evasion this project
refuses to do (same reason Instahyre's block on Actions IPs is left
alone rather than proxied around).
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .sources import Job

API = "https://www.foundit.in/middleware/jobsearch"
SITE = "https://www.foundit.in"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Referer": "https://www.foundit.in/",
}
PAGE_SIZE = 15
MAX_PAGES = 4

# Mirrors hirist.py's agency screen. Foundit also exposes employerTypes,
# which flags consultancies directly, so this only has to catch the ones
# that self-report as "Company" while being staffing firms by name.
_AGENCY_HINTS = ("consult", "staffing", "recruit", "talent", "manpower",
                 "hiring", "placement", "hr services", "hr solutions",
                 "outsourcing", "resourcing")


def _years(v) -> int | None:
    """minimumExperience/maximumExperience arrive as {"years": N}, not as
    a bare int -- a plain `v <= 2` comparison silently matches nothing."""
    if isinstance(v, dict):
        y = v.get("years")
        if isinstance(y, (int, float)):
            return int(y)
    if isinstance(v, (int, float)):
        return int(v)
    return None


def _to_job(item: dict, official_only: bool) -> Job | None:
    title = str(item.get("title") or "").strip()
    if not title:
        return None            # the feed carries null placeholder rows

    company = str(item.get("companyName") or "").strip()
    if not company:
        return None

    if official_only:
        types = [str(t).lower() for t in (item.get("employerTypes") or [])]
        if any("consult" in t or "recruit" in t or "staffing" in t
               for t in types):
            return None
        low = company.lower()
        if any(h in low for h in _AGENCY_HINTS):
            return None

    location = str(item.get("locations") or "").strip()
    if not location:
        return None

    path = str(item.get("seoJdUrl") or item.get("jdUrl") or "").strip()
    if not path:
        return None
    url = path if path.startswith("http") else f"{SITE}{path}"

    ms = item.get("createdAt")
    posted = None
    if isinstance(ms, (int, float)) and ms > 0:
        try:
            posted = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        except Exception:
            posted = None
    if posted is None:
        return None            # never guess a timestamp

    # The band is prepended in the exact shape scorer._min_years_required
    # parses, and FIRST, because that function deliberately takes the
    # first years-figure it sees. Falling back to the `exp` string keeps
    # a stated band ("2-4 Years") authoritative; when nothing is stated,
    # no figure is written at all rather than a fabricated "0 years",
    # which would let an unscreened senior role through as entry-level.
    mn, mx = _years(item.get("minimumExperience")), _years(item.get("maximumExperience"))
    exp_text = str(item.get("exp") or "").strip()
    band = ""
    if exp_text:
        band = f"{exp_text} experience. "
    elif mn is not None and mx is not None and (mn or mx):
        band = f"{mn}-{mx} years experience. "

    skills = item.get("skills")
    skills = skills if isinstance(skills, str) else ""
    designations = " ".join(str(d) for d in (item.get("designations") or []))

    # The TITLE goes into the blob, after the band, specifically so the
    # experience check can see requirements that only ever appear there.
    # Foundit leaves `exp` empty on a large share of listings, and some of
    # those state the requirement in the title instead -- confirmed live:
    # "Machine Learning Engineer_6+yrs" and "AI Engineer (2+ years of
    # Experience)" both scored as entry-level because nothing downstream
    # read the title for a years-figure. Order matters: a stated band
    # stays authoritative because _min_years_required takes the FIRST
    # match, and the title is only consulted when no band was stated.
    blob = f"{band}{title} {skills} {designations}".strip()

    return Job(
        source="foundit",
        company=company,
        title=title,
        location=location,
        url=url,
        posted_at=posted,
        job_id=str(item.get("jobId") or item.get("id") or ""),
        description=blob[:20000],
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
                        "locations": "india",
                        "start": page * PAGE_SIZE,
                        "limit": PAGE_SIZE,
                    })
                    if r.status_code != 200:
                        errors.append(f"foundit[{kw}]: HTTP {r.status_code}")
                        break
                    payload = (r.json() or {}).get("jobSearchResponse") or {}
                    items = payload.get("data") or []
                    if not items:
                        break
                    for it in items:
                        job = _to_job(it, official_only)
                        if job:
                            jobs.append(job)
                    if len(items) < PAGE_SIZE:
                        break
                except Exception as e:
                    errors.append(f"foundit[{kw}]: {type(e).__name__}")
                    break

    unique: dict[str, Job] = {}
    for j in jobs:
        unique.setdefault(j.job_id or j.url, j)
    return list(unique.values()), errors
