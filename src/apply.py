"""Application filler — and, since 2026-08-07, a real submitter.

`prepare()` fills what the Answer Bank can answer honestly, attaches the
tailored resume, screenshots the result, and hands it to PJ. This
function's behaviour has NOT changed and never clicks Submit.

`submit()` is new: it fills fresh and can click the real Submit button on
a job's form. This is genuine automation, not a screenshot — a real
application goes to a real employer when it runs with dry_run=False. Two
things stay absolute regardless of any instruction:

  * CAPTCHA is never bypassed. `submit()` re-checks for one immediately
    before touching the submit control, live, on top of whatever
    `prepare()` recorded earlier — forms change between preparation and
    confirmation. A CAPTCHA present at either point aborts, always.
  * `dry_run` defaults to True. Every caller must pass dry_run=False
    explicitly to fire a real click, and the only place in this codebase
    that does is the confirm handler in commands.py — reached ONLY after
    PJ named one specific job and then sent a separate, literal
    confirmation message. No natural-language phrasing, however
    confident-sounding, reaches dry_run=False on its own; see nlu.py's
    "unsupported" bucket for anything that would act on more than one
    job or skip that second explicit step.

Any question the Answer Bank cannot answer honestly is left blank and
reported, in both functions — a half-filled form a human finishes is
safe; one containing an invented answer is not, automated or otherwise.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import async_playwright

from .answers import Answer, answer_for, load as load_bank

ROOT = Path(__file__).resolve().parents[1]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Extracts every visible control plus its best-guess label.
_FIELD_JS = """() => {
  const out = [];
  document.querySelectorAll('input,select,textarea').forEach((el, i) => {
    if (el.type === 'hidden') return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    let label = '';
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) label = l.innerText;
    }
    if (!label) { const l = el.closest('label'); if (l) label = l.innerText; }
    if (!label) {
      const w = el.closest('div');
      if (w) { const l = w.querySelector('label'); if (l) label = l.innerText; }
    }
    if (!label) label = el.getAttribute('aria-label') || el.placeholder || '';
    el.setAttribute('data-fill-idx', String(i));
    out.push({
      idx: i,
      tag: el.tagName.toLowerCase(),
      type: (el.type || '').toLowerCase(),
      required: !!el.required || /\\*/.test(label),
      label: (label || '').replace(/\\s+/g, ' ').trim().slice(0, 160),
      options: el.tagName === 'SELECT'
        ? Array.from(el.options).map(o => o.text.trim()).slice(0, 40) : [],
    });
  });
  return out;
}"""


# Forms that are publicly fillable without an account. Everything else —
# Naukri, Hirist, Instahyre, LinkedIn — puts the application behind a
# login, and PJ's credentials are never entered by this system. Those get
# handed over as a link instead of half-prepared.
SUPPORTED_HOSTS = (
    "greenhouse.io", "job-boards.greenhouse.io", "boards.greenhouse.io",
    "lever.co", "jobs.lever.co",
    "ashbyhq.com", "jobs.ashbyhq.com",
    "smartrecruiters.com", "jobs.smartrecruiters.com",
)


def is_supported(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in SUPPORTED_HOSTS)


def why_unsupported(url: str) -> str:
    u = (url or "").lower()
    for host, name in (("naukri", "Naukri"), ("hirist", "Hirist"),
                       ("instahyre", "Instahyre"), ("linkedin", "LinkedIn"),
                       ("indeed", "Indeed"), ("wellfound", "Wellfound")):
        if host in u:
            return f"{name} requires a signed-in session to apply"
    return "not a directly fillable application form"


@dataclass
class FillReport:
    url: str
    company: str
    title: str
    filled: list[tuple[str, str]] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)
    required_unanswered: list[str] = field(default_factory=list)
    resume_attached: bool = False
    captcha: bool = False
    screenshot: Path | None = None
    error: str = ""

    @property
    def ready(self) -> bool:
        """True when nothing required is missing. Even then PJ submits."""
        return not self.required_unanswered and not self.error


def _pick_option(value: str, options: list[str]) -> str | None:
    """Match an answer to a <select> option without inventing one."""
    v = value.strip().lower()
    for o in options:
        if o.strip().lower() == v:
            return o
    if v in ("yes", "no"):
        for o in options:
            if o.strip().lower().startswith(v):
                return o
    for o in options:
        if v and v in o.strip().lower():
            return o
    return None


async def _fill_form(page, company: str, title: str,
                     resume_pdf: Path | None, bank: dict) -> FillReport:
    """Fill whatever the Answer Bank can answer honestly. Shared by
    prepare() and submit() so there is exactly one place that decides
    what goes into a field — not two copies that could drift apart."""
    rep = FillReport(url=page.url, company=company, title=title)

    html = (await page.content()).lower()
    rep.captcha = any(k in html for k in
                      ("recaptcha", "hcaptcha", "cf-turnstile"))

    fields = await page.evaluate(_FIELD_JS)

    for f in fields:
        label = f["label"]
        sel = f'[data-fill-idx="{f["idx"]}"]'

        if f["type"] == "file":
            if resume_pdf and not rep.resume_attached:
                try:
                    await page.set_input_files(sel, str(resume_pdf))
                    rep.resume_attached = True
                    rep.filled.append(("Resume", resume_pdf.name))
                except Exception:
                    pass
            continue

        if f["type"] in ("submit", "button", "image", "reset"):
            continue          # never touched here — see submit() below
        if not label:
            continue

        ans: Answer | None = answer_for(label, bank)
        if ans is None:
            rep.unanswered.append(label)
            if f["required"]:
                rep.required_unanswered.append(label)
            continue

        try:
            if f["tag"] == "select":
                opt = _pick_option(ans.value, f["options"])
                if opt is None:
                    # An answer exists but no option matches it. Choosing
                    # the nearest option would be guessing.
                    rep.unanswered.append(
                        f"{label} (no option matches '{ans.value}')")
                    if f["required"]:
                        rep.required_unanswered.append(label)
                    continue
                await page.select_option(sel, label=opt)
                rep.filled.append((label, opt))
            elif f["type"] in ("checkbox", "radio"):
                if ans.value.strip().lower() in ("yes", "true", "1"):
                    await page.check(sel)
                    rep.filled.append((label, "checked"))
            else:
                await page.fill(sel, ans.value)
                rep.filled.append((label, ans.value))
        except Exception:
            rep.unanswered.append(f"{label} (could not fill)")
            if f["required"]:
                rep.required_unanswered.append(label)

    return rep


def _safe_name(company: str, title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", f"{company}_{title}")[:55].strip("_")


async def prepare(url: str, company: str, title: str,
                  resume_pdf: Path | None = None,
                  bank: dict | None = None,
                  headless: bool = True,
                  shot_dir: Path | None = None) -> FillReport:
    """Fill a form and screenshot it. Never submits."""
    bank = bank if bank is not None else load_bank()
    rep = FillReport(url=url, company=company, title=title)
    shot_dir = shot_dir or (ROOT / "output" / "screenshots")
    shot_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            user_agent=UA, viewport={"width": 1366, "height": 1000},
            locale="en-IN")
        page = await ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded",
                                   timeout=45000)
            if resp and resp.status >= 400:
                rep.error = f"HTTP {resp.status}"
                await browser.close()
                return rep
            await page.wait_for_timeout(4000)

            rep = await _fill_form(page, company, title, resume_pdf, bank)
            rep.url = url

            shot = shot_dir / f"{_safe_name(company, title)}.png"
            await page.screenshot(path=str(shot), full_page=True)
            rep.screenshot = shot

        except Exception as e:
            rep.error = f"{type(e).__name__}: {str(e)[:160]}"
        finally:
            await browser.close()

    return rep


def prepare_sync(*a, **kw) -> FillReport:
    return asyncio.run(prepare(*a, **kw))


@dataclass
class SubmitResult:
    """Outcome of submit(). `submitted` is only ever True when dry_run
    was explicitly False AND the click actually fired — never inferred,
    never assumed from a successful fill."""
    rep: FillReport
    submitted: bool = False
    confirmed_by_page: bool = False   # a "thank you"/received-style
                                       # message was found post-click;
                                       # False means "click happened but
                                       # verify manually", not "it failed"
    screenshot: Path | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


_SUCCESS_SIGNALS = (
    "thank you", "application received", "successfully submitted",
    "we've received", "received your application", "application complete",
    "application submitted", "thanks for applying",
)


async def submit(url: str, company: str, title: str,
                 resume_pdf: Path | None = None,
                 bank: dict | None = None,
                 headless: bool = True,
                 shot_dir: Path | None = None,
                 dry_run: bool = True) -> SubmitResult:
    """Fill fresh and, only when dry_run=False, click the real Submit
    button. See the module docstring — dry_run=False must only ever be
    reached after PJ's own explicit, separate, per-job confirmation.

    dry_run=True (the default) does everything up to and including
    locating the submit control, but never touches it — used to verify a
    job is genuinely submittable (no CAPTCHA, nothing required left
    blank, exactly one unambiguous submit control) before ever asking PJ
    to confirm something that would fail or misfire.
    """
    bank = bank if bank is not None else load_bank()
    shot_dir = shot_dir or (ROOT / "output" / "screenshots")
    shot_dir.mkdir(parents=True, exist_ok=True)
    result = SubmitResult(rep=FillReport(url=url, company=company, title=title))

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            user_agent=UA, viewport={"width": 1366, "height": 1000},
            locale="en-IN")
        page = await ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded",
                                   timeout=45000)
            if resp and resp.status >= 400:
                result.error = f"HTTP {resp.status}"
                await browser.close()
                return result
            await page.wait_for_timeout(4000)

            rep = await _fill_form(page, company, title, resume_pdf, bank)
            rep.url = url
            result.rep = rep
            safe = _safe_name(company, title)

            # CAPTCHA is re-checked live, here, regardless of what an
            # earlier prepare() call recorded — forms change, and this is
            # the one check that must never trust stale data.
            if rep.captcha:
                result.error = "CAPTCHA present — cannot submit automatically"
                shot = shot_dir / f"{safe}_blocked.png"
                await page.screenshot(path=str(shot), full_page=True)
                result.screenshot = shot
                await browser.close()
                return result

            if rep.required_unanswered:
                result.error = (
                    f"{len(rep.required_unanswered)} required field(s) "
                    f"still unanswered — cannot submit")
                shot = shot_dir / f"{safe}_blocked.png"
                await page.screenshot(path=str(shot), full_page=True)
                result.screenshot = shot
                await browser.close()
                return result

            submit_ctrl = page.locator(
                'button[type="submit"], input[type="submit"]')
            n = await submit_ctrl.count()
            if n != 1:
                result.error = (
                    f"expected exactly one submit control, found {n} — "
                    f"refusing to guess which one")
                shot = shot_dir / f"{safe}_blocked.png"
                await page.screenshot(path=str(shot), full_page=True)
                result.screenshot = shot
                await browser.close()
                return result

            pre_shot = shot_dir / f"{safe}_pre_submit.png"
            await page.screenshot(path=str(pre_shot), full_page=True)
            result.screenshot = pre_shot

            if dry_run:
                await browser.close()
                return result

            # ── everything above this line is identical whether or not
            # the click actually happens; only dry_run gates it ────────
            await submit_ctrl.first.click()
            await page.wait_for_timeout(5000)

            body = (await page.inner_text("body")).lower()
            result.confirmed_by_page = any(s in body for s in _SUCCESS_SIGNALS)
            result.submitted = True

            post_shot = shot_dir / f"{safe}_post_submit.png"
            await page.screenshot(path=str(post_shot), full_page=True)
            result.screenshot = post_shot

        except Exception as e:
            result.error = f"{type(e).__name__}: {str(e)[:160]}"
        finally:
            await browser.close()

    return result


def submit_sync(*a, **kw) -> SubmitResult:
    return asyncio.run(submit(*a, **kw))


def report_card(rep: FillReport) -> str:
    """Telegram summary. Always states that nothing was submitted."""
    import html as _h
    e = _h.escape
    lines = [f"📝 <b>Application prepared</b>",
             f"<b>{e(rep.company)}</b> — {e(rep.title)}", ""]

    if rep.error:
        lines += [f"⚠️ Could not open the form: {e(rep.error)}",
                  f'<a href="{e(rep.url)}">Open manually →</a>']
        return "\n".join(lines)

    lines.append(f"✅ Filled <b>{len(rep.filled)}</b> field(s)"
                 + ("  ·  📎 resume attached" if rep.resume_attached else ""))

    if rep.required_unanswered:
        lines += ["", f"🛑 <b>{len(rep.required_unanswered)} required "
                      f"field(s) need you</b>"]
        lines += [f"• {e(q[:90])}" for q in rep.required_unanswered[:6]]
    elif rep.unanswered:
        lines += ["", f"❓ {len(rep.unanswered)} optional field(s) left blank"]
        lines += [f"• {e(q[:90])}" for q in rep.unanswered[:4]]

    if rep.captcha:
        lines += ["", "🤖 <i>This form uses CAPTCHA — it can only be "
                      "submitted by you.</i>"]

    lines += ["", "<b>Nothing was submitted.</b> Review the screenshot, "
                  "then finish it yourself:",
              f'<a href="{e(rep.url)}">Open application →</a>']
    return "\n".join(lines)


def confirm_card(result: SubmitResult, disambiguate: bool = False) -> str:
    """Shown before a real submission — this is the confirmation prompt
    itself, so it must show exactly what will be sent and never imply
    anything has happened yet.

    `disambiguate` is set once more than one job is simultaneously
    pending confirmation — PJ then needs to name which one, since a bare
    CONFIRM is only unambiguous when a single job is waiting.
    """
    import html as _h
    e = _h.escape
    rep = result.rep

    if not result.ok:
        return (f"🛑 <b>Can't submit {e(rep.company)} — {e(rep.title)}</b>\n\n"
                f"{e(result.error)}\n\n"
                f'<a href="{e(rep.url)}">Open manually →</a>')

    lines = [
        f"⚠️ <b>About to submit — {e(rep.company)}</b>",
        f"{e(rep.title)}",
        "",
        f"Filled <b>{len(rep.filled)}</b> field(s)"
        + ("  ·  📎 resume attached" if rep.resume_attached else ""),
    ]
    if rep.unanswered:
        lines.append(f"<i>{len(rep.unanswered)} optional field(s) left blank</i>")
    lines += ["", "<b>This cannot be undone once sent.</b>"]
    if disambiguate:
        lines.append(f"Reply <code>CONFIRM {e(rep.company)}</code> to submit "
                     f"this one for real — another job is also waiting on "
                     f"you, so a bare CONFIRM won't know which you mean.")
    else:
        lines.append("Reply <code>CONFIRM</code> to submit for real, or "
                     "anything else to cancel.")
    return "\n".join(lines)


def submitted_card(result: SubmitResult) -> str:
    import html as _h
    e = _h.escape
    rep = result.rep

    if not result.ok:
        return (f"🛑 <b>Submission failed — {e(rep.company)}</b>\n\n"
                f"{e(result.error)}\n\n"
                f'<a href="{e(rep.url)}">Open manually →</a>')

    if not result.submitted:
        return (f"🛑 <b>Not submitted — {e(rep.company)}</b>\n\n"
                "<i>Internal error: reached the report stage without a "
                "real submit attempt. Nothing was sent.</i>")

    lines = [f"✅ <b>Submitted — {e(rep.company)}</b>", f"{e(rep.title)}", ""]
    if result.confirmed_by_page:
        lines.append("The form showed a confirmation message after submitting.")
    else:
        lines.append(
            "<i>Submitted, but no confirmation text was detected on the "
            "resulting page — worth a quick manual check.</i>")
    lines += ["", f'<a href="{e(rep.url)}">Original posting →</a>']
    return "\n".join(lines)
