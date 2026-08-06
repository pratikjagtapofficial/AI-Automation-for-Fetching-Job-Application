# Job Application Automation — Spec

24/7 cloud multi-agent system: finds AI roles, tailors resume per JD, gets approval via
Telegram, applies, and keeps a call-prep record for every application.

**Owner:** Pratik Jagtap (PJ) · **Status:** design locked, not yet built · **Date:** 2026-08-06

---

## Profile

| | |
|---|---|
| Role | AI Developer @ HireTalent, Pune |
| Education | MCA, completed Jun 2026 |
| Has | Python, LangChain, RAG, MCP, Claude Code, vector DBs, FastAPI, prompt engineering, multi-agent/agentic AI |
| Does NOT have | LangGraph, CrewAI/AutoGen, Docker/Kubernetes, Azure/AWS |
| Target roles | AI Developer, AI Engineer, Associate AI Engineer, Agentic AI Engineer, GenAI Engineer (0–2 yrs) |
| Locations | Pune, Bengaluru, Hyderabad, Noida, Gurugram, **remote (top preference)**; anywhere for FAANG-tier |
| Work mode | No hard no — onsite/hybrid/remote all fine, remote ranked highest |

## Locked decisions

| Decision | Choice |
|---|---|
| Host | Oracle Cloud Always Free ARM (Mumbai/Hyderabad region) |
| Expected CTC | **4–6 LPA**, auto-filled as a range |
| Notice period | 30 days |
| Employment | Employed at HireTalent — **quiet search, discretion required** |
| Auto-apply mode | **Prepare-only at first** — fill + screenshot + handoff link, no auto-submit until proven |
| Volume | 10–20 applications/day |
| Notifications | Live for strong fits, digest for the rest |
| Email on forms | pratikjagtapofficial@gmail.com |
| Cover letters | Only when the form requires one |
| LLM | Gemini Flash free tier (⚠️ free-tier data may be used for training) |
| Scraping | Self-hosted Playwright; Apify free credits reserved for hard sites (Naukri/LinkedIn) |
| Unknown screening Q | Ask in Telegram, pause that application, save answer to Answer Bank |
| Stretch roles | Apply anyway, never claim the missing skill, prep honest framing |
| Cowork task | Keep running in parallel until cloud version is trusted |

## Blacklist — never apply

- HireTalent and its clients (critical for quiet search)
- Staffing / recruitment consultancies
- Any company already applied to (dedupe via tracker)

> Large service companies (TCS/Infosys/Wipro/etc.) are **in scope** — not excluded.

## Sources — empirically tested 2026-08-06

**Requirement:** official postings only — company career pages, or postings made by
the company/HR directly. Not agency reposts.

### Access test results

| Source | Raw Playwright | Firecrawl | Apify | Verdict |
|---|---|---|---|---|
| Greenhouse/Lever/Ashby/SmartRecruiters | ✅ free JSON API | — | — | **use** |
| Naukri | ❌ 403 Akamai | ✅ 200 | ✅ structured | **use Apify** |
| Hirist (tech-focused) | ✅ | — | — | **use** |
| Instahyre | ✅ (JS render) | — | — | use |
| CutShort | ⚠️ thin | — | — | maybe |
| Internshala | ✅ | — | — | **skip — wrong roles** |
| Foundit / Wellfound / Indeed | ❌ 403 | — | — | skip |
| LinkedIn | — | — | — | **excluded, ban risk** |

### Measured yield

ATS APIs alone: 4,352 postings → **2** in-scope India/remote junior AI roles.
Structurally weak — those boards are US companies. Naukri is where the Indian
entry-level market actually is.

Internshala scrapes cleanly but its "AI jobs" are professors, trainers and video
editors. Accessibility ≠ relevance.

### Naukri via Apify — actor cost comparison

| Actor | Per 100 jobs | Per month @2×/day |
|---|---|---|
| **valig/naukri-jobs-scraper** | $0.041 | **~$2.50** ✅ fits free tier |
| memo23/naukri-scraper | $0.155 | ~$9.30 |
| muhammetakkurtt/naukri-job-scraper | $0.51 | ~$30 |

**Chosen: `valig/naukri-jobs-scraper`** — 12× cheaper than the one the Cowork task
currently uses, returns 70 structured fields in ~7s, and critically exposes:

- `consultant: false` + `companyApplyJob: true` → **official company posting**, the
  direct implementation of the official-sources-only requirement
- `createdDateText` → exact timestamp, no freshness guessing
- `experience.minimum/maximum`, `salary.*`, `skills.preferred`, `description.full`

Firecrawl also passes Naukri (1 credit/page, `proxy: basic`) — kept as fallback, but
it returns markdown needing parsing where Apify returns clean JSON.

### Cost position — must stay ₹0

Everything above runs at **zero out-of-pocket cost**:

| Component | Free allowance | Our usage | Paid? |
|---|---|---|---|
| GitHub Actions | 2000 min/mo | ~35 min/day | ₹0 |
| ATS JSON APIs | unlimited | unlimited | ₹0 |
| Playwright (Hirist/Instahyre) | own compute | — | ₹0 |
| Apify (Naukri) | $5/mo credits | ~$2.50/mo | ₹0 |
| Gemini Flash | free tier | 10–20 jobs/day | ₹0 |

Apify is the only line item consuming a metered allowance. Guard rails:
1. Cap `maxItems` per run so a runaway loop cannot burn the month's credits.
2. Alert in Telegram at 70% credit consumption.
3. **Zero-dependency fallback:** if the free tier ever changes, drop Naukri and run
   on ATS APIs + Hirist + Instahyre + CutShort alone via self-hosted Playwright.
   Coverage drops materially — Naukri is the largest Indian source — but cost is
   structurally ₹0 with no third party involved.

