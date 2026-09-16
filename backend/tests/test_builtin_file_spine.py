"""Behavioral coverage for the compact built-in file surface."""

from __future__ import annotations

import os
import json
import hashlib
import builtins

import pytest

import builtin_tools
import tools
from capability_broker import (
    CapabilityBroker,
    InvocationContext,
    capability_request_fingerprint,
)
from tests.support.astb_runtime import StaticRuntimeRegistry
from artifacts.store import ContentAddressedArtifactStore
from kernel_runtime.worker_bridge import Variant1FileReadResult, _decode_host_result


@pytest.fixture(autouse=True)
def file_scope(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "VARIANT1_PATCH_JOURNAL_DIR", str(tmp_path / "patch-transactions")
    )
    yield tmp_path


@pytest.mark.asyncio
async def test_read_file_can_continue_after_default_page(file_scope):
    path = file_scope / "large.txt"
    path.write_bytes(
        "".join(f"line {number:04d} value\n" for number in range(1, 801)).encode(
            "utf-8"
        )
    )

    first = await builtin_tools.read_file({"path": str(path)})
    assert "lines 1-500 of 800" in first
    assert "Use offset=501 to continue" in first
    assert "line 0500 value" in first
    assert "line 0501 value" not in first

    second = await builtin_tools.read_file({"path": str(path), "offset": 501})
    assert "lines 501-800 of 800" in second
    assert "line 0501 value" in second
    assert "Use offset=" not in second


@pytest.mark.asyncio
async def test_read_file_offset_and_limit_are_one_indexed_lines(file_scope):
    path = file_scope / "lines.txt"
    path.write_bytes(b"alpha\nbeta\ngamma\ndelta\n")

    result = await builtin_tools.read_file({
        "path": str(path), "offset": 2, "limit": 2,
    })

    assert "lines 2-3 of 4" in result
    assert "beta\ngamma" in result
    assert "alpha" not in result
    assert "delta" not in result
    assert "Use offset=4 to continue" in result


@pytest.mark.asyncio
async def test_read_file_byte_ceiling_stops_on_a_whole_line(file_scope, monkeypatch):
    path = file_scope / "bounded.txt"
    path.write_bytes(b"first\nsecond\nthird\n")
    monkeypatch.setattr(builtin_tools, "MAX_READ_BYTES", 13)

    result = await builtin_tools.read_file({"path": str(path)})

    assert "first\nsecond" in result
    assert "third" not in result
    assert "lines 1-2 of 3" in result
    assert "Use offset=3 to continue" in result


@pytest.mark.asyncio
async def test_read_file_schema_explains_deterministic_continuation():
    definition = next(row for row in builtin_tools._defs() if row[0] == "read_file")
    params, description = definition[3], definition[4]

    assert "1-indexed" in params["offset"]["desc"]
    assert params["offset"]["minimum"] == 1
    assert params["offset"]["default"] is None
    assert params["limit"]["default"] is None
    assert str(builtin_tools.MAX_READ_LINES) in params["limit"]["desc"]
    assert "Python receives the complete text" in description
    assert "typed window" in description


def test_read_file_explicit_none_defaults_validate_as_complete_mode():
    definition = next(row for row in builtin_tools._defs() if row[0] == "read_file")
    validated = tools.validate_arguments(
        "read_file", {"path": "file", "offset": None, "limit": None}, definition[3]
    )
    assert validated == {"path": "file", "offset": None, "limit": None}


