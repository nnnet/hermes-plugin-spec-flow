"""Implementer orchestra (D1) — the single-agent implementer generalized into
a configurable TEAM of sub-roles (architect -> coder -> tester -> fixer) run in
order with handoffs.

HARD INVARIANT under test: with NO team configured (the default), the
implementer takes the EXISTING single-agent path, untouched. The team path is
exercised in isolation with all LLM/file/git building blocks mocked, so the
tests are hermetic (no real LLM, no network, no pytest subprocess)."""
import contextlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from harness import role_worker as rw   # noqa: E402
from harness import llm_backend as lb   # noqa: E402


@contextlib.contextmanager
def _noop_tx(*a, **k):
    yield object()


def _base_ctx(tmp_path):
    """A minimal implementer ctx: the shape make_implementer's callable reads
    ({node,title,workspace,spec,module,specialty,...})."""
    ws = type("WS", (), {"root": str(tmp_path),
                         "_write": lambda self, *a, **k: None})()
    return {"node": "leaf1", "title": "Leaf One", "depth": 2,
            "workspace": ws, "spec": "specs/leaf1.md", "module": "leaf1",
            "specialty": ""}


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

def test_no_team_takes_single_agent_path(monkeypatch, tmp_path):
    """The default (no team) must call the unchanged chat single-agent
    function and NEVER the orchestra runner."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    monkeypatch.setattr(rw, "_chat_only", lambda: True)        # pick chat path
    monkeypatch.setattr(rw.claims, "BOARD", None)              # no dedup claim
    monkeypatch.setattr(rw, "load_skill_md", lambda s: "SYS")
    monkeypatch.setattr(rw, "load_profile_policy", lambda r: ([], []))
    monkeypatch.setattr(rw, "_model_for", lambda *a, **k: "m")

    called = {"single": 0, "orchestra": 0}

    def fake_orchestra(*a, **k):
        called["orchestra"] += 1
        return None

    monkeypatch.setattr(rw, "_orchestra_run", fake_orchestra)
    impl = rw.make_implementer(channel=None)
    # monkeypatch the bound single-agent function via the module-level wrapper:
    # _implement_chat is a closure, so we instead assert the orchestra was NOT
    # called and a single-agent marker fired. Stub the heavy chat internals.
    monkeypatch.setattr(rw, "_ensemble_generate",
                        lambda *a, **k: called.__setitem__("single",
                                                           called["single"] + 1)
                        or json.dumps({"files": {}}))
    monkeypatch.setattr(rw, "_extract_json", lambda r: {"files": {}})
    monkeypatch.setattr(rw, "_write_reply_files", lambda *a, **k: False)
    monkeypatch.setattr(rw.ws_tx if hasattr(rw, "ws_tx") else rw,
                        "transaction", _noop_tx, raising=False)
    import harness.ws_tx as wstx
    monkeypatch.setattr(wstx, "transaction", _noop_tx)
    import harness.pytest_verifier as pvmod
    monkeypatch.setattr(pvmod, "run_suite", lambda *a, **k: (True, ""))
    monkeypatch.setattr(pvmod, "_badness", lambda *a, **k: 0)

    impl(_base_ctx(tmp_path))
    assert called["orchestra"] == 0, "orchestra must NOT run without a team"
    assert called["single"] == 1, "single-agent generation must run"


# ── team path: 4 steps run IN ORDER with handoff ──────────────────────────

def test_four_step_team_runs_in_order_with_handoff(monkeypatch, tmp_path):
    """[architect, coder, tester, fixer] must invoke the steps in order, pass
    the architect's plan into the coder/tester prompt, and pass the tester's
    failing output into the fixer's repair call."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    monkeypatch.setattr(rw, "load_skill_md", lambda s: "SYS")
    monkeypatch.setattr(rw, "_model_for", lambda *a, **k: "m")
    monkeypatch.setattr(lb, "model_for", lambda *a, **k: "m")
    monkeypatch.setattr(rw, "_inline_file", lambda root, rel: "SPEC BODY")

    import harness.ws_tx as wstx
    monkeypatch.setattr(wstx, "transaction", _noop_tx)
    import harness.pytest_verifier as pvmod
    monkeypatch.setattr(pvmod, "run_suite", lambda *a, **k: (True, ""))
    monkeypatch.setattr(pvmod, "_badness", lambda *a, **k: 0)

    order = []
    seen_prompts = {}

    def fake_dialog(prompt, *, role, node, system, allowed, disallowed, cwd,
                    model, channel, specialty="", **kw):
        order.append("architect")            # architect uses _dialog_round
        return "PLAN: def make_note(text): ..."

    def fake_ensemble(prompt, *, node, system, allowed, disallowed, cwd, model,
                      channel, specialty, **kw):
        # coder then tester both go through the ensemble path
        tag = "tester" if "TESTER PASS" in prompt else "coder"
        order.append(tag)
        seen_prompts[tag] = prompt
        return json.dumps({"files": {f"src/{node}.py": "x = 1\n"}})

    def fake_call(prompt, *, system, allowed, disallowed, cwd, model, role,
                  specialty="", **kw):
        order.append("fixer")                # fixer uses _call_model
        seen_prompts["fixer"] = prompt
        return "FILE: src/leaf1.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE"

    # leaf bar: coder/tester leave it RED so the fixer step has work to do
    bar_results = iter([(False, "TESTER_FAIL_TAIL"),   # after coder write
                        (False, "TESTER_FAIL_TAIL"),   # after tester write
                        (True, "fixed")])              # after fixer
    monkeypatch.setattr(rw, "_leaf_bar", lambda *a, **k: next(bar_results))
    monkeypatch.setattr(rw, "_write_reply_files", lambda *a, **k: True)
    monkeypatch.setattr(rw, "_apply_diff_repair", lambda *a, **k: True)
    monkeypatch.setattr(rw, "_dialog_round", fake_dialog)
    monkeypatch.setattr(rw, "_ensemble_generate", fake_ensemble)
    monkeypatch.setattr(rw, "_call_model", fake_call)

    team = [{"role": "architect"}, {"role": "coder"},
            {"role": "tester"}, {"role": "fixer"}]
    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)

    assert order == ["architect", "coder", "tester", "fixer"], \
        f"steps must run in declared order, saw {order}"
    # handoff: architect plan reached the coder prompt
    assert "ARCHITECT PLAN" in seen_prompts["coder"]
    assert "make_note" in seen_prompts["coder"]
    # handoff: tester's failing output reached the fixer's repair prompt
    assert "TESTER_FAIL_TAIL" in seen_prompts["fixer"]


