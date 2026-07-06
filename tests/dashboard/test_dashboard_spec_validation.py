"""The dashboard SURFACES a spec's validation status (node M3, S29).

Why: the origin/provenance badges (M1) say WHERE a datum came from, but a
reader looking at the spec had no way to see whether it is VALIDATED — the
green fact was invisible, and only a failure could ever show (as an 'error'
badge). A spec that passes the oracles must be observably validated: (a) when
viewing the spec itself (per-node badge + IR-tab summary), and (b) in other
places (the node tree gets a 'validated' badge, the run summary states the
counts). This audit reds on the current code (no 'validated' badge anywhere)
and greens once the dashboard re-runs the same oracles the engine uses and
renders their verdict.

What: exercises `_ir_report_html`, `_ir_node_html`, `_node_spec_validation`,
`_spec_validation_summary_html`, `_node_spec_validation_html` and `_build_state`
over a REAL engine-built ir.json (valid) and a hand-built invalid node.
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
    """A REAL, VALID ir.json from the engine (no LLM)."""
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    eng._goal = _GOAL
    eng._constitution = [
        "Standard library ONLY: HTTP through a WSGI app (src/app.py exposes "
        "`wsgi_app`)."]
    assert eng._leaf_owned_routes(dict(_NODE))
    return spec_ir.build_ir(eng)


def _run_dir(tmp_path, ir):
    ws = tmp_path / "run" / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "ir.json").write_text(json.dumps(ir, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    return tmp_path / "run"


# ── (a) viewing the spec itself: per-node badge + tab summary ─────────────────

def test_valid_node_shows_validated_badge(tmp_path):
    """S29.1 GREEN: a node whose spec passes the oracles renders a 'validated'
    check — the reader sees the spec IS valid, not merely present."""
    ir = _built_ir(tmp_path)
    core = ir["nodes"]["core"]
    rep = dash._node_spec_validation("core", core)
    assert rep["ok"], "engine-built node should validate: %r" % rep["errors"]
    h = dash._ir_node_html("core", core)
    assert "validated" in h, "per-node spec view carries no 'validated' badge"
    assert "✓" in h, "validated badge lacks its check glyph"


def test_invalid_node_shows_error_count_not_a_false_check(tmp_path):
    """S29.2 RED-direction: a node with a closed-world violation (an env key
    typo -> unknown key) must NOT show a green check; it states the count so
    the reviewer has the reason. A gate audited only for the happy path could
    green a broken spec — this pins the honest '✗ N errors' path."""
    bad = {"openapi": {"openapi": "3.1.0", "info": {"title": "x",
                                                    "version": "1"},
                       "paths": {}},
           # an unknown key inside the node — the closed world refuses it
           "not_a_real_key": True,
           "files": ["src/core.py"]}
    rep = dash._node_spec_validation("core", bad)
    assert not rep["ok"] and rep["errors"], (
        "an unknown-key node must fail the closed-world oracle")
    h = dash._node_spec_validation_html("core", bad)
    assert "errors" in h and "✓ validated" not in h, (
        "a broken spec must never render a green validated check")


def test_tab_summary_states_the_three_oracle_counts(tmp_path):
    """S29.3 the IR tab head carries a run-level roll-up naming all three
    oracles (closed-world, openapi-lib, jsonschema) — 'validated' is stated
    for the WHOLE spec, not only per node."""
    rd = _run_dir(tmp_path, _built_ir(tmp_path))
    h = dash._ir_report_html(rd)
    assert "Spec validation:" in h, "IR tab head lacks the validation summary"
    for oracle in ("closed-world", "openapi-lib", "jsonschema"):
        assert oracle in h, "summary omits the %s counter" % oracle


# ── (b) in other places: the node tree + run state ────────────────────────────

def test_build_state_carries_validated_badge_on_node(tmp_path):
    """S29.4 a node's run-state carries the per-node spec_validated badge and,
    when the spec is valid, a 'validated' tree episode — success is observable
    on the tree/graph, not only failure."""
    rd = _run_dir(tmp_path, _built_ir(tmp_path))
    (rd / "tree.json").write_text(
        json.dumps({"id": "core", "verdict": "leaf", "children": []}),
        encoding="utf-8")
    st = dash._build_state(rd)
    core = st["nodes"].get("core")
    assert core is not None, "core node missing from state"
    assert "spec_validated" in core, "node state lacks spec_validated badge"
    assert "validated" in core["spec_validated"], (
        "spec_validated badge lost its 'validated' text")
    assert "validated" in core["episodes"], (
        "a valid-spec node carries no 'validated' tree episode")


def test_client_page_renders_spec_validated_and_knows_badge(tmp_path):
    """S29.5 the client page reads spec_validated into the spec panel and the
    tree badge map knows the 'validated' glyph — the wiring reaches the UI."""
    assert "spec_validated" in dash._PAGE, (
        "client page never reads spec_validated into the spec panel")
    assert "validated" in dash._EPISODE_BADGE, (
        "server badge map lacks the 'validated' episode glyph")
