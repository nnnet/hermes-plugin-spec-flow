"""Phase 2 (deterministic, hard) — a leaf must DEFINE the canonical handler it
was contracted to expose by the route -> handler binding.

A leaf that owns POST /notes is told to expose ``def post_notes(payload, query)``.
If the module it wrote does NOT define that symbol (the v120 break: get_notes was
present, post_notes never was), the handler gate fails it at the LEAF, naming the
exact missing handler -- instead of the gap surfacing only later at assembly.
AST-only: no import, no execution, no model opinion. The gate asks for EXACTLY
what the binding ordered (same ownership derivation), so it never demands a
handler the leaf was not contracted to build and never fires on a non-HTTP leaf.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import spec_flow_runner as sfr  # noqa: E402


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    ws = eng.workspace
    ws.enabled = True
    ws.root = str(tmp_path / "wk")
    root = pathlib.Path(ws.root)
    (root / "src").mkdir(parents=True, exist_ok=True)
    # isolate the gate from the doctor/trace plumbing
    eng.emit = lambda *a, **k: None
    eng._doctor_advise = lambda *a, **k: None
    eng.loops = []
    eng._product_contract = lambda: {
        "entry": "src/app.py",
        "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
        "routes": [],
    }
    return eng, root


def _leaf(text):
    return {"id": "notes_api", "title": "Notes API",
            "requirement": text, "spec_markdown": ""}


def test_owning_leaf_with_both_handlers_passes(tmp_path):
    eng, root = _engine(tmp_path)
    (root / "src" / "notes_api.py").write_text(
        "def post_notes(payload, query):\n    return (201, {})\n"
        "def get_notes(payload, query):\n    return (200, [])\n",
        encoding="utf-8")
    node = _leaf("Expose POST /notes and GET /notes returning JSON")
    assert eng._leaf_handler_gate(node, "notes_api", 1, "src/notes_api.py") is True
    assert not any(l.get("type") == "missing-handler" for l in eng.loops)


def test_missing_post_handler_fails(tmp_path):
    # the v120 break: GET present, POST contracted but never defined
    eng, root = _engine(tmp_path)
    (root / "src" / "notes_api.py").write_text(
        "def get_notes(payload, query):\n    return (200, [])\n",
        encoding="utf-8")
    node = _leaf("Expose POST /notes and GET /notes returning JSON")
    assert eng._leaf_handler_gate(node, "notes_api", 1, "src/notes_api.py") is False
    miss = [l for l in eng.loops if l.get("type") == "missing-handler"]
    assert miss and "post_notes" in miss[0]["detail"]


def test_non_http_leaf_is_noop(tmp_path):
    eng, root = _engine(tmp_path)
    (root / "src" / "date_utils.py").write_text(
        "def format_iso(d):\n    return str(d)\n", encoding="utf-8")
    node = _leaf("Format and parse ISO dates")          # owns no declared route
    assert eng._leaf_handler_gate(node, "date_utils", 1, "src/date_utils.py") is True
    assert eng.loops == []


def test_dependency_first_param_is_not_a_handler(tmp_path):
    # a storage fn `post_notes(conn, text)` carries the canonical NAME but is not
    # dispatchable as (payload, query) -- the router would pass a dict where a
    # connection is expected. Treated as missing so the route stays honest-red.
    eng, root = _engine(tmp_path)
    (root / "src" / "notes_api.py").write_text(
        "def post_notes(conn, text):\n    return 1\n"
        "def get_notes(payload, query):\n    return (200, [])\n",
        encoding="utf-8")
    node = _leaf("POST /notes and GET /notes")
    assert eng._leaf_handler_gate(node, "notes_api", 1, "src/notes_api.py") is False


def test_single_param_handler_is_accepted(tmp_path):
    # arity stays lenient: the resolver adapts a one-param `(query)` handler, so
    # the gate must not red a leaf merely for an off-by-one arg list -- the real
    # failure mode is the NAME absent, not the parameter count.
    eng, root = _engine(tmp_path)
    (root / "src" / "notes_api.py").write_text(
        "def post_notes(payload):\n    return (201, {})\n"
        "def get_notes(query):\n    return (200, [])\n",
        encoding="utf-8")
    node = _leaf("POST /notes and GET /notes")
    assert eng._leaf_handler_gate(node, "notes_api", 1, "src/notes_api.py") is True


def test_missing_file_reports_all_owned(tmp_path):
    eng, root = _engine(tmp_path)            # no src/notes_api.py written
    node = _leaf("POST /notes and GET /notes")
    assert eng._leaf_handler_gate(node, "notes_api", 1, "src/notes_api.py") is False
    miss = [l for l in eng.loops if l.get("type") == "missing-handler"]
    assert miss and "post_notes" in miss[0]["detail"] and "get_notes" in miss[0]["detail"]


def test_no_code_rel_is_noop(tmp_path):
    eng, root = _engine(tmp_path)
    node = _leaf("POST /notes")
    assert eng._leaf_handler_gate(node, "notes_api", 1, None) is True
    assert eng.loops == []
