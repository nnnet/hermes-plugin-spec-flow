"""B3 (deterministic, model-independent): a late requirement about a surface an
existing module already serves is ROUTED to edit that module — not forked into a
parallel one. The routing is done in code (route owner match), the implementer
is HANDED the owner module, and a guard drops any fork the worker still writes.

This is the engine layer of the three-part fix (code + prompt + check):
  * code  — _amend_target finds the route owner, code_target routes output here;
  * prompt — the implementer prompt also says edit-in-place (role_worker.py);
  * check — the guard removes a parallel fork so the invariant holds regardless
            of whether the model obeyed.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng    # noqa: E402

_BRANCH = {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 400,
           "open_decisions": 0, "single_concern": False,
           "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True,
         "testable_criteria": True}


def _project():
    return {
        "name": "amend-routing", "goal": "notes service with a web page",
        "target": "x",
        "policy": {"measurable_target": True, "spend_per_action_usd": 0,
                   "human_in_loop": False, "involves_outreach": False,
                   "consent_obtained": True, "legal_exposure": False,
                   "legality_reviewed": True},
        "tree": {"id": "L0", "title": "Root", "metrics": dict(_BRANCH),
                 "children": [{"id": "web_ui", "title": "Web page",
                               "metrics": dict(_LEAF)}]},
    }


def _src(ctx):
    root = pathlib.Path(ctx["workspace"].root)
    (root / "src").mkdir(parents=True, exist_ok=True)
    return root


def test_second_web_requirement_is_routed_into_the_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_REQ_AMEND", "1")
    seen = {}

    def implementer(ctx):
        root = _src(ctx)
        nid = ctx["node"]
        mod = ctx.get("module")
        seen[nid] = mod
        if nid == "web_ui":
            (root / "src" / "web_ui.py").write_text(
                'def page():\n    return "<html>notes</html>"\n'
                '# serves GET /ui\n', encoding="utf-8")
        else:               # routed node: write the module it was handed
            (root / "src" / f"{mod}.py").write_text(
                f'# extended by {nid}\ndef page():\n'
                '    return "<html>nice notes</html>"\n# serves GET /ui\n',
                encoding="utf-8")
        return {"files": []}

    res = eng.run_project(
        _project(), workspace=str(tmp_path / "wk"), depth="execute",
        agents={"implementer": implementer},
        standing_requirements=lambda: [
            ("nice_ui", "Make the existing web page at GET /ui look nice.")])

    # the engine emitted the AMEND routing milestone for nice_ui
    assert [e for e in res.events
            if e.gate == "requirement" and e.verdict == "AMEND"
            and e.task == "nice_ui"]
    # the implementer for nice_ui was HANDED the owner module web_ui (code-level
    # routing), not its own name
    assert seen.get("nice_ui") == "web_ui"
    # and no parallel src/nice_ui.py was left behind — one surface, one module
    assert not (tmp_path / "wk" / "src" / "nice_ui.py").exists()
    assert (tmp_path / "wk" / "src" / "web_ui.py").exists()


def test_guard_drops_a_fork_even_if_the_worker_disobeys(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_REQ_AMEND", "1")

    def implementer(ctx):
        root = _src(ctx)
        nid = ctx["node"]
        if nid == "web_ui":
            (root / "src" / "web_ui.py").write_text(
                'def page():\n    return "<html>notes</html>"\n# GET /ui\n',
                encoding="utf-8")
        else:
            # a DISOBEDIENT worker ignores the handed module and forks its own
            (root / "src" / "nice_ui.py").write_text(
                'def other():\n    return "<html>dup</html>"\n# GET /ui\n',
                encoding="utf-8")
        return {"files": []}

    res = eng.run_project(
        _project(), workspace=str(tmp_path / "wk"), depth="execute",
        agents={"implementer": implementer},
        standing_requirements=lambda: [
            ("nice_ui", "Make the existing web page at GET /ui look nice.")])

    # the guard fired and the parallel fork was removed deterministically
    assert [e for e in res.events if e.gate == "amend_guard"]
    assert not (tmp_path / "wk" / "src" / "nice_ui.py").exists()


def test_unrelated_requirement_is_not_routed(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_REQ_AMEND", "1")
    seen = {}

    def implementer(ctx):
        root = _src(ctx)
        nid = ctx["node"]
        seen[nid] = ctx.get("module")
        if nid == "web_ui":
            (root / "src" / "web_ui.py").write_text("# GET /ui\n",
                                                    encoding="utf-8")
        else:
            (root / "src" / f"{ctx.get('module')}.py").write_text(
                "# unrelated\n", encoding="utf-8")
        return {"files": []}

    res = eng.run_project(
        _project(), workspace=str(tmp_path / "wk"), depth="execute",
        agents={"implementer": implementer},
        standing_requirements=lambda: [
            ("audit_log", "Append every write to an audit log file.")])

    # no route overlap → no AMEND, audit_log keeps its own module
    assert not [e for e in res.events
                if e.gate == "requirement" and e.verdict == "AMEND"]
    assert seen.get("audit_log") == "audit_log"


def test_flag_off_keeps_separate_modules(tmp_path, monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_REQ_AMEND", raising=False)
    seen = {}

    def implementer(ctx):
        root = _src(ctx)
        nid = ctx["node"]
        seen[nid] = ctx.get("module")
        (root / "src" / f"{ctx.get('module')}.py").write_text(
            "# GET /ui\n", encoding="utf-8")
        return {"files": []}

    res = eng.run_project(
        _project(), workspace=str(tmp_path / "wk"), depth="execute",
        agents={"implementer": implementer},
        standing_requirements=lambda: [
            ("nice_ui", "Make the existing web page at GET /ui look nice.")])

    assert not [e for e in res.events
                if e.gate == "requirement" and e.verdict == "AMEND"]
    assert seen.get("nice_ui") == "nice_ui"     # own module, not routed
