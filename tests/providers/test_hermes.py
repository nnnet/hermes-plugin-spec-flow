"""Hermes provider adapter — request shaping + reply parsing (doc §4).

Hermetic: a FAKE transport captures the (url, method, body) the adapter sends
and returns a canned reply; NO network. Asserts the adapter POSTs the RoleTask
to the right agent URL and maps the reply files into RoleResultLike.
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness.providers import get_provider               # noqa: E402
from harness.providers.base import RoleTaskLike          # noqa: E402


def _fake(captured, reply):
    def transport(url, *, method="GET", headers=None, body=None, timeout=30.0):
        captured.append({"url": url, "method": method, "headers": headers,
                         "body": json.loads(body) if body else None})
        return 200, json.dumps(reply)
    return transport


def _task():
    return RoleTaskLike(role="coder", node="leaf1", title="Leaf One",
                        spec="specs/leaf1.md", provider="hermes",
                        context={"gateway": "https://hx.example",
                                 "agent": "senior-dev", "token": "T"})


def test_hermes_posts_roletask_to_agent():
    captured = []
    adapter = get_provider("hermes", transport=_fake(
        captured, {"files": {"src/leaf1.py": "x=1\n"}}))
    res = adapter.execute(_task())
    assert len(captured) == 1
    call = captured[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://hx.example/v1/agents/senior-dev/messages"
    assert call["headers"]["Authorization"] == "Bearer T"
    # the RoleTask travels in the body
    assert call["body"]["role"] == "coder"
    assert call["body"]["node"] == "leaf1"
    assert "message" in call["body"]
    # reply files become artifacts
    assert res.kind == "files"
    assert res.artifacts == {"src/leaf1.py": "x=1\n"}
    assert res.meta["provider"] == "hermes"
    assert res.meta["agent"] == "senior-dev"


def test_hermes_verdict_reply():
    """A bare pass/reasons reply maps to a verdict RoleResult."""
    adapter = get_provider("hermes", transport=_fake(
        [], {"pass": False, "reasons": ["missing endpoint"]}))
    res = adapter.execute(_task())
    assert res.kind == "verdict"
    assert res.verdict == {"pass": False, "reasons": ["missing endpoint"]}


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
