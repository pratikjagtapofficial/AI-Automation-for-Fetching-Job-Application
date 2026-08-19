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
from . import careerpages, foundit, hirist, instahyre, naukri, store
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


# Used to alternate across two daily runs; now queried together in the
# single nightly run (see scout.yml, 2026-08-11 — collapsed from twice
# daily to once to stop Instahyre's bot detection escalating on
# back-to-back scans). Naukri is priced per RESULT, not per run or per
# keyword batch, so combining these into one call is cost-neutral versus
# the old rotation: the same four keywords get queried once a day either
# way, just in one run instead of split across two.
NAUKRI_KEYWORDS = [
    "AI Engineer", "AI Developer", "GenAI Engineer", "Machine Learning Engineer",
]


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
    kw = NAUKRI_KEYWORDS
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

    # ── 3b. Foundit / Monster India (free JSON API, no key, no credits) ──
    # Added 2026-08-19 to keep India coverage alive while Apify's free
    # credit is exhausted (Naukri + careerpages both dead until the cycle
    # resets). Same shape of source as Hirist -- structured experience
    # integers and an epoch posting date -- so it needs no browser and no
    # regex-guessing. See foundit.py for what else was tested and rejected.
    try:
        f_jobs, f_errs = foundit.fetch(
            ["AI Engineer", "AI Developer", "GenAI Engineer",
             "Machine Learning Engineer"],
            official_only=profile["search"].get("official_only", True),
        )
        all_jobs += f_jobs
        errors += f_errs
        print(f"foundit         : {len(f_jobs)} postings")
    except Exception as e:
        errors.append(f"foundit: {type(e).__name__}")
        print(f"foundit         : FAILED {e}")

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

    # ── 5. Career pages / ATS aggregator (metered) ───────────────────
    # Used to be gated to one of the two daily runs to avoid doubling its
    # cost. Scout is now a single nightly run (see scout.yml, 2026-08-11),
    # so that gate no longer does anything useful — it always runs.
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
        # the tailored PDF itself, then split into two categories: public
        # forms get dry-run-checked and staged ready for a bare CONFIRM;
        # anything login-gated (Naukri/Hirist/Instahyre/LinkedIn) is
        # handed over with the resume already attached above — PJ's
        # credentials are never entered by this system.
        prep_budget = profile["limits"].get("max_prepare_per_run", 4)
        pdfs: dict[str, object] = {}

        for j in strong:
            key = store.job_key(j)
            res = tailored.get(key)
            tg.send(tailored_card(j, res), preview=False)

            pdf = getattr(res, "_pdf", None) if res else None
            pdfs[key] = pdf
            if pdf:
                tg.send_document(
                    pdf, caption=f"Tailored for <b>{j.company}</b> — {j.title}")

        if profile["limits"].get("prepare_applications", True):
            ready_jobs = [j for j in strong if applier.is_supported(j.url)]
            login_jobs = [j for j in strong if not applier.is_supported(j.url)]

            for j in login_jobs:
                store.save_preparation(conn, store.job_key(j), filled=0,
                                       unanswered=[], screenshot=None,
                                       supported=False)
            if login_jobs:
                tg.send(
                    f"🔒 <b>{len(login_jobs)} need your login</b>\n"
                    "<i>Resume is attached above — these apply through "
                    "an account this system never signs into.</i>")

            # Dry-run each candidate first (no messages sent yet) so the
            # "N ready" count in the lead-in line is accurate, then send
            # each confirm card knowing up front whether CONFIRM needs a
            # company name to disambiguate.
            staged_results: list[tuple] = []   # (job, key, pdf, result)
            for j in ready_jobs[:prep_budget]:
                key = store.job_key(j)
                pdf = pdfs.get(key) or (ROOT / "output" / "base_resume.pdf")
                try:
                    result = applier.submit_sync(
                        j.url, j.company, j.title, resume_pdf=pdf,
                        dry_run=True)
                except Exception as ex:
                    errors.append(f"prepare[{j.company}]: {type(ex).__name__}")
                    print(f"  dry-run ERROR: {j.company} — {ex}")
                    continue

                rep = result.rep
                if result.ok:
                    store.set_pending_submit(
                        conn, key, j.company, j.title, j.url,
                        str(pdf) if pdf else None)
                store.save_preparation(
                    conn, key, filled=len(rep.filled),
                    unanswered=rep.unanswered,
                    screenshot=str(result.screenshot) if result.screenshot else None,
                    supported=True)
                # Freeze what the employer would receive. Cannot be
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
                if not result.ok:
                    errors.append(f"prepare[{j.company}]: {result.error}")
                print(f"  {'staged' if result.ok else 'FAILED'}: "
                      f"{j.company} — {len(rep.filled)} filled, "
                      f"{len(rep.required_unanswered)} need PJ")
                staged_results.append((j, key, pdf, result))

            staged = [r for (_, _, _, r) in staged_results if r.ok]
            if staged:
                tg.send(
                    f"✅ <b>{len(staged)} ready to apply</b>\n"
                    "<i>Dry-run passed — form filled, no CAPTCHA, exactly "
                    "one submit control found.</i>")
            multi = len(staged) > 1
            for j, key, pdf, result in staged_results:
                tg.send(applier.confirm_card(result, disambiguate=multi),
                       preview=False)
                if result.screenshot:
                    tg.send_document(
                        result.screenshot,
                        caption=(f"Filled form — <b>{j.company}</b>. "
                                 f"<b>Nothing submitted yet.</b>"))

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
