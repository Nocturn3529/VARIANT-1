from __future__ import annotations

import asyncio
import math
import sqlite3
from types import SimpleNamespace

import pytest

from background_tasks import OwnedTaskSet
from capability_broker import InvocationContext
from core_invariants import (
    InjectedFault,
    StrictJSONError,
    cancellation_is_requested,
    canonical_json,
    exhaustive_keyset_pages,
    inject_faults,
    request_fingerprint,
    sqlite_session_connection,
    sqlite_unit_of_work,
    sqlite_wal_connection,
    sqlite_writer_lock,
)
from process_tree import settle_process
from work_fabric.handles import remote_handle_envelope


def test_strict_canonical_json_and_request_fingerprints_have_one_contract():
    assert canonical_json({"b": [2, 1], "a": 1}) == canonical_json(
        {"a": 1, "b": (2, 1)}
    )
    assert request_fingerprint("write", {"value": 1}) == request_fingerprint(
        "write", {"value": 1}
    )
    assert request_fingerprint("write", {"value": 1}) != request_fingerprint(
        "write", {"value": 2}
    )
    assert request_fingerprint("write", {"value": 1}) != request_fingerprint(
        "read", {"value": 1}
    )
    with pytest.raises(StrictJSONError, match=r"non-finite number at \$\.value"):
        canonical_json({"value": math.nan})
    with pytest.raises(StrictJSONError, match="non-string JSON key"):
        canonical_json({1: "ambiguous"})
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(StrictJSONError, match="cyclic JSON value"):
        canonical_json(cyclic)


def test_cancellation_predicates_fail_closed_only_for_authority_errors():
    assert cancellation_is_requested(None) is False
    assert cancellation_is_requested(lambda: False) is False
    assert cancellation_is_requested(lambda: True) is True

    def lost_authority():
        raise RuntimeError("lease disappeared")

    def caller_interrupted():
        raise KeyboardInterrupt

    def caller_cancelled():
        raise asyncio.CancelledError

    assert cancellation_is_requested(lost_authority) is True

    class LostObjectAuthority:
        def is_cancelled(self):
            raise RuntimeError("object lease disappeared")

    class LostEventAuthority:
        def is_set(self):
            raise RuntimeError("event authority disappeared")

    assert cancellation_is_requested(LostObjectAuthority()) is True
    assert cancellation_is_requested(LostEventAuthority()) is True
    with pytest.raises(KeyboardInterrupt):
        cancellation_is_requested(caller_interrupted)
    with pytest.raises(asyncio.CancelledError):
        cancellation_is_requested(caller_cancelled)


def test_keyset_iterator_exhausts_normal_caps_and_rejects_stuck_cursor():
    rows = list(range(501))

    def fetch(after, limit):
        start = 0 if after is None else int(after) + 1
        return rows[start:start + limit]

    assert list(exhaustive_keyset_pages(fetch, lambda item: item)) == rows

    with pytest.raises(RuntimeError, match="did not advance"):
        list(exhaustive_keyset_pages(
            lambda _after, _limit: [1], lambda _item: "same", page_size=1,
        ))


def test_shared_sqlite_unit_of_work_rolls_back_injected_precommit_failure(tmp_path):
    path = str(tmp_path / "authority.sqlite3")

    def connect():
        connection = sqlite3.connect(path, isolation_level=None)
        connection.execute("CREATE TABLE IF NOT EXISTS item(value TEXT NOT NULL)")
        return connection

    with inject_faults("test.before_commit"):
        with pytest.raises(InjectedFault, match="test.before_commit"):
            with sqlite_unit_of_work(
                connect, sqlite_writer_lock(path), fault_name="test.before_commit"
            ) as connection:
                connection.execute("INSERT INTO item VALUES ('not-committed')")

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 0


def test_shared_sqlite_connection_profile_is_exact(tmp_path):
    connection = sqlite_wal_connection(str(tmp_path / "profile.sqlite3"))
    try:
        assert connection.isolation_level is None
        assert connection.row_factory is sqlite3.Row
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
    finally:
        connection.close()


def test_shared_session_sqlite_profile_preserves_both_transaction_modes(tmp_path):
    autocommit = sqlite_session_connection(str(tmp_path / "session-auto.sqlite3"))
    transactional = sqlite_session_connection(
        str(tmp_path / "session-transaction.sqlite3"), autocommit=False,
    )
    try:
        for connection in (autocommit, transactional):
            assert connection.row_factory is sqlite3.Row
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000
        assert autocommit.isolation_level is None
        assert transactional.isolation_level == ""
    finally:
        autocommit.close()
        transactional.close()


@pytest.mark.asyncio
async def test_owned_task_cleanup_settles_before_repeated_cancellation_propagates():
    owner = OwnedTaskSet()
    cleaning = asyncio.Event()
    release = asyncio.Event()

    async def child():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()

    owner.spawn(child(), name="stubborn-child")
    cleanup = asyncio.create_task(owner.cancel_all())
    await cleaning.wait()
    cleanup.cancel()
    await asyncio.sleep(0)
    cleanup.cancel()
    await asyncio.sleep(0)
    assert owner.active_count() == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    await asyncio.sleep(0)
    assert owner.active_count() == 0


@pytest.mark.asyncio
async def test_process_reap_settles_before_repeated_cancellation_propagates():
    waiting = asyncio.Event()
    release = asyncio.Event()

    class Process:
        async def wait(self):
            waiting.set()
            await release.wait()
            return 0

    cleanup = asyncio.create_task(settle_process(Process(), timeout_s=2.0))
    await waiting.wait()
    cleanup.cancel()
    await asyncio.sleep(0)
    cleanup.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup


def test_cell_origin_is_immutable_and_attached_to_every_remote_handle():
    context = InvocationContext(
        chat_id="chat-1",
        run_id="run-1",
        outer_tool_call_id="outer-1",
        cell_execution_id="cell-1",
        nested_call_id="nested-1",
        catalog_release_id="astb.test.release.v1",
        kernel_generation="7",
    )
    reference = SimpleNamespace(
        opaque_id="cap-handle",
        handler_revision="handler.v1",
        catalog_release_id="catalog-1",
        slot_id="slot-1",
        slot_version=2,
    )
    broker = SimpleNamespace(ref_for_name=lambda *_args, **_kwargs: reference)

    envelope = remote_handle_envelope(
        service="work",
        kind="job",
        handle_id="job-1",
        generation=1,
        revision=3,
        metadata={"status": "running"},
        broker=broker,
        context=context,
    )

    assert context.to_dict()["cell_origin"] == context.cell_origin.to_dict()
    assert envelope["$variant1_handle"]["metadata"]["cell_origin"] == {
        "chat_id": "chat-1",
        "run_id": "run-1",
        "outer_tool_call_id": "outer-1",
        "cell_execution_id": "cell-1",
        "nested_call_id": "nested-1",
        "kernel_generation": "7",
    }
