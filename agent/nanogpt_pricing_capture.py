"""Fail-open capture of NanoGPT per-request billing (``x_nanogpt_pricing``).

The provider (nano-gpt.com) ships per-request billing data INSIDE the response:

* non-streaming — a top-level ``x_nanogpt_pricing`` object on the chat response
  (live-verified 2026-09-09, profile pr-nanogpt):
    {"amount": 4.72e-06, "currency": "USD", "cost": 4.72e-06,
     "inputTokens": 13, "outputTokens": 16, "cacheCost": 0,
     "requestId": "req_...", "costUsd": 4.72e-06, "usdCost": 4.72e-06,
     "paymentSource": "USD", "billedToTeam": false, ...}
* streaming — the SAME object rides on the terminal usage chunk (the one with
  ``include_usage``); the OpenAI SDK parks unknown fields on ``model_extra``.

Field semantics (the covered-first signal, live-verified):
  costUsd == 0 with paymentSource == "USD" -> subscription-covered (no balance
  drain).  costUsd > 0 -> prepaid balance spend (USD).  Other payment sources
  must never be treated as balance spend.

This module NEVER raises into the request path: every public entry is wrapped,
all state is best-effort, and when the observer ledger module is missing the
capture is simply inactive. The per-request row is one JSONL line via
``nanogpt-balance-ledger.append_request_row`` when available.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

#: Fail-open switch (default on); set HERMES_DISABLE_NANOGPT_PRICING_CAPTURE=1
#: to turn the whole thing off without a code change.
_ENV_DISABLE = "HERMES_DISABLE_NANOGPT_PRICING_CAPTURE"

#: Set by tests to point the observer import at a fixture module path.
_ENV_LEDGER_PATH = "HERMES_NANOGPT_LEDGER_PATH"

#: NanoGPT base-url marker: the only provider that ships x_nanogpt_pricing.
_BASE_MARKER = "nano-gpt.com"

#: Cheap in-process dedupe: one row per (session, requestId). Unbounded on
#: purpose — requestIds are unique per request; this only defends against a
#: retried/duplicated fold of the SAME response object.
_SEEN: dict[str, float] = {}
_SEEN_MAX = 64
_SEEN_TTL_S = 3600.0

#: Latest captured pricing dict (per process) — introspection/debug aid.
LAST_PRICING: dict[str, Any] | None = None


def _nano_endpoint(agent: Any) -> bool:
    """True only for nano-gpt.com endpoints (any path prefix)."""
    base = str(getattr(agent, "base_url", "") or "").lower()
    return "nano-gpt.com" in base


def extract_pricing(response: Any) -> dict[str, Any] | None:
    """Pull the x_nanogpt_pricing dict from a (non-)streaming response object.

    Handles: plain attribute, SDK ``model_extra``, dict shape, and the
    ``choices=[]``-style terminal chunk that carries ``usage``. Returns None
    when absent or when it is not a dict.
    """
    pricing = getattr(response, "x_nanogpt_pricing", None)
    if pricing is None:
        extra = getattr(response, "model_extra", None)
        if isinstance(extra, dict):
            pricing = extra.get("x_nanogpt_pricing")
    if pricing is None and isinstance(response, dict):
        pricing = response.get("x_nanogpt_pricing")
    if not isinstance(pricing, dict):
        return None
    return pricing


def extract_pricing_from_chunk(chunk: Any) -> dict[str, Any] | None:
    """Same as :func:`extract_pricing` for a streaming chunk."""
    return extract_pricing(chunk)


def _load_ledger_module():
    """Import nanogpt-balance-ledger.py (hyphenated name, so importlib).

    Resolution order: $HERMES_NANOGPT_LEDGER_PATH, then the deployed scripts
    dirs (profile-agnostic), then the quota-governor plugin copy.
    """
    override = os.environ.get(_ENV_LEDGER_PATH_OVERRIDE)
    candidates = []
    if override:
        candidates.append(override)
    home = os.path.expanduser("~")
    hermes_home = os.environ.get("HERMES_HOME") or os.path.join(home, ".hermes")
    for path in (
        os.path.join(hermes_home, "scripts"),
        os.path.join(hermes_home, "profiles", "pr-nanogpt", "scripts"),
        os.path.join(hermes_home, "profiles", "pr-ollama", "scripts"),
    ):
        candidates.append(os.path.join(path, "nanogpt-balance-ledger.py"))
    for path in candidates:
        if path and os.path.isfile(path):
            try:
                import importlib.util

                spec = importlib.util.spec_from_file_location(
                    "nanogpt_balance_ledger", path)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
            except Exception:
                continue
    return None


# Alias kept for readability at the call site below.
_ENV_LEDGER_PATH_OVERRIDE = _ENV_LEDGER_PATH


def _ledger_append(row: dict[str, Any]) -> None:
    """Best-effort observer write; a missing/broken ledger module disables
    capture but never breaks the request path."""
    try:
        mod = _load_ledger_module()
        append = getattr(mod, "append_request_row", None)
        if callable(append):
            append(row)
        else:
            logger.debug("nanogpt pricing: ledger has no append_request_row")
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("nanogpt pricing: ledger append failed: %s", exc)


def _maybe_dedupe(request_id: Any) -> bool:
    """True when this requestId was already folded (and should be skipped)."""
    if not request_id:
        return False
    now = time.time()
    stale = [k for k, ts in _SEEN.items() if now - ts > _SEEN_TTL_S]
    for k in stale:
        _SEEN.pop(k, None)
    if request_id in _SEEN:
        return True
    if len(_SEEN) >= _SEEN_MAX:
        _SEEN.pop(next(iter(_SEEN)))
    _SEEN[request_id] = now
    return False


def capture_from_response(agent: Any, response: Any) -> dict[str, Any] | None:
    """Extract + persist the per-request pricing from a completed response.

    Called on the NON-streaming chat-completions path (and harmlessly on any
    other response object shape — it just won't find the field). Fail-open.
    Returns the captured dict for tests/introspection, or None.
    """
    global LAST_PRICING
    captured: dict[str, Any] | None = None
    try:
        if os.environ.get(_ENV_DISABLE):
            return None
        if not _nano_endpoint(agent):
            return None
        pricing = extract_pricing(response)
        if pricing is None:
            return None
        captured = _capture(agent, pricing)
    except Exception as exc:  # never break the request path
        logger.debug("nanogpt pricing capture failed: %s", exc)
        captured = None
    return captured


def capture_from_chunk(agent: Any, chunk: Any) -> None:
    """Streaming twin of :func:`capture_from_response` — call for every chunk;
    the terminal usage chunk carries the pricing object. Fail-open."""
    global LAST_PRICING
    try:
        if os.environ.get(_ENV_DISABLE):
            return
        if not _nano_endpoint(agent):
            return
        pricing = extract_pricing(chunk)
        if pricing is None:
            return
        _capture(agent, pricing)
    except Exception as exc:  # never break the stream
        logger.debug("nanogpt pricing chunk capture failed: %s", exc)


def _capture(agent: Any, pricing: dict[str, Any]) -> dict[str, Any] | None:
    row_out: dict[str, Any] | None = None
    try:
        request_id = pricing.get("requestId")
        if _maybe_dedupe(request_id):
            return None
        try:
            cost_usd = float(pricing.get("costUsd") or 0.0)
        except (TypeError, ValueError):
            cost_usd = 0.0
        payment_source = pricing.get("paymentSource")
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "request",
            "model": getattr(agent, "model", None),
            "provider": getattr(agent, "provider", None),
            "requestId": request_id,
            "costUsd": cost_usd,
            "paymentSource": payment_source,
            "inputTokens": pricing.get("inputTokens"),
            "outputTokens": pricing.get("outputTokens"),
        }
        _ledger_append(row)
        LAST_PRICING = dict(pricing)
        row_out = row
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("nanogpt pricing fold failed: %s", exc)
        row_out = None
    return row_out


def window_totals(hermes_home: str | None = None):
    """Window accumulators from the ledger's budget state (for the tick)."""
    totals = None
    try:
        mod = _load_ledger_module()
        fn = getattr(mod, "request_window_totals", None)
        if callable(fn):
            totals = fn(hermes_home=hermes_home)
    except Exception:
        totals = None
    return totals