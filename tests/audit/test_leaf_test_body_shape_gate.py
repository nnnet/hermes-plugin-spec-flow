"""Audit rule S10.27 (v158): a leaf TEST asserting a FOREIGN BODY SHAPE on a
contracted route must red AT THE LEAF, at the same seam as the status gate.

v158: the late requirement "MAKE THE NOTES NICE TO READ" (req_a54f9144) was
routed to AMEND src/core.py; its tester wrote HTML assertions
(``assert '<ul' in body`` / ``'<h1>'``) against GET /notes into
tests/test_core.py — but GET /notes' contracted body is FROZEN JSON
(``{items: [...]}``, constitution rule 2); the HTML surface belongs to
GET /ui (web_ui). The status gate (S10.4/S10.6) checks status codes only, so
the foreign-shape tests reached assembly, redded the run (events 139-144) and
the doctor reworked CORE — the wrong artifact — twice.

Contract enforced (all deterministic, marker-level — NO HTML parser):
  * the contracted body MEDIUM of a route is ONE engine datum
    (``_route_media_map``: human wording via ``_product_contract``'s media
    map + the fixed-body routes), read by BOTH the interface contract writer
    and the leaf test gate;
  * a positive membership assertion of an HTML tag marker ('<ul', '<h1>')
    after calling a JSON-media route reds at the leaf, NAMING the contracted
    shape and the HTML-owning route;
  * GREEN edges: the same assertion on the HTML-media route passes; JSON
    assertions on a JSON route pass; a route with unknown/ambiguous media is
    never flagged (lenient — one false positive sinks a run, v151).

Deterministic: unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import json
import pathlib
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health",
             "html_route": "/ui"},
    "routes": [],
    "media": {"/notes": "json", "/health": "json", "/ui": "html"},
}


def _gate(tmp_path, test_body: str, node: dict | None = None,
          contract: dict | None = None) -> "tuple[bool, list]":
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(contract or _CONTRACT)
    node = node or {"id": "core", "title": "Product core",
                    "requirement": "own POST /notes and GET /notes"}
    rel = "tests/test_core.py"
    p = pathlib.Path(eng.workspace.root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(test_body), encoding="utf-8")
    ok = eng._leaf_test_status_gate(node, str(node.get("id")), 1, rel)
    return ok, [l for l in eng.loops if l["type"] == "test-status-mismatch"]


# ---- RED: the v158 defect shape --------------------------------------------

def test_html_marker_on_json_route_is_red(tmp_path):
    ok, loops = _gate(tmp_path, """\
        def _call(method, path):
            return 200, '{"items": []}'


        def test_get_notes_has_list_element():
            code, body = _call("GET", "/notes")
            assert '<ul' in body
    """)
    assert not ok and loops, (
        "a leaf test asserting an HTML marker ('<ul') on GET /notes, whose "
        "contracted body is JSON, must red at the leaf — v158 shipped this "
        "test to assembly and the doctor reworked the wrong artifact")
    detail = loops[0]["detail"]
    assert "JSON" in detail, "the finding must name the contracted shape"
    assert "/ui" in detail, "the finding must name the HTML-owning route"


def test_amend_node_html_marker_is_red_too(tmp_path):
    # the EXACT v158 shape: the tester of an AMEND (code_target=src/core.py,
    # owns no routes -> the status gate's owned set is empty) wrote the HTML
    # assertions — the shape check must not hide behind the amend exemption
    ok, loops = _gate(tmp_path, """\
        def _call(method, path):
            return 200, '{"items": []}'


        def test_notes_nice_heading():
            code, body = _call("GET", "/notes")
            assert '<h1>' in body
    """, node={"id": "req_nice", "title": "Make the notes nice to read",
               "code_target": "src/core.py",
               "requirement": "improve the existing presentation"})
    assert not ok and loops, (
        "an amend node's test asserting HTML on a JSON-contracted route must "
        "red at the leaf (v158: req_a54f9144 wrote them into test_core.py)")


def test_unittest_assertin_html_marker_is_red(tmp_path):
    ok, loops = _gate(tmp_path, """\
        import unittest


        class T(unittest.TestCase):
            def _call(self, method, path):
                return 200, "<ul></ul>"

            def test_list(self):
                code, body = self._call("GET", "/notes")
                self.assertIn("<ul", body)
    """)
    assert not ok and loops, "assertIn('<ul', body) is the same foreign shape"


# ---- GREEN: legitimate edges must stay silent ------------------------------

def test_html_marker_on_html_route_is_legal(tmp_path):
    ok, loops = _gate(tmp_path, """\
        def _call(method, path):
            return 200, "<ul></ul>"


        def test_ui_lists_notes():
            code, body = _call("GET", "/ui")
            assert '<ul' in body
            assert '<h1>' in body
    """, node={"id": "web_ui", "title": "Minimal web interface",
               "requirement": "own GET /ui — an HTML page"})
    assert ok and not loops, (
        "the SAME HTML assertion in the HTML-owning route's test must pass")


def test_json_assertions_on_json_route_are_legal(tmp_path):
    ok, loops = _gate(tmp_path, """\
        import json


        def _call(method, path):
            return 200, '{"items": []}'


        def test_get_notes_items():
            code, body = _call("GET", "/notes")
            data = json.loads(body)
            assert data["items"] == []
    """)
    assert ok and not loops, "JSON assertions on the JSON route must pass"


def test_unknown_media_route_is_lenient(tmp_path):
    contract = dict(_CONTRACT)
    contract["media"] = {}          # nothing classified -> nothing flagged
    ok, loops = _gate(tmp_path, """\
        def _call(method, path):
            return 200, "<p>hi</p>"


        def test_whatever():
            code, body = _call("GET", "/notes")
            assert '<p>' in body
    """, contract=contract)
    assert ok and not loops, (
        "a route with no contracted media must never be flagged (v151: one "
        "false positive sinks a run)")


# ---- the media DATUM itself -------------------------------------------------

def test_media_derived_from_human_text(tmp_path):
    """`_product_contract()['media']` reads the HUMAN wording around each
    METHOD-route mention: a JSON body literal votes json, page/HTML wording
    votes html, a conflict leaves the path unclassified. A bare path mention
    without a METHOD ("... and /notes logic") must NOT poison the vote —
    v158's web_ui requirement names '/notes logic' two words from 'HTML'."""
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._goal = (
        'A tiny notes service. POST /notes accepts {"text": "..."} and '
        'stores it, returning {"id": <int>}; GET /notes returns '
        '{"items": [...]} newest-first.')
    eng._constitution = [
        "Standard library ONLY: HTTP through a WSGI app (src/app.py exposes "
        "`wsgi_app`), storage through sqlite3 in src/db.py.",
        "GET /health responds 200 with {status: ok}.",
        "Serve a server-rendered HTML page reusing the existing notes "
        "storage and /notes logic: GET /ui — an HTML page that lists all "
        "notes and carries an add form.",
    ]
    media = (eng._product_contract() or {}).get("media") or {}
    assert media.get("/notes") == "json", (
        "GET/POST /notes carry JSON body literals in the goal — media must "
        "say json; got %r" % media)
    assert media.get("/ui") == "html", (
        "GET /ui is described as an HTML page — media must say html; "
        "got %r" % media)


def test_interface_contract_carries_media(tmp_path):
    """contracts/interface.json — the machine contract every consumer reads —
    must carry the body medium for classified routes (the S10.27 datum made
    machine-readable, the same way success_status was for v149)."""
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    ws = eng.workspace
    ws.enabled = True
    ws.root = str(tmp_path / "wk")
    pathlib.Path(ws.root).mkdir(parents=True, exist_ok=True)
    eng._product_contract = lambda: dict(_CONTRACT)
    eng._write_interface_contract()
    data = json.loads(
        (pathlib.Path(ws.root) / "contracts" / "interface.json").read_text(
            encoding="utf-8"))
    by = {(r["method"], r["path"]): r for r in data["routes"]}
    assert by[("GET", "/notes")].get("media") == "application/json"
    assert by[("GET", "/ui")].get("media") == "text/html"
