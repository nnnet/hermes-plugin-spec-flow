"""Phase 1 — route -> handler binding is engine-declared DATA on the OWNING leaf.

The leaf that owns a declared route is handed the exact canonical handler name
(``def post_notes(payload, query)``) in its build spec, so the implementer
produces that symbol and the assembled entry imports it by name — nothing is
inferred. A leaf that owns no route gets no binding block.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import spec_flow_runner as sfr  # noqa: E402


def _engine(tmp_path):
    return sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)


def test_owning_leaf_gets_canonical_binding(tmp_path):
    eng = _engine(tmp_path)
    eng._product_contract = lambda: {
        "entry": "src/app.py",
        "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
        "routes": [],
    }
    node = {"id": "notes_api", "title": "Notes API",
            "requirement": "Expose POST /notes and GET /notes returning JSON",
            "spec_markdown": ""}
    block = eng._leaf_route_binding(node)
    # the leaf is told the EXACT symbols, derived from method+path
    assert "def post_notes(payload, query)" in block
    assert "def get_notes(payload, query)" in block
    # a route this leaf does NOT mention is not bound here
    assert "get_health" not in block


def test_non_http_leaf_gets_no_binding(tmp_path):
    eng = _engine(tmp_path)
    eng._product_contract = lambda: {
        "entry": "src/app.py",
        "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
        "routes": [],
    }
    node = {"id": "date_utils", "title": "Date helpers",
            "requirement": "Format and parse ISO dates", "spec_markdown": ""}
    assert eng._leaf_route_binding(node) == ""


def test_no_contract_means_no_binding(tmp_path):
    eng = _engine(tmp_path)
    eng._product_contract = lambda: {}
    node = {"id": "x", "title": "t", "requirement": "POST /notes"}
    assert eng._leaf_route_binding(node) == ""
