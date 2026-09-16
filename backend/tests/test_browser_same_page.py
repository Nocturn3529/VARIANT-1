"""Same-page browser actions use the currently bound fabric session."""

from __future__ import annotations

import pytest

from artifacts import ContentAddressedArtifactStore
from browser_fabric import (
    BrowserBinding,
    bind_browser_binding,
    bind_browser_fabric,
    create_browser_fabric,
)
from test_browser_fabric_phase4 import FakeBrowserAdapter
import tools_web as tools


@pytest.mark.asyncio
async def test_explicit_browser_constraints_override_implicit_binding(tmp_path):
    fabric = _seed_fabric(tmp_path)
    with bind_browser_binding(BrowserBinding()), bind_browser_fabric(fabric):
        _, embedded = await tools._ensure_session({"kind": "embedded"})
        options = {"kind": "managed", "headless": True, "persistent_profile": False}
        _, managed = await tools._ensure_session(options)
        assert managed.kind == "managed" and managed.headless
        assert managed.session_id != embedded.session_id
        assert not fabric.store.get_profile(managed.profile_id).persistent
        _, reused = await tools._ensure_session(options)
        assert reused.session_id == managed.session_id
        _, visible = await tools._ensure_session({**options, "headless": False})
        assert visible.session_id != managed.session_id and not visible.headless
        _, returned = await tools._ensure_session({"kind": "embedded"})
        assert returned.session_id == embedded.session_id
        with pytest.raises(tools.ToolError, match="conflicts with kind"):
            await tools._ensure_session({"session_id": embedded.session_id, **options})
        _, exact = await tools._ensure_session({"session_id": managed.session_id})
        assert exact.session_id == managed.session_id


@pytest.mark.asyncio
async def test_browser_profile_constraints_cannot_silently_change_existing_profile(tmp_path):
    fabric = _seed_fabric(tmp_path)
    with bind_browser_binding(BrowserBinding()), bind_browser_fabric(fabric):
        _, original = await tools._ensure_session({"kind": "managed", "profile_name": "saved"})
        with pytest.raises(tools.ToolError, match="profile conflicts"):
            await tools._ensure_session({"kind": "managed", "profile_name": "saved", "persistent_profile": False})
        _, selected = await tools._ensure_session({"profile_id": original.profile_id})
        assert selected.session_id == original.session_id
        with pytest.raises(tools.ToolError, match="built-in browser is visible"):
            await tools._ensure_session({"kind": "embedded", "headless": True})


def _seed_fabric(tmp_path):
    def factory(kind, profile, session):
        adapter = FakeBrowserAdapter()
        adapter.kind = kind
        return adapter

    return create_browser_fabric(
        data_dir=str(tmp_path / "data"),
        artifact_store=ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
        adapter_factory=factory,
    )


@pytest.mark.asyncio
async def test_browser_attach_uses_admitted_scope_instead_of_pre_kernel_binding(tmp_path):
    from run_context import Variant1RunContext, bind_run_context
    from work_fabric.scope import WorkScope
    fabric = _seed_fabric(tmp_path)
    before = WorkScope(chat_id="browser-chat")
    admitted = before.with_updates(kernel_generation=1, catalog_release_id="catalog-current")
    binding = BrowserBinding(owner_kind="chat", owner_id="browser-chat", scope=before)
    ctx = Variant1RunContext.create(source="chat", run_id="browser-run",
                                   work_scope=admitted, browser_binding=binding)
    with bind_run_context(ctx), bind_browser_fabric(fabric):
        _, embedded = await tools._ensure_session({"kind": "embedded"})
        # Simulate the persisted pointer restored by a subsequent run.
        binding.set_scope(before)
        _, managed = await tools._ensure_session({"kind": "managed", "persistent_profile": False})
        assert managed.kind == "managed"
        assert managed.scope == admitted
        assert binding.scope == admitted and binding.fabric_session_id == managed.session_id
        _, restored = await tools._ensure_session({"kind": "embedded"})
        assert restored.session_id == embedded.session_id


@pytest.mark.asyncio
async def test_browser_click_does_not_reauthorize_current_url(tmp_path):
    fabric = _seed_fabric(tmp_path)
    with bind_browser_binding(BrowserBinding()):
        with bind_browser_fabric(fabric):
            await tools.browser_navigate({"url": "http://127.0.0.1:9222/json"})
            out = await tools.browser_click({
                "target": "mf_aaaaaaaaaaaa_1",
                "position": {"x": 12, "y": 3},
                "force": True,
                "timeout": 2500,
            })

    assert "clicked" in out
    adapter = next(iter(fabric._adapters.values()))
    assert adapter.perform_calls[-1][2]["position"] == {"x": 12, "y": 3}
    assert adapter.perform_calls[-1][2]["force"] is True
    assert adapter.perform_calls[-1][2]["timeout_ms"] == 2500


@pytest.mark.asyncio
async def test_browser_click_accepts_returned_element_ref_mapping(tmp_path):
    fabric = _seed_fabric(tmp_path)
    with bind_browser_binding(BrowserBinding()):
        with bind_browser_fabric(fabric):
            await tools.browser_navigate({"url": "http://127.0.0.1:9222/json"})
            out = await tools.browser_click({
                "target": {
                    "schema": "variant1.browser-element-ref.v1",
                    "backend_ref": "mf_aaaaaaaaaaaa_1",
                },
            })

    assert "clicked" in out


@pytest.mark.asyncio
async def test_browser_fill_does_not_reauthorize_current_url(tmp_path):
    fabric = _seed_fabric(tmp_path)
    with bind_browser_binding(BrowserBinding()):
        with bind_browser_fabric(fabric):
            await tools.browser_navigate({"url": "http://192.168.1.1/"})
            out = await tools.browser_fill({"target": "mf_aaaaaaaaaaaa_1", "text": "x"})

    assert "filled" in out
