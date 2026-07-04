"""Audit rule S12.15 (v164): media conformance is BEHAVIOURAL — each
contracted route's LIVE response must match its contracted body medium.

v164 (2026-07-03T23-02-13__v164__p6-micro-notes): GET /about answered
200 {"name": "notes-service", "version": "1.0"} (JSON) while
contracts/interface.json contracted its media as text/html. Every existing
media reader is code-shape or test-side: the leaf-test gate (S10.27) reds a
TEST asserting the wrong medium, the interface contract PRINTS the medium —
but nothing checked the HANDLER'S live behaviour, so a rework that swapped
the HTML page for a JSON dict sailed through the boot-gate and only the leaf
suite (test_get_about_html) red-ed downstream.

Contract enforced (behaviour-level, same seam as the S12.6 shape probe):
  * the boot-gate probe (`_ROOT_BOOT_PROBE`) checks every contracted route's
    live 2xx response against its media datum (`_route_media_map`):
    text/html -> HTML marker present, application/json -> json.loads
    succeeds;
  * a mismatch is a deterministic RED naming the route, its OWNER LEAF and
    BOTH medias (served vs contracted);
  * GREEN edges: a JSON route serving JSON passes; an HTML route serving
    HTML passes; a route with NO media datum is untouched (lenient);
    non-2xx statuses are owned by the other probe sections, never judged
    here.

Deterministic: real subprocess boot over a tmp workspace, no LLM.
"""
from __future__ import annotations

import pathlib
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_GOAL = (
    'A tiny notes service. HTTP endpoints over a WSGI app: POST /notes '
    'accepts {"text": "..."} and stores it, returning {"id": <int>}; '
    'GET /notes returns {"items": [...]} newest-first; GET /ui serves an '
    'HTML page; GET /about serves an HTML about page.')
_CONSTITUTION = [
    "Standard library ONLY: HTTP through a WSGI app (src/app.py exposes "
    "`wsgi_app`), storage through sqlite3.",
    "Every test sets the NOTES_DB env var to a fresh temp path BEFORE "
    "connecting.",
]
_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health",
             "html_route": "/ui"},
    "routes": [["GET", "/about"]],
    "media": {"/notes": "json", "/health": "json", "/ui": "html",
              "/about": "html"},
}

# a well-behaved app (env-fallback storage, contracted-field-only validation)
# whose ONLY defect is the get_about body medium — the literal v164 shape
_APP = """\
    import json
    import os
    import sqlite3


    def _db():
        conn = sqlite3.connect(os.environ.get("NOTES_DB", ":memory:"))
        conn.execute("CREATE TABLE IF NOT EXISTS notes "
                     "(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)")
        return conn


    def get_health(payload, query):
        return 200, {"status": "ok"}


    def post_notes(payload, query):
        if not isinstance(payload, dict) or "text" not in payload:
            return 400, {"error": "missing field: text"}
        conn = _db()
        cur = conn.execute("INSERT INTO notes (text) VALUES (?)",
                           (payload["text"],))
        conn.commit()
        note_id = cur.lastrowid
        conn.close()
        return 201, {"id": note_id}


    def get_notes(payload, query):
        conn = _db()
        rows = conn.execute("SELECT id, text FROM notes "
                            "ORDER BY id DESC").fetchall()
        conn.close()
        return 200, {"items": [{"id": r[0], "text": r[1]} for r in rows]}


    def get_ui(payload, query):
        return 200, "<html><body><ul>notes</ul></body></html>"


    def get_about(payload, query):
    %(about_body)s

    _ROUTES = {
        ("GET", "/health"): get_health,
        ("GET", "/notes"): get_notes,
        ("GET", "/ui"): get_ui,
        ("GET", "/about"): get_about,
        ("POST", "/notes"): post_notes,
    }

    _STATUS = {200: "200 OK", 201: "201 Created", 400: "400 Bad Request",
               404: "404 Not Found", 500: "500 Internal Server Error"}


    def _send(start_response, code, body):
        if isinstance(body, (dict, list)):
            payload = json.dumps(body).encode("utf-8")
            ctype = "application/json"
        else:
            payload = str(body or "").encode("utf-8")
            ctype = "text/html; charset=utf-8"
        start_response(_STATUS.get(code, "200 OK"),
                       [("Content-Type", ctype)])
        return [payload]


    def wsgi_app(environ, start_response):
        method = (environ.get("REQUEST_METHOD") or "GET").upper()
        path = environ.get("PATH_INFO") or "/"
        fn = _ROUTES.get((method, path))
        if fn is None:
            return _send(start_response, 404, {"error": "not found"})
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = environ["wsgi.input"].read(length) if length > 0 else b""
        payload = {}
        if raw:
            try:
                payload = json.loads(raw)
            except Exception:
                return _send(start_response, 400, {"error": "invalid json"})
        try:
            status, body = fn(payload, {})
        except Exception as exc:
            return _send(start_response, 500,
                         {"error": "internal: %%s: %%s"
                                   %% (type(exc).__name__, exc)})
        return _send(start_response, status, body)
"""

_ABOUT_JSON = '    return 200, {"name": "notes-service", "version": "1.0"}'
_ABOUT_HTML = ('    return 200, "<html><body><h1>About</h1>'
               '<p>a tiny notes service</p></body></html>"')


def _engine(tmp_path, about_body: str, contract: dict = None):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    c = dict(contract or _CONTRACT)
    eng._product_contract = lambda: dict(c)
    eng._goal = _GOAL
    eng._constitution = list(_CONSTITUTION)
    # the ownership datum: about_page owns GET /about (the addressee the
    # finding must name)
    eng.__dict__["_route_owners"] = {("GET", "/about"): {"about_page"}}
    root = pathlib.Path(eng.workspace.root)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text(
        textwrap.dedent(_APP % {"about_body": about_body}), encoding="utf-8")
    return eng


# --- RED: the v164 media violation reds at the boot-gate ----------------------

def test_json_on_contracted_html_route_reds_boot_gate(tmp_path):
    eng = _engine(tmp_path, _ABOUT_JSON)
    ok, detail = eng._assembled_product_boots()
    assert ok is False, (
        "v164: GET /about served a JSON dict while its contracted media is "
        "text/html — the handler side has no behavioural media check, so "
        "the boot-gate probe MUST red it")
    assert "/about" in detail, f"the finding must NAME the route: {detail!r}"
    assert "text/html" in detail and "application/json" in detail, (
        f"the finding must name BOTH medias (served vs contracted): {detail!r}")
    assert "about_page" in detail, (
        f"the finding must name the OWNER LEAF: {detail!r}")


# --- GREEN: conforming media and uncontracted routes stay silent --------------

def test_html_route_serving_html_is_green(tmp_path):
    eng = _engine(tmp_path, _ABOUT_HTML)
    ok, detail = eng._assembled_product_boots()
    assert ok is True, (
        "an HTML route serving HTML (and JSON routes serving JSON) must "
        f"pass the media probe: {detail!r}")


def test_uncontracted_media_route_untouched(tmp_path):
    # /about has NO media datum -> lenient, never judged
    c = dict(_CONTRACT)
    c["media"] = {"/notes": "json", "/health": "json", "/ui": "html"}
    eng = _engine(tmp_path, _ABOUT_JSON, contract=c)
    ok, detail = eng._assembled_product_boots()
    assert ok is True, (
        "a route with no contracted media must never be judged by the "
        f"media probe: {detail!r}")
