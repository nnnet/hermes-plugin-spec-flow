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
    # purge any leaf modules cached from a previous test (each test seeds its own
    # src/ in a fresh tmp dir, but sys.modules would otherwise reuse a stale one)
    for _m in ("db_layer", "notes_post", "notes_get", "ui", "about", "l0",
               "_product_logic", "synth_app", "wsgi_app", "notes_router"):
        sys.modules.pop(_m, None)
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
    assert ("POST", "/notes") in mapping
    assert ("GET", "/notes") in mapping
    assert ("GET", "/ui") in mapping
    assert ("GET", "/about") in mapping
    # /health has no dedicated handler leaf here -> left for the synth to inline;
    # it is the only route that may stay unresolved (never falsely wired).
    assert all(mp == ("GET", "/health") for mp in unresolved), unresolved
    # POST/GET on /notes resolve to DIFFERENT handlers
    assert mapping[("POST", "/notes")][1] != mapping[("GET", "/notes")][1]


def test_canonical_handler_symbol_is_deterministic():
    f = sfr._canonical_handler_symbol
    assert f("GET", "/ui") == "get_ui"
    assert f("POST", "/notes") == "post_notes"
    assert f("GET", "/health") == "get_health"
    assert f("GET", "/") == "get_root"
    assert f("GET", "/notes/{id}") == "get_notes"        # template seg dropped
    assert f("DELETE", "/orders/{oid}/items") == "delete_orders_items"


def test_resolver_reads_the_declared_binding_before_guessing(tmp_path):
    """Root fix for the name-guessing: the engine declares one canonical handler
    symbol per route; if the leaf defines it, the resolver wires it directly,
    regardless of any same-resource distractor that token-matching might score
    higher. The binding is read, not inferred."""
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "ui.py").write_text(
        "def get_ui(payload, query):\n"
        "    return (200, {'html': '<!DOCTYPE html><html></html>'})\n"
        "def render_ui_legacy(payload, query):\n"
        "    return (200, {'html': '<html>old</html>'})\n")
    contract = {
        "entry": "src/app.py", "callable": ["wsgi_app"],
        "boot": {"html_route": "/ui"}, "routes": [["GET", "/ui"]],
    }
    mapping, unresolved = eng._resolve_route_handlers(contract)
    assert mapping[("GET", "/ui")][1] == "get_ui"        # declared name wins
    assert not unresolved


def test_synth_entry_exposes_every_declared_callable_alias(tmp_path):
    """Live v103: a generated test imported the contract callable by name
    (`from app import app`) while the synthesized entry exposed only wsgi_app +
    application -> ImportError RED-ed the product. The entry must expose every
    declared callable name, all bound to the one router object."""
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "notes_endpoints.py").write_text(
        "def get_notes(payload, query):\n    return (200, {'items': []})\n")
    contract = {
        "entry": "src/app.py", "callable": ["wsgi_app", "application", "app"],
        "boot": {"ok_route": "/notes"}, "routes": [["GET", "/notes"]],
    }
    mapping, unresolved = eng._resolve_route_handlers(contract)
    code = eng._synthesize_entry_code(contract, mapping, unresolved)
    ns: dict = {}
    # the module must import its leaves from src/ on the path
    sys.path.insert(0, str(src))
    try:
        exec(compile(code, "app.py", "exec"), ns)
    finally:
        sys.path.remove(str(src))
        sys.modules.pop("notes_endpoints", None)
    for name in ("wsgi_app", "application", "app"):
        assert name in ns, "entry must expose %s" % name
    assert ns["app"] is ns["wsgi_app"] is ns["application"]


