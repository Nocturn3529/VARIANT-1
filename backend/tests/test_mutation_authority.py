from types import SimpleNamespace

import pytest

from session_catalog.mutation_authority import MutationAuthorityController
from session_catalog.profiles import ACTION_SURFACE
from session_catalog.support import SupportMatrix
from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository


ROUTE = {
    "provider": "xai",
    "model": "grok-4.6",
    "adapter": "openai.responses",
}


def _stack(tmp_path):
    repository = SessionRuntimeRepository(str(tmp_path / "astb.sqlite3"))
    runtimes = SessionRuntimeRegistry(repository)
    matrix = SupportMatrix.from_config({
        "astb": {"support_matrix": [{
            "profile": ACTION_SURFACE,
            "provider": "xai",
            "model": "grok-4.6",
            "adapter": "openai.responses",
            "status": "qualified",
        }]}
    })
    flags = {"mutation_frozen": False}
    session_control = SimpleNamespace(
        mutation_allowed=lambda: not flags["mutation_frozen"]
    )
    runtime = SimpleNamespace(
        session_runtimes=runtimes,
        session_control=session_control,
    )
    host = SimpleNamespace(
        router=SimpleNamespace(_support_matrix=matrix),
        require_runtime=lambda: runtime,
        test_flags=flags,
    )
    return repository, runtimes, MutationAuthorityController(host, object())


def test_mutation_toggle_is_off_by_default_and_never_changes_identity(tmp_path):
    _repository, runtimes, migration = _stack(tmp_path)
    before = runtimes.ensure_runtime("toggle-chat", is_new=True)

    initial = migration.mutation_toggle_status("toggle-chat", **ROUTE)
    assert initial["enabled"] is False
    assert initial["available"] is True

    enabled = migration.set_mutation(
        "toggle-chat", enabled=True, expected_revision=0, **ROUTE
    )
    disabled = migration.set_mutation(
        "toggle-chat", enabled=False, expected_revision=1, **ROUTE
    )
    after = runtimes.runtime("toggle-chat")

    assert enabled["authority_revision"] == 1
    assert disabled["authority_revision"] == 2
    assert after.identity == before.identity
    assert after.identity.action_surface == ACTION_SURFACE


def test_mutation_toggle_rejects_a_stale_composer_revision(tmp_path):
    _repository, runtimes, authority = _stack(tmp_path)
    runtimes.ensure_runtime("stale-chat", is_new=True)
    authority.set_mutation(
        "stale-chat", enabled=True, expected_revision=0, **ROUTE
    )

    with pytest.raises(RuntimeError, match="authority CAS failed"):
        authority.set_mutation(
            "stale-chat", enabled=False, expected_revision=0, **ROUTE
        )

    current = runtimes.runtime("stale-chat")
    assert current.mutation_write_enabled is True
    assert current.mutation_authority_revision == 1


def test_history_does_not_lock_the_toggle(tmp_path):
    repository, runtimes, migration = _stack(tmp_path)
    runtimes.ensure_runtime("history-chat", is_new=True)
    repository.link_thread("history-chat", "thread-history", source="chat")

    status = migration.mutation_toggle_status("history-chat", **ROUTE)
    assert status["available"] is True
    assert migration.set_mutation(
        "history-chat", enabled=True, expected_revision=0, **ROUTE
    )["mutation_enabled"] is True


def test_operator_freeze_blocks_elevation_but_not_turning_off(tmp_path):
    _repository, runtimes, migration = _stack(tmp_path)
    runtimes.ensure_runtime("frozen-chat", is_new=True)
    migration.host.test_flags["mutation_frozen"] = True

    with pytest.raises(RuntimeError, match="frozen"):
        migration.set_mutation(
            "frozen-chat", enabled=True, expected_revision=0, **ROUTE
        )

    migration.host.test_flags["mutation_frozen"] = False
    migration.set_mutation(
        "frozen-chat", enabled=True, expected_revision=0, **ROUTE
    )
    migration.host.test_flags["mutation_frozen"] = True
    assert migration.set_mutation(
        "frozen-chat", enabled=False, expected_revision=1, **ROUTE
    )["mutation_enabled"] is False


def test_pending_off_checkpoint_blocks_authority_elevation(tmp_path, monkeypatch):
    import agent_engine

    repository, runtimes, migration = _stack(tmp_path)
    runtimes.ensure_runtime("pending-chat", is_new=True)
    repository.link_thread("pending-chat", "thread-pending", source="chat")
    monkeypatch.setattr(
        agent_engine,
        "mutation_elevation_blocked_by_threads",
        lambda thread_ids, checkpointer=None: (
            True,
            "pending tool call was saved with mutation writes off",
        ),
    )

    status = migration.mutation_toggle_status("pending-chat", **ROUTE)
    assert any(
        row["code"] == "pending_checkpoint_authority"
        for row in status["blockers"]
    )


def test_active_run_blocks_toggle_but_live_kernel_does_not(tmp_path):
    _repository, runtimes, migration = _stack(tmp_path)
    runtimes.ensure_runtime("busy-chat", is_new=True)
    admission = runtimes.try_reserve_run("busy-chat")
    assert admission
    with pytest.raises(RuntimeError, match="active turn"):
        migration.set_mutation(
            "busy-chat", enabled=True, expected_revision=0, **ROUTE
        )
    runtimes.finish_run(admission, status="cancelled")

    lease = SimpleNamespace(state="ready")
    runtimes.install_kernel_lease("busy-chat", lease)
    assert migration.set_mutation(
        "busy-chat", enabled=True, expected_revision=0, **ROUTE
    )["mutation_enabled"] is True
