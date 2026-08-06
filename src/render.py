"""Render resume.yml (+ optional tailoring) to an ATS-safe PDF.

ATS parsers extract a linear text stream. Anything that breaks that
stream costs matches, so this deliberately avoids what looks nice and
parses badly:

  * no tables, columns, or text boxes — single linear flow
  * no headers/footers — parsers often drop or misplace them
  * no icons or text-as-image — contact details must be real text
  * standard Helvetica — always embedded and extractable
  * plain "-" bullets rather than glyphs that extract as garbage

ReportLab is used because it is pure Python: no cairo/pango system
libraries, so the same code runs on Windows locally and on the Ubuntu
GitHub Actions runner without extra setup.
"""

from __future__ import annotations

import re
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, HRFlowable, KeepTogether,
                                PageTemplate, Paragraph, Spacer)

INK = HexColor("#111111")
MUTED = HexColor("#444444")
RULE = HexColor("#999999")

# Tightened progressively if the document spills past one page.
DENSITY_STEPS = [
    {"body": 9.6, "lead": 12.2, "gap": 3.4, "sec": 10.4},
    {"body": 9.2, "lead": 11.5, "gap": 2.8, "sec": 10.0},
    {"body": 8.8, "lead": 10.9, "gap": 2.2, "sec": 9.6},
    {"body": 8.4, "lead": 10.3, "gap": 1.7, "sec": 9.2},
]


def _styles(d: dict) -> dict:
    base = dict(fontName="Helvetica", textColor=INK, leading=d["lead"])
    return {
        "name": ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=17,
                               leading=19.5, textColor=INK, spaceAfter=1.5),
        "headline": ParagraphStyle("headline", fontName="Helvetica",
                                   fontSize=d["body"] + 0.6,
                                   leading=d["lead"], textColor=MUTED,
                                   spaceAfter=2),
        "contact": ParagraphStyle("contact", fontName="Helvetica",
                                  fontSize=d["body"] - 0.3,
                                  leading=d["lead"] - 1, textColor=MUTED),
        "section": ParagraphStyle("section", fontName="Helvetica-Bold",
                                  fontSize=d["sec"], leading=d["sec"] + 2,
                                  textColor=INK, spaceBefore=d["gap"] + 2.5,
                                  spaceAfter=1.5),
        "body": ParagraphStyle("body", fontSize=d["body"], **base),
        "summary": ParagraphStyle("summary", fontSize=d["body"],
                                  alignment=TA_JUSTIFY, **base),
        "role": ParagraphStyle("role", fontName="Helvetica-Bold",
                               fontSize=d["body"] + 0.2, leading=d["lead"],
                               textColor=INK, spaceBefore=d["gap"]),
        "meta": ParagraphStyle("meta", fontName="Helvetica-Oblique",
                               fontSize=d["body"] - 0.6,
                               leading=d["lead"] - 1.5, textColor=MUTED,
                               spaceAfter=1),
        "bullet": ParagraphStyle("bullet", fontSize=d["body"],
                                 leftIndent=8, bulletIndent=0,
                                 spaceAfter=0.8, **base),
    }


def _esc(s) -> str:
    s = "" if s is None else str(s)
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return " ".join(s.split())


def _period(a, b) -> str:
    def fmt(v):
        if v is None:
            return ""
        v = str(v)
        if v.lower() in ("present", "current"):
            return "Present"
        m = re.match(r"(\d{4})-(\d{2})", v)
        if m:
            months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            return f"{months[int(m.group(2))]} {m.group(1)}"
        return v
    return " - ".join(x for x in (fmt(a), fmt(b)) if x)


def _bullet_text(resume: dict, bid: str, tailored: dict | None) -> str | None:
    """Tailored wording when present, else the original. Never invents."""
    if tailored:
        for b in tailored.get("bullets") or []:
            if b.get("id") == bid:
                return str(b.get("text") or "")
    for section in ("experience", "projects"):
        for entry in resume.get(section) or []:
            for b in entry.get("bullets") or []:
                if b.get("id") == bid:
                    return str(b.get("text") or "")
    return None