@pytest.mark.asyncio
async def test_broker_large_read_limit_returns_bounded_pages(file_scope):
    path = file_scope / "many-lines.txt"
    path.write_bytes(b"x\n" * (builtin_tools.MAX_READ_LINES + 3))
    registry = tools.ToolRegistry()
    builtin_tools.register(registry)
    broker = CapabilityBroker(
        registry=registry, runtime_registry=StaticRuntimeRegistry(),
        enabled_resolver=lambda: {"read_file"},
    )
    pages = []
    for index, offset in enumerate((1, builtin_tools.MAX_READ_LINES + 1)):
        receipt = await broker.invoke_name(
            "read_file", {"path": str(path), "offset": offset, "limit": 50_000},
            InvocationContext(
                chat_id="chat-read", run_id="run-read",
                outer_tool_call_id="outer-read", cell_execution_id="cell-read",
                nested_call_id=f"page-{index}",
                catalog_release_id="astb.test.release.v1",
                surface="ipython",
            ),
        )
        assert receipt.ok, receipt.to_dict()
        assert receipt.result_metadata["offset"] == offset
        assert receipt.result_metadata["end"] == min(
            offset + builtin_tools.MAX_READ_LINES - 1, builtin_tools.MAX_READ_LINES + 3
        )
        assert receipt.result_metadata["truncated"] is (index == 0)
        pages.append(receipt.result_value)
    assert [page["text"] for page in pages] == ["x\n" * builtin_tools.MAX_READ_LINES, "x\n" * 3]
    assert "".join(page["text"] for page in pages) == path.read_text(encoding="utf-8")
    assert pages[0]["complete_file"] is False
    assert pages[1]["complete_file"] is False  # tail window is not the whole file
    assert pages[0]["next_offset"] == builtin_tools.MAX_READ_LINES + 1
    assert pages[1]["next_offset"] is None


@pytest.mark.parametrize("limit", [0, -1])
def test_read_limit_still_rejects_nonpositive_values(limit):
    definition = next(row for row in builtin_tools._defs() if row[0] == "read_file")
    with pytest.raises(tools.ToolError, match="minimum"):
        tools.validate_arguments("read_file", {"path": "file", "limit": limit}, definition[3])


def test_apply_patch_description_names_the_creation_action():
    definition = next(row for row in builtin_tools._defs() if row[0] == "apply_patch")
    params, description = definition[3], definition[4]

    assert params["changes"]["items"]["properties"]["action"]["enum"] == [
        "create", "write", "replace", "delete",
    ]
    assert params["changes"]["required"] is False
    assert params["changes"]["coerce_singleton_object"] is True
    assert {"change", "path", "content", "find", "replace"} <= set(params)
    assert "action='create' or action='write' is optional" in description


@pytest.mark.asyncio
async def test_apply_patch_accepts_create_as_write_alias(file_scope):
    path = file_scope / "created.txt"

    result = await builtin_tools.apply_patch({
        "path": str(path),
        "action": "create",
        "content": "created\n",
    })

    assert path.read_text(encoding="utf-8") == "created\n"
    assert "- write " in str(result)


@pytest.mark.asyncio
async def test_read_file_keeps_native_display_and_content_projection(file_scope):
    path = file_scope / "projection.txt"
    path.write_bytes(b"alpha\nbeta\n")

    result = await builtin_tools.read_file({"path": str(path)})

    assert isinstance(result, str)
    assert "lines 1-2 of 2" in result
    assert result.programmatic_value == "alpha\nbeta\n"
    assert result.receipt_metadata["projection"] == "file-content-v2"
    assert result.receipt_metadata["ends_with_newline"] is True
    assert len(result.receipt_metadata["content_sha256"]) == 64


@pytest.mark.asyncio
async def test_default_programmatic_read_is_complete_when_display_is_bounded(file_scope):
    path = file_scope / "r3-client.py"
    lines = [f"value_{index} = {index}  # {'x' * 30}\n" for index in range(291)]
    lines.append("assert value_290 == 290\n")
    source = "".join(lines)
    path.write_text(source, encoding="utf-8", newline="")
    result = await builtin_tools.read_file({"path": str(path)})
    assert result.programmatic_value == source
    assert len(result.programmatic_value.splitlines()) == 292
    assert result.receipt_metadata["truncated"] is True
    assert "Use offset=" in result
    compile(result.programmatic_value, str(path), "exec")
    assert hashlib.sha256(result.programmatic_value.encode()).hexdigest() == hashlib.sha256(source.encode()).hexdigest()


@pytest.mark.asyncio
async def test_explicit_tail_window_never_claims_complete_file(file_scope):
    path = file_scope / "window.txt"
    path.write_text("a\nb\nc\n", encoding="utf-8")
    result = await builtin_tools.read_file({"path": str(path), "offset": 2, "limit": 20})
    value = result.programmatic_value
    assert value["text"] == path.read_bytes().decode("utf-8").splitlines(keepends=True)[1] + path.read_bytes().decode("utf-8").splitlines(keepends=True)[2]
    assert value["complete_file"] is False
    assert value["next_offset"] is None
    assert value["offset"] == 2 and value["end"] == 3 and value["total_lines"] == 3
    decoded = _decode_host_result(value, object())
    assert isinstance(decoded, Variant1FileReadResult)
    assert decoded.text == value["text"] and decoded.complete_file is False
    assert "offset=2" in str(decoded) and "complete_file=False" in str(decoded)


