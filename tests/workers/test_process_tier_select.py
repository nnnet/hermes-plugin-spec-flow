"""Process tiering at the worker boundary: _select_team picks the implementer
path. An explicit per-leaf executor team wins; else a solo-flagged simple leaf
routes to the single-coder path ([]); else the configured orchestra. Pure +
offline — no LLM, no network."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import role_worker as rw   # noqa: E402


_TEAM = [{"role": "coder", "provider": "local"}]


def test_solo_suppresses_global_orchestra(monkeypatch):
    # a configured global team would normally run, but a solo leaf bypasses it
    monkeypatch.setattr(rw, "_implementer_team", lambda: _TEAM)
    assert rw._select_team({"node": "n", "solo": True}) == []


def test_no_solo_runs_global_orchestra(monkeypatch):
    monkeypatch.setattr(rw, "_implementer_team", lambda: _TEAM)
    assert rw._select_team({"node": "n"}) == _TEAM


def test_explicit_executor_team_beats_solo(monkeypatch):
    # an explicit per-leaf team (C1 domain routing) is a deliberate decision and
    # overrides the solo heuristic
    monkeypatch.setattr(rw, "_implementer_team", lambda: [])
    ctx = {"node": "n", "solo": True, "team": _TEAM}
    out = rw._select_team(ctx)
    assert len(out) == 1 and out[0]["role"] == "coder" \
        and out[0]["provider"] == "local"


def test_solo_emits_process_tier_log(monkeypatch):
    monkeypatch.setattr(rw, "_implementer_team", lambda: _TEAM)
    seen = []
    monkeypatch.setattr(rw.llm_log, "log", lambda rec: seen.append(rec))
    rw._select_team({"node": "leaf7", "solo": True})
    tier = [r for r in seen if r.get("event") == "process_tier"]
    assert tier and tier[0]["mode"] == "solo" and tier[0]["node"] == "leaf7"


def test_no_team_no_solo_empty(monkeypatch):
    # no global team, no solo -> empty (single-agent default path) without a log
    monkeypatch.setattr(rw, "_implementer_team", lambda: [])
    seen = []
    monkeypatch.setattr(rw.llm_log, "log", lambda rec: seen.append(rec))
    assert rw._select_team({"node": "n"}) == []
    assert not [r for r in seen if r.get("event") == "process_tier"]
