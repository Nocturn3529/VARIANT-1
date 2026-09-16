from session_context import (
    empty_session_context,
    latest_session_context,
    session_context_from_manifest,
)


def _manifest():
    return {
        "type": "model:request_manifest",
        "manifest_id": "mreq_test",
        "captured_at": 10.0,
        "run": {"run_id": "r1", "source": "chat", "session_id": "s1"},
        "route": {"model": "test-model"},
        "messages": {
            "ordered_rendered": [
                {
                    "role": "system", "text_utf8_bytes": 1200,
                    "tool_argument_utf8_bytes": 0,
                    "tool_result_utf8_bytes": 0, "tool_names": [],
                },
                {
                    "role": "user", "text_utf8_bytes": 800,
                    "tool_argument_utf8_bytes": 0,
                    "tool_result_utf8_bytes": 0, "tool_names": [],
                },
                {
                    "role": "assistant", "text_utf8_bytes": 200,
                    "tool_argument_utf8_bytes": 160,
                    "tool_result_utf8_bytes": 0,
                    "tool_names": ["skill", "mcp_search"],
                },
                {
                    "role": "tool", "text_utf8_bytes": 0,
                    "tool_argument_utf8_bytes": 0,
                    "tool_result_utf8_bytes": 400,
                    "tool_names": ["mcp_search"],
                },
            ],
        },
        "tools": {
            "rendered": [
                {"name": "skill", "category": "skills"},
                {"name": "mcp_search", "category": "mcp:search"},
                {"name": "read_file", "category": "files"},
            ],
            "rendered_schema_metrics": [
                {"name": "skill", "estimated_schema_tokens": 40},
                {"name": "mcp_search", "estimated_schema_tokens": 60},
                {"name": "read_file", "estimated_schema_tokens": 50},
            ],
            "estimated_schema_tokens": 150,
        },
        "budget": {
            "estimated_message_tokens": 716,
            "estimated_input_tokens_lower_bound": 866,
            "context_limit_tokens": 4096,
            "output_reserve_tokens": 256,
        },
        "context_lineage": {
            "items": [
                {"decision": "kept", "producer": "prompt_profile", "bytes_after": 120},
                {"decision": "kept", "producer": "prompt_memory", "bytes_after": 160},
                {"decision": "kept", "producer": "skills_catalog", "bytes_after": 100},
                {"decision": "kept", "producer": "app_catalog", "bytes_after": 60},
            ],
        },
        "usage": {
            "measurement": "provider_reported",
            "input_tokens": 1000,
            "cached_input_tokens": 200,
        },
    }


def test_context_snapshot_uses_provider_total_and_reconciles_categories():
    snapshot = session_context_from_manifest(_manifest())
    assert snapshot is not None
    assert snapshot["session_id"] == "s1"
    assert snapshot["used_tokens"] == 1000
    assert snapshot["context_limit_tokens"] == 4096
    assert snapshot["percent_used"] == 24.4
    assert snapshot["measurement"] == "provider_reported"
    assert snapshot["cached_input_tokens"] == 200
    by_id = {row["id"]: row for row in snapshot["categories"]}
    assert list(by_id) == [
        "messages", "tools", "skills", "mcps", "plugins", "memory", "other",
    ]
    assert sum(row["tokens"] for row in by_id.values()) == 1000
    for category in ("messages", "tools", "skills", "mcps", "plugins", "memory", "other"):
        assert by_id[category]["tokens"] > 0


def test_latest_context_is_scoped_to_chat_session():
    other = _manifest()
    other["run"] = {"session_id": "s2"}
    other["manifest_id"] = "mreq_other"
    snapshot = latest_session_context(
        {"items": [_manifest(), other]}, "s1", context_limit_tokens=8192)
    assert snapshot["manifest_id"] == "mreq_test"

    missing = latest_session_context(
        {"items": [_manifest(), other]}, "new", context_limit_tokens=8192)
    assert missing == empty_session_context("new", context_limit_tokens=8192)
    assert missing["percent_used"] == 0
    assert missing["available_tokens"] == 8192
