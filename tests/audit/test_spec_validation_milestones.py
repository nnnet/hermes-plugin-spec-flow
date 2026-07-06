"""The engine makes spec-validation SUCCESS observable in its milestones
(node M3, S29).

Why: two spec oracles ran but could not report success. (1) `decomposer_ir`'s
OpenAPI seam emitted a `decomposer_openapi` milestone ONLY on FAIL, so 'this
node's document passed the OpenAPI 3.1 library' was never journalled — the
dashboard could only ever surface a failure, never the green fact. (2) the
jsonschema oracle (H7/`spec_ir.jsonschema_errors`) had ZERO live callers: it
ran only from a test, never on the write path, so its independent structural
witness was dead in production. M3 makes both observable: a PASS milestone on
the OpenAPI gate, and a live jsonschema milestone at IR write.

What: over a REAL engine (harness `run_engine`, no LLM), asserts the
`decomposer_openapi` PASS milestone on an accepted library-valid node, and the
`ir_jsonschema` milestone on `_write_ir`. Does not weaken the existing FAIL
paths (the S22 audit still owns those).
"""
import pathlib
import sys

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from harness import run_engine as eng  # noqa: E402
import spec_ir  # noqa: E402
import spec_openapi  # noqa: E402


def _engine(tmp_path, routes=None):
    e = eng.Engine(workspace=str(tmp_path / "wk"), depth=eng.DEPTH_SPEC,
                   interface_policy="ir-required")
    e._product_contract = lambda: {
        "entry": "src/app.py",
        "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
        "routes": routes if routes is not None else [("GET", "/notes")],
    }
    return e


def _valid_openapi(nid, method, path, status="200",
                   media="application/json"):
    # an error response (reusing the operation media) specifies the failure
    # surface — the N6 errors_edges completeness aspect the standardized_spec
    # gate now enforces on every http node.
    op = {"responses": {
        str(status): {"description": "success",
                      "content": {media: {"schema": {}}}},
        "404": {"description": "error", "content": {media: {"schema": {}}}}}}
    return {"openapi": spec_ir.OPENAPI_VERSION,
            "info": {"title": "node %s interface" % nid, "version": "1"},
            "paths": {path: {method.lower(): op}}}


def _http_scenario(method, path, status):
    """One closed behaviour scenario — the N6 behavior aspect for an http node."""
    return {"requirement": "%s %s" % (method, path),
            "when": {"method": method, "path": path},
            "then": {"status": int(str(status))}}


def _machine_part(nodes):
    return {"format": spec_ir.IR_FORMAT, "nodes": nodes}


def _gate_events(e, gate, verdict=None):
    return [ev for ev in e.events
            if ev.gate == gate and (verdict is None or ev.verdict == verdict)]


# -- (a) decomposer_openapi emits a PASS, not only a FAIL ----------------------

def test_valid_node_openapi_emits_a_decomposer_openapi_PASS(tmp_path):
    """S29.6 GREEN: a node whose OpenAPI document clears the maintained
    library emits a NAMED PASS on the `decomposer_openapi` gate — 'passed the
    OpenAPI 3.1 library' is now as observable as a failure.

    RED on the pre-M3 engine: the seam only emitted `decomposer_openapi` FAIL,
    so no PASS milestone existed for the dashboard to surface."""
    if not spec_openapi.openapi_library_available():
        import pytest
        pytest.skip("openapi-spec-validator oracle not installed")
    e = _engine(tmp_path, routes=[("GET", "/notes")])
    out = {"atomic": True, "ir": _machine_part({"core": {
        "files": ["src/core.py"], "effects": [],
        "scenarios": [_http_scenario("GET", "/notes", "200")],
        "openapi": _valid_openapi("core", "GET", "/notes", "200")}})}
    assert e._accept_decomposer_ir({"id": "core"}, out) == [], (
        "a library-valid node must be accepted")
    passes = _gate_events(e, "decomposer_openapi", verdict="PASS")
    assert passes, (
        "a node document that cleared the OpenAPI 3.1 library must emit a "
        "decomposer_openapi PASS milestone — success must be observable")
    # and the FAIL path is untouched: no false failure on a clean document
    assert not _gate_events(e, "decomposer_openapi", verdict="FAIL")


