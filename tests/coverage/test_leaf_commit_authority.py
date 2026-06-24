"""Single git authority (root-cause of v078 NOT READY): a leaf commits its real
src via ``ws_tx`` while the engine ALSO commits a ``feat:`` for the same leaf.
When the two histories diverge the leaf's code is orphaned — it lives in the
repo but is NOT an ancestor of HEAD, and the integrate/boot-gate sees an empty
``src/``. This deterministic repro drives the engine with NO model: an
implementer that writes ``src/<module>.py`` through ``ws_tx.transaction`` exactly
like the orchestra coder does.

INVARIANT (the fix must hold): after a multi-leaf run with git provenance ON,
every leaf's ``src/<module>.py`` is present non-empty in the FINAL working tree
AND reachable from HEAD. No leaf's real code is ever orphaned by a competing
commit authority.
"""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng    # noqa: E402
from harness import ws_tx                 # noqa: E402

_BRANCH = {"modules": 2, "tasks": 8, "interfaces": 2, "estimated_loc": 400,
           "open_decisions": 0, "single_concern": False,
           "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True,
         "testable_criteria": True}


def _project():
    return {
        "name": "commit-authority", "goal": "two cooperating modules",
        "target": "x",
        "policy": {"measurable_target": True, "spend_per_action_usd": 0,
                   "human_in_loop": False, "involves_outreach": False,
                   "consent_obtained": True, "legal_exposure": False,
                   "legality_reviewed": True},
        "tree": {"id": "L0", "title": "Root", "metrics": dict(_BRANCH),
                 "children": [
                     {"id": "db_layer", "title": "DB layer",
                      "metrics": dict(_LEAF)},
                     {"id": "notes_api", "title": "Notes API",
                      "metrics": dict(_LEAF)}]},
    }


def _ws_tx_implementer(ctx):
    """Write the leaf's real src+test through ws_tx — the orchestra's path."""
    root = str(ctx["workspace"].root)
    nid = ctx["node"]
    mod = ctx.get("module") or nid
    with ws_tx.transaction(root, f"leaf:{nid}", "orchestra coder"):
        srcd = pathlib.Path(root) / "src"
        testd = pathlib.Path(root) / "tests"
        srcd.mkdir(parents=True, exist_ok=True)
        testd.mkdir(parents=True, exist_ok=True)
        (srcd / f"{mod}.py").write_text(
            f"def {mod}():\n    return {mod!r}\n", encoding="utf-8")
        (testd / f"test_{mod}.py").write_text(
            f"from src.{mod} import {mod}\n\n\n"
            f"def test_{mod}():\n    assert {mod}() == {mod!r}\n",
            encoding="utf-8")
    return {"files": []}


def _head_has(root: pathlib.Path, rel: str) -> bool:
    """True when `rel` is present in the commit at HEAD (reachable, not orphan)."""
    r = subprocess.run(["git", "-C", str(root), "cat-file", "-e",
                        f"HEAD:{rel}"], capture_output=True)
    return r.returncode == 0


def test_every_leaf_src_survives_to_final_tree_and_head(tmp_path):
    wk = tmp_path / "wk"
    eng.run_project(
        _project(), workspace=str(wk), depth="execute", git_provenance=True,
        node_engine="fsm",
        agents={"implementer": _ws_tx_implementer})

    src = wk / "src"
    for mod in ("db_layer", "notes_api"):
        f = src / f"{mod}.py"
        assert f.is_file() and f.read_text(encoding="utf-8").strip(), (
            f"{mod}.py missing/empty on disk — leaf code orphaned")
        assert _head_has(wk, f"src/{mod}.py"), (
            f"src/{mod}.py is not reachable from HEAD — competing commit "
            f"authority orphaned the leaf's real code")


def test_late_requirement_src_also_survives(tmp_path):
    wk = tmp_path / "wk"
    eng.run_project(
        _project(), workspace=str(wk), depth="execute", git_provenance=True,
        node_engine="fsm",
        agents={"implementer": _ws_tx_implementer},
        standing_requirements=lambda: [
            ("ping_text", "Serve GET /ping returning the plain text pong")])

    # the two base leaves AND the late one must all reach HEAD
    for mod in ("db_layer", "notes_api"):
        assert _head_has(wk, f"src/{mod}.py"), (
            f"src/{mod}.py orphaned after a late injection landed")


# ── the integrate invariant fires loud (never a silent stub-green) ──────────
_HTTP_CONSTITUTION = [
    "HTTP through a WSGI app: src/app.py exposes `wsgi_app`.",
    "POST /notes takes {text} and responds {id}; GET /health responds 200.",
]


def _project_web():
    p = _project()
    p["constitution"] = list(_HTTP_CONSTITUTION)
    return p


def test_integrate_fails_loud_when_a_leaf_module_is_lost(tmp_path, monkeypatch):
    """A leaf that DELIVERS an empty/absent module (the orphan symptom) must
    make root integrate FAIL by name — not green-wash a product whose feature
    module was dropped before the boot-gate."""
    monkeypatch.setenv("SPEC_FLOW_PRE_GATE", "1")

    def implementer(ctx):
        root = ctx["workspace"].root
        nid = ctx["node"]
        mod = ctx.get("module") or nid
        srcd = pathlib.Path(root) / "src"
        srcd.mkdir(parents=True, exist_ok=True)
        if mod == "notes_api":
            (srcd / f"{mod}.py").write_text("", encoding="utf-8")   # orphaned
        else:
            (srcd / f"{mod}.py").write_text(
                f"def {mod}():\n    return {mod!r}\n# real handler\n",
                encoding="utf-8")
        return {"files": []}

    res = eng.run_project(
        _project_web(), workspace=str(tmp_path / "wk"), depth="execute",
        git_provenance=True, agents={"implementer": implementer})

    hit = [e for e in res.events
           if e.gate == "integrate_verify" and e.verdict == "FAIL"
           and "notes_api" in (getattr(e, "detail", "") or "")]
    assert hit, ("integrate did not fail loud on the lost notes_api module — "
                 "a dropped feature module would green-wash as READY")
