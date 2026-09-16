from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

from artifacts.store import ContentAddressedArtifactStore
from coding import CheckRecipe, create_coding_runtime
from coding.git_process import GitProcess
from execution_hosts import create_execution_runtime
from coding.models import (
    CodingConflict,
    GitCommandError,
    ReviewStale,
    UnsafeWorktreeRemoval,
)
from core_invariants import request_fingerprint
from run_context import Variant1RunContext, bind_run_context
import shell_tool
import tools
from work_fabric.scope import WorkScope


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")


def _run(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repository with spaces"
    root.mkdir()
    _run(root, "init", "-b", "main")
    _run(root, "config", "user.name", "VARIANT-1 Test")
    _run(root, "config", "user.email", "variant1@example.invalid")
    (root / "alpha.txt").write_text("alpha\n", encoding="utf-8")
    _run(root, "add", "--", "alpha.txt")
    _run(root, "commit", "-m", "initial")
    return root


def _runtime(tmp_path: Path, **kwargs):
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    execution = create_execution_runtime(
        data_dir=str(tmp_path / "execution"),
        artifact_store=artifacts,
    )
    return create_coding_runtime(
        path=str(tmp_path / "coding.sqlite3"),
        managed_root=str(tmp_path / "managed worktrees"),
        artifact_store=artifacts,
        process_service=execution.processes,
        **kwargs,
    )


def test_real_git_discovery_status_diff_branches_and_log(tmp_path):
    root = _repo(tmp_path)
    runtime = _runtime(tmp_path)
    scope = WorkScope(chat_id="chat_read", workspace_id="workspace_read")

    repo = runtime.repositories.discover(str(root / "alpha.txt"), scope=scope)
    assert repo.root == os.path.realpath(root)
    assert repo.object_format in {"sha1", "sha256"}
    assert repo.branch == "main"

    unicode_path = root / "unicodé name.txt"
    unicode_path.write_text("one\n", encoding="utf-8")
    status = runtime.process.status(
        str(root), repository_id=repo.repository_id
    ).to_dict()
    assert status["dirty"] is True
    assert {item["path"] for item in status["entries"]} == {"unicodé name.txt"}

    diff = runtime.review.observe_diff(
        repo.repository_id,
        target="uncommitted",
        scope=scope,
    ).to_dict()
    assert diff["status_fingerprint"] == status["fingerprint"]
    assert diff["patch_sha256"]
    untracked = next(item for item in diff["files"] if item["path"] == "unicodé name.txt")
    assert untracked["status"] == "?"
    assert untracked["content_ref"].startswith("artifact://sha256/")
    assert any(item.name == "main" for item in runtime.process.branches(str(root)))
    assert runtime.process.commits(str(root), limit=5)[0].subject == "initial"


@pytest.mark.asyncio
async def test_run_command_absorbs_typed_git_reads_and_mutations(
    tmp_path,
):
    root = _repo(tmp_path)
    runtime = _runtime(tmp_path)
    scope = WorkScope(chat_id="chat_seed_git", workspace_id="workspace_seed_git")
    context = Variant1RunContext.create(
        source="chat",
        work_scope=scope,
        metadata={"working_directory": str(root)},
    )
    with bind_run_context(context):
        with pytest.raises(tools.ToolError, match="unknown argument"):
            await shell_tool.run_command({
                "op": "git_discover", "path": str(root),
            })


@pytest.mark.asyncio
async def test_run_command_absorbs_managed_worktree_lifecycle(
    tmp_path,
):
    root = _repo(tmp_path)
    runtime = _runtime(tmp_path)
    parent_scope = WorkScope(
        chat_id="chat_seed_worktree", workspace_id="workspace_seed_worktree"
    )
    parent_context = Variant1RunContext.create(
        source="chat",
        work_scope=parent_scope,
        metadata={"working_directory": str(root)},
    )
    with bind_run_context(parent_context):
        assert not hasattr(shell_tool, "_run_coding_op")


def test_status_fingerprint_tracks_exact_tracked_and_untracked_bytes(tmp_path):
    root = _repo(tmp_path)
    process = GitProcess()
    tracked = root / "alpha.txt"
    tracked.write_text("first dirty value\n", encoding="utf-8")
    untracked = root / "new.bin"
    untracked.write_bytes(b"one")
    first = process.status(str(root))

    tracked.write_text("other dirty value\n", encoding="utf-8")
    second = process.status(str(root))
    assert second.fingerprint != first.fingerprint

    untracked.write_bytes(b"two")
    third = process.status(str(root))
    assert third.fingerprint != second.fingerprint


def test_machine_parsers_preserve_rename_paths_and_reject_truncation():
    oid_a = b"a" * 40
    oid_b = b"b" * 40
    raw = (
        b"# branch.oid " + oid_a + b"\0"
        b"1 .M N... 100644 100644 100644 " + oid_a + b" " + oid_b
        + b"  leading space.txt\0"
        b"2 R. N... 100644 100644 100644 " + oid_a + b" " + oid_b
        + b" R100 renamed.txt\0old name.txt\0"
        b"? unicode-\xe2\x98\x83.txt\0"
    )
    headers, entries = GitProcess.parse_status(raw)
    assert headers["branch.oid"] == "a" * 40
    assert entries[0].path == " leading space.txt"
    assert entries[1].path == "renamed.txt"
    assert entries[1].original_path == "old name.txt"
    assert entries[2].path == "unicode-☃.txt"
    with pytest.raises(GitCommandError, match="truncated"):
        GitProcess.parse_status(raw[:-1])

    raw_diff = (
        b":100644 100644 " + oid_a + b" " + oid_b
        + b" R100\0old name.txt\0renamed.txt\0"
    )
    parsed = GitProcess._parse_raw_diff(raw_diff)
    assert parsed[0]["original_path"] == "old name.txt"
    assert parsed[0]["path"] == "renamed.txt"
    counts = GitProcess._parse_numstat(b"1\t2\t\0old name.txt\0renamed.txt\0")
    assert counts["renamed.txt"] == (1, 2, False)
    assert GitProcess._parse_numstat(b"-\t-\tbinary.dat\0")["binary.dat"] == (None, None, True)


def test_managed_worktrees_are_isolated_durable_and_removed_non_force(tmp_path):
    root = _repo(tmp_path)
    runtime = _runtime(tmp_path)
    discovery_scope = WorkScope(chat_id="chat_parent", workspace_id="workspace_1")
    repo = runtime.repositories.discover(str(root), scope=discovery_scope)

    one = runtime.worktrees.create(
        repo.repository_id,
        purpose="first child",
        scope=WorkScope(
            chat_id="chat_parent", workspace_id="workspace_1", goal_id="goal_1", step_id="step_1"
        ),
        expected_repository_revision=repo.revision,
        expected_head_oid=repo.head_oid,
        idempotency_key="create-step-1",
    )
    two = runtime.worktrees.create(
        repo.repository_id,
        purpose="second child",
        scope=WorkScope(
            chat_id="chat_parent", workspace_id="workspace_1", goal_id="goal_1", step_id="step_2"
        ),
        expected_repository_revision=repo.revision,
        expected_head_oid=repo.head_oid,
        idempotency_key="create-step-2",
    )
    assert one.root != two.root
    assert one.branch != two.branch
    assert Path(one.root).is_dir() and Path(two.root).is_dir()
    assert Path(one.root).is_relative_to(Path(runtime.worktrees.managed_root))
    reopened = _runtime(tmp_path)
    assert reopened.worktrees.get(one.worktree_id).root == one.root
    assert reopened.repositories.get(repo.repository_id).common_dir == repo.common_dir
    replay = runtime.worktrees.create(
        repo.repository_id,
        purpose="first child",
        scope=WorkScope(
            chat_id="chat_parent", workspace_id="workspace_1", goal_id="goal_1", step_id="step_1"
        ),
        expected_repository_revision=repo.revision,
        expected_head_oid=repo.head_oid,
        idempotency_key="create-step-1",
    )
    assert replay.worktree_id == one.worktree_id

    dirty_path = Path(one.root) / "dirty.txt"
    dirty_path.write_text("do not delete", encoding="utf-8")
    with pytest.raises(UnsafeWorktreeRemoval, match="dirty"):
        runtime.worktrees.remove(
            one.worktree_id,
            scope=one.scope,
            expected_revision=one.revision,
            expected_head_oid=one.head_oid,
        )
    assert dirty_path.is_file()

    current_two = runtime.repository.get_worktree(two.worktree_id)
    retired = runtime.worktrees.remove(
        two.worktree_id,
        scope=current_two.scope,
        expected_revision=current_two.revision,
        expected_head_oid=current_two.head_oid,
        idempotency_key="remove-step-2",
    )
    assert retired.state == "retired"
    assert not Path(two.root).exists()
    assert runtime.repository.get_worktree(two.worktree_id).state == "retired"
    removal_replay = runtime.worktrees.remove(
        two.worktree_id,
        scope=current_two.scope,
        expected_revision=current_two.revision,
        expected_head_oid=current_two.head_oid,
        idempotency_key="remove-step-2",
    )
    assert removal_replay.state == "retired"


def test_four_parallel_children_receive_distinct_managed_worktrees(tmp_path):
    root = _repo(tmp_path)
    runtime = _runtime(tmp_path)
    parent = WorkScope(chat_id="chat_parallel", workspace_id="workspace_parallel")
    repo = runtime.repositories.discover(str(root), scope=parent)

    def create(index: int):
        return runtime.worktrees.create(
            repo.repository_id,
            purpose=f"parallel child {index}",
            scope=parent.with_updates(goal_id="goal_parallel", step_id=f"step_{index}"),
            expected_repository_revision=repo.revision,
            expected_head_oid=repo.head_oid,
            idempotency_key=f"parallel-{index}",
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        trees = list(executor.map(create, range(4)))
    assert len({item.root for item in trees}) == 4
    assert len({item.branch for item in trees}) == 4
    assert {item.scope.step_id for item in trees} == {"step_0", "step_1", "step_2", "step_3"}
    for item in trees:
        marker = Path(item.root) / f"{item.scope.step_id}.txt"
        marker.write_text(item.worktree_id, encoding="utf-8")
    assert all(
        len(list(Path(item.root).glob("step_*.txt"))) == 1
        for item in trees
    )


def test_linked_worktree_discovery_preserves_common_repository_identity(tmp_path):
    root = _repo(tmp_path)
    runtime = _runtime(tmp_path)
    scope = WorkScope(chat_id="chat_identity", workspace_id="workspace_identity")
    repo = runtime.repositories.discover(str(root), scope=scope)
    tree = runtime.worktrees.create(
        repo.repository_id,
        scope=scope.with_updates(step_id="step_identity"),
        expected_repository_revision=repo.revision,
        expected_head_oid=repo.head_oid,
    )

    rediscovered = runtime.repositories.discover(tree.root, scope=scope)
    assert rediscovered.repository_id == repo.repository_id
    assert os.path.normcase(rediscovered.root) == os.path.normcase(repo.root)
    assert os.path.normcase(rediscovered.common_dir) == os.path.normcase(repo.common_dir)


def test_reconcile_completes_create_saga_and_refuses_unpushed_retirement(tmp_path):
    root = _repo(tmp_path)
    runtime = _runtime(tmp_path)
    scope = WorkScope(
        chat_id="chat_recovery",
        workspace_id="workspace_recovery",
        goal_id="goal_recovery",
        step_id="step_recovery",
    )
    repo = runtime.repositories.discover(str(root), scope=scope)
    worktree_id = "wt_recovery_slot"
    target = runtime.worktrees._target(repo.repository_id, worktree_id)
    Path(target).parent.mkdir(parents=True)
    base_oid = _run(root, "rev-parse", "HEAD")
    runtime.repository.create_worktree_reservation(
        worktree_id=worktree_id,
        repository_id=repo.repository_id,
        root=target,
        branch="variant1/recovery-slot",
        base_ref="HEAD",
        base_oid=base_oid,
        purpose="recovery",
        scope=scope.with_updates(worktree_id=worktree_id),
    )
    _run(root, "worktree", "add", "-b", "variant1/recovery-slot", target, base_oid)

    recovered = {item.worktree_id: item for item in runtime.worktrees.reconcile(repo.repository_id)}[
        worktree_id
    ]
    assert recovered.state == "active"
    assert recovered.scope.step_id == "step_recovery"

    (Path(target) / "committed.txt").write_text("unique commit\n", encoding="utf-8")
    _run(Path(target), "add", "--", "committed.txt")
    _run(Path(target), "commit", "-m", "unique unpushed work")
    unique_head = _run(Path(target), "rev-parse", "HEAD")
    with pytest.raises(UnsafeWorktreeRemoval, match="unpushed"):
        runtime.worktrees.remove(
            worktree_id,
            scope=recovered.scope,
            expected_revision=recovered.revision,
            expected_head_oid=unique_head,
        )
    assert Path(target).is_dir()


def test_remove_replays_crash_after_retiring_transition(tmp_path):
    root = _repo(tmp_path)
    runtime = _runtime(tmp_path)
    scope = WorkScope(chat_id="chat_remove_crash", step_id="step_remove_crash")
    repo = runtime.repositories.discover(str(root), scope=scope)
    tree = runtime.worktrees.create(
        repo.repository_id,
        scope=scope,
        expected_repository_revision=repo.revision,
        expected_head_oid=repo.head_oid,
    )
    key = "remove-crash-replay"
    request = {
        "worktree_id": tree.worktree_id,
        "revision": tree.revision,
        "expected_head_oid": tree.head_oid,
        "allow_unpushed": False,
    }
    runtime.repository.reserve_operation(
        repository_id=repo.repository_id,
        worktree_id=tree.worktree_id,
        kind="worktree.remove",
        idempotency_key=key,
        request_fingerprint=request_fingerprint(
            "coding.worktree.remove", request
        ),
        before_oid=tree.head_oid,
        scope=tree.scope,
    )
    runtime.repository.transition_worktree(
        tree.worktree_id,
        expected_revision=tree.revision,
        allowed_states=("active",),
        state="retiring",
        head_oid=tree.head_oid,
    )

    retired = runtime.worktrees.remove(
        tree.worktree_id,
        scope=tree.scope,
        expected_revision=tree.revision,
        expected_head_oid=tree.head_oid,
        idempotency_key=key,
    )
    assert retired.state == "retired"
    assert runtime.repository.list_operations(
        worktree_id=tree.worktree_id, limit=1
    )[0]["state"] == "succeeded"


def test_review_findings_checks_approval_and_staleness(tmp_path):
    root = _repo(tmp_path)
    runtime = _runtime(
        tmp_path,
        recipes={
            "unit": CheckRecipe(
                "unit",
                (sys.executable, "-c", "print('checks passed')"),
                timeout_s=30,
            )
        },
    )
    scope = WorkScope(chat_id="chat_review", workspace_id="workspace_review")
    repo = runtime.repositories.discover(str(root), scope=scope)
    (root / "alpha.txt").write_text("reviewed content\n", encoding="utf-8")

    review = runtime.review.start(
        repo.repository_id,
        target="uncommitted",
        scope=scope,
    )
    assert review.patch_ref.startswith("artifact://sha256/")
    assert runtime.review.files(review.review_id)[0].path == "alpha.txt"
    finding = runtime.review.add_finding(
        review.review_id,
        path="alpha.txt",
        line=1,
        severity="warning",
        title="Verify behavior",
        body="Keep this exact behavior covered.",
        scope=scope,
    )
    assert finding.review_id == review.review_id

    check = runtime.checks.run(review.review_id, "unit", scope=scope)
    assert check.state == "passed"
    assert check.log_ref.startswith("artifact://sha256/")
    processes = runtime.checks.process_service.list(
        owner_kind="review_check",
        owner_id=check.check_run_id,
        scope=scope,
    )
    assert len(processes) == 1
    assert processes[0].exit_code == 0
    approved = runtime.review.approve(
        review.review_id,
        expected_revision=review.revision,
        required_checks=("unit",),
        scope=scope,
    )
    assert approved.approved_head_oid == review.head_oid

    # Keep the same porcelain shape but change exact bytes.
    (root / "alpha.txt").write_text("changed after approval\n", encoding="utf-8")
    with pytest.raises(ReviewStale):
        runtime.review.assert_current(review.review_id, scope=scope)
    assert runtime.review.get(review.review_id).state == "stale"


def test_review_scope_is_applied_before_order_and_limit(tmp_path):
    root = _repo(tmp_path)
    runtime = _runtime(tmp_path)
    wanted_scope = WorkScope(
        chat_id="chat-wanted", workspace_id="workspace-wanted",
    )
    other_scope = WorkScope(
        chat_id="chat-other", workspace_id="workspace-other",
    )
    repo = runtime.repositories.discover(str(root), scope=wanted_scope)
    (root / "alpha.txt").write_text("scope test\n", encoding="utf-8")
    wanted = runtime.review.start(
        repo.repository_id, target="uncommitted", scope=wanted_scope,
    )
    for _index in range(3):
        runtime.review.start(
            repo.repository_id, target="uncommitted", scope=other_scope,
        )

    visible = runtime.review.list(scope=wanted_scope, limit=1)

    assert [item.review_id for item in visible] == [wanted.review_id]


def test_cancelled_check_terminates_its_process_tree(tmp_path):
    root = _repo(tmp_path)
    started = tmp_path / "check-started"
    descendant_output = tmp_path / "descendant-survived"
    child_code = (
        "import time; from pathlib import Path; "
        "time.sleep(1.0); "
        f"Path({str(descendant_output)!r}).write_text('late', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"Path({str(started)!r}).write_text('started', encoding='utf-8'); "
        "time.sleep(30)"
    )
    runtime = _runtime(tmp_path, recipes={
        "cancel-tree": CheckRecipe(
            "cancel-tree", (sys.executable, "-c", parent_code), timeout_s=30,
        ),
    })
    scope = WorkScope(chat_id="chat_cancel_check", workspace_id="workspace_cancel")
    repo = runtime.repositories.discover(str(root), scope=scope)
    (root / "alpha.txt").write_text("check cancellation\n", encoding="utf-8")
    review = runtime.review.start(repo.repository_id, target="uncommitted", scope=scope)
    cancellation = threading.Event()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            runtime.checks.run,
            review.review_id,
            "cancel-tree",
            scope=scope,
            cancellation_requested=cancellation.is_set,
        )
        for _ in range(200):
            if started.exists():
                break
            time.sleep(0.01)
        assert started.exists()
        cancellation.set()
        check = future.result(timeout=10)

    assert check.state == "cancelled"
    assert "cancelled" in check.diagnostic
    time.sleep(1.2)
    assert not descendant_output.exists()


def test_fast_forward_reconciles_git_before_atomic_review_completion(
    tmp_path, monkeypatch,
):
    root = _repo(tmp_path)
    runtime = _runtime(tmp_path)
    scope = WorkScope(chat_id="chat_integrate", workspace_id="workspace_integrate")
    repo = runtime.repositories.discover(str(root), scope=scope)
    base_oid = _run(root, "rev-parse", "HEAD")
    _run(root, "checkout", "-b", "feature")
    (root / "alpha.txt").write_text("feature\n", encoding="utf-8")
    _run(root, "add", "--", "alpha.txt")
    _run(root, "commit", "-m", "feature")
    feature_oid = _run(root, "rev-parse", "HEAD")
    _run(root, "checkout", "main")

    review = runtime.review.start(
        repo.repository_id,
        target="branch",
        base="main",
        head="feature",
        scope=scope,
    )
    approved = runtime.review.approve(
        review.review_id,
        expected_revision=review.revision,
        scope=scope,
    )
    original_complete = runtime.repository.complete_review_integration
    calls = 0

    def fail_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated crash window after Git fast-forward")
        return original_complete(**kwargs)

    monkeypatch.setattr(
        runtime.repository, "complete_review_integration", fail_once
    )
    integrated = runtime.review.integrate_fast_forward(
        approved.review_id,
        integration_root=str(root),
        target_branch="main",
        expected_target_oid=base_oid,
        expected_revision=approved.revision,
        scope=scope,
        idempotency_key="integrate-feature",
    )

    assert calls == 2
    assert _run(root, "rev-parse", "HEAD") == feature_oid
    assert integrated.state == "integrated"
    operation = runtime.repository.get_operation_by_key(
        repo.repository_id, "integrate-feature"
    )
    assert operation["state"] == "succeeded"
    assert operation["after_oid"] == feature_oid