### Social media / HR posts — open problem

Officially-posted roles on LinkedIn are the weakest link: LinkedIn is excluded for
ban risk, so HR posts there cannot be monitored safely. No good free substitute
exists yet. Company career pages + Naukri's `companyApplyJob` flag cover most of
the "official" requirement; treat social monitoring as unsolved rather than
pretending it is covered.

---

## Agent pipeline

```
Scout → Scorer → Tailor → Validator → Approver(TG) → Applier → Tracker
                                          ↑              ↓
                                       Watchdog ─────────┘
```

| Agent | Job |
|---|---|
| **Scout** | Cloud port of the Cowork search. 24h freshness rule, no fabricated timestamps. |
| **Scorer** | Dedupe, fit-score vs profile, drop senior/non-AI roles before spending LLM calls. |
| **Tailor** | Resume as structured YAML → tailored per JD → rendered to PDF. Emits before/after diff. |
| **Validator** | **Hard-coded gate, not an LLM.** Rejects any skill keyword outside the whitelist. |
| **Approver** | Telegram card per job: fit score, resume diff, every planned screening answer. |
| **Applier** | Playwright, per-ATS adapters. Screenshots filled form as receipt. |
| **Tracker** | SQLite. Status, history, `/status` and `/pending` commands. |
| **Watchdog** | Heartbeat + retries + failure alerts, so silent death is visible. |

### Non-negotiables

1. **Validator whitelist** — the system must never add LangGraph, Docker, Kubernetes, AWS,
   or Azure to the resume to match a JD. Tailoring ≠ fabrication.
2. **Answer Bank** — screening answers come from a file PJ authored. Never LLM-generated.
   Unknown question → ask in Telegram, save the answer, never guess.
3. **No credentials** — no password entry, ever. Sessions established manually once by PJ;
   bot reuses cookies and pings him when they expire.
4. **No LinkedIn automation** — reading listings only. Automated applying/messaging risks a ban.
5. **Quiet search** — nothing public-facing; never run on a HireTalent device or network.

---

## Call-prep record (the "HR just called" feature)

Every application snapshots, immutably, at submit time:

- **Full JD text** (copied, not linked — postings get deleted before interviews)
- **Exact resume PDF sent** (versions differ per job)
- **Every screening Q&A submitted**
- **CTC quoted + notice period stated** ← the thing you must never contradict on a call
- Applied timestamp, source, ATS, recruiter contact
- Post-submit screenshot

**Telegram UX:** message a company name → 10-second readable card with what you claimed,
what you quoted, JD gaps with honest framing prepared, and buttons for the resume PDF /
full JD / your submitted answers / add a post-call note.

---

## Build phases

| Phase | Deliverable |
|---|---|
| **1** | Oracle box + Telegram bot + SQLite tracker. Listings land in Telegram. No applying. |
| **2** | Resume → YAML. Tailor + Validator. PJ approves diffs, applies manually. |
| **3** | Applier for Greenhouse/Lever/Ashby. Prepare-only, screenshot receipts. |
| **4** | Scout ported to cloud → laptop dependency retired. Naukri/Indeed session apply. |
| **5** | Call-prep records + Telegram briefing cards. |

Each phase is independently useful.

---

## Still needed from PJ

- [ ] Resume file from `D:\Resumes` (master/long version preferred)
- [ ] Telegram bot token + chat ID
- [ ] Oracle Cloud account created (Mumbai or Hyderabad region)
- [ ] Phone number for application forms
- [ ] LinkedIn / GitHub / portfolio URLs
- [ ] MCA college + CGPA/percentage
- [ ] Bachelor's degree, college, year, CGPA
- [ ] Current address as it should appear on forms
- [ ] Current CTC (for forms that ask)

## Known risks

- Oracle ARM capacity is often unavailable in popular regions — may need a retry script.
- Oracle has historically reclaimed idle Always Free instances; keep the box genuinely busy.
- Workday forces per-company account creation — effectively not automatable.
- CAPTCHA-protected forms will fail; these become manual handoffs.
- Gemini free tier trains on submitted data — resume content is exposed.
- Session cookies for Naukri/Indeed expire silently and need periodic manual refresh.

---

## Apify budget allocation (decided 2026-08-06)

The $5/month free credit is shared between the Cowork task and this
pipeline. Measured usage on 2026-08-06 was $0.69 by the 6th — roughly
$4.20/month projected, almost the entire allowance, from Cowork alone.

| System | Frequency | Actor | Cost/mo |
|---|---|---|---|
| Cowork (fallback) | 1×/day | `valig/naukri-jobs-scraper` | ~$1.26 |
| GitHub pipeline | 2×/day | `valig/naukri-jobs-scraper` | ~$2.52 |
| **Total** | | | **~$3.78** |

**LinkedIn is not scraped by either system.** Every available actor costs
$6–10/month on its own — `fantastic-jobs` at $0.16/run, and even
`cheap_scraper` at $0.11/run because it bills a 150-result minimum. No
LinkedIn source fits a zero-cost budget, and it was already excluded for
applying. Company career pages plus Naukri's `companyApplyJob` flag cover
the official-postings requirement without it.

Guard rails:
1. `MAX_ITEMS_PER_SEARCH` caps items per run in `src/naukri.py`.
2. Alert in Telegram at 70% credit consumption (not yet implemented).
3. If the free tier ever changes: drop Naukri, fall back to ATS APIs +
   Hirist + Instahyre via self-hosted Playwright. Coverage drops, cost is
   structurally zero.
