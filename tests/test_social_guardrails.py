import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from src.social_draft import scan_guardrails

cases = [
 ("clean technical post",
  "Spent the week building a resume tailoring pipeline gated by a "
  "deterministic validator — LLMs draft, plain code decides what ships.",
  []),
 ("leaks job search",
  "Open to work and actively looking for AI engineering roles!",
  ["job-search signal"]),
 ("names employer",
  "Big things happening at HireTalent this quarter.",
  ["never mention"]),
 ("claims missing skill",
  "Deployed the whole stack on Kubernetes with Docker.",
  ["not an actual skill"]),
 ("over length",
  "x" * 3500,
  ["over LinkedIn's"]),
]

fails = 0
for name, text, want_substrings in cases:
    flags = scan_guardrails(text)
    joined = " | ".join(flags)
    ok = all(s in joined for s in want_substrings) if want_substrings else not flags
    mark = "PASS" if ok else "FAIL"
    if not ok:
        fails += 1
    print(f"  {mark}  {name:<28} flags={flags}")

print(f"\n{len(cases)-fails}/{len(cases)} passed" + ("" if fails else "  — ALL PASS"))
