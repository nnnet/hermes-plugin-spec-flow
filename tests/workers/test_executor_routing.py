"""C1 — executor roster + task->executor routing.

A leaf is routed to an EXECUTOR (a named agent OR a D1 implementer team-spec) by
its DOMAIN, derived from the node (explicit > project default > auto-inferred >
general). Unknown domains degrade to the roster default, and a missing default
degrades to the engine's normal implementer. All deterministic + offline — no
LLM, except one real precedence test that serves runnable code from a local
server (no mocks on the model path)."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import executors as ex      # noqa: E402
from harness import role_worker as rw    # noqa: E402
from harness import llm_backend as lb    # noqa: E402
from harness import ws_tx                # noqa: E402
from harness_fakeapi import ok           # noqa: E402
import spec_flow_runner as sfr           # noqa: E402


# ── domain inference (reuses the specialty anchors) ───────────────────────

def test_infer_domain_from_title_and_spec():
    assert ex.infer_domain("HTML page for notes", "render a WSGI view") \
        == "frontend"
    assert ex.infer_domain("notes schema", "create the sqlite table") \
        == "database"
    assert ex.infer_domain("POST /notes endpoint", "the request handler") \
        == "api"
    # nothing scores -> the catch-all domain, never empty
    assert ex.infer_domain("widget", "do the thing") == "general"


# ── domain resolution precedence ──────────────────────────────────────────

def test_explicit_node_domain_wins():
    node = {"domain": "payments", "title": "HTML page"}  # title says frontend
    assert ex.resolve_domain(node, {"default_domain": "api"}, auto=True) \
        == "payments"


def test_project_default_domain_then_auto_then_general():
    node = {"title": "sqlite schema", "spec_markdown": "create table notes"}
    # project default pins it regardless of inference
    assert ex.resolve_domain(node, {"default_domain": "api"}, auto=True) == "api"
    # no project default + auto -> inferred
    assert ex.resolve_domain(node, {}, auto=True) == "database"
    # auto off + nothing explicit/default -> general
    assert ex.resolve_domain(node, {}, auto=False) == "general"


# ── roster parsing + executor resolution ──────────────────────────────────

def test_parse_executors_reads_block_else_empty():
    cfg = {"executors": {"frontend": "web-agent",
                         "default": {"team": {"specialists": [{"role": "coder"}]}}}}
    assert ex.parse_executors(cfg)["frontend"] == "web-agent"
    assert ex.parse_executors({}) == {}
    assert ex.parse_executors({"executors": ["bad"]}) == {}
    assert ex.parse_executors("nope") == {}


def test_resolve_executor_known_then_default_then_none():
    roster = {"frontend": "web-agent", "default": "general-agent"}
    assert ex.resolve_executor("frontend", roster) == "web-agent"
    # an UNKNOWN domain is not an error -> the default executor
    assert ex.resolve_executor("payments", roster) == "general-agent"
    # no default -> None (the engine's normal implementer)
    assert ex.resolve_executor("payments", {"frontend": "web-agent"}) is None
    # empty roster -> None
    assert ex.resolve_executor("frontend", {}) is None


# ── executor spec shapes: team vs named agent ─────────────────────────────

def test_team_of_accepts_every_shape():
    canon = {"team": {"specialists": [{"role": "coder"}, {"role": "fixer"}]}}
    assert [s["role"] for s in ex.team_of(canon)] == ["coder", "fixer"]
    assert [s["role"] for s in ex.team_of({"specialists": [{"role": "coder"}]})] \
        == ["coder"]
    assert [s["role"] for s in ex.team_of({"team": [{"role": "coder"}]})] \
        == ["coder"]
    assert [s["role"] for s in ex.team_of([{"role": "coder"}])] == ["coder"]
    # a named agent / empty carries no team
    assert ex.team_of("web-agent") is None
    assert ex.team_of({}) is None
    assert ex.team_of(None) is None


def test_agent_of_extracts_named_agent_only():
    assert ex.agent_of("web-agent") == "web-agent"
    assert ex.agent_of({"agent": "web-agent"}) == "web-agent"
    # a team-spec routes to no single agent
    assert ex.agent_of({"team": {"specialists": [{"role": "coder"}]}}) is None
    assert ex.agent_of(None) is None


# ── engine composition: _resolve_executor(node) -> (domain, team) ─────────

class _Eng:
    """A minimal stand-in carrying only what Engine._resolve_executor reads."""
    def __init__(self, roster, project=None, auto=False):
        self._executors_cfg = roster
        self._project_meta = project or {}
        self._auto_domain = auto


def test_engine_resolves_team_executor_for_a_domain():
    roster = {"frontend": {"team": {"specialists": [{"role": "coder"},
                                                    {"role": "fixer"}]}},
              "default": "general-agent"}
    eng = _Eng(roster, auto=True)
    node = {"id": "web_ui", "title": "HTML page", "spec_markdown": "WSGI view"}
    domain, team = sfr.Engine._resolve_executor(eng, node)
    assert domain == "frontend"
    assert [s["role"] for s in team] == ["coder", "fixer"]


def test_engine_unknown_domain_falls_to_default_no_team():
    # default is a NAMED agent -> team is None (the agent path, not D1 orchestra)
    eng = _Eng({"frontend": "web-agent", "default": "general-agent"}, auto=True)
    domain, team = sfr.Engine._resolve_executor(
        eng, {"id": "pay", "title": "payment charge", "spec_markdown": "refund"})
    assert domain == "payments"          # inferred, not in roster
    assert team is None                  # default is an agent -> no D1 team


def test_engine_no_roster_is_general_none():
    eng = _Eng({}, auto=True)
    assert sfr.Engine._resolve_executor(eng, {"id": "x", "title": "y"}) \
        == ("general", None)


# ── role_worker: a per-leaf ctx team overrides the global team ────────────

def test_normalise_team_accepts_list_and_dict():
    assert [s["role"] for s in rw._normalise_team([{"role": "coder"}])] \
        == ["coder"]
    assert [s["role"] for s in
            rw._normalise_team({"specialists": [{"role": "fixer"}]})] == ["fixer"]
    assert rw._normalise_team("nope") == []
    assert rw._normalise_team(None) == []


def test_ctx_team_overrides_global_implementer_team(monkeypatch, tmp_path,
                                                    fake_openai):
    """The engine routed this leaf to a domain executor team: implement() must
    run THAT team (architect first), not the globally-configured one. Verified
    for real — a local server serves the steps and a genuine pytest greens the
    leaf; only config seams are pinned."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    # global team is just a lone coder; the per-leaf executor team adds an
    # architect up front, so an architect step proves the ctx team won.
    lb.configure_workers({"implementer": {"team": {"specialists":
                                                   [{"role": "coder"}]}}})
    ws_tx.ensure_repo(str(tmp_path))
    spec = tmp_path / "specs" / "leaf1.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# Leaf One\nReturn 1.\n", encoding="utf-8")
    monkeypatch.setattr(rw, "load_skill_md", lambda s: "SYS")
    monkeypatch.setattr(rw, "load_profile_policy", lambda r: ([], []))
    monkeypatch.setattr(rw, "_model_for", lambda *a, **k: "chainhead:free")
    monkeypatch.setattr(lb, "model_for", lambda *a, **k: "chainhead:free")
    monkeypatch.setattr(rw, "_ensemble_size", lambda: 1)
    monkeypatch.setattr(rw, "_chat_only", lambda: True)
    monkeypatch.setattr(rw.claims, "BOARD", None)
    monkeypatch.setattr(rw.llm_backend, "chain_for",
                        lambda role, specialty="": ["chainhead:free"])

    src = "def value():\n    return 1\n"
    test = ("import sys, pathlib\n"
            "sys.path.insert(0, str(pathlib.Path(__file__).resolve()"
            ".parents[1] / 'src'))\n"
            "import leaf1\n"
            "def test_value():\n    assert leaf1.value() == 1\n")
    seen = []

    def router(payload):
        msg = " ".join(m.get("content", "")
                       for m in payload.get("messages", []))
        if "ARCHITECT sub-role" in msg:
            seen.append("architect")
            return 200, ok("PLAN: value() -> 1")
        seen.append("coder")
        return 200, ok(json.dumps({"files": {"src/leaf1.py": src,
                                             "tests/test_leaf1.py": test}}))

    fake_openai(router)

    class _WS:
        enabled = True
        root = str(tmp_path)

        def _write(self, rel, body, kind):
            f = pathlib.Path(self.root) / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body, encoding="utf-8")
            return rel

    impl = rw.make_implementer(channel=None)
    impl({"node": "leaf1", "title": "Leaf One", "module": "leaf1",
          "workspace": _WS(), "spec": "specs/leaf1.md",
          # the per-leaf executor team the engine routed in
          "team": [{"role": "architect"}, {"role": "coder"}]})
    lb.configure_workers(None)

    assert "architect" in seen, "the per-leaf executor team must run (architect)"
