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
import re
import sys
import time
from pathlib import Path

import yaml

from . import apply as applier
from . import callprep
from . import careerpages, hirist, instahyre, naukri, store
from .notify import (Telegram, digest, run_summary, strong_fit_card,
                     tailored_card)
from .render import render
from .scorer import (_blacklisted, _excluded_title, _in_scope_location,
                     _title_matches, score_all)
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


def _cap_per_company(jobs: list, profile: dict) -> list:
    """Keep at most N roles per company, highest-scoring first.

    One employer posting a dozen near-identical variants (Deutsche Telekom
    put out 8+ AI Engineer listings in a single scan) would otherwise fill
    the whole notification with one company and bury everything else.
    """
    cap = profile["limits"].get("max_per_company", 3)
    seen: dict[str, int] = {}
    out = []
    for j in sorted(jobs, key=lambda x: -x.score):
        k = j.company.strip().lower()
        if seen.get(k, 0) >= cap:
            continue
        seen[k] = seen.get(k, 0) + 1
        out.append(j)
    return out


def main() -> int:
    dry = _truthy(os.environ.get("DRY_RUN"))
    profile, targets, resume = load_cfg()
    from .answers import load as _load_bank
    answer_bank = _load_bank()
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

    # ── 3. Hirist (free JSON API, no browser, no credits) ──────────
    try:
        h_jobs, h_errs = hirist.fetch(
            ["AI Engineer", "AI Developer", "GenAI Engineer",
             "Machine Learning Engineer"],
            official_only=profile["search"].get("official_only", True),
        )
        all_jobs += h_jobs
        errors += h_errs
        print(f"hirist          : {len(h_jobs)} postings")
    except Exception as e:
        errors.append(f"hirist: {type(e).__name__}")
        print(f"hirist          : FAILED {e}")

    # ── 4. Instahyre (free JSON API; no posting dates, no JD text) ──
    # The API never returns a description body or a years-of-experience
    # figure -- measured at 0/60 sampled listings. Scoring these directly
    # would silently skip the experience check for the whole source, so
    # cheap filters run first (title/location/blacklist, all free) and
    # only that shortlist gets a real browser fetch of the public job
    # page, which does show the real band (e.g. "5-9 Years") and JD.
    try:
        i_jobs, i_errs = instahyre.fetch()
        print(f"instahyre       : {len(i_jobs)} postings")
        shortlist = [
            j for j in i_jobs
            if _title_matches(j.title, profile)
            and not _excluded_title(j.title, profile)
            and _in_scope_location(j.location)[0]
            and not _blacklisted(j.company, "", profile)
        ]
        print(f"  -> {len(shortlist)} pass cheap filters, "
              f"enriching up to {profile['limits'].get('max_instahyre_enrich', 20)}")
        enriched, e_errs = asyncio.run(instahyre.enrich(
            shortlist, profile["limits"].get("max_instahyre_enrich", 20)))
        cap = profile["limits"].get("max_instahyre_enrich", 20)
        # Anything beyond the enrichment budget has no verified experience
        # band. Cowork's own rule for an unverifiable listing is to drop
        # it rather than pad the count -- so it is excluded here, not
        # merged in as if it had passed.
        all_jobs += enriched[:cap]
        errors += i_errs + e_errs
    except Exception as e:
        errors.append(f"instahyre: {type(e).__name__}")
        print(f"instahyre       : FAILED {e}")

    # ── 5. Career pages / ATS aggregator (metered, once daily) ──────
    # Naukri already spends ~$2.50/mo at 2x/day; adding this at 2x/day
    # too would push combined Apify spend past the $5 free tier the same
    # way the original Cowork-vs-pipeline collision did. Runs once daily
    # instead, on the 14:00 IST slot (the one PJ is more reliably at his
    # laptop for, though this source has no laptop dependency itself).
    from datetime import datetime as _dt, timezone as _tz
    if 6 <= _dt.now(_tz.utc).hour < 14 or _truthy(os.environ.get("FORCE_CAREERPAGES")):
        try:
            c_jobs, c_errs = careerpages.fetch(
                ["AI Engineer", "AI Developer", "GenAI Engineer",
                 "Machine Learning Engineer", "Agentic AI Engineer"])
            all_jobs += c_jobs
            errors += c_errs
            print(f"careerpages     : {len(c_jobs)} postings")
        except Exception as e:
            errors.append(f"careerpages: {type(e).__name__}")
            print(f"careerpages     : FAILED {e}")
    else:
        print("careerpages     : skipped (runs once/day to control Apify cost)")

    print(f"total fetched   : {len(all_jobs)}")

    # ── 5. Score and filter ────────────────────────────────────────
    relevant = score_all(all_jobs, profile)
    print(f"relevant        : {len(relevant)}")

    # ── 6. Dedupe against everything seen before ───────────────────
    with store.connect() as conn:
        fresh = store.filter_new(conn, relevant)
        print(f"new (not seen)  : {len(fresh)}")

        if dry:
            # Deliberately does NOT save. Persisting here would consume the
            # "new" state, so the next real run would find nothing fresh and
            # notify about none of it — a dry run must leave no trace.
            print("DRY_RUN — nothing saved, nothing sent")
            for j in _cap_per_company(fresh, profile):
                print(f"  [{j.score:>3}] {j.company} — {j.title} ({j.location})")
            _report_errors(errors)
            return 0

        fresh = _cap_per_company(fresh, profile)
        store.save(conn, fresh)

        # ── 7. Tailor, then notify ─────────────────────
        tg = Telegram()
        threshold = profile["limits"]["strong_fit_threshold"]
        strong = [j for j in fresh if j.score >= threshold]
        rest = [j for j in fresh if j.score < threshold]

        # Tailor everything worth showing, not just live-ping fits. Real
        # scores cluster at 50-75, so gating on strong_fit_threshold left
        # most matches untailored.
        tailor_at = profile["limits"].get("tailor_threshold",
                                          profile["limits"]["min_score_to_show"])
        cap = profile["limits"].get("max_tailor_per_run", 12)
        to_tailor = sorted([j for j in fresh if j.score >= tailor_at],
                           key=lambda x: -x.score)[:cap]
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

            # Render the tailored PDF so PJ receives an actual file, not
            # just a description of edits. Rendered on demand rather than
            # committed: the tailoring JSON is the durable artefact and a
            # PDF can always be rebuilt from it, so binaries stay out of git.
            if result and result.ok:
                try:
                    safe = re.sub(r"[^A-Za-z0-9]+", "_",
                                  f"{j.company}_{j.title}")[:60].strip("_")
                    pdf, pages = render(
                        resume, ROOT / "output" / f"Pratik_Jagtap_{safe}.pdf",
                        tailored={
                            "headline": result.headline,
                            "summary": result.summary,
                            "skill_order": result.skill_order,
                            "bullets": result.bullets,
                        })
                    result._pdf = pdf
                    result._pages = pages
                    if pages > 1:
                        errors.append(
                            f"render[{j.company}]: {pages} pages — too long "
                            f"for an entry-level resume")
                except Exception as ex:
                    errors.append(f"render[{j.company}]: {type(ex).__name__}")

            tailored[store.job_key(j)] = result
            # Gemini's free tier is rate-limited per minute; a short gap
            # keeps a batch from tripping it.
            time.sleep(4)

        # Strong fits get a full card with the resume diff attached, plus
        # the tailored PDF itself, plus a prepared application where the
        # form is publicly fillable.
        prep_budget = profile["limits"].get("max_prepare_per_run", 4)
        prepared = 0

        for j in strong:
            key = store.job_key(j)
            res = tailored.get(key)
            tg.send(tailored_card(j, res), preview=False)

            pdf = getattr(res, "_pdf", None) if res else None
            if pdf:
                tg.send_document(
                    pdf, caption=f"Tailored for <b>{j.company}</b> — {j.title}")

            if not profile["limits"].get("prepare_applications", True):
                continue

            if not applier.is_supported(j.url):
                # Login-gated boards are handed over rather than
                # half-prepared; PJ's credentials are never entered.
                store.save_preparation(conn, key, filled=0, unanswered=[],
                                       screenshot=None, supported=False)
                continue

            if prepared >= prep_budget:
                continue

            try:
                rep = applier.prepare_sync(
                    j.url, j.company, j.title,
                    resume_pdf=pdf or (ROOT / "output" / "base_resume.pdf"))
                prepared += 1
                store.save_preparation(
                    conn, key, filled=len(rep.filled),
                    unanswered=rep.unanswered,
                    screenshot=str(rep.screenshot) if rep.screenshot else None,
                    supported=True)
                # Freeze what the employer actually received. Cannot be
                # reconstructed later: postings get taken down and the
                # tailored PDF is regenerated on demand.
                callprep.snapshot(
                    conn, key,
                    jd_text=j.description or "",
                    answers=rep.filled,
                    ctc_quoted=(answer_bank.get("compensation", {})
                                .get("expected_text")),
                    resume_path=str(pdf) if pdf else None,
                )

                tg.send(applier.report_card(rep), preview=False)
                if rep.screenshot:
                    tg.send_document(
                        rep.screenshot,
                        caption=(f"Filled form — <b>{j.company}</b>. "
                                 f"<b>Nothing submitted.</b>"))
                if rep.error:
                    errors.append(f"prepare[{j.company}]: {rep.error}")
                print(f"  prepared: {j.company} — {len(rep.filled)} filled, "
                      f"{len(rep.required_unanswered)} need PJ")
            except Exception as ex:
                errors.append(f"prepare[{j.company}]: {type(ex).__name__}")
                print(f"  prepare ERROR: {j.company} — {ex}")

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
