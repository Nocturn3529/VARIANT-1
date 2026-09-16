from __future__ import annotations

import subprocess
from pathlib import Path

from artifacts import ContentAddressedArtifactStore
from coding import create_coding_runtime
from work_fabric.scope import WorkScope


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _stack(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "variant1@example.test")
    _git(root, "config", "user.name", "VARIANT-1 Test")
    (root / "app.py").write_text("alpha\nbeta\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-m", "initial")
    cas = ContentAddressedArtifactStore(str(tmp_path / "cas"))
    runtime = create_coding_runtime(
        path=str(tmp_path / "coding.sqlite3"),
        managed_root=str(tmp_path / "worktrees"),
        artifact_store=cas,
    )
    return root, runtime


def test_ipython_change_becomes_one_durable_review_with_hunks(tmp_path: Path):
    root, runtime = _stack(tmp_path)
    scope = WorkScope(chat_id="chat-review", workspace_id="workspace-review")
    token = runtime.begin_review_observation(str(root), scope=scope)
    assert token is not None

    (root / "app.py").write_text("alpha\ngamma\n", encoding="utf-8")
    review = runtime.finish_review_observation(
        token, tool="ipython", arguments={"code": "write app.py"},
    )

    assert review is not None
    assert review.summary["origin"] == "ipython"
    assert runtime.review.list(scope=scope)[0].review_id == review.review_id
    projected = runtime.review.project_files(review.review_id)
    assert len(projected) == 1
    assert projected[0]["path"] == "app.py"
    kinds = {
        line["kind"]
        for hunk in projected[0]["hunks"]
        for line in hunk["lines"]
    }
    assert {"add", "del"} <= kinds


def test_apply_patch_untracked_file_is_cas_backed_review_evidence(tmp_path: Path):
    root, runtime = _stack(tmp_path)
    scope = WorkScope(chat_id="chat-patch", workspace_id="workspace-patch")
    token = runtime.begin_review_observation(str(root), scope=scope)
    created = root / "new.py"
    created.write_text("print('new')\n", encoding="utf-8")

    review = runtime.finish_review_observation(
        token,
        tool="apply_patch",
        arguments={"changes": [{"path": str(created), "action": "write"}]},
    )
    projected = runtime.review.project_files(review.review_id)

    assert review.summary["origin"] == "apply_patch"
    assert projected[0]["action"] == "create"
    assert projected[0]["hunks"][0]["lines"][0]["kind"] == "add"
    assert projected[0]["patch_ref"]


def test_run_command_head_move_reviews_committed_and_remaining_work(tmp_path: Path):
    root, runtime = _stack(tmp_path)
    scope = WorkScope(chat_id="chat-command", workspace_id="workspace-command")
    token = runtime.begin_review_observation(str(root), scope=scope)
    assert token is not None
    (root / "app.py").write_text("committed change\n", encoding="utf-8")
    _git(root, "add", "--", "app.py")
    _git(root, "commit", "-m", "change tracked file")
    (root / "after.py").write_text("remaining work\n", encoding="utf-8")

    review = runtime.finish_review_observation(
        token, tool="run_command", arguments={},
    )

    assert review is not None
    assert review.target == "working_since"
    projected = {
        item["path"]: item
        for item in runtime.review.project_files(review.review_id)
    }
    assert {"app.py", "after.py"}.issubset(projected)
    assert projected["app.py"]["hunks"]
    assert projected["after.py"]["action"] == "create"


def test_unchanged_or_non_git_tool_has_no_review_sidecar(tmp_path: Path):
    root, runtime = _stack(tmp_path)
    scope = WorkScope(chat_id="chat-no-change")
    token = runtime.begin_review_observation(str(root), scope=scope)
    assert runtime.finish_review_observation(
        token, tool="run_command", arguments={"command": "git status"},
    ) is None
    assert runtime.begin_review_observation(
        str(tmp_path / "not-a-repository"), scope=scope,
    ) is None


def test_passive_review_never_adopts_a_repository_above_the_admitted_root(
    tmp_path: Path,
):
    root, runtime = _stack(tmp_path)
    nested = root / "nested-workspace"
    nested.mkdir()
    scope = WorkScope(chat_id="chat-scoped-review", workspace_id="workspace-nested")

    assert runtime.begin_review_observation(
        str(nested),
        scope=scope,
        admitted_root=str(nested),
        timeout_s=2.0,
    ) is None
    assert runtime.begin_review_observation(
        str(root),
        scope=scope,
        admitted_root=str(root),
        timeout_s=2.0,
    ) is not None


def test_detailed_review_observation_returns_reusable_next_baseline(tmp_path: Path):
    root, runtime = _stack(tmp_path)
    scope = WorkScope(chat_id="chat-review-cache", workspace_id="workspace-review")
    token = runtime.begin_review_observation(
        str(root),
        scope=scope,
        admitted_root=str(root),
        timeout_s=2.0,
    )
    assert token is not None

    (root / "app.py").write_text("changed once\n", encoding="utf-8")
    first = runtime.finish_review_observation_detailed(
        token,
        tool="ipython",
        arguments={"code": "write app.py"},
        timeout_s=2.0,
    )
    assert first["review"] is not None
    assert first["token"]["status_fingerprint"] != token["status_fingerprint"]

    second = runtime.finish_review_observation_detailed(
        first["token"],
        tool="ipython",
        arguments={"code": "read app.py"},
        timeout_s=2.0,
    )
    assert second["review"] is None
    assert second["token"] == first["token"]