# -- (b) the jsonschema oracle runs on the LIVE write path ---------------------

def test_write_ir_emits_a_live_jsonschema_milestone(tmp_path, monkeypatch):
    """S29.7 the H7 jsonschema oracle is exercised on the WRITE path and its
    verdict is journalled as an `ir_jsonschema` milestone — the formerly dead
    oracle (zero live callers) now really runs in production.

    RED on the pre-M3 engine: `_write_ir` never called `jsonschema_errors`, so
    no `ir_jsonschema` milestone was ever emitted."""
    valid_ir = {"format": spec_ir.IR_FORMAT, "product": {},
                "nodes": {"core": {
                    "files": ["src/core.py"],
                    "openapi": _valid_openapi("core", "GET", "/notes")}}}
    e = _engine(tmp_path)
    e._root_id = "L0"
    # feed the write path a ready, valid accumulator so the milestone is about
    # the ORACLE running, not about assembling engine state.
    monkeypatch.setattr(spec_ir, "collect_ir_sources", lambda _e: {})
    monkeypatch.setattr(spec_ir, "build_ir", lambda _e, _s=None: valid_ir)
    monkeypatch.setattr(e, "_atomic_write", lambda *a, **k: None)
    monkeypatch.setattr(e, "_refresh_ir_skeletons", lambda *a, **k: None)
    e._write_ir("test")
    js = _gate_events(e, "ir_jsonschema")
    assert js, "the write path emits no ir_jsonschema milestone"
    assert js[-1].verdict == "PASS", (
        "a jsonschema-clean IR must journal an ir_jsonschema PASS: %r"
        % js[-1].verdict)
    # the oracle is the real one: a structurally broken IR reds the milestone
    e2 = _engine(tmp_path)
    e2._root_id = "L0"
    broken = {"format": spec_ir.IR_FORMAT, "product": {},
              "nodes": {"core": {"files": ["src/core.py"],
                                 "bogus_unknown_key": 1}}}
    monkeypatch.setattr(spec_ir, "build_ir", lambda _e, _s=None: broken)
    monkeypatch.setattr(e2, "_atomic_write", lambda *a, **k: None)
    monkeypatch.setattr(e2, "_refresh_ir_skeletons", lambda *a, **k: None)
    e2._write_ir("test")
    js2 = _gate_events(e2, "ir_jsonschema")
    assert js2 and js2[-1].verdict == "FAIL", (
        "a jsonschema-invalid IR must journal an ir_jsonschema FAIL — the "
        "oracle really runs, it does not rubber-stamp")


# -- (c) ir_written now carries a real PASS/FAIL verdict -----------------------

def test_ir_written_carries_a_closed_world_verdict(tmp_path, monkeypatch):
    """S29.8 the `ir_written` milestone gained a PASS/FAIL verdict from the
    closed-world error count (formerly it was verdict-empty, the count buried
    in the detail string) — the run summary can state 'validated' at a glance.
    """
    valid_ir = {"format": spec_ir.IR_FORMAT, "product": {},
                "nodes": {"core": {
                    "files": ["src/core.py"],
                    "openapi": _valid_openapi("core", "GET", "/notes")}}}
    e = _engine(tmp_path)
    e._root_id = "L0"
    monkeypatch.setattr(spec_ir, "collect_ir_sources", lambda _e: {})
    monkeypatch.setattr(spec_ir, "build_ir", lambda _e, _s=None: valid_ir)
    monkeypatch.setattr(e, "_atomic_write", lambda *a, **k: None)
    monkeypatch.setattr(e, "_refresh_ir_skeletons", lambda *a, **k: None)
    e._write_ir("test")
    written = _gate_events(e, "ir_written")
    assert written, "no ir_written milestone emitted"
    assert written[-1].verdict == "PASS", (
        "a closed-world-clean IR must journal ir_written PASS, not an empty "
        "verdict: %r" % written[-1].verdict)
