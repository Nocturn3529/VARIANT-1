"""Durable task identity and ledger rendering."""

from agent_task import Task, TaskStatus


def test_task_render_static_includes_only_identity_and_goal():
    task = Task(goal="Open Notepad")
    block = task.render_static()
    assert "Open Notepad" in block
    assert "CURRENT TASK" in block
    assert "SUCCESS CRITERIA" not in block
    assert "MILESTONE" not in block


def test_task_render_state_has_status_without_progress_nudges():
    task = Task(goal="Open Notepad", status=TaskStatus.IN_PROGRESS)
    block = task.render_state()
    assert block == "TASK STATE: in_progress"
    assert "STUCK" not in block
    assert "NEXT:" not in block


def test_task_status_is_small_and_terminal_states_are_explicit():
    assert {item.value for item in TaskStatus} == {
        "in_progress", "completed", "failed"
    }