def test_fixer_skipped_when_already_green(monkeypatch, tmp_path):
    """If the coder/tester already left the leaf GREEN, the fixer step is a
    no-op (no repair call) — resilience without wasted LLM calls."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    monkeypatch.setattr(rw, "load_skill_md", lambda s: "SYS")
    monkeypatch.setattr(rw, "_model_for", lambda *a, **k: "m")
    monkeypatch.setattr(lb, "model_for", lambda *a, **k: "m")
    monkeypatch.setattr(rw, "_inline_file", lambda root, rel: "SPEC")
    import harness.ws_tx as wstx
    monkeypatch.setattr(wstx, "transaction", _noop_tx)
    import harness.pytest_verifier as pvmod
    monkeypatch.setattr(pvmod, "run_suite", lambda *a, **k: (True, ""))
    monkeypatch.setattr(pvmod, "_badness", lambda *a, **k: 0)
    monkeypatch.setattr(rw, "_write_reply_files", lambda *a, **k: True)
    monkeypatch.setattr(rw, "_leaf_bar", lambda *a, **k: (True, "green"))
    monkeypatch.setattr(rw, "_ensemble_generate",
                        lambda *a, **k: json.dumps({"files": {"src/leaf1.py": "x=1\n"}}))

    fixer_calls = {"n": 0}
    monkeypatch.setattr(rw, "_call_model",
                        lambda *a, **k: fixer_calls.__setitem__("n", 1) or "")
    monkeypatch.setattr(rw, "_apply_diff_repair", lambda *a, **k: True)

    team = [{"role": "coder"}, {"role": "fixer"}]
    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)
    assert fixer_calls["n"] == 0, "fixer must not run when the leaf is green"


def test_step_error_does_not_crash_run(monkeypatch, tmp_path):
    """A sub-step that raises must be logged and skipped, not crash the run —
    the orchestra still completes and verifies the leaf."""
    monkeypatch.delenv("SPEC_FLOW_IMPLEMENTER_TEAM", raising=False)
    lb.configure_workers(None)
    monkeypatch.setattr(rw, "load_skill_md", lambda s: "SYS")
    monkeypatch.setattr(rw, "_model_for", lambda *a, **k: "m")
    monkeypatch.setattr(lb, "model_for", lambda *a, **k: "m")
    monkeypatch.setattr(rw, "_inline_file", lambda root, rel: "SPEC")
    import harness.ws_tx as wstx
    monkeypatch.setattr(wstx, "transaction", _noop_tx)
    import harness.pytest_verifier as pvmod
    monkeypatch.setattr(pvmod, "run_suite", lambda *a, **k: (True, ""))
    monkeypatch.setattr(pvmod, "_badness", lambda *a, **k: 0)

    def boom(*a, **k):
        raise RuntimeError("step exploded")

    monkeypatch.setattr(rw, "_dialog_round", boom)        # architect explodes
    reached_coder = {"n": 0}

    def fake_ensemble(*a, **k):
        reached_coder["n"] += 1
        return json.dumps({"files": {}})

    monkeypatch.setattr(rw, "_ensemble_generate", fake_ensemble)
    monkeypatch.setattr(rw, "_write_reply_files", lambda *a, **k: False)

    team = [{"role": "architect"}, {"role": "coder"}]
    # must not raise
    rw._orchestra_run(_base_ctx(tmp_path), str(tmp_path), "leaf1", "leaf1",
                      system="SYS", allowed=[], disallowed=[],
                      channel=None, team=team)
    assert reached_coder["n"] == 1, "run continued to the coder after the error"


# ── phase 2: declarative workflow drives the step order ───────────────────

def _wire_team_fakes(monkeypatch, rw, lb, order, seen_prompts, bar_results):
    """Shared fake wiring for the workflow-driven orchestra tests: the four
    role branches record their name + prompt and the leaf bar is scripted."""
    monkeypatch.setattr(rw, "load_skill_md", lambda s: "SYS")
    monkeypatch.setattr(rw, "_model_for", lambda *a, **k: "m")
    monkeypatch.setattr(lb, "model_for", lambda *a, **k: "m")
    monkeypatch.setattr(rw, "_inline_file", lambda root, rel: "SPEC BODY")
    import harness.ws_tx as wstx
    monkeypatch.setattr(wstx, "transaction", _noop_tx)
    import harness.pytest_verifier as pvmod
    monkeypatch.setattr(pvmod, "run_suite", lambda *a, **k: (True, ""))
    monkeypatch.setattr(pvmod, "_badness", lambda *a, **k: 0)

    def fake_dialog(prompt, *, role, node, system, allowed, disallowed, cwd,
                    model, channel, specialty="", **kw):
        order.append("architect")
        return "PLAN: def make_note(text): ..."

    def fake_ensemble(prompt, *, node, system, allowed, disallowed, cwd, model,
                      channel, specialty, **kw):
        tag = "tester" if "TESTER PASS" in prompt else "coder"
        order.append(tag)
        seen_prompts[tag] = prompt
        return json.dumps({"files": {f"src/{node}.py": "x = 1\n"}})

    def fake_call(prompt, *, system, allowed, disallowed, cwd, model, role,
                  specialty="", **kw):
        order.append("fixer")
        seen_prompts["fixer"] = prompt
        return ("FILE: src/leaf1.py\n<<<<<<< SEARCH\nx = 1\n"
                "=======\nx = 2\n>>>>>>> REPLACE")

    monkeypatch.setattr(rw, "_leaf_bar", lambda *a, **k: next(bar_results))
    monkeypatch.setattr(rw, "_write_reply_files", lambda *a, **k: True)
    monkeypatch.setattr(rw, "_apply_diff_repair", lambda *a, **k: True)
    monkeypatch.setattr(rw, "_dialog_round", fake_dialog)
    monkeypatch.setattr(rw, "_ensemble_generate", fake_ensemble)
    monkeypatch.setattr(rw, "_call_model", fake_call)


def test_explicit_sequential_workflow_runs_four_steps_in_order(monkeypatch,
                                                               tmp_path):
    """An explicit `process: sequential` (no edge graph) drives the four steps
    in declared order, identical to the default bare-list behaviour."""
    lb.configure_workers(None)
    order, seen = [], {}
    bar = iter([(True, "green"), (True, "green"),
                (True, "green"), (True, "green")])
    _wire_team_fakes(monkeypatch, rw, lb, order, seen, bar)
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


def test_graph_workflow_loops_fixer_then_reruns_tester(monkeypatch, tmp_path):
    """With a tester->fixer(when tests_failed)->tester(when retry)->DONE(when
    tests_passed) graph, a failing-then-passing tester drives the fixer and
    then a tester RE-RUN (the evaluator-optimizer loop)."""
    lb.configure_workers(None)
    order, seen = [], {}
    # tester RED first, fixer still RED (so `retry` fires), tester GREEN -> DONE
    bar = iter([(False, "RED1"),      # after coder write (tester branch unused)
                (False, "RED_TESTER"),  # tester 1st visit -> RED
                (False, "RED_FIXER"),   # fixer attempt -> still RED -> retry
                (True, "green")])       # tester 2nd visit -> GREEN -> DONE
    _wire_team_fakes(monkeypatch, rw, lb, order, seen, bar)
    graph = {"start": "architect", "edges": [
        {"from": "architect", "to": "coder"},
        {"from": "coder", "to": "tester"},
        {"from": "tester", "to": "fixer", "when": "tests_failed"},
        {"from": "fixer", "to": "tester", "when": "retry"},
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
    assert "TESTER_FAIL" not in str(seen)   # sanity: prompts captured
