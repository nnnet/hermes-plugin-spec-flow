"""Phase 1 — deterministic product-entry synthesis (model-independent assembly).

The engine, not the model, owns the WSGI router. Given leaf modules that expose
high-level ``(payload, query) -> (status, body)`` handlers (the shape leaves
converge on — live v088 had all leaves like this but no entry to dispatch them),
``_resolve_route_handlers`` + ``_synthesize_entry_code`` must emit a bootable
entry that:
  * serves every declared route (no M2 "late route -> 404"),
  * rejects a malformed body with 400, never 500 (no M1),
  * exists with the contract callable at all (no M3 "no entry callable").

These run fully offline (pure AST + an in-process WSGI call), no LLM.
"""
import importlib.util
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402


# --- leaf modules in the v088 shape: business logic, NO entry callable --------
_DB_LAYER = '''\
import os, sqlite3
def _conn():
    db = os.environ.get("NOTES_DB", ":memory:")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY "
              "AUTOINCREMENT, text TEXT NOT NULL)")
    return c
def store_note(text):
    c = _conn(); cur = c.execute("INSERT INTO notes (text) VALUES (?)", (text,))
    c.commit(); nid = cur.lastrowid; c.close(); return nid
def all_notes():
    c = _conn(); rows = c.execute("SELECT id, text FROM notes ORDER BY id DESC"
                                  ).fetchall(); c.close()
    return [{"id": r[0], "text": r[1]} for r in rows]
'''

_NOTES_POST = '''\
import db_layer
def notes_post(payload, query):
    text = (payload or {}).get("text")
    if not text:
        return (400, {"error": "text required"})
    return (201, {"id": db_layer.store_note(text)})
'''

_NOTES_GET = '''\
import db_layer
def get_notes(payload, query):
    return (200, {"items": db_layer.all_notes()})
'''

_UI = '''\
def ui_get(payload, query):
    return (200, "<html><body><h1>Notes</h1></body></html>")
'''

_ABOUT = '''\
def about_page(payload, query):
    return (200, "<html><body>about</body></html>")
'''


def _seed_src(root):
    src = pathlib.Path(root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "db_layer.py").write_text(_DB_LAYER)
    (src / "notes_post.py").write_text(_NOTES_POST)
    (src / "notes_get.py").write_text(_NOTES_GET)
    (src / "ui.py").write_text(_UI)
    (src / "about.py").write_text(_ABOUT)


_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app", "application", "app"],
    "boot": {"ok_route": "/health", "html_route": "/ui",
             "json_roundtrip": "/notes"},
    "routes": [["GET", "/about"]],
}


def _engine(tmp_path):
    return sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)


def _load_entry(code, db_path, src_dir):
    """Materialise the synthesized entry next to the leaves and import it."""
    import os
    os.environ["NOTES_DB"] = str(db_path)
    entry = pathlib.Path(src_dir) / "app.py"
    entry.write_text(code)
    sys.path.insert(0, str(src_dir))
    spec = importlib.util.spec_from_file_location("synth_app", str(entry))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _call(app, method, path, body=None, query=""):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    raw = body if isinstance(body, bytes) else (body.encode() if body else b"")
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
    }
    chunks = app.wsgi_app(environ, start_response)
    out = b"".join(chunks)
    return int(captured["status"].split()[0]), captured["headers"], out


# ---------------------------------------------------------------------------

def test_resolver_maps_every_declared_route(tmp_path):
    eng = _engine(tmp_path)
    _seed_src(eng.workspace.root)
    mapping, unresolved = eng._resolve_route_handlers(_CONTRACT)
    assert unresolved == [], "all declared routes must resolve, got %r" % unresolved
    assert ("POST", "/notes") in mapping
    assert ("GET", "/notes") in mapping
    assert ("GET", "/ui") in mapping
    assert ("GET", "/about") in mapping
    # POST/GET on /notes resolve to DIFFERENT handlers
    assert mapping[("POST", "/notes")][1] != mapping[("GET", "/notes")][1]


