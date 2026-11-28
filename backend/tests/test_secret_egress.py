import json

import pytest

from llm_router import LLMRouter
from model_runtime.secret_egress import SecretEgressBlocked, SecretEgressFirewall
from observability import trace_events


def test_known_managed_secret_is_replaced_before_cloud_dispatch():
    secret = "sk-live-THIS_IS_A_MANAGED_SECRET_123456"
    firewall = SecretEgressFirewall(
        known_secret_resolver=lambda: [("provider.test", secret)],
    )
    result = firewall.project(
        {"messages": [{"role": "user", "content": f"accidental {secret}"}]},
    )
    rendered = json.dumps(result.payload)
    assert secret not in rendered
    assert "VARIANT1_SECRET:provider.test" in rendered
    assert result.known_replacements == 1


def test_accepted_and_blocked_requests_record_metadata_only(tmp_path, monkeypatch):
    recorder = trace_events.TraceRecorder()
    trace_path = tmp_path / "trace.jsonl"
    recorder.configure_for_tests(path=str(trace_path), enabled=True)
    monkeypatch.setattr(trace_events, "RECORDER", recorder)
    router = LLMRouter({}, str(tmp_path), data_dir=str(tmp_path))
    known = "managed-fixture-secret-123456"
    suspected = "abcdefghijklmnopqrstuvwxyz123456"
    monkeypatch.setattr(
        router, "_secret_egress_firewall",
        SecretEgressFirewall(known_secret_resolver=lambda: [("provider.test", known)]),
    )
    accepted = router.prepare_cloud_payload(
        {
            "messages": [{"role": "user", "content": f"fixture prompt {known}"}],
            "media": "fixture media bytes",
            "tools": [{"description": "fixture tool content"}],
        },
        provider="gemini", model="fixture",
    )
    assert "VARIANT1_SECRET:provider.test" in json.dumps(accepted)
    with pytest.raises(SecretEgressBlocked):
        router.prepare_cloud_payload(
            {"messages": [{"role": "user", "content": f"Authorization: Bearer {suspected}"}]},
            provider="gemini", model="fixture",
        )
    recorder.flush()
    text = trace_path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines()]
    events = [row for row in rows if row["event"] == "model:egress"]
    assert [row["status"] for row in events] == ["accepted", "blocked"]
    assert events[0]["attributes"]["known_replacements"] == 1
    assert events[1]["attributes"]["finding_count"] == 1
    for private in (known, suspected, "fixture prompt", "fixture media bytes", "fixture tool content"):
        assert private not in text


def test_schema_describing_an_api_key_is_not_mistaken_for_a_value():
    firewall = SecretEgressFirewall()
    result = firewall.project(
        {
            "tools": [{
                "name": "connect",
                "parameters": {
                    "properties": {"api_key": {"type": "string"}},
                },
            }]
        },
    )
    assert result.payload["tools"][0]["name"] == "connect"


@pytest.mark.parametrize('responses', [False, True])
def test_completed_kernel_history_can_be_withheld_without_exposing_credentials(responses):
    secret = 'SyntheticCredential98765'
    arguments = json.dumps({'code': f'password = "{secret}"\nclient.connect(password)'})
    if responses:
        payload = {'input':[{'type':'function_call','name':'ipython','call_id':'call-a','arguments':arguments},
                            {'type':'function_call_output','call_id':'call-a','output':'handshake failed'}]}
    else:
        payload = {'messages':[{'role':'assistant','tool_calls':[{'id':'call-a','type':'function',
                                'function':{'name':'ipython','arguments':arguments}}]},
                                {'role':'tool','tool_call_id':'call-a','content':'handshake failed'}]}
    original = json.dumps(payload)
    result = SecretEgressFirewall().project(payload)
    assert secret not in json.dumps(result.payload)
    assert 'retained Python state' in json.dumps(result.payload)
    assert result.redacted_history_fields == 1
    assert json.dumps(payload) == original
    arguments_after = (result.payload['input'][0]['arguments'] if responses else
                       result.payload['messages'][0]['tool_calls'][0]['function']['arguments'])
    assert json.loads(arguments_after)['code'].startswith('# ')
    # An unexecuted call cannot be silently treated as completed history.
    root = 'input' if responses else 'messages'
    payload[root].pop()
    with pytest.raises(SecretEgressBlocked):
        SecretEgressFirewall().project(payload)


