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

## Sources

Company career pages & ATS boards (Greenhouse, Lever, Ashby, SmartRecruiters, Workday) →
Naukri, LinkedIn, Indeed, Instahyre, Wellfound, CutShort.
PJ has accounts on all major portals.

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
