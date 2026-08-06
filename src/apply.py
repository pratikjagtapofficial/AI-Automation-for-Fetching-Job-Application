"""Prepare-only application filler.

Opens a job's application form, fills what the Answer Bank can answer
honestly, attaches the tailored resume, screenshots the result, and hands
it to PJ.

IT NEVER SUBMITS. There is no code path here that clicks a submit button.
That is deliberate and structural, not a flag that could be flipped by a
config change:

  * PJ chose prepare-only until the pipeline has a track record.
  * Real forms carry reCAPTCHA (GitLab's does). Bypassing bot detection
    is off the table, so a human completes the last step regardless.
  * Any question the Answer Bank cannot answer honestly is left blank and
    reported. A half-filled form a human finishes is safe; an
    auto-submitted one containing invented answers is not.
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
                    continue          # never touched
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
                            # An answer exists but no option matches it.
                            # Choosing the nearest option would be guessing.
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

            safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{company}_{title}")[:55]
            shot = shot_dir / f"{safe.strip('_')}.png"
            await page.screenshot(path=str(shot), full_page=True)
            rep.screenshot = shot

        except Exception as e:
            rep.error = f"{type(e).__name__}: {str(e)[:160]}"
        finally:
            await browser.close()

    return rep


def prepare_sync(*a, **kw) -> FillReport:
    return asyncio.run(prepare(*a, **kw))


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