@pytest.mark.asyncio
async def test_over_programmatic_cap_uses_existing_scoped_cas(file_scope, monkeypatch):
    path = file_scope / "artifact.txt"
    content = "complete evidence\n" * 20
    path.write_text(content, encoding="utf-8", newline="")
    monkeypatch.setattr(builtin_tools, "MAX_COMPLETE_PROGRAMMATIC_BYTES", 32)
    registry = tools.ToolRegistry()
    builtin_tools.register(registry)
    store = ContentAddressedArtifactStore(str(file_scope / "cas"))
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=StaticRuntimeRegistry(),
        enabled_resolver=lambda: {"read_file"},
        artifact_store=store,
    )
    receipt = await broker.invoke_name(
        "read_file", {"path": str(path)},
        InvocationContext(
            chat_id="chat-read", run_id="run-read", outer_tool_call_id="outer-read",
            cell_execution_id="cell-read", nested_call_id="complete-artifact",
            catalog_release_id="astb.test.release.v1", surface="ipython",
        ),
    )
    value = receipt.result_value
    assert value["schema"] == "variant1.file-read-result.v2"
    assert value["text"] is None and value["complete_file"] is True
    assert store.read_bytes_scoped(value["artifact_ref"], "chat-read") == content.encode()
    assert value["full_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert receipt.result_metadata["programmatic_artifact_ref"] == value["artifact_ref"]
    assert any(ref.ref == value["artifact_ref"] for ref in receipt.artifact_refs)


@pytest.mark.asyncio
async def test_invalid_utf8_decoded_expansion_has_precise_nonprefix_failure(file_scope, monkeypatch):
    path = file_scope / "invalid-large.txt"
    path.write_bytes(b"\xff" * 20)
    monkeypatch.setattr(builtin_tools, "MAX_COMPLETE_PROGRAMMATIC_BYTES", 10)
    monkeypatch.setattr(builtin_tools, "MAX_COMPLETE_ARTIFACT_BYTES", 50)
    with pytest.raises(tools.ToolError) as exc:
        await builtin_tools.read_file({"path": str(path)})
    assert exc.value.code == "decoded_complete_read_exceeds_artifact_limit"
    assert "UTF-8 replacement" in str(exc.value)


@pytest.mark.asyncio
async def test_read_file_detects_in_place_change_during_snapshot(file_scope, monkeypatch):
    path = file_scope / "race.txt"
    old = b"old snapshot\n"
    new = b"new snapshot expanded\n"
    path.write_bytes(old)
    real_open = builtins.open

    class SwapAfterRead:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): return self
        def __exit__(self, *args): return self.handle.__exit__(*args)
        def __getattr__(self, name): return getattr(self.handle, name)
        def read(self, *args):
            value = self.handle.read(*args)
            with real_open(path, "r+b") as writer:
                writer.seek(0)
                writer.write(new)
                writer.flush()
                os.fsync(writer.fileno())
            return value

    def racing_open(target, mode="r", *args, **kwargs):
        handle = real_open(target, mode, *args, **kwargs)
        return SwapAfterRead(handle) if os.path.normcase(str(target)) == os.path.normcase(str(path)) and mode == "rb" else handle

    monkeypatch.setattr(builtins, "open", racing_open)
    with pytest.raises(tools.ToolError, match="file changed during read") as exc:
        await builtin_tools.read_file({"path": str(path)})
    assert exc.value.code == "file_changed_during_read"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"a\nb\n", b"a\r\nb\r\n", b"a\nb", b"a\xffb\n"])
