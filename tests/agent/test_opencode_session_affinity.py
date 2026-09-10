"""x-opencode-session rides on every OpenCode request, on every transport."""

from __future__ import annotations

import pytest

from agent import auxiliary_client as aux
from agent.chat_completion_helpers import build_api_kwargs
from run_agent import AIAgent

_MSGS = [{"role": "user", "content": "hi"}]


def _agent(provider, model, base_url, api_mode=None):
    agent = AIAgent(
        api_key="test-key",
        base_url=base_url,
        model=model,
        provider=provider,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_id="sess-affinity-1",
    )
    if api_mode:
        agent.api_mode = api_mode
        agent._transport = None
        agent._anthropic_base_url = base_url
    return agent


@pytest.mark.parametrize(
    "provider, model, base_url, api_mode",
    [
        ("opencode-go", "glm-5", "https://opencode.ai/zen/go/v1", None),  # chat_completions
        ("opencode-go", "gpt-5.6-luna", "https://opencode.ai/zen/go/v1", None),  # codex_responses
        ("opencode-go", "minimax-m2.7", "https://opencode.ai/zen/go/v1", "anthropic_messages"),
        ("opencode-free", "laguna-s-2.1-free", "https://opencode.ai/zen/v1", None),
        ("custom", "glm-5", "https://opencode.ai/zen/go/v1", None),  # URL-only detection
    ],
)
def test_main_turn_sends_stable_session_header_on_every_transport(provider, model, base_url, api_mode):
    agent = _agent(provider, model, base_url, api_mode)
    first = build_api_kwargs(agent, _MSGS)["extra_headers"]["x-opencode-session"]
    second = build_api_kwargs(agent, _MSGS)["extra_headers"]["x-opencode-session"]
    assert first == second == "sess-affinity-1"

    other = _agent("openrouter", "anthropic/claude-sonnet-4.6", "https://openrouter.ai/api/v1")
    assert "x-opencode-session" not in (build_api_kwargs(other, _MSGS).get("extra_headers") or {})


def test_auxiliary_calls_share_the_main_turn_session_key():
    token = aux.set_runtime_main(
        "opencode-go", "glm-5", base_url="https://opencode.ai/zen/go/v1", session_id="sess-affinity-1"
    )
    try:
        kwargs = aux._build_call_kwargs("opencode-go", "glm-5", _MSGS, base_url="https://opencode.ai/zen/go/v1")
        assert kwargs["extra_headers"]["x-opencode-session"] == "sess-affinity-1"
        other = aux._build_call_kwargs("openrouter", "x", _MSGS, base_url="https://openrouter.ai/api/v1")
        assert "x-opencode-session" not in (other.get("extra_headers") or {})
    finally:
        aux._RUNTIME_MAIN_CONTEXT.reset(token)


def test_iteration_limit_summary_chat_kwargs_carry_session_header():
    """The max-iterations summary is part of the conversation: its chat-wire request must
    carry the same x-opencode-session key as the main turn, or OpenCode routes it to a
    cold backend and the summary loses the conversation's cache (MissingSessionID on
    session-enforcing relays)."""
    import agent.chat_completion_helpers as cch

    kwargs = cch._iteration_summary_chat_kwargs(_agent("opencode-go", "glm-5", "https://opencode.ai/zen/go/v1"), _MSGS)
    assert kwargs["extra_headers"]["x-opencode-session"] == "sess-affinity-1"
    other = cch._iteration_summary_chat_kwargs(
        _agent("openrouter", "glm-5", "https://openrouter.ai/api/v1"), _MSGS
    )
    assert "x-opencode-session" not in (other.get("extra_headers") or {})


def test_iteration_limit_summary_anthropic_wire_carries_session_header(monkeypatch):
    """Same contract on the anthropic_messages wire: the summary attempt's request kwargs
    must include the session header (merged into the SDK's per-request extra_headers)."""
    import agent.chat_completion_helpers as cch

    agent = _agent("opencode-go", "minimax-m2.7", "https://opencode.ai/zen/go/v1", api_mode="anthropic_messages")
    captured: dict = {}

    def fake_managed(agent_, api_request_id, request, callback, *, retry_count=0):
        captured.update(request)
        raise RuntimeError("captured-request")

    monkeypatch.setattr(cch, "_managed_summary_call", fake_managed)
    attempt = cch._anthropic_summary_attempt(agent, _MSGS, "req-1")
    with pytest.raises(RuntimeError, match="captured-request"):
        attempt(0)
    assert captured["extra_headers"]["x-opencode-session"] == "sess-affinity-1"