def test_synth_entry_boots_and_serves_contract(tmp_path):
    eng = _engine(tmp_path)
    _seed_src(eng.workspace.root)
    mapping, unresolved = eng._resolve_route_handlers(_CONTRACT)
    code = eng._synthesize_entry_code(_CONTRACT, mapping, unresolved)
    assert code is not None
    src_dir = pathlib.Path(eng.workspace.root) / "src"
    app = _load_entry(code, tmp_path / "notes.db", src_dir)

    # health (inline-synthesized, no leaf needed)
    st, _h, _b = _call(app, "GET", "/health")
    assert st == 200

    # POST then GET round-trip, newest-first
    st, _h, _b = _call(app, "POST", "/notes", body='{"text": "first"}')
    assert st == 201, _b
    st, _h, _b = _call(app, "POST", "/notes", body='{"text": "second"}')
    assert st == 201
    st, h, b = _call(app, "GET", "/notes")
    assert st == 200 and "application/json" in h["Content-Type"]
    import json
    items = json.loads(b)["items"]
    assert [i["text"] for i in items] == ["second", "first"]  # newest first

    # web /ui is HTML
    st, h, b = _call(app, "GET", "/ui")
    assert st == 200 and "text/html" in h["Content-Type"]
    assert b"<html" in b.lower()

    # late declared route /about wired (no 404)
    st, _h, _b = _call(app, "GET", "/about")
    assert st == 200


def test_malformed_body_is_400_never_500(tmp_path):
    eng = _engine(tmp_path)
    _seed_src(eng.workspace.root)
    mapping, unresolved = eng._resolve_route_handlers(_CONTRACT)
    code = eng._synthesize_entry_code(_CONTRACT, mapping, unresolved)
    src_dir = pathlib.Path(eng.workspace.root) / "src"
    app = _load_entry(code, tmp_path / "n.db", src_dir)
    st, _h, _b = _call(app, "POST", "/notes", body=b"not-json{")
    assert st == 400, "malformed body must be 4xx, got %s" % st


def test_unknown_route_404_wrong_method_405(tmp_path):
    eng = _engine(tmp_path)
    _seed_src(eng.workspace.root)
    mapping, unresolved = eng._resolve_route_handlers(_CONTRACT)
    code = eng._synthesize_entry_code(_CONTRACT, mapping, unresolved)
    src_dir = pathlib.Path(eng.workspace.root) / "src"
    app = _load_entry(code, tmp_path / "n.db", src_dir)
    st, _h, _b = _call(app, "GET", "/nope")
    assert st == 404
    st, _h, _b = _call(app, "DELETE", "/notes")
    assert st == 405


def test_entry_exposes_callable_detects_missing(tmp_path):
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    # file present but NO contract callable (the live v088 failure)
    (src / "app.py").write_text("def helper():\n    return 1\n")
    assert eng._entry_exposes_callable("src/app.py",
                                       ["wsgi_app", "application", "app"]) is False
    (src / "app.py").write_text("def wsgi_app(environ, start_response):\n"
                                "    return []\n")
    assert eng._entry_exposes_callable("src/app.py",
                                       ["wsgi_app", "application", "app"]) is True


_MONOLITH_APP = '''\
import os, json, sqlite3
def _db():
    c = sqlite3.connect(os.environ.get("NOTES_DB", ":memory:"))
    c.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY "
              "AUTOINCREMENT, text TEXT NOT NULL)")
    return c
def handle_health(payload, query):
    return (200, {"status": "ok"})
def post_note(payload, query):
    t = (payload or {}).get("text")
    if not t:
        return (400, {"error": "text required"})
    c = _db(); cur = c.execute("INSERT INTO notes (text) VALUES (?)", (t,))
    c.commit(); nid = cur.lastrowid; c.close(); return (201, {"id": nid})
def get_notes(payload, query):
    c = _db(); rows = c.execute("SELECT id, text FROM notes ORDER BY id DESC"
                                ).fetchall(); c.close()
    return (200, {"items": [{"id": r[0], "text": r[1]} for r in rows]})
def ui_get(payload, query):
    return (200, "<html><body>notes</body></html>")
def about_page(payload, query):
    return (200, "<html>about</html>")
def wsgi_app(environ, start_response):   # the model's own (to be replaced)
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"monolith"]
application = wsgi_app
'''


def test_monolithic_entry_is_harvested_then_synthesized(tmp_path):
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    # everything inside the entry, NO separate handler leaves (live v089 shape)
    (src / "app.py").write_text(_MONOLITH_APP)

    # resolver alone finds nothing (it excludes the entry)
    mapping, unresolved = eng._resolve_route_handlers(_CONTRACT)
    assert ("POST", "/notes") in unresolved

    # harvest relocates the business logic, then re-resolve wires it
    assert eng._harvest_entry_handlers(_CONTRACT) is True
    assert (src / "_product_logic.py").is_file()
    mapping2, unresolved2 = eng._resolve_route_handlers(_CONTRACT)
    assert unresolved2 == [], "after harvest every route must resolve"
    code = eng._synthesize_entry_code(_CONTRACT, mapping2, unresolved2)
    assert code is not None

    app = _load_entry(code, tmp_path / "m.db", src)
    st, _h, _b = _call(app, "GET", "/health")
    assert st == 200
    st, _h, _b = _call(app, "POST", "/notes", body='{"text": "x"}')
    assert st == 201
    st, _h, b = _call(app, "GET", "/notes")
    assert st == 200 and b"x" in b
    st, _h, _b = _call(app, "POST", "/notes", body=b"bad{")
    assert st == 400


