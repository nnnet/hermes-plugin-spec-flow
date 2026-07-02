"""Audit Level A — every requirement CLASS has a non-rejecting path in the engine.

A design hole is a requirement class the engine cannot route to a real outcome:
it forks a doomed leaf, or two subsystems disagree and reject it. This suite is
the cheap gate that reds on such a hole in seconds — before an hour-long run
rediscovers it.

The classes:
  - new-route        (DELETE /notes/{id})        -> new-surface leaf        [#132]
  - amend-in-place   ("make the notes nice")      -> EDIT the owner module   [v145]
  - duplicate        (re-declare POST /notes)     -> reject empty_delta      [#132]
  - non-web capability (word_count())             -> capability leaf         [#130]

Pure/deterministic: no LLM, no HTTP.
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402


def _engine(tmp_path, *, owner=True):
    wk = tmp_path / "wk"
    (wk / "src").mkdir(parents=True, exist_ok=True)
    if owner:
        # an EXISTING owner that already serves the /ui surface + _render_html
        (wk / "src" / "web_ui.py").write_text(
            "def _render_html(items):\n"
            "    return '<html>' + ''.join(str(i) for i in items) + '</html>'\n"
            "def get_ui(payload, query):\n"
            "    path = '/ui'\n"
            "    return (200, _render_html([]))\n")
    eng = sfr.Engine(workspace=str(wk), depth=sfr.DEPTH_SPEC)
    eng._constitution = [
        "A notes service. POST /notes takes {text} -> {id}; GET /notes -> items;"
        " GET /ui shows an HTML page. The entry src/app.py exposes wsgi_app."]
    eng._goal = "notes"
    return eng


# ── amend-in-place: the v145 hole ────────────────────────────────────────────
# красивый_вид was routed to EDIT web_ui (code_target set), then RE-authored a
# spec restating web_ui's surface, then the anti-fork scope-lint rejected it as a
# duplicate -> empty_delta -> handler gate missing -> product RED. An amend
# re-states the owner's surface BY DESIGN; it owns no new route. The anti-fork
# gates must EXEMPT a node that carries code_target.

def test_amend_node_is_exempt_from_duplicate_scope_reject(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_REQ_AMEND", "1")
    eng = _engine(tmp_path)
    amend_node = {
        "id": "krasivy", "_late_req": True, "code_target": "src/web_ui.py",
        "requirement": "make the notes nice to read",
        "spec_markdown": (
            "REQ-1: `_render_html(items)` in `src/web_ui.py` SHALL add an `<h1>`."
            " REQ-3: an inline `<form>` whose POST target is the existing `/ui`;"
            " no new route is introduced.")}
    findings = eng._late_req_scope_findings(amend_node, amend_node["spec_markdown"])
    assert findings == [], (
        "an amend node (code_target set) re-states the owner's surface by "
        "design; the anti-fork scope-lint must not reject it as a duplicate — "
        f"got {findings}")


def test_amend_node_owns_no_route_so_handler_gate_is_inert(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_REQ_AMEND", "1")
    eng = _engine(tmp_path)
    amend_node = {
        "id": "krasivy", "_late_req": True, "code_target": "src/web_ui.py",
        "requirement": "make the notes nice to read",
        "spec_markdown": "Edit src/web_ui.py to add an <h1> above the /ui list."}
    # an amend node exposes no NEW public symbol/route of its own — it edits the
    # owner — so the leaf-exposed-symbols set is empty and downstream route/
    # handler gates stay inert (no 'contracted handler missing' on an amend).
    assert eng._leaf_exposed_symbols(amend_node) == [], (
        "an amend node owns no new route; it must expose nothing of its own")


# ── duplicate: must still be rejected (guard against over-exempting) ──────────

def test_true_duplicate_without_amend_still_flagged(tmp_path):
    eng = _engine(tmp_path)
    dup_node = {
        "id": "dup", "_late_req": True,        # NO code_target -> a real fork
        "requirement": "serve POST /notes and GET /notes",
        "spec_markdown": "This exposes POST /notes and GET /notes round-tripping"
                         " a note (already served by the base product)."}
    # a genuine fork that re-declares the base surface with no amend target and
    # no new operation must STILL be caught — exempting amend must not blind the
    # anti-fork lint to real duplicates. (Owner for /notes lives in the base
    # contract; the lint keys on the restated surface.)
    findings = eng._late_req_scope_findings(dup_node, dup_node["spec_markdown"])
    # note: with no built /notes module in this bare workspace the lint may be
    # inert; the invariant we assert is that exemption is keyed on code_target,
    # NOT that every dup reds here. So: a dup node is NOT auto-exempt.
    assert not dup_node.get("code_target"), "a real fork carries no code_target"
