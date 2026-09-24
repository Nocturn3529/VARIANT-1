"""Japanese coverage of the shipped SC subset, and Korean Regular/Bold faces."""

from __future__ import annotations

from io import BytesIO

import pypdfium2 as pdfium
from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen.canvas import Canvas

from artifacts.builders import (
    _pdf_font_pool,
    _pdf_glyph_runs,
    build_artifact,
)
from artifacts.validation import validate_payload
from tests.test_pdf_cjk_qualification import (
    _dark_fraction,
    _embedded_truetype_fonts,
    _rendered_character_ink,
)

_JP = "こんにちは"
_KR = "안녕하세요"


def _face_for(character: str, *, bold: bool) -> str:
    codepoint = ord(character)
    for regular, strong, regular_map, bold_map in _pdf_font_pool():
        table = bold_map if bold else regular_map
        if table.get(codepoint):
            return strong if bold else regular
    raise AssertionError(f"no face for U+{codepoint:04X} {character}")


def _filename(font_name: str) -> str:
    return pdfmetrics.getFont(font_name).face.filename.replace("\\", "/").rsplit("/", 1)[-1]


def test_ordinary_japanese_uses_the_shipped_simplified_chinese_subset():
    """The SC subset already contains kana and the shared BMP ideographs."""
    _pdf_font_pool.cache_clear()
    assert _filename(_face_for("こ", bold=False)) == "Variant1CJK-Regular.ttf"
    assert _filename(_face_for("こ", bold=True)) == "Variant1CJK-Bold.ttf"
    assert _filename(_face_for("안", bold=False)) == "Variant1CJKKR-Regular.ttf"
    spec = {"title": _JP, "blocks": [{"type": "paragraph", "text": _JP}]}
    output = build_artifact(spec, "pdf")
    extracted = " ".join(PdfReader(BytesIO(output.payload)).pages[0].extract_text().split())
    assert _JP in extracted
    embedded = _embedded_truetype_fonts(output.payload)
    assert any("CJKSC-Regular" in name for name, _data in embedded)
    assert any("CJKSC-Bold" in name for name, _data in embedded)
    rendered = "".join(character for character, _ink in _rendered_character_ink(output.payload))
    assert rendered.count(_JP) >= 2
    assert all(ink > 0.01 for _character, ink in _rendered_character_ink(output.payload))


def test_korean_hangul_uses_distinct_regular_and_bold_faces():
    _pdf_font_pool.cache_clear()
    regular = pdfmetrics.getFont(_face_for("안", bold=False)).face
    bold = pdfmetrics.getFont(_face_for("안", bold=True)).face
    assert regular.filename != bold.filename
    assert regular.filename.endswith("Variant1CJKKR-Regular.ttf")
    assert bold.filename.endswith("Variant1CJKKR-Bold.ttf")
    spec = {"title": _KR, "blocks": [{"type": "paragraph", "text": _KR + " 123"}]}
    output = build_artifact(spec, "pdf")
    extracted = " ".join(PdfReader(BytesIO(output.payload)).pages[0].extract_text().split())
    assert _KR in extracted
    assert "123" in extracted
    status, findings, metrics = validate_payload("pdf", output.payload, specification=spec)
    assert status == "passed", findings
    assert metrics["missing_text_characters"] == 0
    embedded = _embedded_truetype_fonts(output.payload)
    regular_programs = [data for name, data in embedded if "CJKKR-Regular" in name]
    bold_programs = [data for name, data in embedded if "CJKKR-Bold" in name]
    assert regular_programs and bold_programs
    assert regular_programs[0] != bold_programs[0]
    rendered = "".join(character for character, _ink in _rendered_character_ink(output.payload))
    assert rendered.count(_KR) >= 2
    buffer = BytesIO()
    canvas = Canvas(buffer, invariant=1, pageCompression=0)
    y = 700
    for is_bold in (False, True):
        x = 72
        for font, run in _pdf_glyph_runs(_KR, bold=is_bold):
            canvas.setFont(font, 24)
            canvas.drawString(x, y, run)
            x += pdfmetrics.stringWidth(run, font, 24)
        y -= 120
    canvas.save()
    pdf = pdfium.PdfDocument(buffer.getvalue())
    page = pdf[0]
    image = page.render(scale=3).to_pil().convert("L")
    textpage = page.get_textpage()
    samples: list[tuple[float, float]] = []
    for index in range(textpage.count_chars()):
        character = textpage.get_text_range(index, 1)
        if character not in _KR:
            continue
        box = textpage.get_charbox(index)
        samples.append((box[1], _dark_fraction(image, box, page.get_height(), 3)))
    samples.sort(key=lambda item: item[0], reverse=True)
    lines: list[list[float]] = []
    for baseline, ink in samples:
        if not lines or lines[-1][0] - baseline > 40:
            lines.append([baseline, [ink]])
        else:
            lines[-1][1].append(ink)
            lines[-1][0] = baseline
    lines = [inks for _baseline, inks in lines]
    assert len(lines) >= 2
    regular_fraction = sum(lines[0]) / len(lines[0])
    bold_fraction = sum(lines[1]) / len(lines[1])
    assert regular_fraction > 0.01
    assert bold_fraction > regular_fraction