def test_html_route_resolves_to_html_handler_named_after_its_data(tmp_path):
    """Live v103: the model named the UI page handler after the DATA it renders
    (`get_notes_html`), not the route (`/ui`), so the resource-token match found
    no candidate and GET /ui stayed unresolved -> the product served 404. The
    declared HTML route must fall back to the leaf handler that actually PRODUCES
    HTML, regardless of its name."""
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "notes_endpoints.py").write_text(
        "def get_notes(payload, query):\n    return (200, {'items': []})\n"
        "def get_notes_html(payload, query):\n"
        "    html = '<!DOCTYPE html>\\n<html><body>notes</body></html>'\n"
        "    return (200, {'html': html})\n")
    contract = {
        "entry": "src/app.py", "callable": ["wsgi_app"],
        "boot": {"ok_route": "/notes", "html_route": "/ui"},
        "routes": [["GET", "/notes"], ["GET", "/ui"]],
    }
    mapping, unresolved = eng._resolve_route_handlers(contract)
    assert ("GET", "/ui") in mapping, unresolved
    assert mapping[("GET", "/ui")][1] == "get_notes_html"
    # and it must NOT steal the data route
    assert mapping[("GET", "/notes")][1] == "get_notes"


def test_html_fallback_does_not_fire_for_non_html_routes(tmp_path):
    """The HTML fallback is scoped to the declared html_route only — a different
    route with no name-matching handler stays honestly unresolved (never wired to
    an unrelated HTML-producing leaf)."""
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "pages.py").write_text(
        "def render_page(payload, query):\n"
        "    return (200, {'html': '<html>hi</html>'})\n")
    contract = {
        "entry": "src/app.py", "callable": ["wsgi_app"],
        "boot": {"html_route": "/ui"},
        "routes": [["GET", "/ui"], ["DELETE", "/orders"]],
    }
    mapping, unresolved = eng._resolve_route_handlers(contract)
    assert ("GET", "/ui") in mapping                       # html route wired
    assert ("DELETE", "/orders") in unresolved             # unrelated stays open


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


def test_route_without_resource_handler_not_falsely_wired(tmp_path):
    """Regression (live v091): a GET route with NO resource-matching handler must
    stay unresolved and 404, not be wired to get_notes just because the method
    matches. Synthesis still fires for the resolved json_roundtrip; the unbuilt
    route is omitted (honest 404) so the doctor builds a real handler."""
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "notes_post.py").write_text(
        "def post_note(payload, query):\n    return (201, {'id': 1})\n"
        "def get_notes(payload, query):\n    return (200, {'items': []})\n")
    contract = {"entry": "src/app.py", "callable": ["wsgi_app"],
                "boot": {"ok_route": "/health", "html_route": "/ui",
                         "json_roundtrip": "/notes"},
                "routes": [["GET", "/about"]]}
    mapping, unresolved = eng._resolve_route_handlers(contract)
    assert ("GET", "/ui") in unresolved          # no ui handler -> unresolved
    assert ("GET", "/about") in unresolved        # no about handler -> unresolved
    assert ("GET", "/notes") in mapping and ("POST", "/notes") in mapping
    assert mapping[("GET", "/notes")][1] == "get_notes"
    code = eng._synthesize_entry_code(contract, mapping, unresolved)
    assert code is not None                       # json_roundtrip resolved -> fire
    app = _load_entry(code, tmp_path / "x.db", src)
    st, _h, _b = _call(app, "GET", "/ui")
    assert st == 404, "unbuilt /ui must 404, never be wired to get_notes"
    st, _h, _b = _call(app, "GET", "/notes")
    assert st == 200


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


def test_neutralize_rival_entries_delegates_duplicate_app(tmp_path):
    """Phase 3 split-elimination: a second module exposing a WSGI callable beside
    the declared entry (live v089 notes_api.py) has that callable REPLACED by a
    wrapper delegating to the sole declared entry — not hard-stripped — so a
    companion test importing it keeps a working symbol. Business functions are
    kept, the declared entry is untouched, and the neutralised module is stamped
    with the sentinel so the route-redeclare gate no longer flags it."""
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "app.py").write_text(
        "def wsgi_app(environ, start_response):\n    return []\n"
        "application = wsgi_app\n")
    (src / "notes_api.py").write_text(
        "def get_notes(payload, query):\n    return (200, {'items': []})\n"
        "def wsgi_app(environ, start_response):\n    return []\n"
        "application = wsgi_app\n")
    # before: notes_api is a live rival
    assert any(f == "notes_api.py"
               for f, _ in eng._rival_wsgi_entries("src/app.py"))
    assert eng._neutralize_rival_entries("src/app.py") == 1
    rival = (src / "notes_api.py").read_text()
    assert "def wsgi_app(environ, start_response):" in rival   # still importable
    assert "from app import application" in rival              # delegates to entry
    assert "def get_notes" in rival                            # business kept
    assert sfr._NEUTRALIZED_SENTINEL in rival                  # stamped
    assert "def wsgi_app" in (src / "app.py").read_text()      # entry untouched
    # after: the sentinel makes the gate skip our own delegation stub
    assert not any(f == "notes_api.py"
                   for f, _ in eng._rival_wsgi_entries("src/app.py"))


