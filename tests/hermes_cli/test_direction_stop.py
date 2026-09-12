"""Direction-mode emergency stop (DIRECCION-STOP) — agent/direction_stop.py.

OBJ-42 layer 2: while the sentinel at ``$HERMES_HOME/quota-governor/DIRECCION-STOP``
exists, the kanban dispatcher claims no new workers (gateway loop + standalone
``dispatch_once``), the quota-governor tick is inert (plugin repo), and
in-flight workers are never touched. Removing the sentinel resumes dispatch on
the next tick with no restart. Mirrors the agent.estop test layout.
"""

from __future__ import annotations

import logging

import pytest

from agent import direction_stop


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir and reset module log state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    direction_stop._logged_components.clear()
    return tmp_path


# ── sentinel create / remove ────────────────────────────────────────────────


def test_engage_creates_sentinel_and_is_engaged(hermes_home):
    assert direction_stop.is_engaged() is False
    direction_stop.engage()
    assert (hermes_home / "quota-governor" / "DIRECCION-STOP").exists()
    assert direction_stop.is_engaged() is True


def test_disengage_removes_sentinel(hermes_home):
    direction_stop.engage()
    assert direction_stop.disengage() is True
    assert not (hermes_home / "quota-governor" / "DIRECCION-STOP").exists()
    assert direction_stop.is_engaged() is False
    # Disengaging when not engaged is a no-op that reports False.
    assert direction_stop.disengage() is False


def test_reason_and_timestamp_stored(hermes_home):
    direction_stop.engage(reason="mandate halted by user")
    state = direction_stop.get_state()
    assert state is not None
    assert state["reason"] == "mandate halted by user"
    assert state["engaged_at"]  # ISO timestamp string

    import json

    raw = json.loads((hermes_home / "quota-governor" / "DIRECCION-STOP").read_text(encoding="utf-8"))
    assert raw["reason"] == "mandate halted by user"


def test_get_state_none_when_disengaged(hermes_home):
    assert direction_stop.get_state() is None


def test_corrupt_sentinel_still_engages(hermes_home):
    """A hand-touched/corrupt DIRECCION-STOP file must still engage (fail safe)."""
    (hermes_home / "quota-governor").mkdir(parents=True, exist_ok=True)
    (hermes_home / "quota-governor" / "DIRECCION-STOP").write_text("not json", encoding="utf-8")
    assert direction_stop.is_engaged() is True
    state = direction_stop.get_state()
    assert state is not None
    assert state.get("reason") is None


# ── check_dispatch: cheap gate + log-once ───────────────────────────────────


def test_check_dispatch_logs_once_per_engagement(hermes_home, caplog):
    logger = logging.getLogger("test.direction.component")
    direction_stop.engage()
    with caplog.at_level(logging.INFO, logger=logger.name):
        assert direction_stop.check_dispatch("kanban-dispatcher", logger) is True
        assert direction_stop.check_dispatch("kanban-dispatcher", logger) is True
        assert direction_stop.check_dispatch("kanban-dispatcher", logger) is True
    stop_logs = [r for r in caplog.records if "DIRECCION-STOP" in r.getMessage()]
    assert len(stop_logs) == 1

    # Resume then re-engage → logs once more (transition-based, not forever).
    caplog.clear()
    direction_stop.disengage()
    with caplog.at_level(logging.INFO, logger=logger.name):
        assert direction_stop.check_dispatch("kanban-dispatcher", logger) is False
        direction_stop.engage()
        assert direction_stop.check_dispatch("kanban-dispatcher", logger) is True
        assert direction_stop.check_dispatch("kanban-dispatcher", logger) is True
    stop_logs = [r for r in caplog.records if "DIRECCION-STOP" in r.getMessage()]
    assert len(stop_logs) == 1


# ── gateway dispatcher-loop integration ─────────────────────────────────────


def test_gateway_dispatcher_loop_gate(hermes_home):
    from gateway.kanban_watchers import _direction_stop_engaged

    assert _direction_stop_engaged("kanban-dispatcher") is False
    direction_stop.engage(reason="test")
    assert _direction_stop_engaged("kanban-dispatcher") is True
    direction_stop.disengage()
    assert _direction_stop_engaged("kanban-dispatcher") is False


def test_root_gateway_sees_mandate_profile_sentinel(hermes_home, tmp_path, monkeypatch):
    """Deployment reality: the machine-global dispatcher runs from the canonical
    root (HERMES_HOME=~/.hermes) while the mandate's switch lives in the
    pr-ollama profile — the gate must honor it via the constant profile name."""
    root = tmp_path / "root-home"
    root.mkdir()
    profile = root / "profiles" / direction_stop.MANDATE_PROFILE
    # direction_stop binds the file_safety resolvers at import time; patch the
    # module's aliases directly (the ESTOP test suite patches HERMES_HOME the
    # same way through its own fixture).
    monkeypatch.setattr(direction_stop, "_hermes_home", lambda: root)
    monkeypatch.setattr(direction_stop, "_hermes_root", lambda: root)

    assert direction_stop.is_engaged() is False
    (profile / "quota-governor").mkdir(parents=True)
    (profile / "quota-governor" / "DIRECCION-STOP").write_text(
        '{"reason": "mandate", "engaged_at": "2026-09-12T22:00:00+00:00"}', encoding="utf-8"
    )
    assert direction_stop.is_engaged() is True
    state = direction_stop.get_state()
    assert state is not None
    assert state["reason"] == "mandate"
    assert direction_stop.check_dispatch("kanban-dispatch-once", None) is True
    assert direction_stop.disengage() is True
    assert direction_stop.is_engaged() is False


# ── standalone dispatch_once integration ────────────────────────────────────


@pytest.fixture
def board_conn(hermes_home):
    """Real board DB (SCHEMA_SQL) in the temp HERMES_HOME, with one spawnable task."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path(board="default").resolve()))
    kb.init_db()
    with kbc.connect() as conn:
        kb.create_task(conn, title="gate test", assignee="pr-test")
        yield conn


def test_dispatch_once_blocked_when_engaged(board_conn, all_assignees_spawnable):
    """Sentinel present → dispatch_once is a no-write no-op: no claim, no spawn."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    spawn_calls: list = []

    def spy_spawn(task, workspace_path, board=None):
        spawn_calls.append(getattr(task, "id", task))
        return 999999

    direction_stop.engage(reason="unit")
    result = kbd.dispatch_once(board_conn, spawn_fn=spy_spawn)
    assert result.spawned == []
    assert spawn_calls == [], "spawn_fn must not run while DIRECCION-STOP is engaged"
    # The ready task was NOT claimed: it stays visible as spawnable work.
    task = kb.get_task(board_conn, "t_dir_1") if hasattr(kb, "get_task") else None
    if task is not None:
        assert task.status == "ready"


def test_dispatch_once_spawns_normally_when_disengaged(board_conn, all_assignees_spawnable):
    """Sentinel absent → the normal claim/spawn path runs (proves the gate is
    the only thing that changed)."""
    from hermes_cli import kanban_db_dispatch as kbd

    spawn_calls: list = []

    def spy_spawn(task, workspace_path, board=None):
        spawn_calls.append(getattr(task, "id", task))
        return 999999

    result = kbd.dispatch_once(board_conn, spawn_fn=spy_spawn)
    assert result.skipped_locked is False
    assert spawn_calls, "with no sentinel the dispatcher must claim and spawn"
