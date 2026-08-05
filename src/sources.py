"""Fetch and normalise postings from public ATS APIs.

All four providers expose free, unauthenticated JSON endpoints with real
posting timestamps. No scraping, no API keys, no rate limits — which is
why Phase 1 needs neither Playwright nor Apify credits.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx
from dateutil import parser as dateparse

UA = {"User-Agent": "Mozilla/5.0 (compatible; job-scout/1.0)"}
CONCURRENCY = 8
TIMEOUT = 25.0


@dataclass
class Job:
    """A normalised posting. `posted_at` is None when the source gives no
    timestamp — such rows are dropped rather than assigned a guessed date."""
    source: str
    company: str
    title: str
    location: str
    url: str
    posted_at: datetime | None
    job_id: str
    description: str = ""
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["posted_at"] = self.posted_at.isoformat() if self.posted_at else None
        d["reasons"] = "; ".join(self.reasons)
        return d


def _parse(ts: Any) -> datetime | None:
    """Parse a timestamp defensively. Returns None rather than guessing."""
    if ts is None or ts == "":
        return None
    try:
        if isinstance(ts, (int, float)):
            # Providers vary between seconds and milliseconds.
            return datetime.fromtimestamp(
                ts / 1000 if ts > 1e11 else ts, tz=timezone.utc
            )
        dt = dateparse.parse(str(ts))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ── per-provider normalisers ──────────────────────────────────────────

def _greenhouse(slug: str, data: dict) -> list[Job]:
    out = []
    for j in data.get("jobs", []):
        out.append(Job(
            source="greenhouse",
            company=slug,
            title=j.get("title", ""),
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""),
            posted_at=_parse(j.get("updated_at") or j.get("created_at")),
            job_id=str(j.get("id", "")),
            description=j.get("content", "") or "",
        ))
    return out


def _lever(slug: str, data: list) -> list[Job]:
    out = []
    for j in data or []:
        cat = j.get("categories") or {}
        out.append(Job(
            source="lever",
            company=slug,
            title=j.get("text", ""),
            location=cat.get("location", "") or "",
            url=j.get("hostedUrl", ""),
            posted_at=_parse(j.get("createdAt")),
            job_id=str(j.get("id", "")),
            description=j.get("descriptionPlain", "") or "",
        ))
    return out


def _ashby(slug: str, data: dict) -> list[Job]:
    out = []
    for j in data.get("jobs", []):
        out.append(Job(
            source="ashby",
            company=slug,
            title=j.get("title", ""),
            location=j.get("location", "") or "",
            url=j.get("jobUrl", "") or j.get("applyUrl", ""),
            posted_at=_parse(j.get("publishedAt") or j.get("updatedAt")),
            job_id=str(j.get("id", "")),
            description=j.get("descriptionPlain", "") or "",
        ))
    return out


def _smartrecruiters(slug: str, data: dict) -> list[Job]:
    out = []
    for j in data.get("content", []):
        loc = j.get("location") or {}
        city = loc.get("city") or ""
        country = loc.get("country") or ""
        out.append(Job(
            source="smartrecruiters",
            company=slug,
            title=j.get("name", ""),
            location=", ".join(p for p in (city, country) if p),
            url=(j.get("applyUrl")
                 or f"https://jobs.smartrecruiters.com/{slug}/{j.get('id','')}"),
            posted_at=_parse(j.get("releasedDate")),
            job_id=str(j.get("id", "")),
        ))
    return out


PROVIDERS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
                   _greenhouse),
    "lever": ("https://api.lever.co/v0/postings/{slug}?mode=json", _lever),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{slug}", _ashby),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100",
                        _smartrecruiters),
}


async def _fetch(client: httpx.AsyncClient, provider: str, slug: str,
                 sem: asyncio.Semaphore) -> list[Job]:
    url_tpl, normalise = PROVIDERS[provider]
    async with sem:
        try:
            r = await client.get(url_tpl.format(slug=slug), timeout=TIMEOUT)
            if r.status_code != 200:
                return []
            return normalise(slug, r.json())
        except Exception:
            # A single dead board must never take down the whole run.
            return []


async def fetch_all(targets: dict[str, Iterable[str]]) -> list[Job]:
    """targets: {provider: [slug, ...]} -> flat list of normalised jobs."""
    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = []
    async with httpx.AsyncClient(headers=UA, follow_redirects=True) as client:
        for provider, slugs in targets.items():
            if provider not in PROVIDERS:
                continue
            tasks += [_fetch(client, provider, s, sem) for s in slugs]
        results = await asyncio.gather(*tasks)
    return [job for batch in results for job in batch]
