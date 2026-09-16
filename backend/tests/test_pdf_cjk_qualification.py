"""Qualify the bundled TrueType CJK face for portable PDF export."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from artifacts.builders import (
    _pdf_font_pool,
    build_artifact,
    bundled_pdf_cjk_font_path,
)
from artifacts.validation import validate_payload


def _bundled_font() -> Path:
    path = Path(bundled_pdf_cjk_font_path())
    assert path.is_file(), f"missing bundled CJK font: {path}"
    return path


def test_bundled_cjk_font_is_truetype_outline_with_qualified_glyphs():
    payload = _bundled_font().read_bytes()
    assert payload[:4] == b"\x00\x01\x00\x00"
    assert b"glyf" in payload
    assert b"OTTO" not in payload[:4]
    _pdf_font_pool.cache_clear()
    regular, _bold, cmap, _bold_map = _pdf_font_pool()[0]
    assert regular.startswith("Variant1Unicode")
    for codepoint in (0x4E2D, 0x6587, 0x518D, 0x6B21, 0x68C0, 0x67E5):
        assert cmap.get(codepoint), f"missing U+{codepoint:04X}"


def test_bundled_cjk_font_renders_non_empty_ink():
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(_bundled_font()), 48)
    image = Image.new("L", (360, 80), 255)
    ImageDraw.Draw(image).text((8, 8), "Hello 中文再次检查", font=font, fill=0)
    extrema = image.getextrema()
    assert extrema[0] < 200


def test_pdf_registration_logs_actual_cjk_cmap_membership(caplog):
    import logging

    _pdf_font_pool.cache_clear()
    with caplog.at_level(logging.INFO, logger="artifacts.builders"):
        _pdf_font_pool()
    messages = [record.getMessage() for record in caplog.records]
    assert any("has_U+4E2D=True" in message for message in messages)
    assert any("Variant1CJK-Regular.ttf" in message for message in messages)


def test_pdf_embeds_truetype_fontfile2_and_preserves_cjk_text():
    from pypdf import PdfReader

    spec = {
        "title": "Hello 中文",
        "blocks": [{"type": "paragraph", "text": "再次检查 中文"}],
    }
    output = build_artifact(spec, "pdf")
    assert b"/FontFile2" in output.payload
    assert output.payload[:5] == b"%PDF-"
    extracted = PdfReader(BytesIO(output.payload)).pages[0].extract_text()
    normalized = " ".join(extracted.split())
    assert "Hello 中文" in normalized
    assert "再次检查 中文" in normalized
    status, findings, metrics = validate_payload(
        "pdf", output.payload, specification=spec,
    )
    assert status == "passed", findings
    assert metrics["missing_text_characters"] == 0
