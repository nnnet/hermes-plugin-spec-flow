"""Mission-Control provider adapter — create + poll + map (doc §4).

Hermetic: a FAKE transport scripts the create POST then a poll sequence
(running -> done); NO network. Asserts the adapter creates a task assigned to
the agent and maps the finished output's files into RoleResultLike.
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness.providers import get_provider               # noqa: E402
from harness.providers.base import RoleTaskLike          # noqa: E402


def _scripted(captured, create_reply, poll_replies):
    polls = list(poll_replies)

    def transport(url, *, method="GET", headers=None, body=None, timeout=30.0):
        captured.append({"url": url, "method": method,
                         "body": json.loads(body) if body else None})
        if method == "POST" and url.endswith("/api/tasks"):
            return 200, json.dumps(create_reply)
        # GET poll
        return 200, json.dumps(polls.pop(0) if polls else {"status": "done"})
    return transport


def _task():
    return RoleTaskLike(role="tester", node="leaf1", spec="specs/leaf1.md",
                        provider="mission-control",
                        context={"api": "https://mc.example",
                                 "agent_template": "Aegis", "poll_attempts": 5})


def test_mc_creates_and_polls_to_done():
    captured = []
    transport = _scripted(
        captured,
        create_reply={"id": "T-42"},
        poll_replies=[{"status": "running"},
                      {"status": "done",
                       "output": {"files": {"tests/test_leaf1.py": "assert 1\n"}}}])
    adapter = get_provider("mission-control", transport=transport)
    res = adapter.execute(_task())

    create = captured[0]
    assert create["method"] == "POST"
    assert create["url"] == "https://mc.example/api/tasks"
    assert create["body"]["assignee"] == "Aegis"
    assert create["body"]["agent_template"] == "Aegis"
    assert create["body"]["role"] == "tester"
    # polled the created task id
    assert captured[1]["url"] == "https://mc.example/api/tasks/T-42"
    assert captured[1]["method"] == "GET"
    # mapped output
    assert res.kind == "files"
    assert res.artifacts == {"tests/test_leaf1.py": "assert 1\n"}
    assert res.meta["task_id"] == "T-42"


def test_mc_failed_task_is_verdict():
    transport = _scripted([], {"id": "T-1"},
                          [{"status": "failed", "error": "agent crashed"}])
    adapter = get_provider("mission-control", transport=transport)
    res = adapter.execute(_task())
    assert res.kind == "verdict"
    assert res.verdict["pass"] is False
    assert "agent crashed" in res.verdict["reasons"][0]


def test_mc_needs_api_and_assignee():
    adapter = get_provider("mission-control", transport=lambda *a, **k: (200, "{}"))
    with pytest.raises(ValueError):
        adapter.execute(RoleTaskLike(role="tester", node="n",
                                     provider="mission-control",
                                     context={"agent": "x"}))      # no api
    with pytest.raises(ValueError):
        adapter.execute(RoleTaskLike(role="tester", node="n",
                                     provider="mission-control",
                                     context={"api": "u"}))        # no assignee
