"""Audit S16.6: a 405 from the synthesized router MUST carry an `Allow`
header listing the path's contracted methods (RFC 9110 §15.5.6), and the
compiled OpenAPI document MUST declare that header on the shared
RouterMethodNotAllowed response.

Plan 2026-07-04T00-45, node B4 (`router-allow-header`) — born from an honest
Schemathesis finding during the B2 contract-oracle demo: the fuzzer flagged
every 405 as missing `Allow`, which RFC 9110 makes mandatory ("An origin
server MUST generate an Allow header field in a 405 response"). The router
already knows the truth — its own `_ROUTES` table holds every contracted
(method, path) pair — so the header is pure engine data, never guessed.

Named directions (both-directions convention):
- RED  a 405 without `Allow` = advertising less than the router knows; the
  header value must be EXACTLY the path's contracted methods, sorted,
  comma-separated (multi-method and single-method paths both pinned).
- RED  the OpenAPI mirror: the 405 component must declare `headers.Allow`
  (OpenAPI 3.1 response `headers`) and its description must say so.
- GREEN a 404 (path contracted nowhere) carries NO `Allow` — there are no
  methods to list; success responses stay untouched.

Deterministic: engine unit calls over a tmp workspace + crafted IR dicts,
no LLM, no network. Mirrors the harness of test_router_exception_honesty.py.
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402
import spec_ir  # noqa: E402
import spec_openapi  # noqa: E402

_GOAL = ('A tiny notes service over a WSGI app: POST /notes accepts '
         '{"text": "..."} and stores it; GET /notes returns {"items": [...]}.')

_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [],
}

# plain in-memory handlers — this file audits the HTTP glue (Allow header),
# not handler behavior, so no env/db dependency
_NOTES_PLAIN = '''\
def post_notes(payload, query):
    return 201, {"id": 1}


def get_notes(payload, query):
    return 200, {"items": []}
'''


def _synthesize(tmp_path):
    """Why: every case needs the REAL engine-synthesized entry, not a copy.
    What: builds an Engine over tmp, plants the notes leaf, returns
    (entry source, src dir); _ROUTES ends up GET+POST /notes, GET /health.
    Test: this module — the assertion inside fails the run if synthesis
    refuses the shape.
    """
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._goal = _GOAL
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "notes.py").write_text(_NOTES_PLAIN, encoding="utf-8")
    mapping, unresolved = eng._resolve_route_handlers(_CONTRACT)
    code = eng._synthesize_entry_code(_CONTRACT, mapping, unresolved)
    assert code, "the deterministic entry must synthesize for this shape"
    return code, src


def _load_entry(code, src_dir):
    for m in ("notes", "_s166_app"):
        sys.modules.pop(m, None)
    entry = pathlib.Path(src_dir) / "app.py"
    entry.write_text(code, encoding="utf-8")
    sys.path.insert(0, str(src_dir))
    try:
        spec = importlib.util.spec_from_file_location("_s166_app", str(entry))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(src_dir))
    return mod


def _call(app, method, path, body=None):
    """Why: the Allow audit needs the HEADERS, not just the status line.
    What: WSGI-calls the app; returns (status int, headers dict, body str).
    Test: this module's cases; header names keyed verbatim.
    """
    captured = {}

    def start_response(status, headers):
        captured["status"] = int(str(status).split()[0])
        captured["headers"] = dict(headers)

    raw = body if isinstance(body, bytes) else (
        json.dumps(body).encode() if body is not None else b"")
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path,
               "QUERY_STRING": "", "CONTENT_LENGTH": str(len(raw)),
               "wsgi.input": io.BytesIO(raw)}
    chunks = app.wsgi_app(environ, start_response)
    return (captured["status"], captured["headers"],
            b"".join(chunks).decode("utf-8", "replace"))


# ---- RED: router 405 must carry Allow (RFC 9110) ------------------------------

def test_405_on_multi_method_path_lists_all_contracted_methods(tmp_path):
    """DELETE /notes: the path contracts GET and POST — Allow must name
    BOTH, sorted, comma-separated (RFC 9110 list syntax)."""
    code, src = _synthesize(tmp_path)
    app = _load_entry(code, src)
    st, headers, body = _call(app, "DELETE", "/notes")
    assert st == 405 and "method not allowed" in body, (st, body)
    assert headers.get("Allow") == "GET, POST", (
        "RFC 9110 §15.5.6: a 405 MUST carry Allow listing the path's "
        "contracted methods — /notes contracts GET and POST, got headers %r"
        % (headers,))


def test_405_on_single_method_path_lists_that_method(tmp_path):
    """POST /health: the liveness route contracts GET only — Allow: GET."""
    code, src = _synthesize(tmp_path)
    app = _load_entry(code, src)
    st, headers, body = _call(app, "POST", "/health")
    assert st == 405 and "method not allowed" in body, (st, body)
    assert headers.get("Allow") == "GET", (
        "/health contracts exactly GET; Allow must say so, got headers %r"
        % (headers,))


# ---- RED: OpenAPI mirror declares the header ----------------------------------

def _one_path_two_methods_ir():
    return {"format": spec_ir.IR_FORMAT,
            "product": {"entry": "src/app.py", "callable": ["wsgi_app"]},
            "nodes": {"notes": {"files": ["src/notes.py"], "openapi": {
                "openapi": "3.1.0",
                "info": {"title": "spec-flow node notes interface",
                         "version": "1"},
                "paths": {"/notes": {
                    "post": {"x-spec-flow-handler": "post_notes",
                             "responses": {"201": {
                                 "description": "created",
                                 "content": {"application/json":
                                             {"schema": {}}}}}},
                    "get": {"x-spec-flow-handler": "get_notes",
                            "responses": {"200": {
                                "description": "listed",
                                "content": {"application/json":
                                            {"schema": {}}}}}}}}}}}}


def test_openapi_405_component_declares_allow_header():
    """The shared RouterMethodNotAllowed response must declare the Allow
    header (OpenAPI 3.1 response `headers`) and SAY it in the description —
    otherwise the document advertises less than the router does and the
    Schemathesis finding stays open on the contract side."""
    doc = spec_openapi.compile_openapi(_one_path_two_methods_ir())
    comp = doc["components"]["responses"]["RouterMethodNotAllowed"]
    hdr = (comp.get("headers") or {}).get("Allow")
    assert isinstance(hdr, dict) and hdr.get("schema") == {
        "type": "string"}, (
        "the 405 component must declare headers.Allow with a string "
        "schema, got %r" % (comp.get("headers"),))
    assert "Allow" in str(comp.get("description", "")), (
        "the 405 description must mention the Allow header so the "
        "document quotes the router's ACTUAL behavior")
    assert "Allow" in str(hdr.get("description", "")) or \
        "method" in str(hdr.get("description", "")), (
        "the header itself carries a description naming what it lists")
    # the compiled document must still self-lint clean with the header on
    assert spec_openapi.lint_openapi(doc) == [], (
        "declaring the Allow header must not break the structural lint")


# ---- GREEN: no invented Allow anywhere else ------------------------------------

def test_404_carries_no_allow_header(tmp_path):
    """A path contracted NOWHERE has no methods to list — a 404 with an
    Allow header would be invented behavior."""
    code, src = _synthesize(tmp_path)
    app = _load_entry(code, src)
    st, headers, body = _call(app, "GET", "/nowhere")
    assert st == 404 and "not found" in body, (st, body)
    assert "Allow" not in headers, (
        "404 must NOT carry Allow — nothing is contracted there, got %r"
        % (headers,))


def test_success_responses_stay_untouched(tmp_path):
    """The happy path keeps today's shape: 200/201 with the handler's body
    and no Allow header."""
    code, src = _synthesize(tmp_path)
    app = _load_entry(code, src)
    st, headers, body = _call(app, "GET", "/notes")
    assert st == 200 and "items" in body, (st, body)
    assert "Allow" not in headers, (headers,)
    st, headers, body = _call(app, "POST", "/notes", body={"text": "hi"})
    assert st == 201 and "id" in body, (st, body)
    assert "Allow" not in headers, (headers,)