def test_neutralized_rival_companion_test_still_imports(tmp_path):
    """Live v100 regression: the engine neutralised ``health_wsgi``'s ``wsgi_app``
    while ``tests/test_health_wsgi.py`` did ``from health_wsgi import wsgi_app`` —
    a hard strip made that an ImportError, RED-ing the assembled product on a
    phantom ``weak_implementer``. After neutralisation the symbol must still
    import AND delegate to the real entry."""
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    # the sole declared entry returns a recognisable body
    (src / "app.py").write_text(
        "def application(environ, start_response):\n"
        "    start_response('200 OK', [('Content-Type', 'text/plain')])\n"
        "    return [b'from-entry']\n"
        "wsgi_app = application\n")
    (src / "health_wsgi.py").write_text(
        "def check():\n    return True\n"
        "def wsgi_app(environ, start_response):\n"
        "    start_response('200 OK', [])\n"
        "    return [b'rival']\n")
    assert eng._neutralize_rival_entries("src/app.py") == 1
    sys.path.insert(0, str(src))
    try:
        for m in ("health_wsgi", "app"):
            sys.modules.pop(m, None)
        mod = importlib.import_module("health_wsgi")
        assert hasattr(mod, "wsgi_app")           # import does NOT raise
        captured = {}
        mod.wsgi_app({"REQUEST_METHOD": "GET", "PATH_INFO": "/health"},
                     lambda s, h: captured.setdefault("s", s))
        # the wrapper forwarded to the entry, not the old rival body
        assert captured["s"] == "200 OK"
        assert mod.check() is True                # business logic still there
    finally:
        for m in ("health_wsgi", "app"):
            sys.modules.pop(m, None)
        if str(src) in sys.path:
            sys.path.remove(str(src))


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


def test_storage_layer_fn_is_not_wired_as_handler(tmp_path):
    """Live v099 regression: a storage fn whose first param is a CONNECTION
    (insert_note(conn, text), list_notes(conn)) is NOT an HTTP handler — the
    router cannot supply a sqlite3.Connection, so wiring it makes every call
    500. The resolver must reject dep-first-param functions, leaving the route
    unresolved so the engine promotes the real raw-WSGI router instead."""
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "notes_db.py").write_text(
        "def connect():\n    return object()\n"
        "def insert_note(conn, text):\n    return 1\n"
        "def list_notes(conn):\n    return []\n")
    mapping, unresolved = eng._resolve_route_handlers(_SPLIT_CONTRACT)
    # the db-layer fns must NOT be wired to POST/GET /notes
    wired = set(mapping.values()) if mapping else set()
    assert all("insert_note" not in str(w) and "list_notes" not in str(w)
               for w in wired), f"db-layer fn wrongly wired: {mapping}"
    assert ("POST", "/notes") in unresolved and ("GET", "/notes") in unresolved
    # a clean (payload, query) handler in the SAME module is still wired
    (src / "api.py").write_text(
        "def create_note(payload, query):\n    return (201, {'id': 1})\n")
    mapping2, _ = eng._resolve_route_handlers(_SPLIT_CONTRACT)
    assert ("POST", "/notes") in mapping2, "real handler must still resolve"


# --- route-coverage promotion (live v094): the working router is a monolithic
# raw-WSGI blob in a NON-entry module; the declared entry is a /about-only decoy.
# The resolver cannot decompose the blob, so the engine PROMOTES the rival by
# delegating to it from the declared entry (+ the M1 malformed->400 net). -------
_SPLIT_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"ok_route": "/health", "json_roundtrip": "/notes"},
    "routes": [],
}

