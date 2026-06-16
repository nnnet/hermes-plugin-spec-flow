"""A2A provider adapter — Agent Card discovery + tasks/send + poll + artifacts.

Hermetic: a FAKE transport serves the Agent Card on the well-known GET, the
JSON-RPC tasks/send and tasks/get on POST (working -> completed); NO network.
Asserts the JSON-RPC envelope and that completed artifacts (file parts) map to
RoleResultLike.artifacts.
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness.providers import get_provider               # noqa: E402
from harness.providers.base import RoleTaskLike          # noqa: E402

_CARD = {"name": "fixer-crew", "url": "https://a2a.example/rpc"}


def _scripted(captured, card, rpc_sequence):
    seq = list(rpc_sequence)

    def transport(url, *, method="GET", headers=None, body=None, timeout=30.0):
        payload = json.loads(body) if body else None
        captured.append({"url": url, "method": method, "payload": payload})
        if url.endswith("/.well-known/agent.json"):
            return 200, json.dumps(card)
        # JSON-RPC POST — return the next scripted result envelope
        return 200, json.dumps(seq.pop(0) if seq else
                               {"jsonrpc": "2.0", "id": "x",
                                "result": {"status": {"state": "completed"}}})
    return transport


def _task():
    return RoleTaskLike(role="fixer", node="leaf1", spec="specs/leaf1.md",
                        provider="a2a",
                        context={"endpoint": "https://a2a.example",
                                 "token": "TK", "poll_attempts": 5})


def _completed_with_files():
    return {"jsonrpc": "2.0", "id": "fixer-leaf1",
            "result": {"status": {"state": "completed"},
                       "artifacts": [
                           {"name": "code",
                            "parts": [{"type": "file",
                                       "file": {"name": "src/leaf1.py",
                                                "text": "y=2\n"}}]}]}}


def test_a2a_discovers_card_and_sends_task():
    captured = []
    # send returns working, first get returns completed-with-files
    transport = _scripted(
        captured, _CARD,
        [{"jsonrpc": "2.0", "id": "fixer-leaf1",
          "result": {"status": {"state": "working"}}},
         _completed_with_files()])
    adapter = get_provider("a2a", transport=transport)
    res = adapter.execute(_task())

    # 1) discovery hit the well-known path
    assert captured[0]["url"] == "https://a2a.example/.well-known/agent.json"
    assert captured[0]["method"] == "GET"
    # 2) tasks/send to the card-advertised rpc url with the JSON-RPC envelope
    send = captured[1]
    assert send["url"] == "https://a2a.example/rpc"
    assert send["method"] == "POST"
    assert send["payload"]["jsonrpc"] == "2.0"
    assert send["payload"]["method"] == "tasks/send"
    assert send["payload"]["params"]["id"] == "fixer-leaf1"
    # the RoleTask rides as a text part
    part = send["payload"]["params"]["message"]["parts"][0]
    assert part["type"] == "text"
    assert json.loads(part["text"])["role"] == "fixer"
    # 3) polled tasks/get until completed
    assert captured[2]["payload"]["method"] == "tasks/get"
    # 4) artifacts mapped
    assert res.kind == "files"
    assert res.artifacts == {"src/leaf1.py": "y=2\n"}
    assert res.meta["provider"] == "a2a"


def test_a2a_skips_discovery_when_card_supplied():
    captured = []
    transport = _scripted(captured, _CARD, [_completed_with_files()])
    task = _task()
    task.context["agent_card"] = _CARD
    adapter = get_provider("a2a", transport=transport)
    res = adapter.execute(task)
    # first call is tasks/send (no well-known GET)
    assert captured[0]["payload"]["method"] == "tasks/send"
    assert res.artifacts == {"src/leaf1.py": "y=2\n"}


def test_a2a_failed_state_is_verdict():
    transport = _scripted([], _CARD,
                          [{"jsonrpc": "2.0", "id": "x",
                            "result": {"status": {"state": "failed"}}}])
    adapter = get_provider("a2a", transport=transport)
    res = adapter.execute(_task())
    assert res.kind == "verdict"
    assert res.verdict["pass"] is False


def test_a2a_needs_endpoint():
    adapter = get_provider("a2a", transport=lambda *a, **k: (200, "{}"))
    with pytest.raises(ValueError):
        adapter.execute(RoleTaskLike(role="fixer", node="n", provider="a2a"))
