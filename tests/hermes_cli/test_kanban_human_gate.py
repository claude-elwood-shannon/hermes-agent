"""Tests for is_human_gate_pending + specify_triage_task refusal.

Covers the fix for the SWEEP-FIX bug: when a task reaches triage via
``block_loop_detected`` with ``kind=needs_input``, the automated triage
sweep (auto-decompose / specify --all) must NOT re-promote it until a
human comment arrives after the gate event. Without this, the sweep
re-spawns a worker that re-blocks for the same reason, burning quota
in an unbounded loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t", assignee="worker"):
    tid = kb.create_task(conn, title=title, assignee=assignee)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer=assignee)
    assert claimed is not None
    return tid


def _block_loop_to_triage(conn, tid, *, kind="needs_input", reason="need human verdict"):
    """Block → unblock → re-block to force block_loop_detected → triage."""
    kb.block_task(conn, tid, reason=reason, kind=kind)
    kb.unblock_task(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    kb.block_task(conn, tid, reason=reason, kind=kind)
    # Now the task should be in triage via block_loop_detected
    assert kb.get_task(conn, tid).status == "triage"


# ---------------------------------------------------------------------------
# is_human_gate_pending
# ---------------------------------------------------------------------------


def test_human_gate_pending_for_needs_input_in_triage(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        _block_loop_to_triage(conn, tid, kind="needs_input")
        assert kb.is_human_gate_pending(conn, tid) is True


def test_human_gate_not_pending_for_capability(kanban_home):
    """capability blocks are hard walls, not human-input gates — the sweep
    is allowed to re-specify them (a human might work around the wall)."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        _block_loop_to_triage(conn, tid, kind="capability")
        assert kb.is_human_gate_pending(conn, tid) is False


def test_human_gate_not_pending_when_human_commented(kanban_home):
    """A human comment after the gate event lifts the gate."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        _block_loop_to_triage(conn, tid, kind="needs_input")
        # Simulate a human comment (author does not match pr-*/auto-*/etc.)
        kb.add_comment(conn, tid, author="claude", body="OK, proceed.")
        assert kb.is_human_gate_pending(conn, tid) is False


def test_human_gate_still_pending_when_worker_comments(kanban_home):
    """Worker comments (pr-*) do NOT lift the gate — only human comments do."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        _block_loop_to_triage(conn, tid, kind="needs_input")
        # Worker comment — should not lift the gate
        kb.add_comment(conn, tid, author="pr-opencode", body="Still waiting for OK.")
        assert kb.is_human_gate_pending(conn, tid) is True


def test_human_gate_not_pending_when_not_in_triage(kanban_home):
    """A task not in triage has no gate pending."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="fresh", assignee="worker", triage=True)
        # Task is in triage but never blocked → no gate
        assert kb.is_human_gate_pending(conn, tid) is False


def test_human_gate_not_pending_when_no_block_loop(kanban_home):
    """A triage task without any block_loop_detected event has no gate."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="fresh idea", assignee="worker", triage=True)
        assert kb.is_human_gate_pending(conn, tid) is False


# ---------------------------------------------------------------------------
# specify_triage_task refuses human-gated tasks
# ---------------------------------------------------------------------------


def test_specify_refuses_human_gated_task(kanban_home):
    """specify_triage_task returns False (no promotion) when a human gate is
    pending — this is the core fix for the sweep loop."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        _block_loop_to_triage(conn, tid, kind="needs_input")
        ok = kb.specify_triage_task(
            conn, tid, title="Retitled", body="new body", author="auto-decomposer",
        )
        assert ok is False
        # Task stays in triage
        task = kb.get_task(conn, tid)
        assert task.status == "triage"


def test_specify_promotes_after_human_comment(kanban_home):
    """After a human comments on the gated task, specify_triage_task
    succeeds again — the gate is lifted."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        _block_loop_to_triage(conn, tid, kind="needs_input")
        kb.add_comment(conn, tid, author="claude", body="OK")
        ok = kb.specify_triage_task(
            conn, tid, title="Retitled", body="new body", author="auto-decomposer",
        )
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status in ("todo", "ready")


# ---------------------------------------------------------------------------
# list_triage_ids filters human-gated tasks
# ---------------------------------------------------------------------------


def test_list_triage_ids_excludes_human_gated(kanban_home):
    """The decomposer's list_triage_ids skips human-gated tasks so the
    auto-decompose tick never even calls the LLM for them."""
    from hermes_cli import kanban_decompose as decomp

    with kbc.connect_closing() as conn:
        gated = _running_task(conn, title="gated")
        _block_loop_to_triage(conn, gated, kind="needs_input")
        fresh = kb.create_task(conn, title="fresh idea", assignee="worker", triage=True)

    ids = decomp.list_triage_ids()
    assert gated not in ids
    assert fresh in ids


def test_list_triage_ids_includes_gated_after_human_comment(kanban_home):
    """Once a human responds, the task re-enters the triage sweep."""
    from hermes_cli import kanban_decompose as decomp

    with kbc.connect_closing() as conn:
        gated = _running_task(conn, title="gated")
        _block_loop_to_triage(conn, gated, kind="needs_input")
        kb.add_comment(conn, gated, author="claude", body="OK")

    ids = decomp.list_triage_ids()
    assert gated in ids