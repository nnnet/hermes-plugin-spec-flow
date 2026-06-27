"""v101 regression: the duplicate-surface gate must credit a module with a
route only when it actually SERVES it (dispatch evidence), never when the path
string merely appears in prose/comment/error text. A stray '/ui' literal in a
health module falsely owned GET /ui, so the web_ui late requirement was rejected
as an empty delta (route-redeclare) and the UI leaf was never built — the
assembled product then served GET /ui -> 404 and the run came back NOT READY."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from spec_flow_runner import _served_routes, _dup_surface_findings  # noqa: E402


# ─── _served_routes: CODE dispatch evidence only ──────────────────────

def test_served_routes_tuple_table_key():
    body = "_ROUTES = {('POST', '/notes'): post, ('GET', '/notes'): get}"
    assert _served_routes(body) == {"/notes"}


def test_served_routes_decorator_and_dispatch_forms():
    assert _served_routes("@app.route('/ui')\ndef ui(): ...") == {"/ui"}
    assert _served_routes("if path == '/ui':\n    return render()") == {"/ui"}
    assert _served_routes("if path.startswith('/ui'):\n    page()") == {"/ui"}
    assert _served_routes("if path in ('/ui', '/about'):\n    ok()") \
        == {"/ui", "/about"}


def test_served_routes_prose_method_phrase_is_not_served():
    # v102 root: a "GET /ui" phrase in a sibling spec is a MENTION, not serving
    assert _served_routes("GET /ui — an HTML page listing notes") == set()
    assert _served_routes("the router forwards GET /ui to the web_ui leaf") \
        == set()


def test_served_routes_ignores_bare_mention_in_prose_or_comment():
    assert _served_routes("# this module does not handle '/ui'") == set()
    assert _served_routes('return _send(404, "no such route /ui")') == set()
    assert _served_routes("docstring mentions the /ui page in passing") == set()


# ─── _dup_surface_findings: ownership precision ───────────────────────

def test_late_route_not_blocked_by_a_sibling_prose_mention():
    """v102 root: the web_ui spec declares GET /ui; a sibling health+routing
    module DESCRIBES '/ui' in its spec prose ("forwards GET /ui to web_ui") while
    its code only dispatches /notes & /health. The new spec must NOT be flagged
    route-redeclare — /ui is genuinely new surface, only PLANNED in prose, not
    yet served by anyone."""
    spec = "MINIMAL WEB INTERFACE\n- GET /ui — an HTML page listing notes"
    sibling = (
        "the WSGI router serves GET /notes and GET /health, and forwards "
        "GET /ui to the web_ui leaf once it exists\n"
        "_ROUTES = {('GET', '/notes'): get_notes, ('GET', '/health'): health}\n")
    findings = _dup_surface_findings(spec, [("src/health_and_routing.py",
                                             "health_and_routing", sibling)])
    assert not any("route-redeclare" in f and "/ui" in f for f in findings), \
        findings


def test_genuine_route_redeclare_still_fires():
    """A spec that restates GET /notes (already served via a routes table) and
    adds no new route is still correctly flagged — the fix narrows ownership, it
    does not blind the gate."""
    spec = "Add a second handler for GET /notes that returns the same list"
    owner = "_ROUTES = {('GET', '/notes'): get_notes}\n"
    findings = _dup_surface_findings(spec, [("src/notes_endpoints.py",
                                             "notes_endpoints", owner)])
    assert any("route-redeclare" in f and "/notes" in f for f in findings), \
        findings
