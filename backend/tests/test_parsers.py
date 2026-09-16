"""Internal structured outputs use strict whole-response JSON parsing."""

from internal_json import parse_object


def test_extract_json_obj_accepts_complete_object():
    assert parse_object('{"ok":true,"value":3}') == {"ok": True, "value": 3}


def test_extract_json_obj_accepts_single_json_fence():
    assert parse_object('```json\n{"ok":true}\n```') == {"ok": True}


def test_extract_json_obj_rejects_prose_scanning_and_suffix_repair():
    assert parse_object('Here is the result: {"ok":true}') is None
    assert parse_object('{"ok":true') is None


def test_extract_json_obj_rejects_non_object_json():
    assert parse_object('[1,2,3]') is None
