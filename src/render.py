"""Render resume.yml (+ optional tailoring) to a PDF matching PJ's own
template layout: Pratik_Jagtap_Resume_Jr_AIML_Engineer.pdf (base changed
2026-08-07 from the earlier Agentic-AI-framed resume, per PJ).

Only the WORDS are tailored per job — the layout is fixed and must match
what PJ already uses, not a layout this renderer invented. Verified
against the source PDF directly:

  * centered header block: uppercase name, bold black headline (not
    blue — the prior template's headline was blue, this one is not),
    contact line with email/LinkedIn/GitHub all as blue hyperlinks
  * black, bold section headings, each followed by a thin blue rule
    spanning the full width (heading text itself is black on this
    template, only the rule is blue)
  * role/company left + date range right on one line, dates italic,
    en dash ("–") throughout date ranges and title separators
  * small bullet "•" (this template's glyph — the prior one used "●";
    both confirmed ATS-safe by extracting the respective source PDF's
    own bullets with no corruption)

Everything remains linear, extractable text — no tables used for content,
only for the left/right date-alignment trick, which produces the same
text stream a single paragraph would.
"""

from __future__ import annotations

import re
from pathlib import Path

from reportlab.lib.colors import HexColor, black
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, HRFlowable, KeepTogether,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)

# Used ONLY for hyperlink text (email/LinkedIn/GitHub, via link() below).
# Everything else on the page is INK -- see _ruled_heading()'s comment.
LINK_BLUE = "#1155CC"
INK = black

DENSITY_STEPS = [
    {"body": 10.0, "lead": 13.5, "gap": 5.0, "sec": 11.5},
    {"body": 9.5, "lead": 12.8, "gap": 4.2, "sec": 11.0},
    {"body": 9.0, "lead": 12.1, "gap": 3.4, "sec": 10.5},
    {"body": 8.5, "lead": 11.4, "gap": 2.6, "sec": 10.0},
]


def _styles(d: dict) -> dict:
    base = dict(fontName="Helvetica", textColor=INK, leading=d["lead"])
    return {
        "name": ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=18,
                               leading=20, textColor=INK, alignment=TA_CENTER,
                               spaceAfter=2),
        "headline": ParagraphStyle("headline", fontName="Helvetica-Bold",
                                   fontSize=d["body"] + 1.2, leading=d["lead"],
                                   textColor=INK, alignment=TA_CENTER,
                                   spaceAfter=2),
        "contact": ParagraphStyle("contact", fontName="Helvetica",
                                  fontSize=d["body"] - 0.2, leading=d["lead"] - 1,
                                  textColor=INK, alignment=TA_CENTER),
        "section": ParagraphStyle("section", fontName="Helvetica-Bold",
                                  fontSize=d["sec"], leading=d["sec"] + 2,
                                  textColor=INK, spaceBefore=d["gap"] + 2,
                                  spaceAfter=1, alignment=TA_LEFT),
        "body": ParagraphStyle("body", fontSize=d["body"], **base),
        "summary": ParagraphStyle("summary", fontSize=d["body"],
                                  alignment=TA_JUSTIFY, **base),
        "role": ParagraphStyle("role", fontName="Helvetica-Bold",
                               fontSize=d["body"] + 0.3, leading=d["lead"],
                               textColor=INK),
        "date": ParagraphStyle("date", fontName="Helvetica-Oblique",
                               fontSize=d["body"] - 0.3, leading=d["lead"],
                               textColor=INK, alignment=TA_LEFT),
        "bullet": ParagraphStyle("bullet", fontSize=d["body"], leftIndent=12,
                                 bulletIndent=0, spaceAfter=1.2,
                                 bulletFontSize=d["body"] * 0.55, **base),
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
        if v.lower() == "pursuing":
            return "Pursuing"
        m = re.match(r"(\d{4})-(\d{2})", v)
        if m:
            months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            return f"{months[int(m.group(2))]} {m.group(1)}"
        return v
    # En dash, matching the source template's date-range punctuation.
    return " – ".join(x for x in (fmt(a), fmt(b)) if x)


def _bullet_text(resume: dict, bid: str, tailored: dict | None) -> str | None:
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


def _row(left_flow, right_text: str, st: dict, width: float):
    """Title/company on the left, italic date range flush right — the
    layout the source template uses for every entry heading line.

    VALIGN is TOP, not BOTTOM: when the left cell wraps to a second line
    (a long institution name in Education, mainly), a bottom-aligned date
    sits next to that wrapped second line instead of the first line it
    actually belongs to — confirmed live, this put the date under a
    lone-hanging "Pune" instead of next to "Bachelor of Computer
    Applications (BCA), ...". Top-aligning pins the date to the first
    line every time, matching how dates sit on a normal resume.
    """
    right = Paragraph(_esc(right_text), st["date"]) if right_text else Paragraph("", st["date"])
    t = Table([[left_flow, right]], colWidths=[width * 0.72, width * 0.28])
    t.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    return t


def _ruled_heading(text: str, st: dict) -> list:
    # Rule is black, not blue, per PJ (2026-08-11): blue was reserved for
    # actual hyperlink text (email/LinkedIn/GitHub via link()) only —
    # everything else on the page, including these section rules, is black.
    return [
        Paragraph(text.upper(), st["section"]),
        HRFlowable(width="100%", thickness=0.8, color=INK, spaceAfter=3,
                  spaceBefore=0),
    ]


