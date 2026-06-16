"""B2 mechanism 3 — the ENGINE-synthesized assembly leaf.

A constitution that declares a product entry (``wsgi_app`` in ``src/app.py``)
must end with that entry BUILT, even when the decomposer only produced feature
leaves and no node that wires them together. The engine appends one assembly
leaf at the root, LAST, so it sees every feature module already present.

Guards: constitution-keyed + behind ``SPEC_FLOW_PRE_GATE``. With the flag off,
or with no entry declared, NOTHING is injected — p4/p5 stay byte-for-byte the
same. If a feature leaf already built the entry, no assembly node is added.
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng    # noqa: E402

_BRANCH = {"modules": 3, "tasks": 12, "interfaces": 3, "estimated_loc": 900,
           "open_decisions": 0, "single_concern": False,
           "testable_criteria": True}
_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True,
         "testable_criteria": True}

_CONSTITUTION = [
    "Standard library ONLY: HTTP through a WSGI app (src/app.py exposes "
    "`wsgi_app`), storage through sqlite3 in src/db.py.",
    "POST /notes takes {text} -> {id}; GET /notes -> {items}; GET /health 200.",
]


def _project(constitution):
    return {
        "name": "assembly-case", "goal": "tiny notes service", "target": "x",
        "constitution": list(constitution),
        "policy": {"measurable_target": True, "spend_per_action_usd": 0,
                   "human_in_loop": False, "involves_outreach": False,
                   "consent_obtained": True, "legal_exposure": False,
                   "legality_reviewed": True},
        "tree": {
            "id": "L0", "title": "Root", "metrics": dict(_BRANCH),
            "children": [{"id": "notes_db", "title": "DB", "metrics": dict(_LEAF)},
                         {"id": "notes_http", "title": "HTTP", "metrics": dict(_LEAF)}],
        },
    }


def _run(tmp_path, constitution):
    return eng.run_project(_project(constitution),
                           workspace=str(tmp_path / "wk"), depth="spec")


def _children_ids(tree, nid):
    if tree["id"] == nid:
        return [c["id"] for c in tree.get("children", [])]
    for c in tree.get("children", []):
        found = _children_ids(c, nid)
        if found is not None:
            return found
    return None


def test_pre_gate_on_injects_assembly_leaf_at_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_PRE_GATE", "1")
    res = _run(tmp_path, _CONSTITUTION)
    kids = _children_ids(res.project["tree"], "L0")
    assert "product_entry" in kids
    assert "product_entry" in res.tasks
    # it lands LAST — after the feature leaves it must wire together
    assert kids.index("product_entry") > kids.index("notes_db")
    assert kids.index("product_entry") > kids.index("notes_http")


def test_pre_gate_off_injects_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_PRE_GATE", raising=False)
    res = _run(tmp_path, _CONSTITUTION)
    assert "product_entry" not in _children_ids(res.project["tree"], "L0")
    assert "product_entry" not in res.tasks


def test_no_entry_declared_injects_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_PRE_GATE", "1")
    # a constitution with NO wsgi_app/app.py declaration -> boot-gate skips,
    # so does the assembly node (p4/p5 shape)
    res = _run(tmp_path, ["Plain library, no web entry point."])
    assert "product_entry" not in _children_ids(res.project["tree"], "L0")


def test_plugin_does_not_import_contract_checks_for_entry():
    """Regression guard: the assembly node must detect the entry INLINE. The
    first cut did `from tests.harness import contract_checks`, which raises
    ModuleNotFoundError inside a real run (tests/ is not on sys.path) — the
    except swallowed it, entry became None and product_entry never injected,
    yet this very test passed because pytest makes `tests` importable. Pin the
    plugin to inline detection so the test env can't mask the run env again."""
    runner_src = (pathlib.Path(__file__).resolve().parents[2]
                  / "spec_flow_runner.py").read_text(encoding="utf-8")
    # scan IMPORT statements only — a mention inside a comment is fine
    bad = [ln for ln in runner_src.splitlines()
           if ln.lstrip().startswith(("from ", "import "))
           and "contract_checks" in ln]
    assert not bad, f"plugin imports contract_checks: {bad}"


def test_assembly_skipped_when_entry_already_built(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_PRE_GATE", "1")

    # a feature leaf that writes src/app.py itself -> no assembly node needed
    def implementer(ctx):
        root = pathlib.Path(ctx["workspace"].root)
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "src" / "app.py").write_text(
            "def wsgi_app(environ, start_response):\n"
            "    start_response('200 OK', [])\n    return [b'ok']\n",
            encoding="utf-8")
        return {"files": ["src/app.py"]}

    res = eng.run_project(_project(_CONSTITUTION),
                          workspace=str(tmp_path / "wk"), depth="execute",
                          agents={"implementer": implementer})
    assert "product_entry" not in _children_ids(res.project["tree"], "L0")


def test_assembly_node_carries_no_amend_marker():
    # The assembly leaf owns a FIXED engine target (src/app.py); it must be
    # flagged exempt so late-injection routing never re-points it.
    import os as _os
    _os.environ["SPEC_FLOW_PRE_GATE"] = "1"
    try:
        e = eng.Engine.__new__(eng.Engine)
        e._constitution = _CONSTITUTION
        e.workspace = type("W", (), {"root": "/tmp"})()
        node = e._assembly_node()
        assert node and node.get("_no_amend") is True
    finally:
        _os.environ.pop("SPEC_FLOW_PRE_GATE", None)


def test_assembly_entry_is_never_amend_routed(tmp_path, monkeypatch):
    # Even with the late-injection matcher armed and FORCED to return a target
    # for any node, the assembly entry must NOT be re-routed — its code stays
    # src/app.py. Guards the live v020 miss (product_entry mis-AMENDed into
    # src/database_layer.py; harmless only because the spec still forced app.py).
    monkeypatch.setenv("SPEC_FLOW_PRE_GATE", "1")
    monkeypatch.setenv("SPEC_FLOW_REQ_AMEND", "1")
    # any node reaching the matcher would be routed into src/db.py
    monkeypatch.setattr(eng.Engine, "_amend_target",
                        lambda self, node: "src/db.py")
    built = {}

    def implementer(ctx):
        root = pathlib.Path(ctx["workspace"].root)
        (root / "src").mkdir(parents=True, exist_ok=True)
        nid = ctx["node"]
        mod = ctx.get("module") or nid
        built[nid] = {"module": mod, "code_target": ctx.get("code_target")}
        (root / "src" / f"{mod}.py").write_text("# stub\n", encoding="utf-8")
        return {"files": [f"src/{mod}.py"]}

    res = eng.run_project(_project(_CONSTITUTION),
                          workspace=str(tmp_path / "wk"), depth="execute",
                          agents={"implementer": implementer})
    # the assembly leaf ran but was never handed a foreign code_target
    assert "product_entry" in built
    assert built["product_entry"]["code_target"] in (None, "", "src/app.py")
    # and no AMEND milestone was emitted for it
    assert not [e for e in res.events
                if e.gate == "requirement" and e.verdict == "AMEND"
                and e.task == "product_entry"]
