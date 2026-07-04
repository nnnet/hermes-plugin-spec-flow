"""Audit rule S12.14 (v164): the ENGINE'S OWN router is never a launderer —
an exception escaping a leaf handler is an HONEST 500 naming the exception,
NEVER fabricated request validation.

v164 (2026-07-03T23-02-13__v164__p6-micro-notes): the v159/v161 "400
'missing required field: NOTES_DB'" class landed a THIRD time — and this
time the laundering wrapper was the engine's own synthesized router
(`_synthesize_entry_code`): its dispatch mapped ANY KeyError raised inside a
handler to 400 {"error": "missing required field: %s"}. src/core.py read
``os.environ['NOTES_DB']`` inside every handler, so with NOTES_DB unset the
assembled product answered POST /notes, GET /notes and even bodyless GET /ui
with a fabricated client error. The S12.6 boot probe correctly red-ed the
behaviour, but the defect source was the engine template itself, not model
code.

Contract enforced (deterministic, the router template is engine-owned):
  * 400 "missing required field: '<f>'" comes ONLY from the router's OWN
    request validation against the contracted request shape (the S12.1
    `_route_request_fields` datum) — it can therefore only ever name a
    CONTRACTED field, never a config env var;
  * an exception ESCAPING a handler — a config KeyError from os.environ
    included — is an honest 500 {"error": "internal: KeyError: 'NOTES_DB'"}
    naming the exception type and message;
  * GREEN edges: a malformed JSON body is still a 400; a handler EXPLICITLY
    returning (400, {...}) is passed through untouched; a normal 200/201 is
    untouched; a genuinely missing CONTRACTED field is still a 400 naming
    that field.

Deterministic: pure synthesis + in-process WSGI calls, no LLM.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_GOAL = ('A tiny notes service over a WSGI app: POST /notes accepts '
         '{"text": "..."} and stores it; GET /notes returns {"items": [...]}.')

_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [],
}

# the v164 shape: handlers read the NOTES_DB env var with a HARD subscript —
# with the var unset every call raises KeyError('NOTES_DB') INSIDE the handler
_NOTES_ENV_SUBSCRIPT = '''\
import os
import sqlite3


def _db():
    conn = sqlite3.connect(os.environ['NOTES_DB'])
    conn.execute("CREATE TABLE IF NOT EXISTS notes "
                 "(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)")
    return conn


def post_notes(payload, query):
    if payload.get("text") == "":
        return 400, {"error": "text required"}
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
'''


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    # the real run always carries the goal — the S12.1 request-shape datum
    # derives ['text'] for POST /notes from it
    eng._goal = _GOAL
    return eng


def _synthesize(eng):
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "notes.py").write_text(_NOTES_ENV_SUBSCRIPT, encoding="utf-8")
    mapping, unresolved = eng._resolve_route_handlers(_CONTRACT)
    code = eng._synthesize_entry_code(_CONTRACT, mapping, unresolved)
    assert code, "the deterministic entry must synthesize for this shape"
    return code, src


def _load_entry(code, src_dir):
    for m in ("notes", "_s1214_app"):
        sys.modules.pop(m, None)
    entry = pathlib.Path(src_dir) / "app.py"
    entry.write_text(code, encoding="utf-8")
    sys.path.insert(0, str(src_dir))
    try:
        spec = importlib.util.spec_from_file_location("_s1214_app", str(entry))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(src_dir))
    return mod


def _call(app, method, path, body=None):
    captured = {}

    def start_response(status, headers):
        captured["status"] = int(str(status).split()[0])

    raw = body if isinstance(body, bytes) else (
        json.dumps(body).encode() if body is not None else b"")
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path,
               "QUERY_STRING": "", "CONTENT_LENGTH": str(len(raw)),
               "wsgi.input": io.BytesIO(raw)}
    chunks = app.wsgi_app(environ, start_response)
    return captured["status"], b"".join(chunks).decode("utf-8", "replace")


# ---- RED: the v164 launderer -------------------------------------------------

def test_config_keyerror_is_honest_500_not_fabricated_400(tmp_path):
    eng = _engine(tmp_path)
    code, src = _synthesize(eng)
    os.environ.pop("NOTES_DB", None)
    app = _load_entry(code, src)
    st, body = _call(app, "GET", "/notes")
    assert st == 500, (
        "v164: a KeyError('NOTES_DB') escaping the handler (os.environ "
        "subscript, config starvation) must surface as an HONEST 500 — the "
        "old router branch laundered it into 400 'missing required field', "
        "fabricating request validation; got %s %r" % (st, body))
    assert "KeyError" in body and "NOTES_DB" in body, (
        "the 500 must NAME the exception (internal: KeyError: 'NOTES_DB'), "
        "got %r" % body)
    assert "missing required field" not in body, (
        "the router must never fabricate request validation for a handler "
        "exception, got %r" % body)


def test_bodyless_route_config_keyerror_also_500(tmp_path):
    # the v161/v164 tell: even a BODYLESS GET carried the fabricated field
    # demand — after the fix it is the same honest 500
    eng = _engine(tmp_path)
    code, src = _synthesize(eng)
    os.environ.pop("NOTES_DB", None)
    app = _load_entry(code, src)
    st, body = _call(app, "GET", "/notes")
    assert st == 500 and "missing required field" not in body, (st, body)


# ---- GREEN: legitimate 400s and normal paths stay untouched -------------------

def test_malformed_json_body_still_400(tmp_path):
    eng = _engine(tmp_path)
    code, src = _synthesize(eng)
    os.environ["NOTES_DB"] = str(tmp_path / "g1.db")
    app = _load_entry(code, src)
    st, body = _call(app, "POST", "/notes", body=b"not-json{")
    assert st == 400, (
        "a malformed request body is a genuine client error — 400 stays; "
        "got %s %r" % (st, body))


def test_handler_explicit_400_is_preserved(tmp_path):
    eng = _engine(tmp_path)
    code, src = _synthesize(eng)
    os.environ["NOTES_DB"] = str(tmp_path / "g2.db")
    app = _load_entry(code, src)
    # 'text' is present (contracted shape satisfied) but empty — the HANDLER
    # itself returns (400, {...}); the router must pass it through untouched
    st, body = _call(app, "POST", "/notes", body={"text": ""})
    assert st == 400 and "text required" in body, (st, body)


def test_missing_contracted_field_still_400_naming_it(tmp_path):
    # the engine-owned request validation (S12.1 datum) keeps the v141
    # guarantee: a genuinely missing CONTRACTED field is a 400 naming it —
    # and ONLY a contracted field can ever be named
    eng = _engine(tmp_path)
    code, src = _synthesize(eng)
    os.environ["NOTES_DB"] = str(tmp_path / "g3.db")
    app = _load_entry(code, src)
    st, body = _call(app, "POST", "/notes", body={"note": "wrong field"})
    assert st == 400 and "text" in body, (
        "a missing CONTRACTED field ('text') is genuine request validation "
        "— 400 naming the field stays; got %s %r" % (st, body))
    assert "NOTES_DB" not in body


def test_normal_200_201_untouched(tmp_path):
    eng = _engine(tmp_path)
    code, src = _synthesize(eng)
    os.environ["NOTES_DB"] = str(tmp_path / "g4.db")
    app = _load_entry(code, src)
    st, _ = _call(app, "POST", "/notes", body={"text": "hello"})
    assert st == 201
    st, body = _call(app, "GET", "/notes")
    assert st == 200 and "hello" in body
