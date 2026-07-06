"""STAGE 12 extension (S12.17): the synthesized router validates request-body
field TYPES, not just presence (node H4, plan 2026-07-04T00-45; principles-
audit finding F3).

Why: the router's request gate asserted only that a required field is PRESENT
(S12.14) — `{"text": 12345}` where the contract types `text` as a string
sailed through the 400 gate into the handler. A type the IR DECLARES is a
contract datum; a weak model returning/accepting the wrong type must be
refused at the engine-owned gate, not left to leaf luck.

What is pinned here:
  * S12.17 when the IR (a decomposer machine fragment) declares a request
    field's type, the synthesized router 400s a value of the wrong type,
    naming the field; a correctly-typed value passes to the handler;
  * a field whose type the IR never recorded (`{f: {}}`) is NOT type-checked
    — the engine invents no type (honest gap, S13.1); presence-only, as
    before;
  * bool is not an integer and integer is not a number-only accident: the
    JSON→Python mapping is exact (integer rejects True, string rejects 12345).

Deterministic: an in-process WSGI call against the synthesized entry; no LLM.
"""
from __future__ import annotations

import importlib.util
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402


_POST = '''\
def post_notes(payload, query):
    return (201, {"id": 1, "text": (payload or {}).get("text")})
'''


def _engine(tmp_path):
    return sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)


def _seed(root):
    src = pathlib.Path(root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "notes_post.py").write_text(_POST)
    return src


def _typed_fragment(field_type):
    """A decomposer IR fragment typing POST /notes' `text` field."""
    schema = {"type": "object",
              "properties": {"text": {"type": field_type}},
              "required": ["text"],
              "additionalProperties": False}
    op = {"x-spec-flow-handler": "post_notes",
          "requestBody": {"required": True,
                          "content": {"application/json": {"schema": schema}}},
          "responses": {"201": {"description": "ok",
                                 "content": {"application/json":
                                             {"schema": {}}}}}}
    openapi = {"openapi": "3.1.0", "info": {"title": "notes", "version": "1"},
               "paths": {"/notes": {"post": op}}}
    return {"notes": {"files": ["src/notes_post.py"], "openapi": openapi}}


def _synth(eng, fragment):
    eng.__dict__["_decomposer_ir_nodes"] = fragment
    contract = {"entry": "src/app.py", "callable": ["wsgi_app"],
                "routes": [["POST", "/notes"]]}
    mapping, unresolved = eng._resolve_route_handlers(contract)
    return eng._synthesize_entry_code(contract, mapping, unresolved)


def _load(code, src_dir):
    for _m in ("notes_post", "synth_app"):
        sys.modules.pop(_m, None)
    entry = pathlib.Path(src_dir) / "app.py"
    entry.write_text(code)
    sys.path.insert(0, str(src_dir))
    spec = importlib.util.spec_from_file_location("synth_app", str(entry))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _call(app, method, path, body):
    cap = {}

    def start_response(status, headers):
        cap["status"] = status

    raw = body.encode() if isinstance(body, str) else body
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
               "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
    list(app.wsgi_app(environ, start_response))
    return int(cap["status"].split()[0])


# ── S12.15 wrong type is refused at the gate ────────────────────────────────

def test_wrong_type_is_400(tmp_path):
    eng = _engine(tmp_path)
    src = _seed(eng.workspace.root)
    code = _synth(eng, _typed_fragment("string"))
    app = _load(code, src)
    assert _call(app, "POST", "/notes", '{"text": 12345}') == 400, (
        "a string field given an integer must be refused at the engine gate "
        "(F3) — not passed to the handler")


def test_correct_type_passes(tmp_path):
    eng = _engine(tmp_path)
    src = _seed(eng.workspace.root)
    code = _synth(eng, _typed_fragment("string"))
    app = _load(code, src)
    assert _call(app, "POST", "/notes", '{"text": "hello"}') == 201, (
        "a correctly-typed value must reach the handler unharmed")


def test_bool_is_not_an_integer(tmp_path):
    eng = _engine(tmp_path)
    src = _seed(eng.workspace.root)
    code = _synth(eng, _typed_fragment("integer"))
    app = _load(code, src)
    assert _call(app, "POST", "/notes", '{"text": true}') == 400, (
        "JSON true is a boolean, not an integer — the mapping is exact")


# ── GREEN direction: no declared type = no type check (honest gap) ──────────

def test_untyped_field_is_not_type_checked(tmp_path):
    eng = _engine(tmp_path)
    src = _seed(eng.workspace.root)
    frag = _typed_fragment("string")
    # strip the type: the IR records the field but no type (a prose-derived
    # shape, {f: {}}) — the engine must invent none
    frag["notes"]["openapi"]["paths"]["/notes"]["post"]["requestBody"][
        "content"]["application/json"]["schema"]["properties"]["text"] = {}
    code = _synth(eng, frag)
    app = _load(code, src)
    assert _call(app, "POST", "/notes", '{"text": 12345}') == 201, (
        "with no recorded type the gate stays presence-only — the engine "
        "invents no type (honest gap)")
