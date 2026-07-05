"""Implementer orchestra (D1) — the single-agent implementer generalized into
a configurable TEAM of sub-roles (architect -> coder -> tester -> fixer) run in
order with handoffs.

HARD INVARIANT under test: with NO team configured (the default), the
implementer takes the EXISTING single-agent path, untouched.

The team path is exercised for REAL: a real git workspace, real file writes and
a real pytest leaf bar. Each step's reply is served by the local fake-OpenAI
server as RUNNABLE code, so the leaf's red/green is a genuine pytest verdict, not
a scripted boolean — the workflow driver reacts to a real coder->tester->fixer
red->green. Step order is read from the prompt the harness actually sent the
server; only deliberate negatives stay doubles (the orchestra-not-run spy, the
architect fault injection)."""
import contextlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import role_worker as rw   # noqa: E402
from harness import llm_backend as lb   # noqa: E402
from harness import ws_tx               # noqa: E402
from harness_fakeapi import ok          # noqa: E402


# ── a real leaf the orchestra writes and verifies under a genuine pytest ──
_SRC_GREEN = "def value():\n    return 1\n"
_SRC_RED = "def value():\n    return 0\n"        # wrong -> the green test fails
_TEST = ("import sys, pathlib\n"
         "sys.path.insert(0, str(pathlib.Path(__file__).resolve()"
         ".parents[1] / 'src'))\n"
         "import leaf1\n"
         "def test_value():\n    assert leaf1.value() == 1\n")
_GREEN = {"src/leaf1.py": _SRC_GREEN, "tests/test_leaf1.py": _TEST}
_RED = {"src/leaf1.py": _SRC_RED, "tests/test_leaf1.py": _TEST}


class _RealWS:
    """A real workspace: writes land on disk so the orchestra's transaction,
    pytest run and leaf bar all execute against real files."""
    enabled = True

    def __init__(self, root):
        self.root = str(root)

    def _write(self, rel, body, kind):
        f = pathlib.Path(self.root) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
        return rel


def _base_ctx(tmp_path):
    """A minimal implementer ctx with a REAL workspace + spec on disk."""
    spec = pathlib.Path(tmp_path) / "specs" / "leaf1.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# Leaf One\nReturn the constant 1.\n", encoding="utf-8")
    return {"node": "leaf1", "title": "Leaf One", "depth": 2,
            "workspace": _RealWS(tmp_path), "spec": "specs/leaf1.md",
            "module": "leaf1", "specialty": ""}


def _real_env(monkeypatch, tmp_path, *, model_head="chainhead:free"):
    """Wire the orchestra to run for REAL — real git workspace, real pytest,
    real file writes, real leaf bar. ONLY config seams are pinned (skill text,
    chain-head model, single-member ensemble, the chat path)."""
    ws_tx.ensure_repo(str(tmp_path))
    monkeypatch.setattr(rw, "load_skill_md", lambda s: "SYS")
    monkeypatch.setattr(rw, "_model_for", lambda *a, **k: model_head)
    monkeypatch.setattr(lb, "model_for", lambda *a, **k: model_head)
    monkeypatch.setattr(rw, "_ensemble_size", lambda *a, **k: 1)
    monkeypatch.setattr(rw, "_chat_only", lambda: True)
    monkeypatch.setattr(rw.llm_backend, "chain_for",
                        lambda *a, **k: [model_head])


def _make_router(order, seen, *, coder):
    """A fake-OpenAI router that identifies the orchestra step from the prompt
    the harness actually sent and serves runnable code for it: the architect a
    plan, the coder its `coder` files, the tester a (re-asserted) test, the
    fixer the corrected source. Records step order + the prompt each step saw."""
    def router(payload):
        msg = " ".join(m.get("content", "")
                       for m in payload.get("messages", []))
        if "The test run FAILED" in msg:           # repair prompt -> fixer
            order.append("fixer")
            seen["fixer"] = msg
            return 200, ok(json.dumps({"files": {"src/leaf1.py": _SRC_GREEN}}))
        if "ARCHITECT sub-role" in msg:
            order.append("architect")
            return 200, ok("PLAN: def make_note(text): persist and return it")
        if "TESTER PASS" in msg:                   # strengthen tests; src as-is
            order.append("tester")
            seen["tester"] = msg
            return 200, ok(json.dumps({"files": {"tests/test_leaf1.py": _TEST}}))
        order.append("coder")
        seen["coder"] = msg
        return 200, ok(json.dumps({"files": coder}))
    return router


