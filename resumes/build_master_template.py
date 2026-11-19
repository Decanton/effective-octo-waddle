"""Builds resumes/master_cv_template.docx: the ONE master CV template.

The template holds the page setup and the named paragraph styles every
tailored CV uses (CV Name, CV Contact, CV Heading, CV Role, CV Company,
CV Body, CV Bullet). The tailor opens this file, keeps its styles and page
setup, and writes content into it, so every CV has the same ATS-safe,
single-column look.

You can open the .docx in Word and change fonts, sizes or spacing of those
styles. The tailor will follow your changes. Do not rename the styles.

Run:  python resumes/build_master_template.py
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = HERE / "master_cv_template.docx"

FONT = "Calibri"
DARK = RGBColor(0x1F, 0x1F, 0x1F)
ACCENT = RGBColor(0x1F, 0x3A, 0x5F)
TEXT_WIDTH_CM = 21.0 - 1.8 * 2  # A4 width minus left/right margins


def _para_style(doc, name, size, bold=False, italic=False, color=DARK,
                before=0, after=2, align=None, line=1.0):
    styles = doc.styles
    style = styles[name] if name in [s.name for s in styles] else styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
    style.base_style = styles["Normal"]
    f = style.font
    f.name, f.size, f.bold, f.italic, f.color.rgb = FONT, Pt(size), bold, italic, color
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(attr), FONT)
    pf = style.paragraph_format
    pf.space_before, pf.space_after, pf.line_spacing = Pt(before), Pt(after), line
    if align is not None:
        pf.alignment = align
    return style


def _bottom_border(style):
    ppr = style.element.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for k, v in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "1F3A5F")):
        bottom.set(qn(k), v)
    pbdr.append(bottom)
    ppr.append(pbdr)


def build(path: Path = TEMPLATE_PATH) -> Path:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(1.8)
    sec.top_margin = sec.bottom_margin = Cm(1.5)

    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = FONT, Pt(10.5)

    _para_style(doc, "CV Name", 20, bold=True, color=ACCENT, after=2, align=WD_ALIGN_PARAGRAPH.CENTER)
    _para_style(doc, "CV Headline", 11, bold=True, after=2, align=WD_ALIGN_PARAGRAPH.CENTER)
    _para_style(doc, "CV Contact", 9.5, after=1, align=WD_ALIGN_PARAGRAPH.CENTER)
    h = _para_style(doc, "CV Heading", 11.5, bold=True, color=ACCENT, before=10, after=4)
    _bottom_border(h)
    h.paragraph_format.keep_with_next = True
    role = _para_style(doc, "CV Role", 10.5, bold=True, before=6, after=0)
    role.paragraph_format.keep_with_next = True
    role.paragraph_format.tab_stops.add_tab_stop(Cm(TEXT_WIDTH_CM), WD_TAB_ALIGNMENT.RIGHT)
    comp = _para_style(doc, "CV Company", 10.5, italic=True, after=2)
    comp.paragraph_format.keep_with_next = True
    comp.paragraph_format.tab_stops.add_tab_stop(Cm(TEXT_WIDTH_CM), WD_TAB_ALIGNMENT.RIGHT)
    _para_style(doc, "CV Body", 10.5, after=2, line=1.05, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
    bullet = _para_style(doc, "CV Bullet", 10.5, after=1, line=1.05)
    bullet.paragraph_format.left_indent = Cm(0.5)
    bullet.paragraph_format.first_line_indent = Cm(-0.35)

    # Sample content so the template is readable when opened in Word.
    doc.add_paragraph("FULL NAME", style="CV Name")
    doc.add_paragraph("Headline", style="CV Headline")
    doc.add_paragraph("City, Country | Phone | Email", style="CV Contact")
    doc.add_paragraph("PROFESSIONAL SUMMARY", style="CV Heading")
    doc.add_paragraph("Summary text.", style="CV Body")
    doc.add_paragraph("WORK EXPERIENCE", style="CV Heading")
    doc.add_paragraph("Job Title\tMM/YYYY – Present", style="CV Role")
    doc.add_paragraph("Company, Location", style="CV Company")
    doc.add_paragraph("•\tAchievement bullet.", style="CV Bullet")

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


if __name__ == "__main__":
    print(f"Template written to {build()}")
