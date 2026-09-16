"""Chat composer attachments: parse, load paths, display labels.

Extracted from chat_pipeline so the turn pipeline stays orchestration-focused.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import io
import os
import re
import tempfile

# User-attached files on the chat WebSocket message (Main Deck composer).
_MAX_ATTACH_IMAGE_B64 = 8_000_000   # ~6 MB decoded per frontend image
_MAX_ATTACH_TEXT_BODY_CHARS = 100_000
MAX_ATTACH_TEXT_TOTAL_CHARS = 100_000
_MAX_ATTACH_TEXT_TOTAL_CHARS = MAX_ATTACH_TEXT_TOTAL_CHARS
_MAX_PATH_TEXT_INLINE_BYTES = 12 * 1024
_MAX_ATTACHMENTS = 8
_MAX_USER_IMAGES = 4
_MAX_MODEL_IMAGES = 8
_MAX_MODEL_IMAGE_B64_TOTAL = 18_000_000
_MAX_PATH_IMAGE_BYTES = 40 * 1024 * 1024
_MAX_PATH_TEXT_BYTES = 200 * 1024
_MAX_DECODED_IMAGE_PIXELS = 48_000_000
_MAX_IMAGE_DIMENSION = 16_384
_FULL_IMAGE_EDGE = 2_048
_TILE_IMAGE_EDGE = 1_400
_IMAGE_OUTPUT_BYTES = 3_800_000
_IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".jfif",
    ".tif", ".tiff", ".heic", ".heif", ".avif",
}
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".html", ".htm",
    ".css", ".js", ".ts", ".tsx", ".jsx", ".py", ".log", ".yml", ".yaml", ".toml",
    ".ini", ".cfg", ".env", ".sh", ".ps1", ".bat", ".c", ".cpp", ".h", ".rs",
    ".go", ".java", ".sql",
}


def normalize_image_b64(raw) -> str:
    """Strip a data URL and strictly validate bounded base64 transport text."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    if s.lower().startswith("data:") and "," in s:
        s = s.split(",", 1)[1].strip()
    # Browser encoders are whitespace-free. Accept harmless whitespace from
    # legacy clients, then validate the actual alphabet and padding strictly.
    s = "".join(s.split())
    if not s or len(s) > _MAX_ATTACH_IMAGE_B64:
        return ""
    try:
        padded = s + ("=" * ((4 - len(s) % 4) % 4))
        base64.b64decode(padded, validate=True)
    except (ValueError, binascii.Error):
        return ""
    return s


def _decode_image_b64(raw) -> bytes:
    normalized = normalize_image_b64(raw)
    if not normalized:
        return b""
    try:
        return base64.b64decode(
            normalized + ("=" * ((4 - len(normalized) % 4) % 4)),
            validate=True,
        )
    except (ValueError, binascii.Error):
        return b""


def _fit_image(img, max_edge: int):
    from PIL import Image

    width, height = img.size
    scale = min(1.0, float(max_edge) / max(width, height, 1))
    if scale >= 1.0:
        return img.copy()
    size = (
        max(1, round(width * scale)),
        max(1, round(height * scale)),
    )
    return img.resize(size, Image.Resampling.LANCZOS)


def _flatten_rgb(img):
    from PIL import Image

    if img.mode == "RGB":
        return img
    if img.mode in {"RGBA", "LA"} or "transparency" in getattr(img, "info", {}):
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        return background.convert("RGB")
    return img.convert("RGB")


def _encode_model_image(img, *, prefer_lossless: bool, max_edge: int) -> tuple[bytes, str]:
    """Encode one bounded model image, preserving screenshot pixels when practical."""
    fitted = _fit_image(img, max_edge)
    if prefer_lossless:
        for edge in (max_edge, 1_800, 1_600, 1_280):
            candidate = _fit_image(img, min(max_edge, edge))
            buf = io.BytesIO()
            candidate.save(buf, format="PNG", optimize=True, compress_level=7)
            data = buf.getvalue()
            if len(data) <= _IMAGE_OUTPUT_BYTES:
                return data, "image/png"
        # Very noisy screenshots can exceed the wire budget after a lossless
        # resize. A high-quality JPEG is the bounded final fallback.
        fitted = _fit_image(img, min(max_edge, 1_600))
    rgb = _flatten_rgb(fitted)
    for quality in (92, 86, 78):
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        data = buf.getvalue()
        if len(data) <= _IMAGE_OUTPUT_BYTES or quality == 78:
            return data, "image/jpeg"
    raise ValueError("image could not be encoded")