# ── config helper: env override + WORKERS_CFG, default empty ──────────────

def test_team_default_is_empty(monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    assert rw._implementer_team() == []


def test_team_from_workers_block():
    lb.configure_workers({"implementer": {"team": [
        {"role": "coder"}, {"role": "fixer", "model": "free/x:free"}]}})
    team = rw._implementer_team()
    lb.configure_workers(None)
    assert [s["role"] for s in team] == ["coder", "fixer"]
    assert team[1]["model"] == "free/x:free"


def test_team_env_override_is_json(monkeypatch):
    lb.configure_workers(None)
    monkeypatch.setenv(
        "SPEC_FLOW_IMPLEMENTER_TEAM",
        json.dumps([{"role": "architect", "skill": "spec-implement"},
                    {"role": "coder"}]))
    team = rw._implementer_team()
    assert [s["role"] for s in team] == ["architect", "coder"]
    assert team[0]["skill"] == "spec-implement"


def test_team_bad_env_falls_back_to_empty(monkeypatch):
    lb.configure_workers(None)
    monkeypatch.setenv("SPEC_FLOW_IMPLEMENTER_TEAM", "{not json")
    assert rw._implementer_team() == []


# ── invariant: NO team -> the EXISTING single-agent path is taken ─────────

def test_no_team_takes_single_agent_path(monkeypatch, tmp_path, fake_openai):
    """The default (no team) must take the unchanged chat single-agent path and
    NEVER the orchestra runner. The single path runs for real against a local
    server + real workspace; the orchestra runner is a spy proving it stays at
    zero calls."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    _real_env(monkeypatch, tmp_path, model_head="openrouter/x:free")
    monkeypatch.setattr(rw.claims, "BOARD", None)              # no dedup claim
    monkeypatch.setattr(rw, "load_profile_policy", lambda r: ([], []))

    orch = {"n": 0}
    monkeypatch.setattr(rw, "_orchestra_run",
                        lambda *a, **k: orch.__setitem__("n", orch["n"] + 1))

    srv = fake_openai([(200, ok(json.dumps({"files": _GREEN})))])
    impl = rw.make_implementer(channel=None)
    impl(_base_ctx(tmp_path))

    assert orch["n"] == 0, "orchestra must NOT run without a team"
    assert len(srv.requests) >= 1, "single-agent generation must run for real"
    assert (tmp_path / "src" / "leaf1.py").exists(), "the leaf landed for real"


# ── team path: 4 steps run IN ORDER with handoff ──────────────────────────

def test_four_step_team_runs_in_order_with_handoff(monkeypatch, tmp_path,
                                                   fake_openai):
    """[architect, coder, tester, fixer] must invoke the steps in order, pass
    the architect's plan into the coder/tester prompt, and pass the tester's
    REAL failing output into the fixer's repair call. The coder ships a red
    leaf, the fixer greens it under a genuine pytest run."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    _real_env(monkeypatch, tmp_path)

    order, seen = [], {}
    fake_openai(_make_router(order, seen, coder=_RED))

    team = [{"role": "architect"}, {"role": "coder"},
            {"role": "tester"}, {"role": "fixer"}]
    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)

    assert order == ["architect", "coder", "tester", "fixer"], \
        f"steps must run in declared order, saw {order}"
    # handoff: architect plan reached the coder prompt
    assert "ARCHITECT PLAN" in seen["coder"]
    assert "make_note" in seen["coder"]
    # handoff: the tester's REAL failing output reached the fixer's repair prompt
    assert "test_value" in seen["fixer"]
    # the fixer's real repair greened the leaf on disk
    body = (tmp_path / "src" / "leaf1.py").read_text(encoding="utf-8")
    assert body.strip().endswith("return 1")