async def test_read_file_complete_hashes_match_decoded_newline_policy(file_scope, raw):
    path = file_scope / "encoding.txt"
    path.write_bytes(raw)
    result = await builtin_tools.read_file({"path": str(path)})
    decoded = raw.decode("utf-8", errors="replace")
    assert result.programmatic_value == decoded
    assert result.receipt_metadata["programmatic_bytes"] == len(decoded.encode())
    assert result.receipt_metadata["programmatic_sha256"] == hashlib.sha256(decoded.encode()).hexdigest()
    assert result.receipt_metadata["source_sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.asyncio
async def test_explicit_empty_and_long_line_results_are_unambiguous(file_scope, monkeypatch):
    empty = file_scope / "empty.txt"
    empty.write_bytes(b"")
    empty_value = (await builtin_tools.read_file({"path": str(empty), "limit": 1})).programmatic_value
    assert empty_value["complete_file"] is True and empty_value["text"] == ""
    long_path = file_scope / "long.txt"
    long_path.write_text("x" * 100 + "\n", encoding="utf-8")
    monkeypatch.setattr(builtin_tools, "MAX_READ_BYTES", 12)
    result = await builtin_tools.read_file({"path": str(long_path), "limit": 1})
    assert result.programmatic_value["complete_file"] is False
    assert result.receipt_metadata["display_truncated"] is True
    assert result.receipt_metadata["programmatic_file_complete"] is False


@pytest.mark.asyncio
async def test_read_file_missing_exact_path_does_not_imply_a_search(file_scope):
    nested = file_scope / "nested"
    nested.mkdir()
    (nested / "app.py").write_text("print('found')", encoding="utf-8")

    with pytest.raises(tools.ToolError) as exc:
        await builtin_tools.read_file({"path": str(file_scope / "app.py")})

    message = str(exc.value)
    assert "exact path" in message
    assert "does not search subdirectories" in message
    assert "use glob" in message


@pytest.mark.asyncio
async def test_glob_uses_real_patterns_and_recursive_double_star(file_scope):
    (file_scope / "alpha.py").write_text("", encoding="utf-8")
    (file_scope / "alpha_py.txt").write_text("", encoding="utf-8")
    src = file_scope / "src"
    src.mkdir()
    (src / "top.ts").write_text("", encoding="utf-8")
    deep = src / "deep"
    deep.mkdir()
    (deep / "nested.ts").write_text("", encoding="utf-8")

    py = await builtin_tools.glob_files({"path": str(file_scope), "pattern": "*.py"})
    assert "alpha.py" in py
    assert "alpha_py.txt" not in py

    ts = await builtin_tools.glob_files({
        "path": str(file_scope), "pattern": "src/**/*.ts",
    })
    assert "top.ts" in ts
    assert "nested.ts" in ts

    shallow = await builtin_tools.glob_files({
        "path": str(file_scope), "pattern": "src/*.ts",
    })
    assert "top.ts" in shallow
    assert "nested.ts" not in shallow

    recursive_all = await builtin_tools.glob_files({
        "path": str(file_scope), "pattern": "**/*",
    })
    assert "4 match(es)" in recursive_all
    assert "alpha.py" in recursive_all
    assert "alpha_py.txt" in recursive_all
    assert "top.ts" in recursive_all
    assert "nested.ts" in recursive_all


@pytest.mark.asyncio
async def test_bare_glob_reports_directory_truncation(file_scope, monkeypatch):
    monkeypatch.setattr(builtin_tools, "MAX_LIST", 2)
    for name in ("a.txt", "b.txt", "c.txt"):
        (file_scope / name).write_text(name, encoding="utf-8")

    result = await builtin_tools.glob_files({"path": str(file_scope)})

    assert "2 of 3 items" in result
    assert "truncated" in result
    assert "c.txt" not in result


@pytest.mark.asyncio
async def test_grep_skips_ignored_generated_and_binary_files(file_scope):
    (file_scope / "kept.txt").write_text("needle", encoding="utf-8")
    ignored = file_scope / "node_modules"
    ignored.mkdir()
    (ignored / "noise.txt").write_text("needle", encoding="utf-8")
    (file_scope / "binary.bin").write_bytes(b"needle\0more")

    result = await builtin_tools.grep_files({
        "path": str(file_scope), "pattern": "needle",
    })
    assert "kept.txt" in result
    assert "noise.txt" not in result
    assert "binary.bin" not in result


@pytest.mark.asyncio
async def test_glob_uses_the_same_ignore_rules_as_grep(file_scope):
    (file_scope / "kept.txt").write_text("kept", encoding="utf-8")
    ignored = file_scope / "node_modules"
    ignored.mkdir()
    (ignored / "noise.txt").write_text("ignored", encoding="utf-8")
    cache = file_scope / "cache"
    cache.mkdir()
    (cache / "generated.txt").write_text("ignored", encoding="utf-8")
    (file_scope / ".gitignore").write_text("cache/\n", encoding="utf-8")

    recursive = await builtin_tools.glob_files({
        "path": str(file_scope), "pattern": "**/*.txt",
    })
    listing = await builtin_tools.glob_files({"path": str(file_scope)})

    assert "kept.txt" in recursive
    assert "noise.txt" not in recursive
    assert "generated.txt" not in recursive
    assert "node_modules" not in listing
    assert "[dir] cache" not in listing


@pytest.mark.asyncio
async def test_grep_reports_when_result_limit_stops_search(file_scope):
    (file_scope / "many.txt").write_text(
        "needle one\nneedle two\nneedle three\n", encoding="utf-8")

    result = await builtin_tools.grep_files({
        "path": str(file_scope), "pattern": "needle", "limit": 2,
    })

    assert "2 match(es)" in result
    assert "result limit reached" in result
    assert "more matches may exist" in result


@pytest.mark.asyncio
async def test_apply_patch_batches_write_replace_and_delete(file_scope):
    existing = file_scope / "existing.txt"
    removed = file_scope / "removed.txt"
    created = file_scope / "created.txt"
    existing.write_text("old old", encoding="utf-8")
    removed.write_text("gone", encoding="utf-8")

    result = await builtin_tools.apply_patch({"changes": [
        {"path": str(existing), "action": "replace", "find": "old",
         "replace": "new", "count": 1},
        {"path": str(created), "action": "write", "content": "created"},
        {"path": str(removed), "action": "delete"},
    ]})

    assert "Applied 3 file operation(s)" in result
    assert existing.read_text(encoding="utf-8") == "new old"
    assert created.read_text(encoding="utf-8") == "created"
    assert not removed.exists()


@pytest.mark.asyncio
async def test_apply_patch_rollback_wal_is_owned_by_the_broker_operation(
    file_scope, monkeypatch,
):
    target = file_scope / "broker-owned.txt"
    arguments = {"changes": [
        {"path": str(target), "action": "write", "content": "owned"},
    ]}
    captured: list[dict] = []
    real_write = builtin_tools._write_patch_journal

    def capture(path, document):
        captured.append(json.loads(json.dumps(document)))
        return real_write(path, document)

    monkeypatch.setattr(builtin_tools, "_write_patch_journal", capture)
    registry = tools.ToolRegistry()
    builtin_tools.register(registry)
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=StaticRuntimeRegistry(),
        enabled_resolver=lambda: {"apply_patch"},
    )
    context = InvocationContext(
        chat_id="chat-patch",
        run_id="run-patch",
        outer_tool_call_id="outer-patch",
        cell_execution_id="cell-patch",
        nested_call_id="nested-patch",
        catalog_release_id="astb.test.release.v1",
        idempotency_key="idem-patch",
    )

    receipt = await broker.invoke_name("apply_patch", arguments, context)

    assert receipt.ok
    assert target.read_text(encoding="utf-8") == "owned"
    assert captured
    wal = captured[0]
    assert wal["schema"] == "variant1.file-patch-rollback-wal.v2"
    assert wal["role"] == "internal_rollback_wal"
    operation = wal["broker_operation"]
    assert operation["outer_tool_call_id"] == "outer-patch"
    assert operation["nested_call_id"] == "nested-patch"
    assert operation["idempotency_key"] == "idem-patch"
    assert operation["operation_id"].startswith("capop_")
    assert operation["request_fingerprint"] == capability_request_fingerprint(
        "apply_patch", arguments,
    )
    assert wal["transaction_id"] == captured[-1]["transaction_id"]


