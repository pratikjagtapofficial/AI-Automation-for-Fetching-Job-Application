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
import time
from pathlib import Path

import yaml

from . import naukri, store
from .notify import (Telegram, digest, run_summary, strong_fit_card,
                     tailored_card)
from .scorer import score_all
from .tailor import tailor
from .sources import fetch_all

ROOT = Path(__file__).resolve().parents[1]


def load_cfg() -> tuple[dict, dict, dict]:
    profile = yaml.safe_load(
        (ROOT / "config" / "profile.yml").read_text(encoding="utf-8"))
    targets = yaml.safe_load(
        (ROOT / "config" / "targets.yml").read_text(encoding="utf-8"))
    resume = yaml.safe_load(
        (ROOT / "config" / "resume.yml").read_text(encoding="utf-8"))
    return profile, targets, resume


def _truthy(v: str | None) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


# Rotated across runs to double keyword coverage at unchanged cost.
NAUKRI_KEYWORD_SETS = [
    ["AI Engineer", "AI Developer"],                    # 02:00 IST run
    ["GenAI Engineer", "Machine Learning Engineer"],    # 14:00 IST run
]


def _keyword_set() -> list[str]:
    """Alternate on UTC hour. The 02:00 IST run is 20:30 UTC (hour 20),
    the 14:00 IST run is 08:30 UTC (hour 8); anything else (a manual or
    dispatched run) falls back to the primary set."""
    from datetime import datetime, timezone
    hour = datetime.now(timezone.utc).hour
    override = os.environ.get("NAUKRI_KEYWORD_SET")
    if override and override.isdigit():
        return NAUKRI_KEYWORD_SETS[int(override) % len(NAUKRI_KEYWORD_SETS)]
    return NAUKRI_KEYWORD_SETS[1] if 6 <= hour < 14 else NAUKRI_KEYWORD_SETS[0]


def main() -> int:
    dry = _truthy(os.environ.get("DRY_RUN"))
    profile, targets, resume = load_cfg()
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

    # ── 2. Naukri via Apify (metered) ──────────────────────────────
    # Each keyword is one actor run at ~$0.021. Location stays "india":
    # Naukri's national search already spans every target city, so adding
    # cities multiplies cost for zero extra coverage.
    #
    # Keyword sets ALTERNATE between runs rather than all firing each
    # time. Four keywords twice daily would cost ~$5.04/mo and blow the
    # free tier on its own; rotating two per run covers all four daily at
    # half that, and a posting missed in the morning is caught that night
    # while still inside the 24h freshness window.
    kw = _keyword_set()
    locs = ["india"]
    print(f"naukri keywords : {kw}")
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

        # ── 5. Tailor strong fits, then notify ─────────────────────
        tg = Telegram()
        threshold = profile["limits"]["strong_fit_threshold"]
        strong = [j for j in fresh if j.score >= threshold]
        rest = [j for j in fresh if j.score < threshold]

        # Tailor everything worth showing, not just live-ping fits. Real
        # scores cluster at 50-75, so gating on strong_fit_threshold left
        # most matches untailored.
        tailor_at = profile["limits"].get("tailor_threshold",
                                          profile["limits"]["min_score_to_show"])
        to_tailor = [j for j in fresh if j.score >= tailor_at]
        tailored: dict[str, object] = {}

        for j in to_tailor:
            result = None
            try:
                result = tailor(j, resume, profile)
                if result.ok:
                    result._old_headline = resume["headline"]["default"]
                    saved = store.save_tailoring(conn, store.job_key(j), {
                        "headline": result.headline,
                        "summary": result.summary,
                        "skill_order": result.skill_order,
                        "bullets": result.bullets,
                        "rationale": result.rationale,
                        "warnings": (result.verdict.warnings
                                     if result.verdict else []),
                    })
                    if not saved:
                        errors.append(
                            f"tailor[{j.company}]: computed but not persisted "
                            f"(key mismatch) — /diff will not find it")
                    print(f"  tailored: {j.company} — {j.title}")
                else:
                    errors.append(
                        f"tailor[{j.company}]: {result.error}")
                    print(f"  tailor FAILED: {j.company} — {result.error}")
            except Exception as ex:
                errors.append(f"tailor[{j.company}]: {type(ex).__name__}")
                print(f"  tailor ERROR: {j.company} — {ex}")

            tailored[store.job_key(j)] = result
            # Gemini's free tier is rate-limited per minute; a short gap
            # keeps a batch from tripping it.
            time.sleep(4)

        # Strong fits get a full card with the resume diff attached.
        for j in strong:
            tg.send(tailored_card(j, tailored.get(store.job_key(j))),
                    preview=False)

        # Everything else batches, with a marker showing which were
        # tailored so PJ knows a /diff is available.
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
