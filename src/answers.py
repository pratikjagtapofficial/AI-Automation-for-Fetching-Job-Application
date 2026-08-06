"""Answer Bank lookup.

Maps a form question to an answer PJ authored. Deterministic pattern
matching — deliberately not an LLM.

The whole safety property of Phase 3 rests here: an unanswerable question
must return UNKNOWN so the application pauses and PJ is asked, rather than
producing something plausible. A model asked "years of Kubernetes
experience?" returns a number; that number goes to a real employer under
PJ's name. So this returns None instead, every time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ANSWERS_PATH = ROOT / "config" / "answers.yml"


@dataclass
class Answer:
    value: str
    source: str          # which bank entry produced it
    confident: bool = True


def load(path: Path | None = None) -> dict:
    return yaml.safe_load(
        (path or ANSWERS_PATH).read_text(encoding="utf-8")) or {}


def _norm(q: str) -> str:
    q = (q or "").lower()
    q = re.sub(r"[*∗]", "", q)            # required-field markers
    q = re.sub(r"[^a-z0-9+/ ]", " ", q)
    return " ".join(q.split())


# A label is either a FIELD ("Email", "Phone") or a QUESTION ("Are you
# subject to any employment agreements with your current employer?").
#
# They must not share a rule table. Broad field patterns fire inside long
# questions and answer something else entirely: on a real GitLab form,
# "current employer" inside a restrictive-covenants question filled
# "HireTalent" into a yes/no field, and a sponsorship question matched a
# location pattern and filled "Pune". Both would have been false
# statements to an employer.
_INTERROGATIVE = re.compile(
    r"^\s*(are|is|do|does|did|have|has|had|will|would|can|could|should|may|"
    r"were|was|which|what|why|how|when|where|who|please|describe|tell|"
    r"if you|in your)\b", re.I)


def _is_question(label: str) -> bool:
    l = (label or "").strip()
    if not l:
        return False
    return bool(l.endswith("?") or _INTERROGATIVE.match(l) or len(l) > 70)


# Applied ONLY to short field labels, never to questions.
FIELD_RULES: list[tuple[str, str]] = [
    (r"\bfirst name\b|\bgiven name\b",                "identity.first_name"),
    (r"\blast name\b|\bsurname\b|\bfamily name\b",    "identity.last_name"),
    (r"\bfull name\b|^name$|\byour name\b",           "identity.full_name"),
    (r"\be-?mail\b",                                  "identity.email"),
    (r"\bphone\b|\bmobile\b|\bcontact number\b",      "identity.phone"),
    (r"\blinkedin\b",                                 "identity.linkedin"),
    (r"\bgithub\b",                                   "identity.github"),
    (r"\bportfolio\b|\bpersonal website\b",           "identity.portfolio"),
    (r"\bpostal code\b|\bzip\b|\bpin ?code\b",        "identity.postal_code"),
    (r"\bstreet\b|\baddress line\b|\bfull address\b", "identity.street_address"),
    (r"\bcity\b|\bcurrent location\b|\blocation\b",   "identity.city"),
    (r"\bstate\b|\bprovince\b",                       "identity.state"),
    (r"\bcountry\b",                                  "identity.country"),
    (r"\bgender\b",                            "demographics.gender"),
    (r"\bethnic|\brace\b",                     "demographics.ethnicity"),
    (r"\bveteran\b",                           "demographics.veteran_status"),
    (r"\bdisabilit",                           "demographics.disability_status"),
    (r"\bcurrent (employer|company|organi)",   "experience.current_employer"),
    (r"\bcurrent (title|designation|role)\b",  "experience.current_title"),
    (r"\bnotice period\b",                     "availability.notice_period_text"),
    (r"\bcurrent (ctc|salary|compensation)\b|\bpresent ctc\b",
                                               "compensation.current_ctc_lpa"),
    (r"\bexpected (ctc|salary|compensation)\b", "compensation.expected_text"),
    (r"\bhighest (qualification|degree|education)\b",
                                               "education.highest_degree"),
    # Points at the BCA: it is the awarded degree, so 7.78 is the honest
    # answer. The MCA has no final score — the degree is not conferred.
    (r"\b(cgpa|gpa|percentage|marks|aggregate)\b", "education.bca_score"),
    (r"\bgraduation year\b|\byear of (passing|graduation)\b",
                                               "education.graduation_year"),
    (r"\buniversity\b|\bcollege\b|\binstitut",  "education.mca_institution"),
    (r"\b(total|overall).*(experience|exp)\b|^years of experience",
                                               "experience.total_years_text"),
]

# Applied to full questions. Every pattern must be specific enough that it
# cannot match a question about something else — these run against long
# free-text sentences.
QUESTION_RULES: list[tuple[str, str]] = [
    (r"require sponsorship|need sponsorship|require.*visa sponsor|"
     r"\bsponsorship for (employment|work)\b",
                                              "work_authorization.requires_sponsorship"),
    (r"authori[sz]ed to work.*\bindia\b|legally.*work.*\bindia\b|"
     r"eligible to work.*\bindia\b",
                                    "work_authorization.authorized_to_work_in_india"),
    (r"authori[sz]ed to work.*(united states|\busa?\b)",
                                              "work_authorization.authorized_us"),

    # A verified fact, so answering is honest. Location *preference*
    # questions ("Are you currently located in Bangalore?") deliberately
    # stay unmatched — those are screening criteria, not identity.
    (r"country of residence|country (you are|you're) (located|residing)",
                                              "identity.country"),
    (r"\bnotice period\b",                    "availability.notice_period_text"),
    (r"earliest.*(start|join)|when can you (start|join)|"
     r"available.*(to )?start|start date",     "availability.earliest_start"),
    (r"willing to relocate|open to relocat|able to relocate",
                                              "availability.willing_to_relocate"),
    (r"currently employed",                   "availability.currently_employed"),
    (r"open to remote|comfortable.*remote|willing.*work remote",
                                              "availability.open_to_remote"),

    (r"current (ctc|salary|compensation)|present ctc",
                                              "compensation.current_ctc_lpa"),
    (r"expected (ctc|salary|compensation)|salary expectation|"
     r"desired (salary|compensation)|compensation expectation",
                                              "compensation.expected_text"),

    (r"\b(cgpa|gpa)\b|what.*(percentage|aggregate).*(marks|score)",
                                              "education.bca_score"),
    (r"year of (passing|graduation)|graduation year",
                                              "education.graduation_year"),
    (r"highest (qualification|degree|level of education)",
                                              "education.highest_degree"),

    (r"(total|overall|how many).*(years|yrs).*(experience|exp)\b",
                                              "experience.total_years_text"),
    (r"reason for (change|leaving|looking)|why are you looking",
                                              "experience.reason_for_change"),

    (r"how did you (hear|find|learn) (about|of)",  "misc.how_did_you_hear"),
    (r"background check",       "misc.willing_to_undergo_background_check"),
    (r"\b(convicted|criminal record)\b",          "misc.criminal_record"),
]

# Shape of an experience question at all.
_EXP_SHAPE = re.compile(r"\b(years?|yrs?|experience|exp)\b", re.I)

# Words meaning "experience overall" rather than with a named technology.
_GENERIC_EXP = (
    "total", "overall", "professional", "work", "working", "relevant",
    "industry", "it", "software", "combined", "cumulative", "full time",
)

# "...with X", "...in X", "...using X" -> the question names something
# specific, so answering with total experience would answer a different
# question than the one asked.
_NAMES_SOMETHING = re.compile(
    r"\b(with|using|in|on)\s+([A-Za-z][A-Za-z0-9 +#./_-]{1,40})", re.I)


def _dig(bank: dict, path: str):
    cur = bank
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _fmt(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return str(v)


ESCALATE = object()   # sentinel: must ask PJ, must not fall through


def _skill_years(question: str, bank: dict):
    """-> Answer | ESCALATE | None.

    Scans the WHOLE question for a known skill rather than extracting a
    substring: a fixed-width capture truncated "…have with Python?" to
    "py" and silently missed a skill PJ actually has.

    Returns ESCALATE when the question asks about experience with some
    *named* thing that is not in the bank. Falling through to the generic
    "years of experience" rule would answer "1 year" to "years of
    experience with Rust?" — answering a different question than the one
    asked, on a real application.
    """
    if not _EXP_SHAPE.search(question):
        return None

    low = question.lower()
    table = bank.get("skill_years") or {}
    for skill, years in table.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(skill.lower())}(?![a-z0-9])",
                     low):
            return Answer(str(years), f"skill_years.{skill}")

    m = _NAMES_SOMETHING.search(question)
    if m:
        named = m.group(2).strip().rstrip(".?:").lower()
        # "total years of experience in software" is generic, not a named
        # technology, so it may safely fall through.
        if named and not any(g == named or named.startswith(g + " ")
                             or g in named.split()
                             for g in _GENERIC_EXP):
            return ESCALATE
    return None


def _learned(question: str, bank: dict) -> Answer | None:
    qn = _norm(question)
    for item in bank.get("learned") or []:
        if _norm(str(item.get("question", ""))) == qn:
            return Answer(str(item.get("answer", "")), "learned")
    return None


def answer_for(question: str, bank: dict | None = None) -> Answer | None:
    """-> Answer, or None when PJ must be asked. Never guesses."""
    bank = bank if bank is not None else load()
    if not question or not question.strip():
        return None

    if (a := _learned(question, bank)):
        return a

    sk = _skill_years(question, bank)
    if sk is ESCALATE:
        return None
    if sk is not None:
        return sk

    q = _norm(question)
    # Questions never consult FIELD_RULES: those patterns are broad by
    # design and would match the wrong clause inside a long sentence.
    table = QUESTION_RULES if _is_question(question) else (
        FIELD_RULES + QUESTION_RULES)
    for pattern, path in table:
        if re.search(pattern, q):
            val = _fmt(_dig(bank, path))
            if val is None:
                # Rule matched but the bank entry is null — genuinely
                # unknown, so escalate rather than fall through to a
                # looser rule that would answer the wrong thing.
                return None
            return Answer(val, path)
    return None


def remember(question: str, value: str, path: Path | None = None) -> None:
    """Append a PJ-supplied answer so the same question is never re-asked."""
    p = path or ANSWERS_PATH
    bank = load(p)
    bank.setdefault("learned", []).append(
        {"question": question.strip(), "answer": value.strip()})
    p.write_text(yaml.safe_dump(bank, sort_keys=False, allow_unicode=True),
                 encoding="utf-8")


def missing_required(bank: dict | None = None) -> list[str]:
    """Bank entries that are null and will block real applications."""
    bank = bank if bank is not None else load()
    required = [
        ("identity.street_address", "postal address"),
        ("identity.postal_code", "PIN code"),
        ("education.bca_score", "BCA CGPA/percentage"),
        ("compensation.current_ctc_lpa", "current CTC"),
    ]
    return [label for path, label in required if _dig(bank, path) is None]