# the coder's REAL router — raw WSGI, self-contained, unguarded json.loads (M1).
_WSGI_MONOLITH = '''\
import json
_NOTES = []
def wsgi_app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")
    if path == "/health" and method == "GET":
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]
    if path == "/notes" and method == "POST":
        n = int(environ.get("CONTENT_LENGTH") or 0)
        data = json.loads(environ["wsgi.input"].read(n))   # unguarded -> M1 net
        _NOTES.append(data["text"])
        start_response("200 OK", [("Content-Type", "application/json")])
        return [json.dumps({"id": len(_NOTES)}).encode()]
    if path == "/notes" and method == "GET":
        items = [{"id": i + 1, "text": t} for i, t in enumerate(_NOTES)]
        start_response("200 OK", [("Content-Type", "application/json")])
        return [json.dumps({"items": items}).encode()]
    start_response("404 Not Found", [("Content-Type", "application/json")])
    return [b'{"error": "not found"}']
'''

# the declared entry the weak coder wrote — a decoy that serves only /about.
_DECOY_ENTRY = '''\
def wsgi_app(environ, start_response):
    if environ.get("PATH_INFO") == "/about":
        start_response("200 OK", [("Content-Type", "text/html")])
        return [b"<html>about</html>"]
    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"not found"]
'''


def _seed_split(root):
    src = pathlib.Path(root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "wsgi_app.py").write_text(_WSGI_MONOLITH)
    (src / "app.py").write_text(_DECOY_ENTRY)
    return src


def test_promote_rival_entry_emits_delegation(tmp_path):
    """The promote helper returns a thin entry that imports the rival's callable
    and keeps that module (keep_stem), so a SINGLE declared entry serves the
    contract without the engine fabricating any business logic."""
    eng = _engine(tmp_path)
    _seed_split(eng.workspace.root)
    out = eng._promote_rival_entry(_SPLIT_CONTRACT)
    assert out is not None
    code, keep = out
    assert keep == "wsgi_app"
    assert "from wsgi_app import wsgi_app as _sf_inner" in code
    assert "def wsgi_app(environ, start_response):" in code
    assert "JSONDecodeError" in code             # M1 net present
    assert "application = wsgi_app" in code


def test_promote_picks_router_over_decoy_rival(tmp_path):
    """With two rival WSGI modules, the engine promotes the one that references
    the most declared route paths (the real router), not a /about-only decoy."""
    eng = _engine(tmp_path)
    src = _seed_split(eng.workspace.root)
    (src / "extra.py").write_text(
        "def wsgi_app(environ, start_response):\n"
        "    if environ.get('PATH_INFO') == '/about':\n"
        "        start_response('200 OK', []); return [b'x']\n"
        "    start_response('404 Not Found', []); return [b'']\n")
    code, keep = eng._promote_rival_entry(_SPLIT_CONTRACT)
    assert keep == "wsgi_app"                     # the /notes,/health router wins
    assert "from wsgi_app import" in code


def test_split_router_is_promoted_and_boots_and_m1(tmp_path):
    """End-to-end: the resolver declines the monolith, so _try_synthesize_entry
    PROMOTES the rival; the assembled product boots its contract and a malformed
    body is answered 400 (the engine's M1 net), never a 500 crash."""
    eng = _engine(tmp_path)
    eng.workspace.enabled = True
    eng._constitution = [
        "HTTP through a WSGI app (src/app.py exposes `wsgi_app`).",
        "FROZEN: POST /notes takes {text} responds {id}; GET /notes responds "
        "{items:[{id,text}]}; GET /health 200.",
    ]
    src = _seed_split(eng.workspace.root)
    assert eng._try_synthesize_entry() is True
    entry_src = (src / "app.py").read_text()
    assert "engine-promoted delegation" in entry_src
    # the assembled product really boots its contract (un-mockable subprocess)
    ok, detail = eng._assembled_product_boots()
    assert ok, detail
    # M1: a malformed body is 400, not a 500 crash
    app = _load_entry(entry_src, tmp_path / "m1.db", src)
    status, _h, _b = _call(app, "POST", "/notes", body=b"{bad json")
    assert status == 400
