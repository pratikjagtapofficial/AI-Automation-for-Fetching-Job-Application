"""Scout runner — the entry point GitHub Actions invokes.

    python -m src.scout

Flow: fetch -> score -> dedupe -> notify -> persist.

Nothing here fails loudly enough to lose the whole run: a dead source is
recorded as an error and reported in the run summary, because a partial
scan is far more useful than no scan.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import yaml

from . import naukri, store
from .notify import Telegram, digest, run_summary, strong_fit_card
from .scorer import score_all
from .sources import fetch_all

ROOT = Path(__file__).resolve().parents[1]


def load_cfg() -> tuple[dict, dict]:
    profile = yaml.safe_load(
        (ROOT / "config" / "profile.yml").read_text(encoding="utf-8"))
    targets = yaml.safe_load(
        (ROOT / "config" / "targets.yml").read_text(encoding="utf-8"))
    return profile, targets


def _truthy(v: str | None) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    dry = _truthy(os.environ.get("DRY_RUN"))
    profile, targets = load_cfg()
    errors: list[str] = []
    all_jobs = []

    # ── 1. ATS boards (free JSON APIs, unlimited) ──────────────────
    ats = {
        p: (targets.get(p) or {}).get("candidates") or []
        for p in ("greenhouse", "lever", "ashby", "smartrecruiters")
    }
    try:
        ats_jobs = asyncio.run(fetch_all(ats))
        all_jobs += ats_jobs
        print(f"ats boards      : {len(ats_jobs)} postings")
    except Exception as e:
        errors.append(f"ats: {type(e).__name__}")
        print(f"ats boards      : FAILED {e}")

    # ── 2. Naukri via Apify (metered — keep the search matrix small) ─
    kw = ["AI Engineer", "AI Developer"]
    locs = ["india"]
    try:
        n_jobs, n_errs = naukri.fetch(
            kw, locs,
            official_only=profile["search"].get("official_only", True),
        )
        all_jobs += n_jobs
        errors += n_errs
        print(f"naukri          : {len(n_jobs)} postings")
    except Exception as e:
        errors.append(f"naukri: {type(e).__name__}")
        print(f"naukri          : FAILED {e}")

    print(f"total fetched   : {len(all_jobs)}")

    # ── 3. Score and filter ────────────────────────────────────────
    relevant = score_all(all_jobs, profile)
    print(f"relevant        : {len(relevant)}")

    # ── 4. Dedupe against everything seen before ───────────────────
    with store.connect() as conn:
        fresh = store.filter_new(conn, relevant)
        print(f"new (not seen)  : {len(fresh)}")
        store.save(conn, fresh)

        if dry:
            print("DRY_RUN — no Telegram messages sent")
            for j in fresh:
                print(f"  [{j.score:>3}] {j.company} — {j.title} ({j.location})")
            _report_errors(errors)
            store.record_run(conn, len(all_jobs), len(relevant), 0, errors)
            return 0

        # ── 5. Notify ──────────────────────────────────────────────
        tg = Telegram()
        threshold = profile["limits"]["strong_fit_threshold"]
        strong = [j for j in fresh if j.score >= threshold]
        rest = [j for j in fresh if j.score < threshold]

        for j in strong:
            tg.send(strong_fit_card(j), preview=False)

        if rest or not strong:
            tg.send(digest(rest, threshold))

        tg.send(run_summary(len(all_jobs), len(relevant), len(fresh), errors))

        store.mark_notified(conn, fresh)
        store.record_run(conn, len(all_jobs), len(relevant), len(fresh), errors)

    print(f"notified        : {len(fresh)}")
    _report_errors(errors)
    return 0


def _report_errors(errors: list[str]) -> None:
    """A skipped source must never look like a source that found nothing.
    Both print '0 postings'; only this distinguishes them."""
    if not errors:
        return
    print(f"\n!! {len(errors)} source problem(s):")
    for e in errors:
        print("  -", e)


if __name__ == "__main__":
    sys.exit(main())
