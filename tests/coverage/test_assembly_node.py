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


def test_routes_without_named_entry_injects_nothing(tmp_path, monkeypatch):
    # STRICT anti-guess: the human declared HTTP routes but never named the
    # entry FILE or its callable. The engine must NOT fall back to a hardcoded
    # src/app.py / wsgi_app convention — an incomplete contract yields {} and no
    # assembly node (guessing the entry would be a hardcoded solution assumption).
    monkeypatch.setenv("SPEC_FLOW_PRE_GATE", "1")
    routes_only = [
        "Standard library ONLY for an HTTP service.",
        "POST /notes takes {text} -> {id}; GET /notes -> {items}; GET /health 200.",
    ]
    res = _run(tmp_path, routes_only)
    assert "product_entry" not in _children_ids(res.project["tree"], "L0")


def test_callable_unnamed_injects_nothing(tmp_path, monkeypatch):
    # routes + a file named, but no "exposes <callable>" -> still no assembly
    # (the engine will not guess the callable name).
    monkeypatch.setenv("SPEC_FLOW_PRE_GATE", "1")
    file_only = [
        "HTTP service; the entry lives in src/app.py, storage in src/db.py.",
        "POST /notes -> {id}; GET /notes -> {items}; GET /health 200.",
    ]
    res = _run(tmp_path, file_only)
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


def test_assembly_node_pins_app_target_and_lists_built_api(tmp_path):
    # Two binding properties of the assembly leaf:
    #  1. code_target == src/app.py — the leaf pipeline derives the output file
    #     from the node id unless code_target overrides it; without the pin the
    #     engine tracked src/product_entry.py while the boot-gate wanted app.py
    #     (the v024 'wrote a test, never built the entry' miss).
    #  2. the spec carries the REAL public API of the already-built src modules
    #     (AST-extracted) so a weak model can import-and-route mechanically.
    import os as _os
    _os.environ["SPEC_FLOW_PRE_GATE"] = "1"
    try:
        ws = tmp_path / "wk"
        (ws / "src").mkdir(parents=True)
        (ws / "src" / "notes_database.py").write_text(
            "def store(text):\n    return 1\n"
            "def all_notes():\n    return []\n"
            "def _private():\n    return 0\n", encoding="utf-8")
        e = eng.Engine.__new__(eng.Engine)
        e._constitution = _CONSTITUTION
        e.workspace = type("W", (), {"root": str(ws)})()
        node = e._assembly_node()
        assert node and node["code_target"] == "src/app.py"
        spec = node["spec_markdown"]
        assert "src/notes_database.py" in spec
        assert "def store(text)" in spec and "def all_notes()" in spec
        assert "_private" not in spec      # private symbols are excluded
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


# ── ROOT boot-gate: the final corpus verdict is RED when the assembled product
#    does not serve its frozen contract, even if every module unit-test is green
_GOOD_APP = '''
import json
_N = []
def wsgi_app(environ, start_response):
    m = environ["REQUEST_METHOD"]; p = environ["PATH_INFO"]
    def reply(code, body, ct="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        start_response(str(code) + " OK", [("Content-Type", ct)]); return [data]
    if p == "/health": return reply(200, {"ok": True})
    if p == "/ui": return reply(200, b"<html>notes</html>", "text/html")
    if p == "/notes" and m == "POST":
        n = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
        _N.append({"id": len(_N)+1, "text": json.loads(n)["text"]}); return reply(201, _N[-1])
    if p == "/notes": return reply(200, {"items": list(reversed(_N))})
    return reply(404, {"error": "no route"})
'''

# only serves "/" — the live v020 shape that 404'd its whole contract
_BAD_APP = '''
def wsgi_app(environ, start_response):
    if environ["PATH_INFO"] == "/":
        start_response("200 OK", [("Content-Type", "text/html")]); return [b"<html>hi</html>"]
    start_response("404 Not Found", [("Content-Type", "text/plain")]); return [b"Not Found"]
'''