def test_harden_entry_wraps_unguarded_json_loads(tmp_path):
    """Phase 2 net: an LLM entry that parses the body without try/except must be
    wrapped so a malformed body answers 400, never 500. Idempotent."""
    eng = _engine(tmp_path)
    eng.workspace.enabled = True
    eng._product_contract = lambda: _CONTRACT      # bypass constitution parse
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "app.py").write_text(
        "import json\n"
        "def wsgi_app(environ, start_response):\n"
        "    n = int(environ.get('CONTENT_LENGTH') or 0)\n"
        "    body = environ['wsgi.input'].read(n)\n"
        "    json.loads(body)            # unguarded -> 500 on malformed\n"
        "    start_response('200 OK', [('Content-Type', 'application/json')])\n"
        "    return [b'{}']\n"
    )
    assert eng._harden_entry() is True
    hardened = (src / "app.py").read_text()
    assert "_spec_flow_hardened" in hardened and "application = wsgi_app" in hardened
    app = _load_entry(hardened, tmp_path / "h.db", src)
    st, _h, _b = _call(app, "POST", "/notes", body=b"not-json{")
    assert st == 400, "hardened entry must answer 400 on malformed body, got %s" % st
    # a well-formed body still flows through untouched
    st, _h, _b = _call(app, "POST", "/notes", body=b'{"text": "ok"}')
    assert st == 200
    assert eng._harden_entry() is True             # idempotent, no double-wrap
    assert (src / "app.py").read_text().count("_spec_flow_hardened") == 1


def test_json_parse_unguarded_detector():
    """Phase 4 selector bias: flag an entry that parses the body without a guard,
    pass one that wraps it (or that does not read the body at all)."""
    import spec_flow_tools as sft
    unguarded = (
        "import json\n"
        "def wsgi_app(environ, start_response):\n"
        "    body = environ['wsgi.input'].read()\n"
        "    data = json.loads(body)\n"
        "    return []\n"
    )
    assert sft.json_parse_unguarded(unguarded) is not None
    guarded = (
        "import json\n"
        "def wsgi_app(environ, start_response):\n"
        "    body = environ['wsgi.input'].read()\n"
        "    try:\n"
        "        data = json.loads(body)\n"
        "    except Exception:\n"
        "        data = {}\n"
        "    return []\n"
    )
    assert sft.json_parse_unguarded(guarded) is None
    # a non-body module (a pure handler leaf) is never flagged
    leaf = "def get_notes(payload, query):\n    return (200, {'items': []})\n"
    assert sft.json_parse_unguarded(leaf) is None


def test_neutralize_rival_entries_strips_duplicate_app(tmp_path):
    """Phase 3 split-elimination: a second module exposing a WSGI callable beside
    the declared entry (live v089 notes_api.py) is stripped of that callable while
    its business functions are kept; the declared entry is left untouched."""
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "app.py").write_text(
        "def wsgi_app(environ, start_response):\n    return []\n")
    (src / "notes_api.py").write_text(
        "def get_notes(payload, query):\n    return (200, {'items': []})\n"
        "def wsgi_app(environ, start_response):\n    return []\n"
        "application = wsgi_app\n")
    assert eng._neutralize_rival_entries("src/app.py") == 1
    rival = (src / "notes_api.py").read_text()
    assert "def wsgi_app" not in rival
    assert "application = wsgi_app" not in rival
    assert "def get_notes" in rival          # business logic preserved
    assert "def wsgi_app" in (src / "app.py").read_text()   # entry untouched


def test_synth_returns_none_when_critical_route_unresolved(tmp_path):
    eng = _engine(tmp_path)
    # only a health leaf, no notes/ui/about handlers -> json_roundtrip unresolved
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "h.py").write_text("def get_health(payload, query):\n"
                              "    return (200, {'status': 'ok'})\n")
    mapping, unresolved = eng._resolve_route_handlers(_CONTRACT)
    assert ("POST", "/notes") in unresolved
    code = eng._synthesize_entry_code(_CONTRACT, mapping, unresolved)
    assert code is None, "must fall back to LLM when a critical route is unresolved"