@pytest.mark.asyncio
async def test_apply_patch_applies_repeated_paths_in_order(file_scope):
    target = file_scope / "ordered.txt"
    target.write_text("first=one\nsecond=two\nstatus=pending\n", encoding="utf-8")

    result = await builtin_tools.apply_patch({"changes": [
        {"path": str(target), "action": "replace", "find": "one", "replace": "ONE"},
        {"path": str(target), "action": "replace", "find": "two", "replace": "TWO"},
        {"path": str(target), "action": "replace", "find": "pending", "replace": "complete"},
    ]})

    assert target.read_text(encoding="utf-8") == "first=ONE\nsecond=TWO\nstatus=complete\n"
    assert "Applied 3 file operation(s)" in result


@pytest.mark.asyncio
async def test_apply_patch_infers_unambiguous_write_and_replace_actions(file_scope):
    target = file_scope / "inferred.txt"

    await builtin_tools.apply_patch({"changes": [
        {"path": str(target), "content": "before"},
    ]})
    await builtin_tools.apply_patch({"changes": [
        {"path": str(target), "find": "before", "replace": "after"},
    ]})

    assert target.read_text(encoding="utf-8") == "after"


@pytest.mark.asyncio
async def test_apply_patch_normalizes_single_object_and_direct_forms(file_scope):
    target = file_scope / "natural-shapes.txt"

    await builtin_tools.apply_patch({
        "changes": {"path": str(target), "content": "before"},
    })
    await builtin_tools.apply_patch({
        "change": {"path": str(target), "find": "before", "replace": "middle"},
    })
    await builtin_tools.apply_patch({
        "path": str(target), "find": "middle", "replace": "after",
    })

    assert target.read_text(encoding="utf-8") == "after"


