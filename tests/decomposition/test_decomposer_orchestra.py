"""D2 — decomposer orchestra (drafter → critic → reconciler).

Symmetric to D1: with NO decomposer team the decomposer is the single agent,
unchanged. A configured team runs a drafter that proposes the split, a critic
that challenges it, and a reconciler that finalises — every call through the
single door. Offline: the model door is a real local server (no mocks); each
step is identified by the prompt the harness actually sent it."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import llm_decomposer as dc   # noqa: E402
from harness import llm_backend as lb      # noqa: E402
from harness_fakeapi import ok             # noqa: E402

_FREE = "openrouter/x:free"

_PROJECT = {"goal": "A tiny notes service",
            "target": "POST then GET round-trips a note",
            "constitution": ["stdlib only", "POST /notes -> {id}"]}


def _ctx(depth=1):
    return {"project": _PROJECT,
            "node": {"id": "notes", "title": "Notes service"},
            "depth": depth, "parent": "L0", "ancestors": ["L0"],
            "existing_nodes": []}


def _draft(children):
    return json.dumps({"atomic": False,
                       "metrics": {"modules": 2, "tasks": 7, "interfaces": 2,
                                   "estimated_loc": 110, "open_decisions": 1,
                                   "single_concern": False,
                                   "testable_criteria": True},
                       "children": [{"id": c, "title": c.upper()}
                                    for c in children]})


def _team_env(monkeypatch, roles, **models):
    specialists = [{"role": r, "model": models.get(r, "")} for r in roles]
    monkeypatch.setenv("SPEC_FLOW_DECOMPOSER_TEAM",
                       json.dumps({"specialists": specialists}))


def _prep(monkeypatch):
    monkeypatch.setattr(lb, "_free_down_until", 0.0)
    monkeypatch.setattr(dc, "MODEL", _FREE)   # default model -> the free server
    lb.configure_workers(None)


# ── config parsing ────────────────────────────────────────────────────────

def test_decomposer_team_default_empty(monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_DECOMPOSER_TEAM", raising=False)
    lb.configure_workers(None)
    assert dc._decomposer_team() == []


def test_decomposer_team_shapes_and_role_model(monkeypatch):
    _team_env(monkeypatch, ["drafter", "critic", "reconciler"],
              critic="free/c:free")
    team = dc._decomposer_team()
    assert [s["role"] for s in team] == ["drafter", "critic", "reconciler"]
    assert dc._role_model(team, "critic") == "free/c:free"
    assert dc._role_model(team, "drafter") == ""        # no model -> default
    assert dc._role_model(team, "missing") == ""


# ── no team -> single agent, unchanged ────────────────────────────────────

def test_no_team_is_single_call(monkeypatch, fake_openai):
    monkeypatch.delenv("SPEC_FLOW_DECOMPOSER_TEAM", raising=False)
    _prep(monkeypatch)
    srv = fake_openai([(200, ok(_draft(["api", "db"])))])
    out = dc.decompose(_ctx())
    assert [c["id"] for c in out["children"]] == ["api", "db"]
    assert len(srv.requests) == 1, "the default decomposer is one call"


# ── full orchestra: drafter -> critic -> reconciler ───────────────────────

def test_orchestra_reconciler_output_wins_with_handoff(monkeypatch, fake_openai):
    _team_env(monkeypatch, ["drafter", "critic", "reconciler"])
    _prep(monkeypatch)
    seen = {}

    def router(payload):
        msg = " ".join(m.get("content", "")
                       for m in payload.get("messages", []))
        if "You are the RECONCILER" in msg:
            seen["reconciler"] = msg
            # the reconciler drops the duplicate child the critic flagged
            return 200, ok(_draft(["api"]))
        if "You are the CRITIC" in msg:
            seen["critic"] = msg
            return 200, ok("DEFECT: child db duplicates api; fix: depends_on api")
        seen["drafter"] = msg
        return 200, ok(_draft(["api", "db"]))

    srv = fake_openai(router)
    out = dc.decompose(_ctx())

    # the reconciler's corrected decomposition is what the engine receives
    assert [c["id"] for c in out["children"]] == ["api"], \
        "the reconciler's final decomposition must win"
    # three steps ran, in order
    assert len(srv.requests) == 3
    # handoff: the drafter's JSON + the critic's findings reached the reconciler
    assert "db" in seen["reconciler"] and "duplicates" in seen["reconciler"]


def test_critic_no_defects_keeps_draft(monkeypatch, fake_openai):
    _team_env(monkeypatch, ["drafter", "critic", "reconciler"])
    _prep(monkeypatch)

    def router(payload):
        msg = " ".join(m.get("content", "")
                       for m in payload.get("messages", []))
        if "You are the RECONCILER" in msg:
            raise AssertionError("reconciler must not run on a clean draft")
        if "You are the CRITIC" in msg:
            return 200, ok("NO DEFECTS")
        return 200, ok(_draft(["api", "db"]))

    srv = fake_openai(router)
    out = dc.decompose(_ctx())
    assert [c["id"] for c in out["children"]] == ["api", "db"]
    assert len(srv.requests) == 2, "clean draft -> no reconciler call"


def test_reconciler_bad_json_falls_back_to_draft(monkeypatch, fake_openai):
    _team_env(monkeypatch, ["drafter", "critic", "reconciler"])
    _prep(monkeypatch)
    # DECOMPOSE_ATTEMPTS retries the reconciler; every attempt returns junk, so
    # the orchestra degrades to the (valid) draft rather than crashing the run.
    monkeypatch.setenv("SPEC_FLOW_DECOMPOSE_ATTEMPTS", "1")

    def router(payload):
        msg = " ".join(m.get("content", "")
                       for m in payload.get("messages", []))
        if "You are the RECONCILER" in msg:
            return 200, ok("sorry, no json here")
        if "You are the CRITIC" in msg:
            return 200, ok("DEFECT: something; fix it")
        return 200, ok(_draft(["api", "db"]))

    fake_openai(router)
    out = dc.decompose(_ctx())
    assert [c["id"] for c in out["children"]] == ["api", "db"], \
        "a botched reconciler must not lose the valid draft"


def test_per_role_model_reaches_the_backend(monkeypatch, fake_openai):
    _team_env(monkeypatch, ["drafter", "critic"], drafter="free/drafter:free")
    _prep(monkeypatch)
    seen = {}

    def router(payload):
        msg = " ".join(m.get("content", "")
                       for m in payload.get("messages", []))
        seen[payload["model"]] = True
        if "You are the CRITIC" in msg:
            return 200, ok("NO DEFECTS")
        return 200, ok(_draft(["api"]))

    fake_openai(router)
    dc.decompose(_ctx())
    # the drafter used its declared model; the critic fell back to the default
    assert "free/drafter:free" in seen
    assert _FREE in seen
