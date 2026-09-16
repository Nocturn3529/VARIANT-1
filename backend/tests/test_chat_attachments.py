"""Composer attachment parsing, validation, and image preprocessing."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest

import chat_attachments
from chat_attachments import (
    attachment_labels_from_suffix,
    display_user_message,
    normalize_image_b64,
    parse_chat_attachments,
    prepare_image_observations,
    strip_inlined_attachments,
)


def _image_bytes(fmt="PNG", size=(48, 32), color=(20, 40, 80)) -> bytes:
    pytest.importorskip("PIL")
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format=fmt)
    return buf.getvalue()


def _image_b64(fmt="PNG", size=(48, 32), color=(20, 40, 80)) -> str:
    return base64.b64encode(_image_bytes(fmt, size, color)).decode("ascii")


def test_normalize_image_strips_data_url_and_rejects_bad_alphabet():
    raw = base64.b64encode(b"transport-bytes").decode("ascii")
    assert normalize_image_b64(f"data:image/png;base64,{raw}") == raw
    assert normalize_image_b64(raw) == raw
    assert normalize_image_b64("not%%%base64") == ""
    assert normalize_image_b64("") == ""
    assert normalize_image_b64(None) == ""


def test_normalize_image_rejects_huge_payload():
    assert normalize_image_b64("A" * 9_000_000) == ""


def test_parse_attachments_keeps_multiple_images_and_text():
    first = _image_b64("PNG", color=(1, 2, 3))
    second = _image_b64("JPEG", color=(4, 5, 6))
    images, text = parse_chat_attachments([
        {"name": "notes.txt", "kind": "text", "text": "hello world"},
        {"name": "shot.png", "kind": "image", "mime": "image/png", "data": first},
        {"name": "photo.jpg", "kind": "image", "mime": "image/jpeg", "data": second},
    ])

    assert len(images) == 2
    assert {item["media_type"] for item in images} == {"image/png", "image/jpeg"}
    assert "[Attached file: notes.txt]" in text
    assert "hello world" in text
    assert "[Attached image: shot.png]" in text
    assert "[Attached image: photo.jpg]" in text


def test_parse_attachments_rejects_decodable_non_image():
    fake = base64.b64encode(b"not an image").decode("ascii")
    images, text = parse_chat_attachments([
        {"name": "fake.png", "mime": "image/png", "data": fake},
    ])
    assert images == []
    assert "invalid or missing" in text


def test_parse_attachments_empty_list():
    assert parse_chat_attachments(None) == ([], "")
    assert parse_chat_attachments([]) == ([], "")
    assert parse_chat_attachments("nope") == ([], "")


def test_parse_attachments_caps_aggregate_text_but_still_finds_later_image():
    images, text = parse_chat_attachments([
        {"name": "first.txt", "kind": "text", "text": "a" * 70_000},
        {"name": "second.txt", "kind": "text", "text": "b" * 70_000},
        {"name": "later.png", "kind": "image", "data": _image_b64()},
    ])

    assert len(images) == 1
    assert len(text) <= 100_000
    assert "exceeded the 12 KiB inline limit" in text


def test_dense_png_gets_full_frame_and_four_tiles():
    observations = prepare_image_observations(
        _image_bytes("PNG", size=(1600, 900)),
        name="desktop.png",
    )
    assert len(observations) == 5
    assert [row["variant"] for row in observations] == [
        "full", "tile_1", "tile_2", "tile_3", "tile_4",
    ]
    assert all(row["media_type"] == "image/png" for row in observations)


def test_multiple_user_images_are_capped_at_four():
    rows = [
        {"name": f"{index}.png", "kind": "image", "data": _image_b64(color=(index, 0, 0))}
        for index in range(5)
    ]
    images, text = parse_chat_attachments(rows)
    assert len(images) == 4
    assert "maximum 4 images" in text


def test_load_png_path_attachment_preserves_lossless_pixels_and_hides_path(tmp_path: Path):
    img_path = tmp_path / "shot.png"
    img_path.write_bytes(_image_bytes("PNG", size=(64, 48)))
    images, text = parse_chat_attachments(
        [{"name": "shot.png", "kind": "image", "path": str(img_path)}],
    )
    assert len(images) == 1
    assert base64.b64decode(images[0]["data_b64"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert "shot.png" in text
    assert str(tmp_path) not in text


def test_path_attachment_loads_from_disk(tmp_path: Path):
    img_path = tmp_path / "desktop-shot.png"
    img_path.write_bytes(_image_bytes("PNG", size=(40, 30)))
    images, text = parse_chat_attachments(
        [{"name": "desktop-shot.png", "kind": "image", "path": str(img_path)}],
    )
    assert images
    assert "desktop-shot.png" in text
    assert str(tmp_path) not in text


def test_path_text_status_is_structured_and_does_not_duplicate_client_copy(
    tmp_path: Path,
):
    doc_path = tmp_path / "missing-notes.txt"
    doc_path.write_text("the word missing is ordinary content", encoding="utf-8")

    images, text = parse_chat_attachments([{
        "name": doc_path.name,
        "kind": "text",
        "path": str(doc_path),
        "text": "the word missing is ordinary content",
    }])

    assert images == []
    assert text.count("the word missing is ordinary content") == 1
    assert "unreadable" not in text


def test_path_image_budget_omission_never_claims_pixels_were_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    img_path = tmp_path / "shot.png"
    img_path.write_bytes(_image_bytes("PNG", size=(64, 48)))
    monkeypatch.setattr(chat_attachments, "_MAX_MODEL_IMAGE_B64_TOTAL", 1)

    images, text = parse_chat_attachments([
        {"name": "shot.png", "kind": "image", "path": str(img_path)},
    ])

    assert images == []
    assert "image omitted" in text.lower()
    assert "[Attached image: shot.png]" not in text


def test_mime_declared_and_extensionless_path_images_are_decoded(tmp_path: Path):
    img_path = tmp_path / "camera-payload"
    img_path.write_bytes(_image_bytes("PNG", size=(40, 30)))

    images, text = parse_chat_attachments([{
        "name": "camera-payload",
        "kind": "path",
        "mime": "application/octet-stream",
        "path": str(img_path),
    }])

    assert images
    assert "[Attached image: camera-payload]" in text


def test_missing_path_uses_image_bytes_fallback_without_false_failure(tmp_path: Path):
    missing = tmp_path / "moved-photo.png"
    images, text = parse_chat_attachments([{
        "name": "moved-photo.png",
        "kind": "image",
        "mime": "image/png",
        "path": str(missing),
        "data": _image_b64(),
    }])

    assert images
    assert "[Attached image: moved-photo.png]" in text
    assert "path missing" not in text.lower()


def test_large_text_path_is_a_tool_readable_reference_not_an_inlined_dump(
    tmp_path: Path,
):
    doc_path = tmp_path / "large-plan.md"
    sentinel = "SENTINEL-LARGE-BODY"
    doc_path.write_text(sentinel + ("\nplanning detail" * 2_000), encoding="utf-8")

    images, text = parse_chat_attachments([
        {"name": "large-plan.md", "kind": "text", "path": str(doc_path)},
    ])

    assert images == []
    assert "large-plan.md" in text
    assert str(doc_path) in text
    assert "contents not inlined" in text
    assert "read_file" in text
    assert sentinel not in text
    assert len(text) < 1_000


def test_large_pathless_text_is_staged_as_tool_readable_file(tmp_path: Path):
    sentinel = "PATHLESS-LARGE-SENTINEL"
    body = sentinel + ("\ncontext Zażółć" * 20_000) + "\nTAIL-MUST-SURVIVE"

    images, text = parse_chat_attachments(
        [{"name": "upload.txt", "kind": "text", "text": body}],
        staging_root=str(tmp_path / "staged"),
    )

    assert images == []
    assert "contents not inlined" in text
    assert "read_file" in text
    assert sentinel not in text
    staged = next((tmp_path / "staged").rglob("*upload.txt"))
    assert staged.read_text(encoding="utf-8") == body


def test_display_user_message_keeps_composer_hides_bodies():
    display, labels = display_user_message(
        "please fix the pieces",
        attach_suffix="\n\n[Attached file: chess.html]\n<!DOCTYPE html>\n<html>…",
    )
    assert display == "please fix the pieces"
    assert labels == [{"name": "chess.html", "kind": "text"}]
    assert "<!DOCTYPE" not in display


def test_display_user_message_attachment_only():
    display, labels = display_user_message(
        "",
        attach_suffix="\n\n[Attached file: notes.txt]\nhello world",
    )
    assert display == "Attached notes.txt"
    assert labels[0]["name"] == "notes.txt"


def test_strip_inlined_attachments_legacy_transcript():
    raw = (
        "this chess app is unfinished\n\n"
        "[Attached file: chess.html]\n"
        "<!DOCTYPE html>\n<html lang=\"en\">\n" + ("x" * 200)
    )
    display, labels = strip_inlined_attachments(raw)
    assert display == "this chess app is unfinished"
    assert labels == [{"name": "chess.html", "kind": "text"}]
    assert "DOCTYPE" not in display


def test_attachment_label_keeps_hyphenated_filename():
    labels = attachment_labels_from_suffix(
        "\n\n[Attached image: my-photo-final.png]"
    )
    assert labels == [{"name": "my-photo-final.png", "kind": "image"}]
