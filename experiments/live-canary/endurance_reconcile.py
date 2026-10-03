"""Optional usage-only reconciliation; never fetch stored generation content."""
import json
import math
from urllib.parse import urlencode
import urllib.request


def generation_usage(value):
    def integer(key):
        raw=value.get(key)
        return raw if type(raw) is int and 0 <= raw <= 10**12 else None
    fields={'input_tokens':'native_tokens_prompt','output_tokens':'native_tokens_completion',
            'reasoning_tokens':'native_tokens_reasoning','cached_input_tokens':'native_tokens_cached'}
    result={key:integer(source) for key,source in fields.items()}
    result['reported_fields']=[key for key,count in result.items() if count is not None]
    prompt,output=result['input_tokens'],result['output_tokens']
    result.update(measurement='provider_native_generation_metadata',provider_reported=bool(result['reported_fields']),estimated=False,
                  total_tokens=prompt+output if prompt is not None and output is not None else None,
                  prompt_token_volume=prompt,token_volume=prompt+output if prompt is not None and output is not None else None)
    cached=result['cached_input_tokens']
    result['uncached_input_tokens']=max(0,prompt-cached) if prompt is not None and cached is not None else None
    cost=value.get('total_cost')
    if type(cost) in (int,float) and math.isfinite(cost) and cost >= 0:
        result['cost_usd']=cost
        result['reported_fields'].append('cost_usd')
    return result


def reconcile_usage(ledger, key, *, maximum=100, fetch=None):
    if fetch is None:
        def fetch(identity):
            request=urllib.request.Request('https://openrouter.ai/api/v1/generation?'+urlencode({'id':identity}),
                headers={'Authorization':'Bearer '+key})
            with urllib.request.urlopen(request,timeout=15) as response:
                return json.load(response).get('data')
    updated,missing,failures=0,0,[]
    cap=max(1,min(int(maximum),500))
    with ledger._connect() as conn:
        rows=conn.execute("SELECT manifest_id,response_json FROM model_usage_request WHERE provider='openrouter' "
            "AND json_extract(response_json,'$.provider_generation_id') LIKE 'gen-%' "
            "AND (usage_json IS NULL OR json_extract(usage_json,'$.cost_usd') IS NULL "
            "OR json_extract(usage_json,'$.input_tokens') IS NULL OR json_extract(usage_json,'$.output_tokens') IS NULL) "
            "ORDER BY ordinal LIMIT ?",(cap,)).fetchall()
    for row in rows:
        identity=json.loads(row['response_json'])['provider_generation_id']
        try:
            value=fetch(identity)
            if not isinstance(value,dict) or value.get('id')!=identity:
                raise ValueError('Generation metadata identity mismatch')
            usage=generation_usage(value)
            if not usage['reported_fields'] and 'cost_usd' not in usage:
                missing += 1
                continue
            ledger.patch_usage(row['manifest_id'],usage,partial=True)
            updated += 1
        except Exception as exc:
            failures.append({'manifest_id':row['manifest_id'],'type':type(exc).__name__})
    return {'updated':updated,'unavailable':missing,'failures':failures,
            'limitations':'Only known generation IDs can be reconciled. Missing native counters remain unknown; normalized router token counts and stored prompt/completion content are excluded.'}
