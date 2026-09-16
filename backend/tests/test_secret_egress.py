import json

import pytest

from artifacts.store import ContentAddressedArtifactStore
from model_runtime.secret_egress import SecretEgressBlocked, SecretEgressFirewall


def _read_json(store, ref):
    return json.loads(store.read_bytes(ref).decode("utf-8"))


def test_known_managed_secret_is_replaced_before_cloud_dispatch(tmp_path):
    store = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    secret = "sk-live-THIS_IS_A_MANAGED_SECRET_123456"
    firewall = SecretEgressFirewall(
        artifact_store=store,
        known_secret_resolver=lambda: [("provider.test", secret)],
    )
    result = firewall.project(
        {"messages": [{"role": "user", "content": f"accidental {secret}"}]},
        provider="xai",
        model="grok-4.6",
        scope="chat-a",
    )
    rendered = json.dumps(result.payload)
    assert secret not in rendered
    assert "VARIANT1_SECRET:provider.test" in rendered
    assert result.known_replacements == 1
    assert result.local_ref and result.cloud_ref
    # Configured vault values are absent even from the private source artifact.
    assert secret not in json.dumps(_read_json(store, result.local_ref))


def test_unregistered_likely_secret_blocks_and_records_sanitized_projection(tmp_path):
    store = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    firewall = SecretEgressFirewall(artifact_store=store)
    with pytest.raises(SecretEgressBlocked) as caught:
        firewall.project(
            {"messages": [{
                "role": "user",
                "content": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
            }]},
            provider="anthropic",
            model="claude-test",
            scope="chat-b",
        )
    error = caught.value
    assert error.local_ref and error.cloud_ref
    assert "abcdefghijklmnopqrstuvwxyz123456" not in json.dumps(
        _read_json(store, error.cloud_ref)
    )
    assert "VARIANT1_SUSPECTED_SECRET:BLOCKED" in json.dumps(
        _read_json(store, error.cloud_ref)
    )


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
        provider="xai",
        model="grok-4.6",
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
    result = SecretEgressFirewall().project(payload, provider='test', model='test')
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
        SecretEgressFirewall().project(payload, provider='test', model='test')


def test_unresolved_user_secret_is_a_host_preflight_failure():
    from observability.run_receipts import terminal_cause_class
    with pytest.raises(SecretEgressBlocked) as caught:
        SecretEgressFirewall().project({'messages':[{'role':'user','content':'password = SyntheticCredential98765'}]}, provider='test', model='test')
    assert terminal_cause_class(caught.value.terminal_reason) == 'harness'


def test_local_password_expression_does_not_trigger_literal_assignment_block():
    arguments = json.dumps({'code': "password=local_config.get('server_password')\nclient.connect(password)"})
    payload = {'messages':[{'role':'assistant','tool_calls':[{'id':'a','type':'function','function':{'name':'ipython','arguments':arguments}}]},
                           {'role':'tool','tool_call_id':'a','content':'connected locally'}]}
    result = SecretEgressFirewall().project(payload, provider='test', model='test')
    assert result.payload == payload and result.redacted_history_fields == 0


def test_known_secret_resolver_failure_blocks_even_a_benign_payload():
    def broken_resolver():
        raise RuntimeError("vault backend unavailable")

    firewall = SecretEgressFirewall(known_secret_resolver=broken_resolver)
    with pytest.raises(SecretEgressBlocked) as caught:
        firewall.project(
            {"messages": [{"role": "user", "content": "hello"}]},
            provider="xai",
            model="grok-4.6",
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
        provider="xai",
        model="grok-4.6",
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
        provider="openai-codex",
        model="gpt-test",
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
        provider="openai-codex",
        model="gpt-test",
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
            provider="openai-codex",
            model="gpt-test",
        )

    assert caught.value.findings[0]["code"] == "token_prefix"
    assert caught.value.findings[0]["path"] == "/input/0/encrypted_content"
