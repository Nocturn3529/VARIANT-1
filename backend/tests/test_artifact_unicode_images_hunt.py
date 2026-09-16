from __future__ import annotations

import base64
from io import BytesIO
import zipfile

import pytest

from artifacts.builders import build_artifact
from artifacts.validation import validate_payload


def _png(color: str) -> bytes:
    from PIL import Image

    output = BytesIO()
    Image.new("RGB", (32, 16), color).save(output, format="PNG")
    return output.getvalue()


def _image_block(payload: bytes, *, alt: str = "Red image") -> dict:
    return {
        "type": "image", "data_base64": base64.b64encode(payload).decode("ascii"),
        "mime_type": "image/png", "alt": alt,
    }


def test_pdf_preserves_cjk_text_in_extractable_unicode_mapping():
    from pypdf import PdfReader

    spec = {
        "title": "Hello 中文",
        "blocks": [{"type": "paragraph", "text": "再次检查 中文"}],
    }
    output = build_artifact(spec, "pdf")
    extracted = PdfReader(BytesIO(output.payload)).pages[0].extract_text()
    # Mixed outline/CID faces can insert breaks in pypdf extraction; the
    # unicode mapping still has to preserve the CJK codepoints in order.
    normalized = " ".join(extracted.split())
    assert "Hello 中文" in normalized
    assert "再次检查 中文" in normalized
    status, findings, metrics = validate_payload(
        "pdf", output.payload, specification=spec,
    )
    assert status == "passed", findings
    assert metrics["missing_text_characters"] == 0


def test_pdf_rejects_supplementary_emoji_instead_of_silently_losing_it():
    with pytest.raises(ValueError, match="supplementary U\\+1F600"):
        build_artifact({"blocks": [{"type": "paragraph", "text": "Smile 😀"}]}, "pdf")


def test_validator_fails_an_old_latin_font_pdf_that_lost_specified_chinese():
    from reportlab.pdfgen.canvas import Canvas

    buffer = BytesIO()
    canvas = Canvas(buffer, invariant=1)
    canvas.setFont("Helvetica", 12)
    canvas.drawString(60, 700, "Hello 中文")
    canvas.save()
    spec = {"blocks": [{"type": "paragraph", "text": "Hello 中文"}]}
    status, findings, metrics = validate_payload("pdf", buffer.getvalue(), specification=spec)
    assert status == "failed"
    assert any(item["code"] == "pdf_text_loss" for item in findings)
    assert metrics["missing_text_characters"] >= 2


def test_embedded_image_and_neighboring_text_survive_all_supported_builders():
    from pypdf import PdfReader

    spec = {
        "title": "Picture",
        "blocks": [
            {"type": "paragraph", "text": "Before"},
            _image_block(_png("red")),
            {"type": "paragraph", "text": "After"},
        ],
    }
    markdown = build_artifact(spec, "md").payload.decode("utf-8")
    html = build_artifact(spec, "html").payload.decode("utf-8")
    assert "![Red image](data:image/png;base64," in markdown
    assert '<img src="data:image/png;base64,' in html
    assert 'alt="Red image"' in html
    for format_name in ("docx", "pptx"):
        output = build_artifact(spec, format_name)
        with zipfile.ZipFile(BytesIO(output.payload)) as archive:
            media = [name for name in archive.namelist() if "/media/" in name]
            assert media, f"{format_name} discarded its image"
            assert any(archive.read(name) == _png("red") for name in media)
    pdf = build_artifact(spec, "pdf")
    page = PdfReader(BytesIO(pdf.payload)).pages[0]
    text = page.extract_text()
    assert all(value in text for value in ("Before", "Red image", "After"))
    assert page["/Resources"].get("/XObject") is not None
    assert validate_payload("pdf", pdf.payload, specification=spec)[0] == "passed"


def test_image_bytes_are_part_of_the_saved_spec_identity_and_paths_are_rejected(tmp_path):
    path = tmp_path / "mutable.png"
    path.write_bytes(_png("red"))
    with pytest.raises(ValueError, match="image.path is not reproducible"):
        build_artifact({"blocks": [{"type": "image", "path": str(path)}]}, "docx")
    red = {"blocks": [_image_block(_png("red"))]}
    blue = {"blocks": [_image_block(_png("blue"))]}
    first = build_artifact(red, "docx")
    second = build_artifact(red, "docx")
    changed = build_artifact(blue, "docx")
    assert first.normalized_sha256 == second.normalized_sha256
    assert first.normalized_sha256 != changed.normalized_sha256


def test_runtime_freezes_image_path_before_source_mutation(tmp_path):
    from tests.test_artifact_runtime import _runtime
    from work_fabric.scope import WorkScope
    path = tmp_path / 'source.png'
    red = _png('red')
    path.write_bytes(red)
    source = {'blocks': [{'type': 'image', 'path': str(path)}]}
    _, _, runtime = _runtime(tmp_path / 'runtime')
    _, _, saved = runtime._store_spec(source, scope=WorkScope(chat_id='chat-a'), artifact_id='image-test')
    path.write_bytes(_png('blue'))
    assert source['blocks'][0]['path'] == str(path)
    assert 'path' not in saved['blocks'][0]
    assert base64.b64decode(saved['blocks'][0]['data_base64']) == red
    old = build_artifact(saved, 'docx')
    path.unlink()
    assert build_artifact(saved, 'docx').normalized_sha256 == old.normalized_sha256


def test_unsupported_image_content_rejects_instead_of_disappearing():
    image = _image_block(_png("red"))
    with pytest.raises(ValueError, match="latex cannot render image blocks"):
        build_artifact({"blocks": [image]}, "latex")
    with pytest.raises(ValueError, match="not valid base64"):
        build_artifact({"blocks": [{"type": "image", "data_base64": "!!!"}]}, "pdf")
    with pytest.raises(ValueError, match="unsupported image format"):
        from PIL import Image

        output = BytesIO()
        Image.new("RGB", (4, 4), "red").save(output, format="WEBP")
        build_artifact({"blocks": [{"type": "image", "data_base64": base64.b64encode(output.getvalue()).decode()}]}, "pdf")
    with pytest.raises(ValueError, match="unsupported artifact block type"):
        build_artifact({"blocks": [{"type": "unknown", "text": "not optional"}]}, "md")
