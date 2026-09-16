"""Viewport request validation and image dimensions shared by browser adapters."""
from __future__ import annotations
from typing import Any, Mapping
from .models import BrowserValidationError


def viewport_request(params: Mapping[str, Any]) -> dict[str, Any]:
    mode = str(params.get('mode') or 'fixed')
    if mode == 'auto':
        if params.get('width') is not None or params.get('height') is not None:
            raise BrowserValidationError('INVALID_VIEWPORT: auto mode does not accept dimensions')
        return {'mode': 'auto'}
    width, height = params.get('width'), params.get('height')
    if mode not in {'fixed', 'explicit'} or type(width) is not int or type(height) is not int or not (320 <= width <= 3840 and 240 <= height <= 2160):
        raise BrowserValidationError('INVALID_VIEWPORT: width must be 320–3840 and height 240–2160 CSS pixels')
    return {'mode': 'fixed', 'width': width, 'height': height}


def image_dimensions(data: bytes) -> dict[str, int]:
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
        return {'image_width': int.from_bytes(data[16:20], 'big'), 'image_height': int.from_bytes(data[20:24], 'big')}
    return {}