def _build(resume: dict, tailored: dict | None, d: dict) -> list:
    st = _styles(d)
    flow: list = []
    c = resume["contact"]

    flow.append(Paragraph(_esc(c["name"]), st["name"]))

    headline = (tailored or {}).get("headline") or resume["headline"]["default"]
    flow.append(Paragraph(_esc(headline), st["headline"]))

    bits = [c.get("location"), c.get("phone"), c.get("email"),
            c.get("linkedin"), c.get("github")]
    # Contact details as plain text, never icons — parsers cannot read
    # glyphs, and a missed phone number costs a callback.
    flow.append(Paragraph(
        " | ".join(_esc(b) for b in bits if b), st["contact"]))
    flow.append(Spacer(1, d["gap"]))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=RULE,
                           spaceAfter=d["gap"]))

    summary = (tailored or {}).get("summary") or resume["summary"]["default"]
    if summary:
        flow.append(Paragraph("PROFESSIONAL SUMMARY", st["section"]))
        flow.append(Paragraph(_esc(summary), st["summary"]))

    # Skills, in the tailored group order when supplied.
    groups = {g["name"]: g["items"] for g in resume["skills"]["groups"]}
    order = [n for n in ((tailored or {}).get("skill_order") or [])
             if n in groups]
    order += [n for n in groups if n not in order]
    if order:
        flow.append(Paragraph("TECHNICAL SKILLS", st["section"]))
        for name in order:
            items = ", ".join(_esc(i) for i in groups[name])
            flow.append(Paragraph(
                f"<b>{_esc(name)}:</b> {items}", st["body"]))

    def entries(section: str, heading: str) -> None:
        rows = resume.get(section) or []
        if not rows:
            return
        flow.append(Paragraph(heading, st["section"]))
        for entry in rows:
            block = []
            if section == "experience":
                title = f"{_esc(entry['role'])} | {_esc(entry['company'])}"
            else:
                title = _esc(entry.get("name", ""))
            period = _period(entry.get("from"), entry.get("to"))
            block.append(Paragraph(title, st["role"]))
            if period:
                block.append(Paragraph(_esc(period), st["meta"]))
            for b in entry.get("bullets") or []:
                txt = _bullet_text(resume, b.get("id", ""), tailored)
                if txt:
                    block.append(Paragraph(_esc(txt), st["bullet"],
                                           bulletText="-"))
            # Keep a role and its first lines together across a break.
            flow.append(KeepTogether(block))

    entries("experience", "PROFESSIONAL EXPERIENCE")
    entries("projects", "PROJECTS")

    edu = (resume.get("education") or {}).get("items") or []
    if edu:
        flow.append(Paragraph("EDUCATION", st["section"]))
        for e in edu:
            line = f"<b>{_esc(e['degree'])}</b>, {_esc(e['institution'])}"
            period = _period(e.get("from"), e.get("to"))
            if e.get("score"):
                line += f" — {_esc(e['score'])}"
            if period:
                line += f" ({_esc(period)})"
            flow.append(Paragraph(line, st["body"]))
            # An unawarded degree must say so on the page itself. A date
            # range alone reads as "completed", which would be false.
            if e.get("note"):
                flow.append(Paragraph(_esc(e["note"]), st["meta"]))

    certs = (resume.get("certifications") or {}).get("items") or []
    if certs:
        flow.append(Paragraph("CERTIFICATIONS", st["section"]))
        flow.append(Paragraph(" | ".join(_esc(x) for x in certs), st["body"]))

    ach = (resume.get("achievements") or {}).get("items") or []
    if ach:
        flow.append(Paragraph("ACHIEVEMENTS", st["section"]))
        flow.append(Paragraph(" | ".join(_esc(x) for x in ach), st["body"]))

    return flow


class _Counter(BaseDocTemplate):
    """Counts pages so density can be retried without writing a file."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.page_count = 0

    def afterPage(self):
        self.page_count += 1


def render(resume: dict, out_path: str | Path,
           tailored: dict | None = None,
           target_pages: int = 1) -> tuple[Path, int]:
    """Write the PDF. -> (path, pages).

    Tries progressively tighter density until it fits target_pages. A
    fresher resume running to two pages reads badly, but silently
    shrinking to unreadable is worse — so the steps are bounded and the
    real page count is returned for the caller to report.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    pages = 99
    for step in DENSITY_STEPS:
        doc = _Counter(
            str(out), pagesize=A4,
            leftMargin=14 * mm, rightMargin=14 * mm,
            topMargin=11 * mm, bottomMargin=11 * mm,
            title=f"{resume['contact']['name']} - Resume",
            author=resume["contact"]["name"],
            subject=(tailored or {}).get("headline")
                    or resume["headline"]["default"],
        )
        frame = Frame(doc.leftMargin, doc.bottomMargin,
                      doc.width, doc.height, id="body",
                      leftPadding=0, rightPadding=0,
                      topPadding=0, bottomPadding=0)
        doc.addPageTemplates([PageTemplate(id="main", frames=[frame])])
        doc.build(_build(resume, tailored, step))
        pages = doc.page_count
        if pages <= target_pages:
            break

    return out, pages
