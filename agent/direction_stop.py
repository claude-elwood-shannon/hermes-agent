"""Direction-mode emergency stop (DIRECCION-STOP) — a resumable mandate halt.

OBJ-42 layer 2. Complements :mod:`agent.estop`: while the sentinel at
``$HERMES_HOME/quota-governor/DIRECCION-STOP`` exists, the kanban dispatcher
claims no new workers (this module) and the quota-governor tick is inert (the
plugin repo's ``quota-governor-tick.sh``). In-flight workers are never
touched; removing the sentinel resumes dispatch on the next tick with no
restart. The body is optional JSON ``{"reason", "engaged_at"}``; a corrupt or
empty file still counts as engaged (fail safe, e.g. ``touch .../DIRECCION-STOP``).
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Same profile-aware resolvers the ESTOP sentinel uses (fail-open to ~/.hermes).
from agent.file_safety import _hermes_home_path as _hermes_home
from agent.file_safety import _hermes_root_path as _hermes_root

SENTINEL_NAME = "DIRECCION-STOP"
SENTINEL_SUBDIR = "quota-governor"

# Per-component "logged already for this engagement" flags: log once per
# engagement, not per tick.
_log_lock = threading.Lock()
_logged_components: set[str] = set()

# The mandate's switch lives in the pr-ollama profile (layer-1 tick looks
# there), while the machine-global dispatcher may run from ANY gateway —
# including one whose own HERMES_HOME is the canonical root or another
# profile. Candidate paths: the active process home first, then the
# pr-ollama profile resolved from the canonical root (a constant name, never
# a hardcoded absolute path).
MANDATE_PROFILE = "pr-ollama"


def sentinel_path() -> Path:
    """Primary path of the DIRECCION-STOP sentinel (this process's home)."""
    return _hermes_home() / SENTINEL_SUBDIR / SENTINEL_NAME


def _candidate_sentinel_paths() -> list[Path]:
    """Active home first, then the mandate profile under the canonical root."""
    candidates = [sentinel_path()]
    try:
        profile = _hermes_root() / "profiles" / MANDATE_PROFILE / SENTINEL_SUBDIR / SENTINEL_NAME
    except Exception:
        return candidates
    try:
        distinct = profile.resolve() != candidates[0].resolve()
    except Exception:
        distinct = profile != candidates[0]
    if distinct:
        candidates.append(profile)
    return candidates


def is_engaged() -> bool:
    """True while ANY candidate sentinel file exists; fail safe (True) on stat errors.

    Fail-safe direction matches agent.estop: an unreadable home directory must
    not silently re-enable autonomous dispatch. A missing directory is a clean
    disengage (the common case), not an error.
    """
    saw_stat_error = False
    for path in _candidate_sentinel_paths():
        try:
            if path.exists():
                return True
        except OSError:
            saw_stat_error = True
    return saw_stat_error


def get_state() -> Optional[dict]:
    """Return ``{"reason", "engaged_at"}`` or None when not engaged; an unreadable/
    corrupt body still reports engaged with both fields None. Reads the first
    candidate path that actually exists (the stop may live in the mandate
    profile while this process runs from another home)."""
    if not is_engaged():
        return None
    state: dict = {"reason": None, "engaged_at": None}
    for path in _candidate_sentinel_paths():
        try:
            if not path.exists():
                continue
        except OSError:
            return state
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return state
        with suppress(ValueError, AttributeError):
            data = json.loads(raw)
            if isinstance(data, dict):
                state = {
                    "reason": data.get("reason") or None,
                    "engaged_at": data.get("engaged_at") or None,
                }
        return state
    return state


def engage(reason: Optional[str] = None) -> Path:
    """Create the sentinel. Idempotent; re-engaging updates the file."""
    path = sentinel_path()
    payload = {"engaged_at": datetime.now(timezone.utc).isoformat(), "reason": reason or None}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        with suppress(OSError):  # Best effort: an empty/partial sentinel still engages (fail safe).
            path.touch(exist_ok=True)
    return path


def disengage() -> bool:
    """Remove every visible sentinel (process-local and mandate profile); True
    when at least one file was actually removed."""
    lifted = False
    for path in _candidate_sentinel_paths():
        try:
            path.unlink()
            lifted = True
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return lifted


def check_dispatch(component: str, logger: Optional[logging.Logger] = None) -> bool:
    """Log-once-per-engagement helper for dispatch gates; True when ENGAGED (stopped).

    Mirrors ``agent.estop.check_paused`` call semantics inverted for dispatch
    gating: return True when the direction stop is active and the caller must
    not claim/spawn. Engaging mid-flight logs on the first checked tick;
    disengaging resets the log-once state so a later re-engagement logs again.
    """
    if not is_engaged():
        with _log_lock:
            _logged_components.discard(component)
        return False
    if logger is not None:
        with _log_lock:
            if component not in _logged_components:
                _logged_components.add(component)
                state = get_state()
                reason = state.get("reason") if isinstance(state, dict) else None
                tag = f" ({reason})" if reason else ""
                logger.warning(
                    "kanban dispatcher: DIRECCION-STOP%s active — direction mandate halted "
                    "by user; no new workers will be claimed or spawned until the sentinel "
                    "at %s is removed. In-flight workers are not touched.",
                    tag,
                    sentinel_path(),
                )
    return True
