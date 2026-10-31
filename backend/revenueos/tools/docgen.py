"""Render proposal content into a .pptx pitch deck and a .docx proposal.

Deliberately plain, editable layouts: reps will tweak them, so no locked-down template.
Drop your own template in later by loading it with Presentation(path) / Document(path).
"""
import re
from pathlib import Path

from docx import Document
from docx.shared import Pt, RGBColor
from pptx import Presentation
from pptx.dml.color import RGBColor as PptxRGB
from pptx.util import Inches, Pt as PptxPt

from ..config import settings


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:60] or "document"


def build_deck(path: Path, title: str, subtitle: str, slides: list[dict]) -> Path:
    """slides: [{title, bullets: [str], speaker_notes?: str}]"""
    brand = PptxRGB(*_rgb(settings.brand_color))
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)

    cover = prs.slides.add_slide(prs.slide_layouts[0])
    cover.shapes.title.text = title
    cover.placeholders[1].text = subtitle
    cover.shapes.title.text_frame.paragraphs[0].runs[0].font.color.rgb = brand

    for s in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = s["title"]
        slide.shapes.title.text_frame.paragraphs[0].runs[0].font.color.rgb = brand
        slide.shapes.title.text_frame.paragraphs[0].runs[0].font.size = PptxPt(32)
        body = slide.placeholders[1]
        body.left, body.top, body.width, body.height = Inches(0.8), Inches(1.6), Inches(11.7), Inches(5.3)
        tf = body.text_frame
        tf.clear()
        for i, bullet in enumerate(s.get("bullets", [])):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = bullet
            p.font.size = PptxPt(20)
        if s.get("speaker_notes"):
            slide.notes_slide.notes_text_frame.text = s["speaker_notes"]

    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(path)
    return path


def build_proposal_docx(path: Path, title: str, subtitle: str, sections: list[dict]) -> Path:
    """sections: [{heading, paragraphs: [str], bullets: [str]}]"""
    brand = RGBColor(*_rgb(settings.brand_color))
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)

    h = doc.add_heading(title, level=0)
    for run in h.runs:
        run.font.color.rgb = brand
    doc.add_paragraph(subtitle).italic = True

    for sec in sections:
        heading = doc.add_heading(sec["heading"], level=1)
        for run in heading.runs:
            run.font.color.rgb = brand
        for para in sec.get("paragraphs", []):
            doc.add_paragraph(para)
        for bullet in sec.get("bullets", []):
            doc.add_paragraph(bullet, style="List Bullet")

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path