def _vision_image(data: bytes, media_type: str, *, name: str, variant: str) -> dict:
    return {
        "data_b64": base64.b64encode(data).decode("ascii"),
        "media_type": media_type,
        "origin": "current_user",
        "detail": "high",
        "name": name[:200],
        "variant": variant,
    }


def prepare_image_observations(
    raw: bytes,
    *,
    name: str = "image",
    declared_mime: str = "",
    allow_tiles: bool = True,
) -> list[dict]:
    """Validate, orient, resize, and optionally tile one user image.

    Dense lossless desktop captures receive a full frame plus four overlapping
    quadrants. Photos and additional attachments remain one image. Returned
    dictionaries are transient model observations; callers must not checkpoint
    their ``data_b64`` fields.
    """
    if not raw or len(raw) > _MAX_PATH_IMAGE_BYTES:
        return []
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError

        with Image.open(io.BytesIO(raw)) as opened:
            # Header dimensions are available before pixel decode, so enforce
            # VARIANT-1's stricter cap before ``load`` allocates the raster.
            width, height = opened.size
            if (
                width < 1 or height < 1
                or width > _MAX_IMAGE_DIMENSION
                or height > _MAX_IMAGE_DIMENSION
                or width * height > _MAX_DECODED_IMAGE_PIXELS
            ):
                return []
            opened.load()
            source_format = str(opened.format or "").upper()
            frame = ImageOps.exif_transpose(opened).copy()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return []

    declared = str(declared_mime or "").lower()
    lossless_source = source_format in {"PNG", "BMP"} or declared in {
        "image/png", "image/bmp", "image/x-png",
    }
    width, height = frame.size
    dense_screenshot = bool(
        lossless_source
        and width >= 1_200
        and height >= 700
        and width * height >= 900_000
    )
    full_data, full_mime = _encode_model_image(
        frame,
        prefer_lossless=lossless_source,
        max_edge=_FULL_IMAGE_EDGE,
    )
    images = [_vision_image(full_data, full_mime, name=name, variant="full")]

    if not (allow_tiles and dense_screenshot):
        return images

    overlap_x = max(8, round(width * 0.025))
    overlap_y = max(8, round(height * 0.025))
    mid_x, mid_y = width // 2, height // 2
    boxes = (
        (0, 0, min(width, mid_x + overlap_x), min(height, mid_y + overlap_y)),
        (max(0, mid_x - overlap_x), 0, width, min(height, mid_y + overlap_y)),
        (0, max(0, mid_y - overlap_y), min(width, mid_x + overlap_x), height),
        (max(0, mid_x - overlap_x), max(0, mid_y - overlap_y), width, height),
    )
    for index, box in enumerate(boxes, start=1):
        tile = frame.crop(box)
        data, media_type = _encode_model_image(
            tile,
            prefer_lossless=True,
            max_edge=_TILE_IMAGE_EDGE,
        )
        images.append(_vision_image(
            data,
            media_type,
            name=name,
            variant=f"tile_{index}",
        ))
    return images


@dataclass(frozen=True)
class PathAttachmentLoad:
    """Structured path-read outcome; rendered prose is never used as status."""

    images: tuple[dict, ...]
    text: str
    ok: bool
    kind: str
    name: str


