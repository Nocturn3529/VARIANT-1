"""Qualify portable Simplified Chinese PDF faces, not a six-character sample."""

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
    bundled_pdf_cjk_font_path,
)
from artifacts.validation import validate_payload


_ORDINARY = "你好世界"
_REPORT = "测试报告 123，。"
_SAMPLE = _ORDINARY + _REPORT + "中文再次检查"


def _cmap(bold: bool) -> dict[int, int]:
    _pdf_font_pool.cache_clear()
    _regular, _bold, regular_map, bold_map = _pdf_font_pool()[0]
    return bold_map if bold else regular_map


def test_portable_face_covers_ordinary_simplified_chinese_not_only_the_old_sample():
    regular = _cmap(False)
    bold = _cmap(True)
    assert bundled_pdf_cjk_font_path() != bundled_pdf_cjk_font_path(bold=True)
    for character in _SAMPLE:
        codepoint = ord(character)
        assert regular.get(codepoint), f"regular missing U+{codepoint:04X} {character}"
        assert bold.get(codepoint), f"bold missing U+{codepoint:04X} {character}"
    assert len(regular) > 1000


def test_bold_latin_and_chinese_use_a_heavier_face_than_regular():
    _pdf_font_pool.cache_clear()
    regular_name, bold_name, _regular_map, _bold_map = _pdf_font_pool()[0]
    regular_face = pdfmetrics.getFont(regular_name).face
    bold_face = pdfmetrics.getFont(bold_name).face
    assert regular_face.filename != bold_face.filename
    assert pdfmetrics.stringWidth("Hello", bold_name, 24) > pdfmetrics.stringWidth(
        "Hello", regular_name, 24
    )
    assert _pdf_glyph_runs("Hello 你好", bold=False)[0][0] == regular_name
    assert _pdf_glyph_runs("Hello 你好", bold=True)[0][0] == bold_name


def _embedded_truetype_fonts(payload: bytes) -> list[tuple[str, bytes]]:
    found = []
    reader = PdfReader(BytesIO(payload))
    for page in reader.pages:
        font_dict = page["/Resources"]["/Font"].get_object()
        for font in font_dict.values():
            item = font.get_object()
            descriptor = item.get("/FontDescriptor")
            if descriptor is None:
                continue
            descriptor = descriptor.get_object()
            stream = descriptor.get("/FontFile2")
            if stream is None:
                continue
            found.append((str(item.get("/BaseFont") or ""), stream.get_data()))
    return found


def _dark_fraction(image, box, page_height: float, scale: float) -> float:
    left, bottom, right, top = box
    crop = image.crop((
        int(left * scale),
        int((page_height - top) * scale),
        int(right * scale) + 1,
        int((page_height - bottom) * scale) + 1,
    ))
    pixels = list(crop.getdata())
    if not pixels:
        return 0.0
    return sum(1 for pixel in pixels if pixel < 200) / len(pixels)


def _rendered_character_ink(payload: bytes) -> list[tuple[str, float]]:
    pdf = pdfium.PdfDocument(payload)
    page = pdf[0]
    scale = 3
    image = page.render(scale=scale).to_pil().convert("L")
    textpage = page.get_textpage()
    found = []
    for index in range(textpage.count_chars()):
        character = textpage.get_text_range(index, 1)
        if not character.strip():
            continue
        found.append((character, _dark_fraction(
            image, textpage.get_charbox(index), page.get_height(), scale,
        )))
    return found


def test_exported_pdf_embeds_both_weights_and_renders_chinese():
    spec = {
        "title": _ORDINARY,
        "blocks": [
            {"type": "paragraph", "text": _ORDINARY},
            {"type": "paragraph", "text": _REPORT},
        ],
    }
    output = build_artifact(spec, "pdf")
    extracted = " ".join(PdfReader(BytesIO(output.payload)).pages[0].extract_text().split())
    assert _ORDINARY in extracted
    assert "测试报告" in extracted
    assert "123" in extracted
    status, findings, metrics = validate_payload("pdf", output.payload, specification=spec)
    assert status == "passed", findings
    assert metrics["missing_text_characters"] == 0

    embedded = _embedded_truetype_fonts(output.payload)
    regular_programs = [data for name, data in embedded if "CJKSC-Regular" in name]
    bold_programs = [data for name, data in embedded if "CJKSC-Bold" in name]
    assert regular_programs and bold_programs
    assert regular_programs[0] != bold_programs[0]
    assert all(b"/FontFile2" in output.payload for _name, _data in embedded)

    rendered = "".join(character for character, _ink in _rendered_character_ink(output.payload))
    assert rendered.count(_ORDINARY) >= 2
    assert "测试报告" in rendered
    assert all(ink > 0.01 for _character, ink in _rendered_character_ink(output.payload))

    _pdf_font_pool()
    buffer = BytesIO()
    canvas = Canvas(buffer, invariant=1, pageCompression=0)
    y = 700
    for bold in (False, True):
        x = 72
        for font, run in _pdf_glyph_runs(_ORDINARY, bold=bold):
            canvas.setFont(font, 24)
            canvas.drawString(x, y, run)
            x += pdfmetrics.stringWidth(run, font, 24)
        y -= 48
    canvas.save()
    pdf = pdfium.PdfDocument(buffer.getvalue())
    page = pdf[0]
    image = page.render(scale=3).to_pil().convert("L")
    textpage = page.get_textpage()
    by_line: dict[int, list[float]] = {}
    for index in range(textpage.count_chars()):
        character = textpage.get_text_range(index, 1)
        if character not in _ORDINARY:
            continue
        box = textpage.get_charbox(index)
        by_line.setdefault(int(box[1] // 20), []).append(_dark_fraction(
            image, box, page.get_height(), 3,
        ))
    lines = [by_line[key] for key in sorted(by_line, reverse=True)]
    assert len(lines) >= 2
    regular_fraction = sum(lines[0]) / len(lines[0])
    bold_fraction = sum(lines[1]) / len(lines[1])
    assert regular_fraction > 0.01
    assert bold_fraction > regular_fraction
