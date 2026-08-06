"""The safety-critical test: unknown questions must NOT be answered."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.answers import answer_for, load, missing_required

bank = load()

ANSWERABLE = [
    ("First Name *", "Pratik"),
    ("Email", "pratikjagtapofficial@gmail.com"),
    ("Phone number", "+91 9699504720"),
    ("LinkedIn Profile URL", "https://www.linkedin.com/in/pratikdjagtap/"),
    ("What is your notice period?", "30 days"),
    ("Expected CTC", "4-6 LPA"),
    ("Are you willing to relocate?", "Yes"),
    ("Do you require visa sponsorship?", "No"),
    ("Total years of experience", "1 year"),
    ("How many years of experience do you have with Python?", "2"),
    ("Years of experience with Docker", "0"),
    ("Years of experience in Kubernetes", "0"),
    ("Gender", "Decline to self-identify"),
    ("How did you hear about us?", "Company careers page"),
    ("Current employer", "HireTalent"),
]

# Must return None -> pause and ask PJ. Answering any of these would put
# an unverified claim on a real application.
MUST_ESCALATE = [
    "What is your CGPA?",                      # bank value is null
    "Current CTC",                             # null
    "Street address",                          # null
    "PIN code",                                # null
    "How many years of experience with Rust?", # unknown technology
    "Years of experience with Snowflake",      # unknown technology
    "Why do you want to work at Acme Corp?",   # free text, never canned
    "Describe a time you handled conflict.",   # behavioural
    "What is your favourite programming paradigm?",
    "Do you have a valid passport?",           # not in bank
]

fails = 0
print("=== SHOULD ANSWER ===")
for q, want in ANSWERABLE:
    a = answer_for(q, bank)
    got = a.value if a else None
    ok = got == want
    if not ok:
        fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {q:<52} -> {got!r}")

print("\n=== MUST ESCALATE (answer must be None) ===")
for q in MUST_ESCALATE:
    a = answer_for(q, bank)
    ok = a is None
    if not ok:
        fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {q:<52} -> "
          f"{'escalated' if ok else repr(a.value)}")

print("\n=== BLOCKING GAPS ===")
for m in missing_required(bank):
    print("  -", m)

total = len(ANSWERABLE) + len(MUST_ESCALATE)
print(f"\n{total - fails}/{total}" + ("  ALL PASS" if not fails else "  FAILURES"))
sys.exit(1 if fails else 0)