def _load_path_attachment(
    path: str,
    *,
    declared_image: bool = False,
    declared_mime: str = "",
    allow_tiles: bool = True,
) -> PathAttachmentLoad:
    """Load a user-picked absolute path for the chat vision/context path.

    Composer attach is an explicit user action, so read the file for this turn.
    """
    import os
    name = os.path.basename(path.rstrip("\\/")) or path
    if not os.path.exists(path):
        return PathAttachmentLoad(
            (), f"\n\n[Attached path missing: {path}]", False, "path", name,
        )
    if os.path.isdir(path):
        try:
            entries = sorted(os.listdir(path))[:40]
            listing = "\n".join(f"- {e}" for e in entries) or "(empty)"
            more = ""
            try:
                total = len(os.listdir(path))
                if total > 40:
                    more = f"\n…and {total - 40} more"
            except Exception:
                pass
            return PathAttachmentLoad(
                (), f"\n\n[Attached folder: {path}]\n{listing}{more}",
                True, "folder", name,
            )
        except Exception as e:
            return PathAttachmentLoad(
                (), f"\n\n[Attached folder unreadable: {path} ({e})]",
                False, "folder", name,
            )
    ext = os.path.splitext(name)[1].lower()
    try:
        size = os.path.getsize(path)
    except Exception:
        size = 0
    image_hint = bool(
        declared_image
        or str(declared_mime or "").lower().startswith("image/")
        or ext in _IMAGE_EXTS
    )
    # Unknown/extensionless files may still be camera or clipboard images.
    # Pillow performs bounded header validation; ordinary known text files skip
    # the probe entirely.
    probe_image = image_hint or (ext not in _TEXT_EXTS and size <= _MAX_PATH_IMAGE_BYTES)
    if image_hint and size > _MAX_PATH_IMAGE_BYTES:
        return PathAttachmentLoad(
            (), f"\n\n[Attached image too large: {name} ({size} bytes)]",
            False, "image", name,
        )
    if probe_image:
        try:
            with open(path, "rb") as f:
                raw = f.read()
            images = prepare_image_observations(
                raw,
                name=name,
                declared_mime=declared_mime,
                allow_tiles=allow_tiles,
            )
            if images:
                return PathAttachmentLoad(
                    tuple(images), "", True, "image", name,
                )
            if image_hint:
                return PathAttachmentLoad(
                    (), f"\n\n[Attached image could not be decoded: {name}]",
                    False, "image", name,
                )
        except Exception as e:
            if image_hint:
                return PathAttachmentLoad(
                    (), f"\n\n[Attached image unreadable: {name} ({e})]",
                    False, "image", name,
                )
    if ext in _TEXT_EXTS or size <= _MAX_PATH_TEXT_BYTES:
        # A path-backed file remains available through read_file/grep, so a
        # large body must not consume the entire first model request.  Keep
        # small convenience attachments inline and project larger ones as a
        # retrievable reference.  Pathless uploads cannot use this policy and
        # are instead caught by the final context-admission gate.
        if size > _MAX_PATH_TEXT_INLINE_BYTES:
            return PathAttachmentLoad((), (
                f"\n\n[Attached file: {name} - path {path}; {size} bytes; "
                "contents not inlined]\n"
                "Use read_file with focused line ranges or grep to inspect "
                "this local file before answering."
            ), True, "text", name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                body = f.read(_MAX_ATTACH_TEXT_BODY_CHARS + 1)
            if len(body) > _MAX_ATTACH_TEXT_BODY_CHARS:
                body = body[:_MAX_ATTACH_TEXT_BODY_CHARS] + "\n…(truncated)"
            return PathAttachmentLoad(
                (), f"\n\n[Attached file: {name} — path {path}]\n{body}",
                True, "text", name,
            )
        except Exception as e:
            return PathAttachmentLoad(
                (), f"\n\n[Attached file unreadable: {name} ({e})]",
                False, "text", name,
            )
    return PathAttachmentLoad((), (
        f"\n\n[Attached path: {path}]. Binary contents are not inlined; "
        "VARIANT-1 can open it with tools."
    ), True, "path", name)


def _stage_pathless_text(
    body: str, name: str, staging_root: str,
) -> str:
    configured_root = str(staging_root or "").strip()
    if not configured_root:
        return ""
    root = os.path.abspath(configured_root)
    raw = str(body).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "file.txt"))
    safe_name = safe_name.strip(".-")[:120] or "file.txt"
    folder = os.path.join(root, digest[:2])
    os.makedirs(folder, exist_ok=True)
    destination = os.path.join(folder, f"{digest}-{safe_name}")
    if os.path.isfile(destination):
        return destination
    handle = tempfile.NamedTemporaryFile(
        mode="wb", dir=folder, prefix=digest + ".", suffix=".tmp",
        delete=False,
    )
    temporary = handle.name
    try:
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return destination


def validate_chat_attachments(raw) -> None:
    """Validate the composer envelope before any attachment can be dropped."""
    if raw is None:
        return
    if not isinstance(raw, list):
        raise ValueError("attachments must be an array")
    if len(raw) > _MAX_ATTACHMENTS:
        raise ValueError(f"at most {_MAX_ATTACHMENTS} attachments can be sent in one message")
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each attachment must be an object")
        for field in ("path", "name", "kind", "type", "mime", "media_type", "data", "text"):
            if item.get(field) is not None and not isinstance(item[field], str):
                raise ValueError(f"attachment {field} must be text")