def test_unresolved_user_secret_is_a_host_preflight_failure():
    from observability.run_receipts import terminal_cause_class
    with pytest.raises(SecretEgressBlocked) as caught:
        SecretEgressFirewall().project({'messages':[{'role':'user','content':'password = SyntheticCredential98765'}]})
    assert terminal_cause_class(caught.value.terminal_reason) == 'harness'


def test_local_password_expression_does_not_trigger_literal_assignment_block():
    arguments = json.dumps({'code': "password=local_config.get('server_password')\nclient.connect(password)"})
    payload = {'messages':[{'role':'assistant','tool_calls':[{'id':'a','type':'function','function':{'name':'ipython','arguments':arguments}}]},
                           {'role':'tool','tool_call_id':'a','content':'connected locally'}]}
    result = SecretEgressFirewall().project(payload)
    assert result.payload == payload and result.redacted_history_fields == 0


def test_known_secret_resolver_failure_blocks_even_a_benign_payload():
    def broken_resolver():
        raise RuntimeError("vault backend unavailable")

    firewall = SecretEgressFirewall(known_secret_resolver=broken_resolver)
    with pytest.raises(SecretEgressBlocked) as caught:
        firewall.project(
            {"messages": [{"role": "user", "content": "hello"}]},
        )

    assert len(caught.value.findings) == 1
    finding = caught.value.findings[0]
    assert finding["code"] == "known_secret_resolution_failed"
    assert finding["path"] == "/"
    assert len(finding["fingerprint"]) == 18
    assert "vault backend unavailable" not in str(caught.value)


def test_known_secret_resolver_empty_result_is_not_a_failure():
    firewall = SecretEgressFirewall(known_secret_resolver=lambda: [])
    result = firewall.project(
        {"messages": [{"role": "user", "content": "hello"}]},
    )
    assert result.payload["messages"][0]["content"] == "hello"
    assert result.known_replacements == 0


def test_provider_reasoning_ciphertext_is_replayed_byte_exact_without_scanning():
    firewall = SecretEgressFirewall()
    ciphertext = "opaque-sk-" + ("A" * 32) + "-continuation"

    result = firewall.project(
        {
            "input": [{
                "type": "reasoning",
                "encrypted_content": ciphertext,
                "summary": [],
            }],
        },
    )

    assert result.payload["input"][0]["encrypted_content"] == ciphertext


def test_reasoning_ciphertext_is_not_corrupted_by_known_secret_substitution():
    managed = "sk-live-THIS_IS_A_MANAGED_SECRET_123456"
    firewall = SecretEgressFirewall(
        known_secret_resolver=lambda: [("provider.test", managed)]
    )

    result = firewall.project(
        {
            "input": [{
                "type": "reasoning",
                "encrypted_content": managed,
                "summary": [],
            }],
        },
    )

    assert result.payload["input"][0]["encrypted_content"] == managed
    assert result.known_replacements == 0


def test_ordinary_encrypted_content_field_remains_firewalled():
    firewall = SecretEgressFirewall()
    suspected = "sk-" + ("B" * 32)

    with pytest.raises(SecretEgressBlocked) as caught:
        firewall.project(
            {
                "input": [{
                    "type": "message",
                    "encrypted_content": suspected,
                }],
            },
        )

    assert caught.value.findings[0]["code"] == "token_prefix"
    assert caught.value.findings[0]["path"] == "/input/0/encrypted_content"
