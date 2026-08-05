"""Test every candidate ATS board slug against the live API.

Company ATS slugs cannot be guessed reliably, so this script is the only
thing that decides what goes into the verified source list. Slugs that
don't respond are marked dead rather than silently polling nothing.

    python scripts/verify_boards.py            # report only
    python scripts/verify_boards.py --write    # rewrite targets.yml
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ROOT / "config" / "targets.yml"

# Each entry: url template -> function extracting the job list from JSON.
BOARDS = {
    "greenhouse": (
        "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        lambda d: d.get("jobs", []),
    ),
    "lever": (
        "https://api.lever.co/v0/postings/{slug}?mode=json",
        lambda d: d if isinstance(d, list) else [],
    ),
    "ashby": (
        "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        lambda d: d.get("jobs", []),
    ),
    "smartrecruiters": (
        "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        lambda d: d.get("content", []),
    ),
}

CONCURRENCY = 8


async def check(client: httpx.AsyncClient, board: str, slug: str,
                sem: asyncio.Semaphore) -> tuple[str, str, int]:
    """Return (slug, status, job_count). Never raises."""
    url_tpl, extract = BOARDS[board]
    async with sem:
        try:
            r = await client.get(url_tpl.format(slug=slug), timeout=20.0)
        except Exception:
            return slug, "error", 0

    if r.status_code != 200:
        return slug, "dead", 0
    try:
        jobs = extract(r.json())
    except Exception:
        return slug, "dead", 0

    # A board that responds 200 with zero postings is live but idle —
    # worth keeping, since it may post later.
    return slug, "ok", len(jobs)


async def main(write: bool) -> int:
    cfg = yaml.safe_load(TARGETS.read_text(encoding="utf-8"))
    sem = asyncio.Semaphore(CONCURRENCY)
    results: dict[str, dict] = {}

    headers = {"User-Agent": "Mozilla/5.0 (compatible; job-scout/1.0)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        for board in BOARDS:
            slugs = (cfg.get(board) or {}).get("candidates") or []
            if not slugs:
                continue
            print(f"\n{board}  ({len(slugs)} candidates)")
            print("-" * 52)

            out = await asyncio.gather(
                *(check(client, board, s, sem) for s in slugs)
            )
            live, total = [], 0
            for slug, status, count in sorted(out):
                mark = {"ok": "OK  ", "dead": "DEAD", "error": "ERR "}[status]
                print(f"  {mark} {slug:<26} {count:>4} jobs")
                if status == "ok":
                    live.append(slug)
                    total += count
            results[board] = {"verified": live, "jobs": total}
            print(f"  -> {len(live)}/{len(slugs)} live, {total} postings")

    grand = sum(v["jobs"] for v in results.values())
    verified = sum(len(v["verified"]) for v in results.values())
    print(f"\n{'=' * 52}\n{verified} live boards, {grand} total postings\n")

    if write:
        for board, data in results.items():
            cfg[board]["candidates"] = data["verified"]
            cfg[board]["status"] = "verified"
        TARGETS.write_text(
            yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        print(f"Rewrote {TARGETS.relative_to(ROOT)} with verified slugs only.")
        print("NOTE: this strips the explanatory comments from the file.")
    else:
        print("Report only. Re-run with --write to prune targets.yml.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--write", action="store_true",
                   help="rewrite targets.yml keeping only verified slugs")
    sys.exit(asyncio.run(main(p.parse_args().write)))
