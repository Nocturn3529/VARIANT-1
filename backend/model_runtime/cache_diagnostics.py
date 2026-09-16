"""Process-local equality evidence for provider input; never retain its values."""
from __future__ import annotations

from collections import Counter, OrderedDict
import hashlib
import hmac
import json
import secrets
import threading
import uuid
from typing import Any

_CACHE_HEADERS = ('x-client-request-id', 'session_id', 'x-grok-conv-id')


def _has_image(item: Any) -> bool:
    if isinstance(item, dict):
        if item.get('type') in {'image', 'image_url', 'input_image'}:
            return True
        return _has_image(item.get('content'))
    if isinstance(item, list):
        return any(_has_image(value) for value in item)
    return False


class CacheEqualityRecorder:
    """Bounded, content-free previous-request state, isolated by chat and lane.

    Equality IDs use an unpersisted random key. They can be compared only within
    this recorder lifetime and are not unkeyed prompt/payload hashes. Input item
    IDs compare canonical JSON values, not provider-internal tokenization.
    """

    def __init__(self, *, max_scopes: int = 128, max_items: int = 2048, ordered_limit: int = 128):
        self._key = secrets.token_bytes(32)
        self.scope_id = uuid.uuid4().hex
        self.max_scopes = max(1, int(max_scopes))
        self.max_items = max(1, int(max_items))
        self.ordered_limit = max(1, int(ordered_limit))
        self._previous: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def _id(self, domain: str, value: Any) -> str:
        digest = hmac.new(self._key, domain.encode() + b'\0', hashlib.sha256)
        for chunk in json.JSONEncoder(
            ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False,
        ).iterencode(value):
            digest.update(chunk.encode('utf-8'))
        return digest.hexdigest()

    def record(
        self, *, payload: dict, transport: str, owner: str, lane: tuple,
        manifest_id: str, header_names: tuple[str, ...] | None = None,
    ) -> dict:
        if transport == 'responses':
            instructions = payload.get('instructions', '')
            raw_items = payload.get('input', [])
            items = raw_items if isinstance(raw_items, list) else [raw_items]
            instruction_source = 'instructions'
        elif transport in {'chat_completions', 'chat'}:
            items = payload.get('messages') or []
            if not isinstance(items, list):
                return {'available': False, 'reason': 'unsupported_input_shape'}
            instructions = [item for item in items if isinstance(item, dict)
                            and item.get('role') in {'system', 'developer'}]
            instruction_source = 'messages.system_and_developer'
        else:
            return {'available': False, 'reason': 'transport_not_projected'}

        ids = [self._id('input-item', item) for item in items]
        instructions_id = self._id('instructions', instructions)
        settings_id = self._id('prefix-settings', {
            key: payload[key] for key in (
                'model', 'tools', 'parallel_tool_calls', 'tool_choice',
                'text', 'reasoning', 'context_management',
            ) if key in payload
        })
        input_id = self._id('ordered-input', ids)
        image_items = [(index, ids[index]) for index, item in enumerate(items) if _has_image(item)]
        image_ids = [identity for _index, identity in image_items[:16]]
        image_truncated = len(image_items) > 16
        reasoning = [item for item in items if isinstance(item, dict) and item.get('type') == 'reasoning']
        replay_bytes = sum(len(item['encrypted_content'].encode('utf-8')) for item in reasoning
                           if isinstance(item.get('encrypted_content'), str))
        current = {
            'manifest_id': manifest_id, 'instructions_id': instructions_id,
            'settings_id': settings_id, 'input_id': input_id,
            'item_ids': tuple(ids[:self.max_items]), 'item_count': len(ids),
            'image_ids': tuple(image_ids), 'image_truncated': image_truncated,
        }
        previous = None
        if owner:
            key = self._id('comparison-owner', [owner, *lane])
            with self._lock:
                previous = self._previous.pop(key, None)
                self._previous[key] = current
                while len(self._previous) > self.max_scopes:
                    self._previous.popitem(last=False)

        comparison: dict[str, Any] = {'available': previous is not None}
        image_transition = None
        if previous is not None:
            prior_ids = previous['item_ids']
            first_changed = next((index for index, pair in enumerate(zip(prior_ids, ids))
                                  if pair[0] != pair[1]), None)
            compared = min(len(prior_ids), len(ids))
            if (first_changed is None and previous['item_count'] != len(ids)
                    and min(previous['item_count'], len(ids)) < self.max_items):
                first_changed = min(previous['item_count'], len(ids))
            complete_prior = previous['item_count'] <= self.max_items
            unchanged = previous['input_id'] == input_id
            if unchanged:
                append_only = True
            elif first_changed is not None and first_changed < previous['item_count']:
                append_only = False
            elif complete_prior:
                append_only = first_changed == previous['item_count'] and len(ids) > previous['item_count']
            else:
                append_only = None
            comparison.update(
                previous_manifest_id=previous['manifest_id'],
                instructions_unchanged=previous['instructions_id'] == instructions_id,
                prefix_settings_unchanged=previous['settings_id'] == settings_id,
                input_unchanged=unchanged, input_append_only=append_only,
                first_changed_item_index=first_changed,
                common_item_prefix=len(ids) if unchanged else first_changed if first_changed is not None else compared,
                comparison_truncated=not complete_prior and not unchanged and first_changed is None,
            )
            if not image_truncated and not previous['image_truncated']:
                before, after = Counter(previous['image_ids']), Counter(image_ids)
                image_transition = {
                    'added_items': sum((after-before).values()),
                    'removed_items': sum((before-after).values()),
                    'retained_items': sum((before & after).values()),
                }

        if len(ids) > self.ordered_limit:
            head = min(16, self.ordered_limit // 2)
            positions = list(range(head)) + list(range(len(ids)-(self.ordered_limit-head), len(ids)))
        else:
            positions = list(range(len(ids)))
        known_headers = {str(name).lower() for name in header_names} if header_names is not None else None
        return {
            'available': True, 'schema': 'variant1.prompt-equality.v1',
            'equality_scope_id': self.scope_id,
            'equality_method': 'process_local_hmac_sha256_canonical_values',
            'instruction_source': instruction_source, 'instructions_id': instructions_id,
            'prefix_settings_id': settings_id, 'ordered_input_id': input_id,
            'input_item_count': len(ids),
            'ordered_item_ids': [{'index': index, 'id': ids[index]} for index in positions],
            'ordered_items_omitted': len(ids)-len(positions),
            'comparison': comparison,
            'reasoning_replay': {'items': len(reasoning), 'encrypted_utf8_bytes': replay_bytes},
            'image_items': [{'index': index, 'id': identity} for index, identity in image_items[:16]],
            'image_items_omitted': max(0, len(image_items)-16),
            'image_item_transition': image_transition,
            'cache_affinity_header_presence': {
                name: name in known_headers if known_headers is not None else None
                for name in _CACHE_HEADERS
            },
            'limits': 'Value equality and image-item changes, not provider tokenization or a cache-hit guarantee. Comparison state is bounded and process-local; no request values or header values are retained.',
        }


CACHE_EQUALITY = CacheEqualityRecorder()
