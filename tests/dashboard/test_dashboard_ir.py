"""The dashboard's IR tab renders workspace/ir.json read-only.

Why: ir.json is the engine's source of truth for the product's contracted
surface (Phase I). The dashboard must surface it honestly — routes with an
origin badge (⚙ engine convention vs 📜 spec datum), the symbol/env/dep/effect
blocks each omitted when absent, and — critically — a truthful placeholder when
the artifact has not been written yet (never a blank or a stack trace).

What: _ir_report_html + helpers over a REAL ir.json (built by the engine,
mirroring tests/audit/test_ir_scenarios_schema.py) and a hand-built rich
fixture, plus the absent / malformed / non-object edge cases and _build_state
wiring.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import live_dashboard as dash  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402
import spec_ir  # noqa: E402

_GOAL = (
    'A tiny notes service. Two HTTP endpoints over a WSGI app: POST /notes '
    'accepts {"text": "..."} and stores it, returning {"id": <int>}; '
    'GET /notes returns {"items": [...]} newest-first.')
_CONTRACT = {
    "entry": "src/app.py", "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [], "media": {"/notes": "json", "/health": "json"}}
_NODE = {"id": "core", "title": "Product core",
         "requirement": "own POST /notes and GET /notes and GET /health"}


def _built_ir(tmp_path):
    """A REAL ir.json from the engine (no LLM), like the schema audit test."""
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    eng._goal = _GOAL
    eng._constitution = [
        "Standard library ONLY: HTTP through a WSGI app (src/app.py exposes "
        "`wsgi_app`)."]
    assert eng._leaf_owned_routes(dict(_NODE))
    return spec_ir.build_ir(eng)


def _run_dir(tmp_path, ir):
    """Write `ir` into a run-dir's workspace/ir.json and return the run-dir."""
    ws = tmp_path / "run" / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "ir.json").write_text(json.dumps(ir, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    return tmp_path / "run"


_RICH = {
    "format": "spec-flow ir v1",
    "product": {"kind": "http-wsgi", "entry": "src/app.py",
                "callable": ["wsgi_app"]},
    "nodes": {"core": {
        "openapi": {"openapi": "3.1.0", "paths": {"/list": {"get": {
            "x-spec-flow-handler": "list_items",
            "x-spec-flow-status-source": "convention",
            "responses": {"200": {"content": {"text/html": {"schema": {}}}}}}}},
            "x-spec-flow-gaps": [
                "GET /list response 200 body shape not recorded"]},
        "symbols": {"exposes": [{"name": "list_items", "args": ["req"]}],
                    "consumes": [{"from": "store", "name": "all_notes",
                                  "args": []}]},
        "env": [{"name": "MAX_ITEMS", "rule": "int, default 100"}],
        "files": ["src/core.py"],
        "children": ["store", "web_ui"],
        "scenarios": [
            {"requirement": "core",
             "given": {"env": {"MAX_ITEMS": "5"},
                       "state": [{"method": "POST", "path": "/list"}]},
             "when": {"method": "GET", "path": "/list"},
             "then": {"status": 200, "media": "text/html",
                      "body_check": {"contains": "<ul"}}}],
        "dependencies": ["httpx"],
        "effects": ["network", "fs-write"]}}}


# ── real built ir.json ────────────────────────────────────────────────────────

def test_built_ir_renders_product_and_routes_table(tmp_path):
    rd = _run_dir(tmp_path, _built_ir(tmp_path))
    h = dash._ir_report_html(rd)
    assert "<table>" in h, "routes table missing"
    assert "/notes" in h and "/health" in h, "owned routes not listed"
    assert "src/app.py" in h, "product entry not in header"
    assert "irdump" in h, "raw ir.json dump missing"


def test_built_ir_marks_engine_convention_badge(tmp_path):
    rd = _run_dir(tmp_path, _built_ir(tmp_path))
    h = dash._ir_report_html(rd)
    # engine picks the success status by REST convention -> ⚙ badge
    assert "⚙" in h, "convention (⚙) badge missing on engine status"


# ── rich hand-built fixture: gaps + all datum blocks ──────────────────────────

def test_rich_ir_shows_gaps_and_all_datum_blocks(tmp_path):
    rd = _run_dir(tmp_path, _RICH)
    h = dash._ir_report_html(rd)
    assert "x-spec-flow-gaps" in h and "body shape not recorded" in h
    assert "list_items" in h, "exposes symbol missing"
    assert "all_notes" in h, "consumes symbol missing"
    assert "MAX_ITEMS" in h, "env entry missing"
    assert "httpx" in h, "dependency missing"
    assert "network" in h and "fs-write" in h, "effects missing"


def _structural(html):
    """The rendered IR minus the folded raw-json dump — so an assertion proves
    a STRUCTURAL render, never a hit inside the raw dump.

    Why: every datum also appears in the <details>сырой ir.json dump; testing
    against that would green a missing structural block.
    What: returns the HTML up to the raw-dump <details> marker.
    Test: the returned text lacks the 'irdump' pre and keeps the node blocks.
    """
    cut = html.find("<details><summary>сырой ir.json")
    return html if cut < 0 else html[:cut]


def test_rich_ir_shows_scenarios_given_when_then(tmp_path):
    # a node's Given/When/Then scenarios are the executable meaning of the
    # contracted surface — the IR tab must render them structurally, not hide
    # them in the raw dump only (M1: full IR accumulator on the dashboard).
    h = _structural(dash._ir_report_html(_run_dir(tmp_path, _RICH)))
    assert "Given" in h, "scenario Given clause not rendered"
    assert "When" in h, "scenario When clause not rendered"
    assert "Then" in h, "scenario Then clause not rendered"
    # the concrete GWT content of the fixture scenario must be visible
    assert "MAX_ITEMS" in h  # given.env
    assert "GET" in h and "/list" in h  # when
    assert "200" in h  # then.status


def test_rich_ir_shows_files_and_children(tmp_path):
    # files (owned modules) and children (sub-nodes) are IR datums the
    # accumulator carries per node — the tab must surface both structurally.
    h = _structural(dash._ir_report_html(_run_dir(tmp_path, _RICH)))
    assert "src/core.py" in h, "node files not rendered"
    assert "store" in h and "web_ui" in h, "node children not rendered"
    assert "файлы" in h, "files block has no label"
    assert "children" in h or "дочерние" in h, "children block has no label"


def test_ir_scenario_given_state_prior_steps(tmp_path):
    # given.state carries prior when-steps (e.g. a POST before a GET); the
    # scenario block must show them so a reader sees the precondition chain.
    h = _structural(dash._ir_report_html(_run_dir(tmp_path, _RICH)))
    assert "POST" in h, "given.state prior step (POST) not rendered"


def test_absent_datum_blocks_are_omitted(tmp_path):
    # a node with only routes must NOT emit empty symbol/env/dep/effect/
    # scenario/files/children blocks (the IR's own "missing datum => absent"
    # law mirrored on the dashboard)
    thin = {"format": "spec-flow ir v1", "product": {"entry": "src/app.py"},
            "nodes": {"core": {"openapi": {"paths": {"/x": {"get": {
                "x-spec-flow-handler": "h",
                "responses": {"200": {"content": {}}}}}}}}}}
    h = dash._ir_report_html(_run_dir(tmp_path, thin))
    assert "exposes" not in h and "consumes" not in h
    assert "dependencies" not in h and "effects" not in h
    assert "сценарии" not in h and "children" not in h and "файлы" not in h


# ── honest handling of missing / broken artifacts ────────────────────────────

def test_absent_ir_returns_empty(tmp_path):
    (tmp_path / "run" / "workspace").mkdir(parents=True)
    assert dash._ir_report_html(tmp_path / "run") == "", (
        "absent ir.json must yield '' so the client shows its placeholder")


def test_malformed_ir_reports_readable_note(tmp_path):
    ws = tmp_path / "run" / "workspace"
    ws.mkdir(parents=True)
    (ws / "ir.json").write_text("{ not json ", encoding="utf-8")
    h = dash._ir_report_html(tmp_path / "run")
    assert "не читается" in h
    assert "Traceback" not in h


def test_non_object_ir_reports_note(tmp_path):
    ws = tmp_path / "run" / "workspace"
    ws.mkdir(parents=True)
    (ws / "ir.json").write_text("[1, 2, 3]", encoding="utf-8")
    h = dash._ir_report_html(tmp_path / "run")
    assert "не является" in h


# ── OpenAPI-first: the machine document is primary, prose is derived ──────────
# The new spec format (K3/S23) makes the per-node OpenAPI 3.1 document the
# primary interface carrier; specs/*.md is COMPILED FROM it (section
# "## Interface (compiled from the machine OpenAPI)"). The dashboard must say
# so with one glance: the node's spec panel labels the OpenAPI document
# "primary: machine OpenAPI 3.1" and the prose "derived / compiled from OpenAPI".

def test_spec_provenance_marks_openapi_primary_prose_derived():
    # a node whose IR carries an OpenAPI document with paths -> the panel
    # marks the machine document primary and the prose derived
    node = _RICH["nodes"]["core"]
    h = dash._node_spec_provenance_html(node)
    assert "primary" in h.lower(), "OpenAPI document not marked primary"
    assert "OpenAPI 3.1" in h, "machine OpenAPI 3.1 label missing"
    assert "derived" in h.lower(), "prose not marked derived"
    assert "OpenAPI" in h, "'compiled from OpenAPI' provenance missing"


def test_spec_provenance_absent_without_openapi():
    # a node with no OpenAPI document (a non-service leaf) carries no interface
    # document, so no primary/derived badge is claimed
    assert dash._node_spec_provenance_html({"files": ["src/l0.py"]}) == ""


def test_node_state_carries_spec_provenance(tmp_path):
    # the per-node state must expose the provenance badge so the client spec
    # tab can show it above the (derived) markdown prose
    rd = _run_dir(tmp_path, _RICH)
    # place a matching tree so _build_state has a 'core' node
    (rd / "tree.json").write_text(
        json.dumps({"id": "core", "children": []}), encoding="utf-8")
    st = dash._build_state(rd)
    core = st["nodes"].get("core")
    assert core is not None, "core node missing from state"
    assert "spec_primary" in core, "node state lacks spec_primary provenance"
    assert "OpenAPI" in core["spec_primary"], "provenance badge lost OpenAPI"


def test_client_spec_tab_shows_provenance_marker():
    # the client renderNodeBody spec branch must inject the provenance badge
    # (nd.spec_primary) before the derived markdown
    assert "spec_primary" in dash._PAGE, (
        "client page never reads spec_primary; prose is not marked derived")


# ── _build_state wiring ───────────────────────────────────────────────────────

def test_build_state_wires_reports_ir(tmp_path):
    rd = _run_dir(tmp_path, _built_ir(tmp_path))
    st = dash._build_state(rd)
    assert "ir" in st["reports"], "reports.ir not wired into _build_state"
    assert "<table>" in st["reports"]["ir"], "reports.ir lost the routes table"