_WSGI_CONST = ["HTTP through a WSGI app (src/app.py exposes `wsgi_app`).",
               "POST /notes -> {id}; GET /notes -> {items}; GET /health 200."]


def _engine_with_ws(tmp_path, app_src, constitution, goal=None):
    ws_root = tmp_path / "wk"
    (ws_root / "src").mkdir(parents=True)
    (ws_root / "src" / "app.py").write_text(app_src, encoding="utf-8")
    e = eng.Engine.__new__(eng.Engine)
    e.workspace = type("W", (), {"enabled": True, "root": str(ws_root)})()
    e._constitution = list(constitution)
    # the contract is DERIVED from the human description (constitution + goal);
    # default to a web goal, but the skip case passes a non-web one
    e._goal = ("notes service POST /notes GET /notes" if goal is None else goal)
    return e


def test_root_boot_gate_red_when_product_404s_its_contract(tmp_path):
    e = _engine_with_ws(tmp_path, _BAD_APP, _WSGI_CONST)
    ok, detail = e._assembled_product_boots()
    assert not ok and "/health" in detail


def test_root_boot_gate_green_on_serving_product(tmp_path):
    e = _engine_with_ws(tmp_path, _GOOD_APP, _WSGI_CONST)
    ok, detail = e._assembled_product_boots()
    assert ok, detail


def test_root_boot_gate_skipped_without_wsgi_entry(tmp_path):
    # a product whose HUMAN description names no HTTP route is not boot-gated:
    # the contract is DERIVED from text, so neither constitution nor goal may
    # mention a route (library/CLI/pipeline) → boot-gate skips.
    e = _engine_with_ws(tmp_path, _BAD_APP, ["Plain library, no web entry."],
                        goal="a reusable parsing library, no HTTP service")
    ok, _ = e._assembled_product_boots()
    assert ok


def test_force_rebuilds_when_entry_present_but_broken(tmp_path):
    # REACTIVE heal: the entry exists but is RED (404s its contract). The normal
    # (proactive) path skips an existing entry; force=True must still produce the
    # rebuild spec so the doctor's reconcile_check can re-assemble it.
    import os as _os
    _os.environ["SPEC_FLOW_PRE_GATE"] = "1"
    try:
        ws = tmp_path / "wk"
        (ws / "src").mkdir(parents=True)
        (ws / "src" / "app.py").write_text(  # entry PRESENT but incomplete
            "def wsgi_app(environ, start_response):\n"
            "    start_response('404 Not Found', [])\n    return [b'']\n",
            encoding="utf-8")
        e = eng.Engine.__new__(eng.Engine)
        e._constitution = _CONSTITUTION
        e.workspace = type("W", (), {"root": str(ws)})()
        assert e._assembly_node() is None            # proactive: entry exists
        node = e._assembly_node(force=True)           # reactive heal
        assert node and node["code_target"] == "src/app.py"
    finally:
        _os.environ.pop("SPEC_FLOW_PRE_GATE", None)


def test_late_requirements_ride_into_assembly_spec(tmp_path):
    # the injected human requirements (e.g. /ping, /ui) are NOT in the
    # constitution but must be routed by the entry — they must appear in the
    # rebuild spec so the assembler wires them (the v054 RED: /ping unrouted).
    import os as _os
    _os.environ["SPEC_FLOW_PRE_GATE"] = "1"
    try:
        e = eng.Engine.__new__(eng.Engine)
        e._constitution = _CONSTITUTION
        e._standing_requirements = [
            ("ping", "Serve GET /ping returning the plain text pong.")]
        e.workspace = type("W", (), {"root": str(tmp_path)})()
        node = e._assembly_node(force=True)
        assert node and "GET /ping" in node["spec_markdown"]
        assert "Late human requirements" in node["spec_markdown"]
    finally:
        _os.environ.pop("SPEC_FLOW_PRE_GATE", None)
