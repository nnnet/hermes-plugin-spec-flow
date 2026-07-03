"""Phase 6 (deterministic, report-only) — the decomposition plan check surfaces
ownership problems as findings: a declared route with no owner leaf (orphan) or
two owner leaves (duplicate, e.g. the v122 second get_notes). DIAGNOSTIC only;
findings are plain JSON-safe strings and never flip READY.

Reworked after v151: the report reads the engine's ownership DATUM
(_route_owners, recorded by _leaf_owned_routes) — never spec prose. The old
spec-text grep counted amend specs that merely QUOTE the owner module's source
as edit context, reporting "7 owner leaves" phantom duplicates.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import spec_flow_runner as sfr  # noqa: E402


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: {
        "entry": "src/app.py",
        "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
        "routes": [],
    }
    return eng


def _own(eng, nid, requirement):
    # ownership enters the datum through the REAL seam: a node whose own
    # text names the route, run through _leaf_owned_routes
    routes = eng._leaf_owned_routes(
        {"id": nid, "title": nid, "requirement": requirement})
    return routes


def test_orphan_route_reported(tmp_path):
    eng = _engine(tmp_path)
    # a leaf owns /notes; NOTHING owns /health -> orphan
    assert _own(eng, "notes", "serve POST /notes and GET /notes")
    rep = eng._plan_ownership_report()
    assert any("/health" in f and "orphan" in f for f in rep)
    assert not any("/notes" in f and "orphan" in f for f in rep)


def test_duplicate_route_reported(tmp_path):
    eng = _engine(tmp_path)
    assert _own(eng, "health", "serve GET /health")
    assert _own(eng, "reader", "GET /notes lists notes")
    assert _own(eng, "writer", "POST /notes stores; also GET /notes")
    rep = eng._plan_ownership_report()
    assert any("/notes" in f and "duplicate" in f for f in rep)


def test_amend_leaf_never_owns(tmp_path):
    # an amend node (code_target set) EDITS the owner module; quoting the
    # owner's routes in its text must not register ownership (v151)
    eng = _engine(tmp_path)
    assert _own(eng, "core", "serve POST /notes, GET /notes, GET /health")
    amend = {"id": "beautify", "title": "make notes pretty",
             "code_target": "src/core.py",
             "requirement": "polish GET /notes output; keep GET /health"}
    assert eng._leaf_owned_routes(amend) == []
    rep = eng._plan_ownership_report()
    assert not any("duplicate" in f for f in rep), rep


def test_no_data_means_no_findings(tmp_path):
    # degenerate: nothing recorded ownership — the report attests nothing
    # instead of spraying phantom orphans
    eng = _engine(tmp_path)
    assert eng._plan_ownership_report() == []


def test_report_is_json_safe(tmp_path):
    eng = _engine(tmp_path)
    _own(eng, "notes", "GET /notes")
    rep = eng._plan_ownership_report()
    json.dumps(rep)                      # must not raise
    assert all(isinstance(x, str) for x in rep)
