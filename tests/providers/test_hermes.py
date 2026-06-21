"""Hermes provider adapter — request shaping + reply parsing (doc §4).

Hermetic: a FAKE transport captures the (url, method, body) the adapter sends
and returns a canned reply; NO network. The Hermes gateway is its OpenAI-
compatible api_server, so the adapter POSTs an OpenAI chat completion to
``/v1/chat/completions`` and maps the assistant content (a ``{"files": {...}}``
or ``{"pass": ...}`` object) into RoleResultLike.
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness.providers import get_provider               # noqa: E402
from harness.providers.base import RoleTaskLike          # noqa: E402


def _chat(content: str) -> dict:
    """An OpenAI chat-completion envelope carrying ``content``."""
    return {"choices": [{"index": 0,
                         "message": {"role": "assistant", "content": content}}]}


def _fake(captured, reply):
    def transport(url, *, method="GET", headers=None, body=None, timeout=30.0):
        captured.append({"url": url, "method": method, "headers": headers,
                         "body": json.loads(body) if body else None})
        return 200, json.dumps(reply)
    return transport


def _task():
    return RoleTaskLike(role="coder", node="leaf1", title="Leaf One",
                        spec="specs/leaf1.md", provider="hermes",
                        model="claude/haiku",
                        context={"gateway": "https://hx.example",
                                 "agent": "senior-dev", "token": "T"})


def test_hermes_posts_chat_completion():
    captured = []
    adapter = get_provider("hermes", transport=_fake(
        captured, _chat(json.dumps({"files": {"src/leaf1.py": "x=1\n"}}))))
    res = adapter.execute(_task())
    assert len(captured) == 1
    call = captured[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://hx.example/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer T"
    # the RoleTask travels as chat messages; role/node land in the system content
    msgs = call["body"]["messages"]
    assert msgs[0]["role"] == "system"
    assert "coder" in msgs[0]["content"] and "leaf1" in msgs[0]["content"]
    assert msgs[-1]["role"] == "user"
    assert call["body"]["model"] == "claude/haiku"
    # a {"files": ...} content becomes artifacts
    assert res.kind == "files"
    assert res.artifacts == {"src/leaf1.py": "x=1\n"}
    assert res.meta["provider"] == "hermes"
    assert res.meta["agent"] == "senior-dev"


def test_hermes_verdict_reply():
    """A pass/reasons object in the assistant content maps to a verdict result."""
    adapter = get_provider("hermes", transport=_fake(
        [], _chat(json.dumps({"pass": False, "reasons": ["missing endpoint"]}))))
    res = adapter.execute(_task())
    assert res.kind == "verdict"
    assert res.verdict == {"pass": False, "reasons": ["missing endpoint"]}


def test_hermes_plain_text_reply_kept():
    """Non-JSON content is still returned as the agent's reply (no artifacts)."""
    adapter = get_provider("hermes", transport=_fake([], _chat("all good")))
    res = adapter.execute(_task())
    assert res.artifacts == {}
    assert res.meta["reply"] == "all good"


def test_hermes_json_in_code_fence():
    adapter = get_provider("hermes", transport=_fake(
        [], _chat("here:\n```json\n{\"files\": {\"a.py\": \"1\"}}\n```")))
    res = adapter.execute(_task())
    assert res.artifacts == {"a.py": "1"}


def test_hermes_http_error_raises():
    def transport(url, *, method="GET", headers=None, body=None, timeout=30.0):
        return 500, "boom"
    adapter = get_provider("hermes", transport=transport)
    with pytest.raises(RuntimeError) as exc:
        adapter.execute(_task())
    assert "500" in str(exc.value)


def test_hermes_needs_gateway_and_agent():
    adapter = get_provider("hermes", transport=lambda *a, **k: (200, "{}"))
    with pytest.raises(ValueError):
        adapter.execute(RoleTaskLike(role="coder", node="n", provider="hermes",
                                     context={"agent": "x"}))   # no gateway
    with pytest.raises(ValueError):
        adapter.execute(RoleTaskLike(role="coder", node="n", provider="hermes",
                                     context={"gateway": "u"}))  # no agent
