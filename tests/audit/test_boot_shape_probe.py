"""Audit rule S12.6 (v161): config-vs-request confusion is judged by LIVE
BEHAVIOUR at the boot-gate, not only by code shape.

v161 (2026-07-03T18-39-04__v161__p6-micro-notes): the v159 class landed AGAIN
despite the S12.1 AST gate — this time the required-field surface lived in a
SHARED dispatch wrapper: the engine-synthesized router maps ANY KeyError to
400 {"error": "missing required field: %s"} while src/core.py reads
``os.environ['NOTES_DB']`` inside every handler. With NOTES_DB unset the
product answered EVERY route — even bodyless GET /ui — with
400 "missing required field: 'NOTES_DB'". The AST leaf gate
(`_leaf_request_shape_gate`) only sees ``payload[...]`` shapes in the OWNER
handler; a KeyError laundered through a shared wrapper is invisible to it.

Contract enforced (behaviour-level, code-shape-independent):
  * the boot-gate probe (`_ROOT_BOOT_PROBE`) exercises EVERY contracted route
    with its CONTRACTED example payload (the S12.1 `_route_request_fields`
    datum; GET/DELETE = no body) in an environment where the constitution's
    env vars are ABSENT;
  * a 4xx naming a required field OUTSIDE the contracted request shape is a
    DETERMINISTIC boot-gate RED naming the field, the route, and — when the
    field is a constitution env var — 'X is an environment variable
    (constitution), never a request field';
  * GREEN edges: an app validating ONLY contracted fields passes; a
    config-starved 5xx stays legal (an honest server-side config error is
    not request-shape confusion); a 4xx naming a CONTRACTED field cannot
    occur — the probe sends every contracted field — so it is deliberately
    not judged (documented blind spot, not a hole: the suite/roundtrip
    sections already own functional failures).

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
    'HTML page.')
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
    "routes": [],
    "media": {"/notes": "json", "/health": "json", "/ui": "html"},
}

# The literal v161 shape: an engine-router-style SHARED wrapper that turns ANY
# KeyError into 400 "missing required field", over handlers that read the
# NOTES_DB env var with a hard subscript. No `payload[...]` anywhere — the
# S12.1 AST gate is structurally blind to it.
_APP_SHARED_WRAPPER = """\
    import io
    import json

    from core import get_health, get_notes, get_ui, post_notes

    _ROUTES = {
        ("GET", "/health"): get_health,
        ("GET", "/notes"): get_notes,
        ("GET", "/ui"): get_ui,
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
        except KeyError as exc:
            return _send(start_response, 400,
                         {"error": "missing required field: %s" % exc})
        except Exception as exc:
            return _send(start_response, 500,
                         {"error": "handler failed: %s" % exc})
        return _send(start_response, status, body)


    application = wsgi_app
"""

_CORE_ENV_SUBSCRIPT = """\
    import os
    import sqlite3


    def _db():
        conn = sqlite3.connect(os.environ['NOTES_DB'])
        conn.execute("CREATE TABLE IF NOT EXISTS notes "
                     "(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)")
        return conn


    def post_notes(payload, query):
        if not isinstance(payload, dict) or 'text' not in payload:
            return 400, {"error": "missing field: text"}
        conn = _db()
        cur = conn.execute("INSERT INTO notes (text) VALUES (?)",
                           (payload['text'],))
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
        _, data = get_notes(payload, query)
        rows = "".join("<li>%s</li>" % i["text"] for i in data["items"])
        return 200, "<html><body><h1>Notes</h1><ul>%s</ul></body></html>" % rows


    def get_health(payload, query):
        return 200, {"status": "ok"}
"""

# GREEN twin: identical surface, but a MISSING env var falls back to an
# in-memory database — the app validates ONLY contracted request fields
# (post_notes 400s on a missing 'text', which the probe always sends).
_CORE_ENV_FALLBACK = _CORE_ENV_SUBSCRIPT.replace(
    "os.environ['NOTES_DB']", 'os.environ.get("NOTES_DB", ":memory:")')


def _engine(tmp_path, core_body: str):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    eng._goal = _GOAL
    eng._constitution = list(_CONSTITUTION)
    root = pathlib.Path(eng.workspace.root)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text(
        textwrap.dedent(_APP_SHARED_WRAPPER), encoding="utf-8")
    (root / "src" / "core.py").write_text(
        textwrap.dedent(core_body), encoding="utf-8")
    return eng


# --- RED: the v161 shared-wrapper shape reds at the boot-gate -----------------

def test_shared_wrapper_config_400_reds_boot_gate(tmp_path):
    eng = _engine(tmp_path, _CORE_ENV_SUBSCRIPT)
    ok, detail = eng._assembled_product_boots()
    assert ok is False, (
        "v161: with NOTES_DB unset the product 400s EVERY route naming "
        "'NOTES_DB' as a missing required field — a config KeyError "
        "laundered through a SHARED wrapper; the AST leaf gate cannot see "
        "it, so the boot-gate probe MUST red it behaviourally")
    assert "NOTES_DB" in detail, (
        f"the finding must NAME the field, got: {detail!r}")
    assert "environment variable" in detail and "constitution" in detail, (
        "the finding must state 'NOTES_DB is an environment variable "
        f"(constitution), never a request field', got: {detail!r}")
    assert any(("%s %s" % (m, p)) in detail
               for m, p in (("GET", "/ui"), ("GET", "/notes"),
                            ("POST", "/notes"), ("GET", "/health"))), (
        f"the finding must NAME the probed route, got: {detail!r}")


def test_finding_names_route_and_contracted_shape(tmp_path):
    eng = _engine(tmp_path, _CORE_ENV_SUBSCRIPT)
    ok, detail = eng._assembled_product_boots()
    assert ok is False
    # the contracted shape is part of the attribution: the reader must see
    # what the route actually accepts next to the foreign field
    assert "request" in detail.lower(), detail


# --- GREEN: only-contracted-fields validation passes --------------------------

def test_contracted_only_validation_is_green(tmp_path):
    eng = _engine(tmp_path, _CORE_ENV_FALLBACK)
    ok, detail = eng._assembled_product_boots()
    assert ok is True, (
        "an app that validates ONLY contracted request fields (post_notes "
        "requires 'text', which the probe sends) must pass the shape probe: "
        f"{detail!r}")


def test_config_starved_5xx_stays_legal(tmp_path):
    # A server that answers 500 when its config is absent is HONEST — a
    # config error is a server-side condition, not request validation.
    core_500 = _CORE_ENV_SUBSCRIPT.replace(
        "os.environ['NOTES_DB']",
        'os.environ["NOTES_DB"] if "NOTES_DB" in os.environ else '
        '(_ for _ in ()).throw(RuntimeError("NOTES_DB not configured"))')
    eng = _engine(tmp_path, core_500)
    ok, detail = eng._assembled_product_boots()
    assert ok is True, (
        "a config-starved 5xx (RuntimeError -> handler failed -> 500) is an "
        "honest server error, never the request-shape confusion class: "
        f"{detail!r}")