def parse_chat_attachments(
    raw, *, staging_root: str = "",
) -> tuple[list[dict], str]:
    """Parse frontend attachments into transient images plus model-only text.

    Up to four user images become validated typed observations. A single dense
    screenshot may expand to a full frame plus four tiles, bounded to eight
    model images and an aggregate base64 budget. Paths selected by the user are
    loaded server-side.
    """
    if not isinstance(raw, list):
        return [], ""
    images: list[dict] = []
    original_image_count = 0
    aggregate_b64_chars = 0
    tiled_user_image = False
    text_parts: list[str] = []

    def accept_prepared(prepared: list[dict]) -> int:
        nonlocal aggregate_b64_chars
        accepted = 0
        remaining_count = _MAX_MODEL_IMAGES - len(images)
        for observation in prepared[:remaining_count]:
            chars = len(str(observation.get("data_b64") or ""))
            if aggregate_b64_chars + chars > _MAX_MODEL_IMAGE_B64_TOTAL:
                break
            aggregate_b64_chars += chars
            images.append(observation)
            accepted += 1
        return accepted

    for item in raw[:_MAX_ATTACHMENTS]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "file").strip()[:200] or "file"
        kind = str(item.get("kind") or item.get("type") or "").strip().lower()
        mime = str(item.get("mime") or item.get("media_type") or "").strip().lower()
        path = str(item.get("path") or "").strip()
        is_image = kind == "image" or mime.startswith("image/")
        is_text = kind in ("text", "file") or mime.startswith("text/") or mime in (
            "application/json", "application/xml",
        )

        if is_image and original_image_count >= _MAX_USER_IMAGES:
            text_parts.append(
                f"\n\n[Attached image omitted: {name} "
                f"(maximum {_MAX_USER_IMAGES} images per message)]"
            )
            continue

        # Prefer server-side path load: no large base64 WebSocket copy and the
        # backend owns format validation, orientation, and screenshot tiling.
        path_load: PathAttachmentLoad | None = None
        if path:
            path_load = _load_path_attachment(
                path,
                declared_image=is_image,
                declared_mime=mime,
                allow_tiles=not tiled_user_image and not images,
            )
            if path_load.ok and path_load.kind == "image":
                if original_image_count >= _MAX_USER_IMAGES:
                    text_parts.append(
                        f"\n\n[Attached image omitted: {name} "
                        f"(maximum {_MAX_USER_IMAGES} images per message)]"
                    )
                    continue
                accepted = accept_prepared(list(path_load.images))
                if accepted:
                    original_image_count += 1
                    tiled_user_image = tiled_user_image or accepted > 1
                    text_parts.append(f"\n\n[Attached image: {name}]")
                    if accepted < len(path_load.images):
                        text_parts.append(
                            f"\n[Image detail views omitted: {name} "
                            "(model image context budget reached)]"
                        )
                else:
                    text_parts.append(
                        f"\n\n[Attached image omitted: {name} "
                        "(model image context budget exhausted)]"
                    )
                continue
            if path_load.ok:
                if path_load.text:
                    text_parts.append(path_load.text)
                # A successful path read/reference is authoritative. Never
                # inline a renderer copy of the same file a second time.
                continue

        if is_image and original_image_count < _MAX_USER_IMAGES:
            # ``data`` is the sole pathless-image wire field. Disk-backed
            # attachments use ``path``; the removed image/image_b64 aliases
            # must not quietly keep a second frontend protocol alive.
            raw_bytes = _decode_image_b64(item.get("data") or "")
            prepared = prepare_image_observations(
                raw_bytes,
                name=name,
                declared_mime=mime,
                allow_tiles=not tiled_user_image and not images,
            ) if raw_bytes else []
            accepted = accept_prepared(prepared)
            if accepted:
                original_image_count += 1
                tiled_user_image = tiled_user_image or accepted > 1
                text_parts.append(f"\n\n[Attached image: {name}]")
                continue
            if path:
                if path_load is not None and path_load.text:
                    text_parts.append(path_load.text)
                continue
            text_parts.append(f"\n\n[Attached image invalid or missing data: {name}]")
            continue
        if is_image:
            text_parts.append(
                f"\n\n[Attached image omitted: {name} "
                f"(maximum {_MAX_USER_IMAGES} images per message)]"
            )
            continue
        if is_text or item.get("text") is not None:
            body = str(item.get("text") or "")
            if body.strip() and staging_root and len(body.encode("utf-8")) > _MAX_PATH_TEXT_INLINE_BYTES:
                staged = _stage_pathless_text(body, name, staging_root)
                text_parts.append(
                    f"\n\n[Attached file: {name} - path {staged}; {len(body.encode('utf-8'))} bytes; contents not inlined]\n"
                    "Use read_file with focused line ranges or grep to inspect this staged local file before answering."
                )
                continue
            if len(body) > _MAX_ATTACH_TEXT_BODY_CHARS:
                body = body[:_MAX_ATTACH_TEXT_BODY_CHARS] + "\n…(truncated)"
            if body.strip():
                if len(body.encode("utf-8")) > _MAX_PATH_TEXT_INLINE_BYTES:
                    text_parts.append(
                        f"\n\n[Attached file omitted: {name}; pathless text "
                        "exceeded the 12 KiB inline limit and staging was unavailable]"
                    )
                else:
                    text_parts.append(f"\n\n[Attached file: {name}]\n{body}")
            elif path_load is not None and path_load.text:
                text_parts.append(path_load.text)
            else:
                text_parts.append(f"\n\n[Attached file: {name} (empty)]")
            continue
        if path_load is not None and path_load.text:
            text_parts.append(path_load.text)
            continue
        if name:
            text_parts.append(
                f"\n\n[Attached file: {name} — binary types are not inlined; "
                f"attach an image/text file or provide its readable local path.]"
            )
    text_suffix = "".join(text_parts)
    if len(text_suffix) > _MAX_ATTACH_TEXT_TOTAL_CHARS:
        marker = "\n\n…(attachment text truncated at 100,000 characters total)"
        keep = max(0, _MAX_ATTACH_TEXT_TOTAL_CHARS - len(marker))
        text_suffix = text_suffix[:keep].rstrip() + marker
    return images, text_suffix


