"""Lossless wire compaction for native browser handle collections."""
from __future__ import annotations

import base64
import copy
from typing import Any

from core_invariants import canonical_json

_BROWSER = 'variant1.browser-observation-result.v1'
_WIRE_BROWSER = 'variant1.browser-observation-wire.v1'
_VARIABLE_METADATA = frozenset({'backend_ref', 'name', 'title', 'url', 'role', 'actions'})


def pack_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {'$variant1_bytes': base64.b64encode(value).decode('ascii')}
    if isinstance(value, list):
        return [pack_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) in ({'$variant1_bytes'}, {'$variant1_literal'}):
        return {'$variant1_literal': {key: pack_value(val) for key, val in value.items()}}
    elements = value.get('elements')
    if value.get('schema') == _BROWSER and isinstance(elements, list) and len(elements) > 1:
        templates: list[dict] = []
        indexes: dict[str, int] = {}
        compact = []
        for item in elements:
            if not isinstance(item, dict) or set(item) != {'$variant1_handle'}:
                break
            handle = item['$variant1_handle']
            if not isinstance(handle, dict) or 'id' not in handle or not isinstance(handle.get('metadata'), dict):
                break
            unique = {key: val for key, val in handle['metadata'].items() if key in _VARIABLE_METADATA}
            shared = {key: val for key, val in handle.items() if key not in {'id', 'metadata'}}
            shared['metadata'] = {key: val for key, val in handle['metadata'].items() if key not in _VARIABLE_METADATA}
            key = canonical_json(shared)
            if key not in indexes:
                indexes[key] = len(templates)
                templates.append(shared)
            compact.append({'template': indexes[key], 'id': handle.get('id'), 'metadata': unique})
        else:
            return {
                'schema': _WIRE_BROWSER,
                'value': {key: pack_value(val) for key, val in value.items() if key != 'elements'},
                'templates': templates, 'elements': compact,
            }
    return {key: pack_value(val) for key, val in value.items()}


def unpack_value(value: Any) -> Any:
    if isinstance(value, list):
        return [unpack_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {'$variant1_literal'}:
        return {key: unpack_value(val) for key, val in value['$variant1_literal'].items()}
    if set(value) == {'$variant1_bytes'}:
        return base64.b64decode(value['$variant1_bytes'], validate=True)
    if value.get('schema') == _WIRE_BROWSER:
        result = unpack_value(value['value'])
        templates = value['templates']
        handles = []
        for item in value['elements']:
            template = copy.deepcopy(templates[item['template']])
            handles.append({'$variant1_handle': {
                **template, 'id': item['id'],
                'metadata': {**template['metadata'], **item['metadata']},
            }})
        result['elements'] = handles
        return result
    return {key: unpack_value(val) for key, val in value.items()}