@pytest.mark.asyncio
async def test_registered_apply_patch_normalizes_single_changes_object(file_scope):
    target = file_scope / "registered-singleton.txt"
    registry = tools.ToolRegistry()
    builtin_tools.register(registry)

    result = await registry.get("apply_patch").run({
        "changes": {"path": str(target), "content": "normalized"},
    })

    assert target.read_text(encoding="utf-8") == "normalized"
    assert "Applied 1 file operation(s)" in result


@pytest.mark.asyncio
async def test_apply_patch_rejects_ambiguous_public_forms(file_scope):
    target = file_scope / "ambiguous.txt"
    with pytest.raises(tools.ToolError, match="exactly one"):
        await builtin_tools.apply_patch({
            "changes": [{"path": str(target), "content": "one"}],
            "path": str(target),
            "content": "two",
        })


@pytest.mark.asyncio
async def test_apply_patch_rolls_back_prior_files_on_commit_failure(file_scope, monkeypatch):
    first = file_scope / "first.txt"
    second = file_scope / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    real_replace = os.replace

    def fail_second_candidate(source, destination):
        if ".variant1-patch-" in str(source) and str(destination) == str(second):
            raise OSError("injected commit failure")
        return real_replace(source, destination)

    monkeypatch.setattr(builtin_tools.os, "replace", fail_second_candidate)
    with pytest.raises(OSError, match="injected commit failure"):
        await builtin_tools.apply_patch({"changes": [
            {"path": str(first), "action": "write", "content": "changed one"},
            {"path": str(second), "action": "write", "content": "changed two"},
        ]})

    assert first.read_text(encoding="utf-8") == "one"
    assert second.read_text(encoding="utf-8") == "two"
    assert not list(file_scope.glob(".variant1-*.tmp"))


def test_apply_patch_recovers_a_process_crash_after_partial_replacement(
    file_scope, monkeypatch,
):
    first = file_scope / "crash-first.txt"
    second = file_scope / "crash-second.txt"
    first.write_text("before-one", encoding="utf-8")
    second.write_text("before-two", encoding="utf-8")
    prepared = builtin_tools._prepare_patch({"changes": [
        {"path": str(first), "action": "write", "content": "after-one"},
        {"path": str(second), "action": "write", "content": "after-two"},
    ]})
    real_replace = os.replace

    def terminate_between_files(source, destination):
        if str(source).endswith(".candidate") and str(destination) == str(second):
            raise SystemExit("simulated process termination")
        return real_replace(source, destination)

    monkeypatch.setattr(builtin_tools.os, "replace", terminate_between_files)
    with pytest.raises(SystemExit, match="simulated process termination"):
        builtin_tools._apply_direct_patch(prepared)

    assert first.read_text(encoding="utf-8") == "after-one"
    assert second.read_text(encoding="utf-8") == "before-two"
    journal_root = file_scope / "patch-transactions"
    assert list(journal_root.glob("*.json"))

    monkeypatch.setattr(builtin_tools.os, "replace", real_replace)
    assert builtin_tools._recover_patch_transactions() == 1
    assert first.read_text(encoding="utf-8") == "before-one"
    assert second.read_text(encoding="utf-8") == "before-two"
    assert not list(journal_root.glob("*.json"))
    assert not list(file_scope.glob(".variant1-patch-*"))
