"""Run-scoped path resolution for file tools."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from file_paths import effective_path
import host_prompt
from host_run_context import make_run_context
from project_context import host_project_context
from project_context import chat_project_context
from project_context import ProjectBindingError
from run_context import Variant1RunContext, bind_run_context
from work_fabric.scope import WorkScope


def test_relative_path_uses_current_project_directory(
    tmp_path: Path,
):
    selected = tmp_path / "selected"
    selected.mkdir()
    ctx = Variant1RunContext.create(
        source="chat",
        metadata={"working_directory": str(selected)},
    )

    with bind_run_context(ctx):
        assert effective_path("app.py") == str((selected / "app.py").resolve())


def test_drive_less_posix_path_uses_project_directory_on_windows(tmp_path: Path):
    selected = tmp_path / "selected"
    selected.mkdir()
    ctx = Variant1RunContext.create(
        source="chat",
        metadata={"working_directory": str(selected)},
    )

    with bind_run_context(ctx):
        result = effective_path("/app.py")

    expected = (selected / "app.py").resolve() if os.name == "nt" else Path("/app.py")
    assert result == str(expected)


def test_host_project_context_uses_packaged_data_or_deployment_override(
    tmp_path: Path, monkeypatch,
):
    app_root = tmp_path / "resources"
    data_dir = tmp_path / "user-data"
    config_dir = data_dir / "config"
    selected = tmp_path / "selected"
    for path in (app_root, config_dir, selected):
        path.mkdir(parents=True, exist_ok=True)
    host = SimpleNamespace(
        app_root=str(app_root), data_dir=str(data_dir), config_dir=str(config_dir),
    )

    monkeypatch.delenv("VARIANT1_PROJECT_ROOT", raising=False)
    assert host_project_context(host).cwd == str(data_dir.resolve())

    monkeypatch.setenv("VARIANT1_PROJECT_ROOT", str(selected))
    assert host_project_context(host).cwd == str(selected.resolve())


def test_chat_project_context_prefers_durable_chat_binding(tmp_path: Path):
    fallback = tmp_path / "fallback"
    selected = tmp_path / "selected"
    fallback.mkdir()
    selected.mkdir()
    sessions = SimpleNamespace(
        get_project=lambda chat_id: (
            {"root": str(selected), "name": "selected"}
            if chat_id == "chat-a" else None
        ),
    )
    host = SimpleNamespace(
        app_root=str(fallback), data_dir=str(fallback),
        require_runtime=lambda: SimpleNamespace(sessions=sessions),
    )

    assert chat_project_context(host, "chat-a").cwd == str(selected.resolve())
    assert chat_project_context(host, "chat-b").cwd == str(fallback.resolve())


def test_bound_missing_project_fails_instead_of_redirecting(tmp_path: Path):
    fallback = tmp_path / "fallback"
    missing = tmp_path / "removed-project"
    fallback.mkdir()
    sessions = SimpleNamespace(
        get_project=lambda _chat_id: {
            "root": str(missing), "name": "removed-project",
        },
    )
    host = SimpleNamespace(
        app_root=str(fallback), data_dir=str(fallback),
        require_runtime=lambda: SimpleNamespace(sessions=sessions),
    )

    with pytest.raises(ProjectBindingError, match="unavailable"):
        chat_project_context(host, "chat-a")

    sessions.get_project = lambda _chat_id: (_ for _ in ()).throw(
        OSError("database unavailable")
    )
    with pytest.raises(ProjectBindingError, match="could not resolve"):
        chat_project_context(host, "chat-a")


def test_new_chat_run_prompt_and_goal_roots_share_bound_project(tmp_path: Path):
    from goals.host_handlers import GoalHostHandlers

    fallback = tmp_path / "fallback"
    selected = tmp_path / "selected"
    fallback.mkdir()
    selected.mkdir()
    sessions = SimpleNamespace(
        get_project=lambda chat_id: (
            {"root": str(selected), "name": "selected"}
            if chat_id == "chat-a" else None
        ),
    )
    runtime = SimpleNamespace(
        sessions=sessions,
        session_runtimes=SimpleNamespace(
            ensure_runtime=lambda _chat_id: SimpleNamespace(
                to_dict=lambda: {"runtime_chat_id": "chat-a"},
            ),
        ),
        memory=SimpleNamespace(
            store=SimpleNamespace(render_profile=lambda: ""),
        ),
    )
    host = SimpleNamespace(
        app_root=str(fallback), data_dir=str(fallback),
        require_runtime=lambda: runtime,
        emit_activity=lambda *_args, **_kwargs: None,
    )
    session = SimpleNamespace(viewed_session_id="chat-a", interrupt=False)

    context = make_run_context(host, "chat", "turn", session=session)
    assert context.metadata["working_directory"] == str(selected.resolve())
    assert context.metadata["project_roots"] == [str(selected.resolve())]
    with bind_run_context(context):
        prompt = host_prompt.prompt_context(host, [])
    assert prompt.cwd == str(selected.resolve())
    assert tuple(prompt.project_roots) == (str(selected.resolve()),)
    assert GoalHostHandlers(host, None)._project_roots(
        WorkScope(chat_id="chat-a"),
    ) == (str(selected.resolve()),)