def test_fixer_skipped_when_already_green(monkeypatch, tmp_path, fake_openai):
    """If the coder already left the leaf GREEN (a real pytest pass), the fixer
    step is a no-op (no repair call) — resilience without wasted LLM calls."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    _real_env(monkeypatch, tmp_path)

    order, seen = [], {}
    fake_openai(_make_router(order, seen, coder=_GREEN))

    team = [{"role": "coder"}, {"role": "fixer"}]
    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)
    assert order == ["coder"], "fixer must not run when the leaf is green"


def test_step_error_does_not_crash_run(monkeypatch, tmp_path, fake_openai):
    """A sub-step that raises must be logged and skipped, not crash the run —
    the orchestra still continues to the coder. The architect fault is injected
    deliberately; everything else runs for real."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    _real_env(monkeypatch, tmp_path)

    # the architect explodes; the coder's ensemble uses the SAME _dialog_round,
    # so fault ONLY the architect prompt and let the coder reach the server.
    _orig_dialog = rw._dialog_round

    def boom(prompt, **k):
        if "ARCHITECT sub-role" in prompt:
            raise RuntimeError("step exploded")
        return _orig_dialog(prompt, **k)

    monkeypatch.setattr(rw, "_dialog_round", boom)

    order, seen = [], {}
    fake_openai(_make_router(order, seen, coder=_GREEN))

    team = [{"role": "architect"}, {"role": "coder"}]
    # must not raise
    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)
    assert order == ["coder"], "run continued to the coder after the error"


# ── phase 2: declarative workflow drives the step order ───────────────────

def test_explicit_sequential_workflow_runs_four_steps_in_order(monkeypatch,
                                                               tmp_path,
                                                               fake_openai):
    """An explicit `process: sequential` (no edge graph) drives the four steps
    in declared order. A green coder means the leaf is already passing, so the
    fixer's `if passed: continue` skips it — identical to the bare list."""
    lb.configure_workers(None)
    _real_env(monkeypatch, tmp_path)
    order, seen = [], {}
    fake_openai(_make_router(order, seen, coder=_GREEN))
    # carry the flow spec via the env (the orchestra re-sources it read-only);
    # `sequential` with no edges == today.
    monkeypatch.setenv("SPEC_FLOW_IMPLEMENTER_TEAM", json.dumps({
        "process": "sequential",
        "specialists": [{"role": "architect"}, {"role": "coder"},
                        {"role": "tester"}, {"role": "fixer"}]}))
    team = [{"role": "architect"}, {"role": "coder"},
            {"role": "tester"}, {"role": "fixer"}]
    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)
    # green throughout means the fixer's `if passed: continue` skips it.
    assert order == ["architect", "coder", "tester"], \
        f"sequential workflow order broke: {order}"


def test_graph_workflow_loops_fixer_then_reruns_tester(monkeypatch, tmp_path,
                                                       fake_openai):
    """With a tester->fixer(when tests_failed)->tester(when tests_passed)->DONE
    graph, a coder that ships a RED leaf drives the fixer and then a tester
    RE-RUN that confirms the fixer's REAL green (the evaluator-optimizer loop),
    all off genuine pytest verdicts — the fixer actually repairs the code and
    the second tester pass sees the real green before DONE."""
    lb.configure_workers(None)
    _real_env(monkeypatch, tmp_path)
    order, seen = [], {}
    fake_openai(_make_router(order, seen, coder=_RED))
    graph = {"start": "architect", "edges": [
        {"from": "architect", "to": "coder"},
        {"from": "coder", "to": "tester"},
        {"from": "tester", "to": "fixer", "when": "tests_failed"},
        {"from": "fixer", "to": "tester", "when": "tests_passed"},
        {"from": "tester", "to": "DONE", "when": "tests_passed"}]}
    monkeypatch.setenv("SPEC_FLOW_IMPLEMENTER_TEAM", json.dumps({
        "workflow": graph,
        "specialists": [{"role": "architect"}, {"role": "coder"},
                        {"role": "tester"}, {"role": "fixer"}]}))
    team = [{"role": "architect"}, {"role": "coder"},
            {"role": "tester"}, {"role": "fixer"}]
    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)
    assert order == ["architect", "coder", "tester", "fixer", "tester"], \
        f"evaluator-optimizer loop did not drive as expected: {order}"
    # the loop greened the leaf for real before reaching DONE
    body = (tmp_path / "src" / "leaf1.py").read_text(encoding="utf-8")
    assert body.strip().endswith("return 1")
