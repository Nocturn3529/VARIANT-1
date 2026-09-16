from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from coding.checks import CheckRecipe, CheckService
from coding.models import ReviewRecord, ReviewStale
from coding.review import ReviewService
from work_fabric.scope import WorkScope


def _review() -> ReviewRecord:
    return ReviewRecord(
        review_id="review-race", repository_id="repository-race", worktree_id="",
        root="isolated-fake-root", target="working", base_ref="", base_oid="",
        head_ref="HEAD", head_oid="head-1", status_fingerprint="status-1",
        patch_ref="", patch_sha256="", summary={}, state="open",
        approved_head_oid="", scope=WorkScope(chat_id="chat-review"),
        revision=1, created_at=0, updated_at=0,
    )


def test_review_approval_stales_a_external_mutation_at_commit_boundary():
    state = {"review": _review(), "fingerprint": "status-1"}

    class Repository:
        def get_review(self, _review_id):
            return state["review"]

        def transition_review(self, _review_id, *, state: str, **_kwargs):
            if state == "approved":
                # Simulate same-user external FS/Git mutation after the last
                # status read but before approval is durably written.
                state_holder["fingerprint"] = "status-2"
            state_holder["review"] = replace(
                state_holder["review"], state=state,
                revision=state_holder["review"].revision + 1,
            )
            return state_holder["review"]

    state_holder = state
    git = SimpleNamespace(
        status=lambda _root, **_kwargs: SimpleNamespace(
            head_oid="head-1", fingerprint=state_holder["fingerprint"],
        ),
    )
    reviews = ReviewService(Repository(), git, worktrees=object())
    with pytest.raises(ReviewStale, match="no longer match"):
        reviews.approve(
            "review-race", expected_revision=1,
            scope=WorkScope(chat_id="chat-review"),
        )
    assert state_holder["review"].state == "stale"


def test_review_approval_succeeds_when_exact_snapshot_stays_current():
    holder = {"review": _review()}

    class Repository:
        def get_review(self, _identity):
            return holder["review"]

        def transition_review(self, _identity, *, state: str, **_kwargs):
            holder["review"] = replace(
                holder["review"], state=state,
                approved_head_oid="head-1" if state == "approved" else "",
                revision=holder["review"].revision + 1,
            )
            return holder["review"]

    git = SimpleNamespace(
        status=lambda _root, **_kwargs: SimpleNamespace(
            head_oid="head-1", fingerprint="status-1",
        ),
    )
    reviews = ReviewService(Repository(), git, worktrees=object())
    approved = reviews.approve(
        "review-race", expected_revision=1, scope=WorkScope(chat_id="chat-review"),
    )
    assert approved.state == "approved"
    assert approved.approved_head_oid == "head-1"


@pytest.mark.parametrize(
    ("cancelled", "timed_out", "expected"),
    [(True, False, "cancelled"), (False, True, "failed")],
)
def test_check_runtime_cancel_status_is_distinct_from_timeout(cancelled, timed_out, expected):
    review = _review()
    finished = []
    repository = SimpleNamespace(
        create_check=lambda **_kwargs: SimpleNamespace(check_run_id="check-1"),
        finish_check=lambda _identity, **kwargs: finished.append(kwargs) or SimpleNamespace(state=kwargs["state"]),
        get_review=lambda _identity: review,
    )
    reviews = SimpleNamespace(
        assert_current=lambda _identity, **_kwargs: review,
        git=SimpleNamespace(status=lambda _root, **_kwargs: SimpleNamespace(
            head_oid=review.head_oid, fingerprint=review.status_fingerprint,
        )),
    )
    completed = SimpleNamespace(
        cancelled=cancelled, timed_out=timed_out,
        process=SimpleNamespace(exit_code=None),
        stdout=b"", stderr=b"", truncated=False, artifact_refs=(), duration_ms=1,
    )
    process = SimpleNamespace(run_bounded=lambda *_args, **_kwargs: completed)
    checks = CheckService(
        repository, reviews, recipes={"test": CheckRecipe("test", ("noop",))},
        process_service=process,
    )
    result = checks.run("review-race", "test", scope=WorkScope(chat_id="chat-review"))
    assert result.state == expected
    assert finished[0]["state"] == expected
