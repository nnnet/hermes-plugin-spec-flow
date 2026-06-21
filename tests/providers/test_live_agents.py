"""LIVE provider checks — actually call the MC and Hermes agents over the network.

These are integration tests (not hermetic): they hit the REAL services and are
SKIPPED unless the operator env carries the inbound keys and the service answers.
Run them with the keys exported, e.g.:

    set -a; . infra/hermes/.hermes/.env; set +a
    python3 -m pytest providers/test_live_agents.py -v -s

What they prove:
  * Hermes: a chat completion to the gateway's api_server is answered by the
    Hermes agent (non-empty reply) — the adapter targets the real endpoint
    (/v1/chat/completions) with the real inbound key (API_SERVER_KEY).
  * Mission-Control: a task is CREATED and its id is parsed from the
    {"task": {"id": ...}} envelope. Completion needs an ONLINE crew member; if
    none is online the task stays queued and the adapter reports
    'did not finish in N polls' — that still proves create+id work (the old
    'no task id' bug is gone). A real terminal result is also accepted.
"""
import os
import pathlib
import sys
import urllib.request

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness.providers import get_provider               # noqa: E402
from harness.providers.base import RoleTaskLike          # noqa: E402

HERMES_GATEWAY = os.environ.get("HERMES_GATEWAY_URL", "http://127.0.0.1:8642")
MC_API = os.environ.get("HERMES_MC_API_URL", "http://127.0.0.1:3000")
HERMES_KEY = os.environ.get("API_SERVER_KEY")
MC_KEY = os.environ.get("HERMES_MC_API_KEY")
# the gateway runs the agent loop; allow it room
LIVE_TIMEOUT = float(os.environ.get("SPEC_FLOW_LIVE_TIMEOUT", "120"))


def _reachable(url: str) -> bool:
    try:
        req = urllib.request.Request(url + "/health", method="GET")
        with urllib.request.urlopen(req, timeout=5):       # nosec B310
            return True
    except Exception:                                       # noqa: BLE001
        return False


@pytest.mark.skipif(not HERMES_KEY, reason="API_SERVER_KEY not in env")
def test_live_hermes_agent_answers():
    if not _reachable(HERMES_GATEWAY):
        pytest.skip(f"hermes gateway {HERMES_GATEWAY} not reachable")
    model = os.environ.get("HERMES_LIVE_MODEL", "xiaomimimo/mimo-v2.5")
    task = RoleTaskLike(
        role="tester", node="live-probe", title="liveness",
        provider="hermes", model=model,
        params={"max_tokens": 64},
        context={"gateway": HERMES_GATEWAY, "agent": "tester",
                 "token": HERMES_KEY})
    res = get_provider("hermes").execute(task)             # real network
    # the Hermes agent answered (files when it returned JSON, else raw reply)
    assert res.meta.get("provider") == "hermes"
    assert (res.artifacts or res.verdict
            or (res.meta.get("reply") or "").strip()), res.meta


@pytest.mark.skipif(not MC_KEY, reason="HERMES_MC_API_KEY not in env")
def test_live_mission_control_creates_task():
    if not _reachable(MC_API):
        pytest.skip(f"mission-control {MC_API} not reachable")
    task = RoleTaskLike(
        role="fixer", node="live-probe", title="liveness",
        provider="mission-control",
        context={"api": MC_API, "agent_template": "fixer",
                 "api_key": MC_KEY, "poll_attempts": 2})
    try:
        res = get_provider("mission-control").execute(task)  # real network
    except RuntimeError as exc:
        msg = str(exc)
        # create+id must work; the only acceptable failure is "no online crew
        # member ran it" (task stayed queued -> poll budget exhausted)
        assert "did not finish" in msg, msg
        assert "no task id" not in msg and "create failed" not in msg, msg
        return
    # if a crew member WAS online, we got a real terminal result
    assert res.meta.get("provider") == "mission-control"
    assert res.meta.get("task_id")
