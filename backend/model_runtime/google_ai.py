"""Cloud Code Assist envelope around the canonical Gemini request and parser."""
from __future__ import annotations

import hashlib
import time
import uuid

from google_ai_oauth import BASE_URL, headers


def prepare_request(payload, *, project_id, model, access_token, cache_identity=None):
    if not project_id:
        raise ValueError('Google AI subscription has no account project; reconnect it in Accounts.')
    request = dict(payload)
    identity = str(getattr(cache_identity, 'key', '') or uuid.uuid4().hex)
    digest = hashlib.sha256((project_id + ':' + identity).encode()).digest()
    request['sessionId'] = str(int.from_bytes(digest[:8], 'big', signed=True))
    if request.get('tools'):
        request['toolConfig'] = {'functionCallingConfig': {'mode': 'VALIDATED'}}
    envelope = {'project': project_id, 'model': model, 'request': request,
                'requestId': f'agent/{digest.hex()[:12]}/{int(time.time() * 1000)}/{uuid.uuid4().hex}/0',
                'userAgent': 'antigravity', 'requestType': 'agent'}
    return BASE_URL + '/v1internal:streamGenerateContent?alt=sse', headers(access_token), envelope


def unwrap_response(value):
    if not isinstance(value, dict):
        raise ValueError('Google AI stream returned a non-object event.')
    if value.get('error'):
        return value
    response = value.get('response')
    return response if isinstance(response, dict) else value
