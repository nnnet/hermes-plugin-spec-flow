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


def test_absent_datum_blocks_are_omitted(tmp_path):
    # a node with only routes must NOT emit empty symbol/env/dep/effect blocks
    thin = {"format": "spec-flow ir v1", "product": {"entry": "src/app.py"},
            "nodes": {"core": {"openapi": {"paths": {"/x": {"get": {
                "x-spec-flow-handler": "h",
                "responses": {"200": {"content": {}}}}}}}}}}
    h = dash._ir_report_html(_run_dir(tmp_path, thin))
    assert "exposes" not in h and "consumes" not in h
    assert "dependencies" not in h and "effects" not in h


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


# ── _build_state wiring ───────────────────────────────────────────────────────

def test_build_state_wires_reports_ir(tmp_path):
    rd = _run_dir(tmp_path, _built_ir(tmp_path))
    st = dash._build_state(rd)
    assert "ir" in st["reports"], "reports.ir not wired into _build_state"
    assert "<table>" in st["reports"]["ir"], "reports.ir lost the routes table"
