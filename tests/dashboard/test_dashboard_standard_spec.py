"""The dashboard SHOWS the STANDARDIZED machine spec, not prose (node N5, S33).

Why (user 2026-07-06, single criterion): a spec is complete for a weak model
ONLY as machine data in a STANDARD — OpenAPI 3.1 operations for an HTTP node,
Gherkin Given/When/Then scenarios (+ typed `symbols`) for a non-HTTP one. The
dashboard must therefore SHOW that machine completeness in the standard, with a
per-standard validation badge run by the SAME oracles the engine uses
(`spec_openapi.validate_openapi_library`, `spec_ir.gherkin_errors`,
`spec_ir.jsonschema_errors`); the `.md` prose is a DERIVED, secondary view. Two
holes existed: (1) the IR-tab placeholder LIED — it described the OLD behaviour
("ir.json is written only after the tree is realized"), while I1/I2/I3 made
ir.json a LIVE incremental artifact written under `_ir_write_lock` after EVERY
processed leaf (`_write_ir_incremental` in `_visit`); (2) a node's "Спека" panel
rendered ONLY prose — the machine standard (routes table / Gherkin scenarios +
symbols) and its standard badge lived only in the separate IR tab, so the
per-node spec view never showed the machine completeness.

What: exercises `_PAGE` (the IR-tab hint text), `_node_standard_badge_html`
(the per-standard validation badge), `_node_standard_spec_html` (the machine
spec block) and `_build_state` (the `spec_standard` node datum) over a REAL
engine-built HTTP node and a hand-built non-HTTP node carrying Gherkin
scenarios. RED before N5: the placeholder still says "after the tree is
realized"; a non-HTTP node's standard block shows no Given/When/Then and no
Gherkin badge.
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

# A NON-HTTP node: no openapi/paths, but a closed Gherkin scenario + symbols —
# the machine carrier a weak model needs for a pure-capability leaf.
_NONHTTP_NODE = {
    "openapi": {"openapi": "3.1.0", "info": {"title": "reverse", "version": "1"},
                "paths": {}},
    "symbols": {"exposes": ["reverse_text(s: str) -> str"]},
    "scenarios": [
        {"requirement": "reverse a string",
         "given": {"env": {}, "state": []},
         "when": {"method": "CALL", "path": "reverse_text"},
         "then": {"status": "ok", "body_check": {"equals": "cba"}}}],
}


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


# ── (1) the IR-tab placeholder must be HONEST about the LIVE increment ────────

def test_ir_tab_hint_does_not_lie_about_end_of_run_write():
    """S33.1 the IR-tab hint must NOT describe the OLD end-of-run behaviour.
    RED on current code: the placeholder says the artifact is written after the
    tree is realized — a stale description of a superseded flow (pre-I1/I2/I3).
    """
    page = dash._PAGE
    lower = page.lower()
    # the exact stale claims (RU + EN) that lied about end-of-run writing
    assert "после того как дерево реализовано" not in page, (
        "IR-tab placeholder still claims ir.json is written after the tree is "
        "realized — a stale lie; it is a LIVE per-leaf increment")
    assert "after the tree is realized" not in lower, (
        "IR-tab comment still claims end-of-run write")
    assert "после реализации дерева" not in page, (
        "IR-tab hint still frames ir.json as a post-realization contract")


def test_ir_tab_hint_states_the_live_increment():
    """S33.1 GREEN direction: the hint tells the truth — a live increment under
    a lock, written per processed leaf, empty until the first leaf closes."""
    page = dash._PAGE
    # truthful markers: incremental / per-leaf / lock
    assert ("инкремент" in page or "по ходу" in page), (
        "IR-tab hint does not describe the live incremental write")
    assert ("лок" in page.lower() or "_ir_write_lock" in page), (
        "IR-tab hint does not mention the write lock")


# ── (2) a NON-HTTP node's panel shows the Gherkin STANDARD, not only prose ────

def test_nonhttp_node_standard_block_shows_gherkin_scenarios():
    """S33.2 a non-HTTP node's standard spec block shows Given/When/Then from
    the machine carrier plus the exposed symbol signature — the machine spec,
    not text. RED: `_node_standard_spec_html` does not exist yet."""
    html = dash._node_standard_spec_html("reverse", dict(_NONHTTP_NODE))
    assert "When" in html and "Then" in html, (
        "non-HTTP node standard block shows no Gherkin When/Then")
    assert "reverse_text" in html, (
        "non-HTTP node standard block omits the exposed symbol signature")


def test_nonhttp_node_badge_is_gherkin_not_openapi():
    """S33.3 the standard badge of a non-HTTP node is a Gherkin badge (with an
    error count) and marks the absence of an HTTP interface as legitimate — it
    must NOT pretend an OpenAPI interface exists. RED: no badge helper yet."""
    badge = dash._node_standard_badge_html("reverse", dict(_NONHTTP_NODE))
    assert "Gherkin" in badge, "non-HTTP node lacks a Gherkin standard badge"
    assert ("no HTTP interface" in badge or "нет HTTP" in badge), (
        "non-HTTP node's badge does not mark the missing HTTP interface as ok")


def test_http_node_badge_shows_openapi(tmp_path):
    """S33.3 GREEN direction: an HTTP node's badge names the OpenAPI 3.1
    standard with a zero-error validation over the real engine-built IR."""
    ir = _built_ir(tmp_path)
    core = ir["nodes"]["core"]
    badge = dash._node_standard_badge_html("core", core)
    assert "OpenAPI 3.1" in badge, "HTTP node badge does not name OpenAPI 3.1"


def test_build_state_carries_standard_spec_on_node(tmp_path):
    """S33.2 a node's run-state carries a `spec_standard` datum (the machine
    spec block the panel renders BEFORE the derived prose). RED: `_build_state`
    does not populate it yet."""
    ir = _built_ir(tmp_path)
    rd = _run_dir(tmp_path, ir)
    (rd / "tree.json").write_text(
        json.dumps({"id": "core", "verdict": "leaf", "children": []}),
        encoding="utf-8")
    st = dash._build_state(rd)
    core = st["nodes"].get("core")
    assert core is not None, "core node missing from state"
    assert "spec_standard" in core, (
        "node state lacks the spec_standard machine-spec datum")
    assert core["spec_standard"], "spec_standard is empty for an HTTP node"


def test_client_page_reads_spec_standard_into_spec_panel():
    """S33.2 the client page renders spec_standard into the spec panel BEFORE
    the derived prose — the machine standard reaches the UI, prose is secondary.
    """
    page = dash._PAGE
    assert "spec_standard" in page, (
        "client page never reads spec_standard into the spec panel")
    assert ("производн" in page), (
        "spec panel does not mark the prose as derived/secondary")


def test_no_existing_tab_removed():
    """S33.4 the standard block/badge only ADD — every pre-existing tab (the
    global tabs and the node tabs) must still be present in the page."""
    page = dash._PAGE
    for tab in ("'graph'", "'flow'", "'timeline'", "'agents'", "'hitl'",
                "'idle'", "'compare'", "'report'", "'ir'", "'inputs'",
                "'summary'", "'oracle'"):
        assert tab in page, "global tab %s vanished from the page" % tab
    for ntab in ("'spec'", "'versions'", "'code'", "'test'", "'contract'"):
        assert ntab in page, "node tab %s vanished from the page" % ntab