# Markers used when inlining attachments into the model prompt.
_ATTACH_MARKER_RE = re.compile(
    r"\[Attached\s+(file|image|path):\s*([^\]\n]+?)(?:\s+[—-]\s+.*?)?\]",
    re.IGNORECASE,
)
# Split user text into composer part + optional inlined attachment dump.
_ATTACH_SPLIT_RE = re.compile(
    r"\n\n\[Attached\s+(?:file|image|path):",
    re.IGNORECASE,
)


def attachment_labels_from_suffix(suffix: str) -> list[dict]:
    """Compact attachment chips for durable transcript display (no file bodies)."""
    labels: list[dict] = []
    seen: set[str] = set()
    for m in _ATTACH_MARKER_RE.finditer(str(suffix or "")):
        kind_raw = (m.group(1) or "file").lower()
        name = (m.group(2) or "file").strip()[:200] or "file"
        key = f"{kind_raw}:{name.lower()}"
        if key in seen:
            continue
        seen.add(key)
        kind = "image" if kind_raw == "image" else ("path" if kind_raw == "path" else "text")
        labels.append({"name": name, "kind": kind})
    return labels[:_MAX_ATTACHMENTS]


def _labels_to_display_line(labels: list[dict], *, has_image: bool = False) -> str:
    if labels:
        if len(labels) == 1:
            kind = labels[0].get("kind") or "text"
            name = labels[0].get("name") or "file"
            if kind == "image":
                return f"📷 {name}"
            return f"Attached {name}"
        names = ", ".join(str(a.get("name") or "file") for a in labels[:3])
        more = f" +{len(labels) - 3}" if len(labels) > 3 else ""
        return f"Attached {len(labels)} files: {names}{more}"
    if has_image:
        return "📷 Attached image"
    return ""


def strip_inlined_attachments(text: str) -> tuple[str, list[dict]]:
    """Recover display text + chips from a legacy transcript that stored file bodies."""
    raw = str(text or "")
    labels = attachment_labels_from_suffix(raw)
    m = _ATTACH_SPLIT_RE.search(raw)
    if m:
        head = raw[: m.start()].strip()
        return head or _labels_to_display_line(labels), labels
    return raw.strip(), labels


def display_user_message(
    composer_text: str,
    *,
    attach_suffix: str = "",
    has_image: bool = False,
) -> tuple[str, list[dict]]:
    """Build the short user bubble for chat history (not the model prompt).

    Returns ``(display_text, attachment_labels)``. File bodies stay out of the
    transcript so drag-and-drop HTML/etc. does not explode the UI.
    """
    # Peel any already-inlined model suffix off the composer field first.
    text, recovered = strip_inlined_attachments(composer_text)
    labels = attachment_labels_from_suffix(attach_suffix) or recovered
    if text:
        return text, labels
    line = _labels_to_display_line(labels, has_image=has_image)
    return line, labels


