"""Phase 4 — a role whose worker config declares an EXTERNAL team delegates the
WHOLE role as ONE RoleTask to a provider adapter and writes the returned
artifacts back into the workspace (doc §3/§4 CASE 4). The engine is BLIND to the
remote team's specialists/workflow.

Hermetic: a fake transport (no network); the runner is loaded standalone via the
same loader the test suite already uses for the plugin runner.
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import run_engine as eng    # noqa: E402  (re-exports the runner)
from harness import llm_backend as lb    # noqa: E402


class _WS:
    def __init__(self, root):
        self.root = root
        self.written = {}

    def _write(self, rel, content, kind):
        self.written[rel] = content
        return rel


def _fake_transport(sent, reply):
    def transport(url, *, method="GET", headers=None, body=None, timeout=30.0):
        sent.append({"url": url, "method": method,
                     "body": json.loads(body) if body else None})
        return 200, json.dumps(reply)
    return transport


def test_external_team_delegates_one_roletask(tmp_path):
    # implementer is a fully external A2A team (the engine never sees inside)
    lb.configure_workers({"implementer": {"team": {
        "external": True, "provider": "hermes",
        "gateway": "https://crew.example", "agent": "impl-crew"}}})
    sent = []
    transport = _fake_transport(
        sent, {"files": {"src/leaf1.py": "x = 1\n",
                         "tests/test_leaf1.py": "assert True\n"}})

    agents = eng._wrap_external_teams(None, transport=transport)
    lb.configure_workers(None)

    assert "implementer" in agents          # the role got a delegation wrapper
    ws = _WS(str(tmp_path))
    agents["implementer"]({"node": "leaf1", "title": "Leaf One",
                           "spec": "specs/leaf1.md", "goal": "g",
                           "workspace": ws})

    # exactly ONE RoleTask was delegated for the whole role ...
    assert len(sent) == 1
    assert sent[0]["url"] == "https://crew.example/v1/agents/impl-crew/messages"
    assert sent[0]["body"]["role"] == "implementer"
    assert sent[0]["body"]["node"] == "leaf1"
    # ... and the returned artifacts were written back into the workspace
    assert ws.written == {"src/leaf1.py": "x = 1\n",
                          "tests/test_leaf1.py": "assert True\n"}


def test_no_external_team_leaves_agents_unchanged():
    lb.configure_workers({"implementer": {"team": {
        "specialists": [{"role": "coder"}]}}})   # inline team, NOT external
    agents = eng._wrap_external_teams({"implementer": "SENTINEL"})
    lb.configure_workers(None)
    assert agents["implementer"] == "SENTINEL"   # untouched (today's path)


def test_external_team_bogus_provider_errors(tmp_path):
    lb.configure_workers({"decomposer": {"team": {
        "external": True, "provider": "bogus", "endpoint": "u"}}})
    agents = eng._wrap_external_teams(None, transport=lambda *a, **k: (200, "{}"))
    lb.configure_workers(None)
    ws = _WS(str(tmp_path))
    with pytest.raises(ValueError) as exc:
        agents["decomposer"]({"node": "root", "workspace": ws})
    assert "bogus" in str(exc.value)