def _build(resume: dict, tailored: dict | None, d: dict, width: float) -> list:
    st = _styles(d)
    flow: list = []
    c = resume["contact"]

    flow.append(Paragraph(_esc(c["name"]).upper(), st["name"]))

    headline = (tailored or {}).get("headline") or resume["headline"]["default"]
    flow.append(Paragraph(_esc(headline), st["headline"]))

    def link(label, url):
        return f'<link href="{_esc(url)}" color="{LINK_BLUE}"><u>{_esc(label)}</u></link>'

    bits = [c.get("location"), c.get("phone")]
    bits = [_esc(b) for b in bits if b]
    if c.get("email"):
        bits.append(link(c["email"], f'mailto:{c["email"]}'))
    if c.get("linkedin"):
        bits.append(link("LinkedIn", c["linkedin"]))
    if c.get("github"):
        bits.append(link("GitHub", c["github"]))
    flow.append(Paragraph(" | ".join(bits), st["contact"]))
    flow.append(Spacer(1, d["gap"]))

    summary = (tailored or {}).get("summary") or resume["summary"]["default"]
    if summary:
        flow += _ruled_heading("Professional Summary", st)
        flow.append(Paragraph(_esc(summary), st["summary"]))

    groups = {g["name"]: g["items"] for g in resume["skills"]["groups"]}
    order = [n for n in ((tailored or {}).get("skill_order") or []) if n in groups]
    order += [n for n in groups if n not in order]
    if order:
        flow += _ruled_heading("Technical Skills", st)
        for name in order:
            items = ", ".join(_esc(i) for i in groups[name])
            flow.append(Paragraph(f"<b>{_esc(name)}:</b> {items}", st["body"]))

    def entries(section: str, heading: str) -> None:
        rows = resume.get(section) or []
        if not rows:
            return
        flow.extend(_ruled_heading(heading, st))
        for entry in rows:
            if section == "experience":
                left_text = (f'<b>{_esc(entry["role"])}</b>  |  '
                            f'{_esc(entry["company"])}'
                            + (f' – {_esc(entry["company_note"])}'
                               if entry.get("company_note") else ""))
            else:
                left_text = f'<b>{_esc(entry.get("name", ""))}</b>'
            left = Paragraph(left_text, st["role"])
            period = _period(entry.get("from"), entry.get("to"))

            block = [_row(left, period, st, width)]
            for b in entry.get("bullets") or []:
                txt = _bullet_text(resume, b.get("id", ""), tailored)
                if txt:
                    # "•" (U+2022) was tested against this template and
                    # rejected: it extracts as a control character (\x7f)
                    # from ReportLab's base-14 Helvetica, confirmed
                    # independent of the bulletText mechanism -- it breaks
                    # even typed directly into the paragraph text. A real
                    # TrueType font fixes it, but Arial can't be bundled
                    # into the repo (redistribution licensing) and isn't
                    # present on the Ubuntu Actions runner, so that fix
                    # isn't portable. "●" is confirmed clean on this exact
                    # stack, which the ATS-passing requirement outweighs
                    # over an exact bullet-glyph match.
                    block.append(Paragraph(_esc(txt), st["bullet"], bulletText="●"))
            flow.append(KeepTogether(block))

    entries("experience", "Professional Experience")
    entries("projects", "Projects")

    edu = (resume.get("education") or {}).get("items") or []
    if edu:
        flow += _ruled_heading("Education", st)
        for e in edu:
            # Score/CGPA is intentionally never rendered here, per PJ:
            # his own source resume's education section does not show
            # it, so the tailored PDF should not either. It still lives
            # in answers.yml for application forms that explicitly ask
            # for a CGPA -- this only controls what appears on the page.
            left = Paragraph(
                f'<b>{_esc(e["degree"])}</b>, {_esc(e["institution"])}',
                st["body"])
            period = _period(e.get("from"), e.get("to"))
            flow.append(_row(left, period, st, width))

    certs = (resume.get("certifications") or {}).get("items") or []
    if certs:
        flow += _ruled_heading("Certifications", st)
        flow.append(Paragraph(" | ".join(_esc(x) for x in certs), st["body"]))

    ach = (resume.get("achievements") or {}).get("items") or []
    if ach:
        flow += _ruled_heading("Achievements", st)
        flow.append(Paragraph(" | ".join(_esc(x) for x in ach), st["body"]))

    return flow


class _Counter(BaseDocTemplate):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.page_count = 0

    def afterPage(self):
        self.page_count += 1


def render(resume: dict, out_path: str | Path,
           tailored: dict | None = None,
           target_pages: int = 1) -> tuple[Path, int]:
    """Write the PDF, matching PJ's own template layout. -> (path, pages)."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    left_margin = right_margin = 16 * mm
    content_width = A4[0] - left_margin - right_margin

    pages = 99
    for step in DENSITY_STEPS:
        doc = _Counter(
            str(out), pagesize=A4,
            leftMargin=left_margin, rightMargin=right_margin,
            topMargin=12 * mm, bottomMargin=11 * mm,
            title=f"{resume['contact']['name']} - Resume",
            author=resume["contact"]["name"],
            subject=(tailored or {}).get("headline") or resume["headline"]["default"],
        )
        frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                      id="body", leftPadding=0, rightPadding=0,
                      topPadding=0, bottomPadding=0)
        doc.addPageTemplates([PageTemplate(id="main", frames=[frame])])
        doc.build(_build(resume, tailored, step, content_width))
        pages = doc.page_count
        if pages <= target_pages:
            break

    return out, pages
