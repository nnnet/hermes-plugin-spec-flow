"""437: delta as acceptance. A late-requirement leaf that produced no real
delta (its owned module is missing or only blanks/comments) must NOT pass as
implemented — the gate records an empty-delta loop and routes the empty_delta
cause to the doctor. Decided by the artifact, not the model. Inert for ordinary
nodes."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from spec_flow_runner import Engine, Workspace   # noqa: E402


def _engine(tmp):
    e = Engine.__new__(Engine)
    e.workspace = Workspace(root=tmp, enabled=True).open()
    e.review_policy = {"min_delta_lines": 1}
    e.loops = []
    e._doctor_calls = []
    e._doctor_advise = lambda *a, **k: e._doctor_calls.append((a, k)) or None
    e.emit = lambda *a, **k: None
    return e


def _write(tmp, rel, body):
    p = os.path.join(tmp, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write(body)


def test_non_late_node_is_inert():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        assert e._late_req_delta_gate({"id": "n"}, "n", 4, "src/x.py") is True
        assert e.loops == [] and e._doctor_calls == []


def test_missing_owner_file_is_empty_delta():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        ok = e._late_req_delta_gate({"_late_req": True}, "n", 4, "src/gone.py")
        assert ok is False
        assert any(lp["type"] == "empty-delta" for lp in e.loops)
        assert e._doctor_calls and e._doctor_calls[0][0][3] == "delta_gate"


def test_blank_and_comment_only_is_empty_delta():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/hollow.py", "\n\n# just a comment\n   \n")
        ok = e._late_req_delta_gate({"_late_req": True}, "n", 4, "src/hollow.py")
        assert ok is False
        # the finding fed to the doctor names an empty delta the detector reads
        ev = e._doctor_calls[0][0][5]
        assert "adds no new symbol" in ev["scope_findings"][0]


def test_real_code_passes():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/real.py", "def handler(req):\n    return 200, {}\n")
        ok = e._late_req_delta_gate({"_late_req": True}, "n", 4, "src/real.py")
        assert ok is True
        assert e.loops == [] and e._doctor_calls == []


def test_declared_route_without_handler_is_empty_delta():
    # v062 regression: a late req declares GET /about but the owned module (with
    # existing /ui code) gains NO about handler -> the line floor passes yet the
    # route 404s. The route-level delta check must reject it.
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/web_ui.py",
               "def render_notes_html(notes):\n    return '<html>'\n"
               "def handle_get_ui(environ, sr):\n    sr('200 OK', [])\n    return [b'x']\n")
        node = {"_late_req": True,
                "title": "ADD AN ABOUT PAGE. Serve GET /about as an HTML page."}
        ok = e._late_req_delta_gate(node, "about_page", 4, "src/web_ui.py")
        assert ok is False
        assert any("/about" in str(lp.get("detail", "")) for lp in e.loops)
        assert e._doctor_calls and e._doctor_calls[-1][0][3] == "delta_gate"


def test_declared_route_with_handler_passes():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/web_ui.py",
               "def handle_get_ui(environ, sr):\n    return [b'x']\n"
               "def about_handler(environ, sr):\n    sr('200 OK', [])\n    return [b'about']\n")
        node = {"_late_req": True,
                "title": "ADD AN ABOUT PAGE. Serve GET /about as an HTML page."}
        ok = e._late_req_delta_gate(node, "about_page", 4, "src/web_ui.py")
        assert ok is True
        assert e.loops == []


def test_late_req_without_route_is_inert_when_nonempty():
    # a late req that declares NO HTTP route (e.g. 'make notes nicer') only needs
    # a non-empty delta — the route check must not false-fire.
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/web_ui.py", "def prettify(n):\n    return n.upper()\n")
        node = {"_late_req": True, "title": "MAKE THE NOTES NICER TO READ."}
        ok = e._late_req_delta_gate(node, "nice", 4, "src/web_ui.py")
        assert ok is True and e.loops == []


def test_sibling_served_route_is_not_charged_to_this_node():
    # v110 root: a late node (красивый_вид) whose spec NARRATES the whole service
    # ('/notes', '/ui') but whose OWN delta is a presentation tweak must NOT be
    # charged with routes a SIBLING module already serves. The false REJECT here
    # reworked the owner module and broke the synthesised app.py (ImportError).
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        # a sibling already serves /notes and /ui via code dispatch
        _write(tmp, "src/notes_service.py",
               "def app(environ, sr):\n"
               "    if environ['PATH_INFO'] == '/notes':\n        return []\n"
               "    if environ['PATH_INFO'] == '/ui':\n        return []\n")
        # this node's own module has real (non-route) code — a real delta
        _write(tmp, "src/pretty.py",
               "def prettify(items):\n    return sorted(items)\n")
        _write(tmp, "specs/pretty.md",
               "MAKE THE NOTES NICE TO READ. The service serves GET /notes and "
               "GET /ui; present the notes returned by GET /notes nicely.")
        node = {"_late_req": True, "title": "MAKE THE NOTES NICE TO READ."}
        ok = e._late_req_delta_gate(node, "pretty", 4, "src/pretty.py")
        assert ok is True                       # not charged with /notes or /ui
        assert e.loops == [] and e._doctor_calls == []


def test_own_new_route_still_enforced_when_sibling_absent():
    # the guard only drops SIBLING-served routes: a genuinely NEW route this node
    # declares (and nobody else serves) is still enforced — v062 must hold.
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/web_ui.py",
               "def handle_get_ui(environ, sr):\n    return [b'x']\n")
        node = {"_late_req": True,
                "title": "ADD AN ABOUT PAGE. Serve GET /about as an HTML page."}
        ok = e._late_req_delta_gate(node, "about_page", 4, "src/web_ui.py")
        assert ok is False                      # /about is new, unserved -> FAIL
        assert any("/about" in str(lp.get("detail", "")) for lp in e.loops)


class _DoctorOn:
    enabled = True


def test_pass_closes_stale_empty_delta_cause():
    # FALSE-RED root (v067): a late-req leaf first ran hollow -> empty_delta cause
    # opened; the doctor reworked it; this re-run now passes -> the gate MUST
    # close the stale cause, else integrate records a false RED on a working product.
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        e._doctor = _DoctorOn()
        e._doctor_states = {"about_page": {"last_cause": "empty_delta"}}
        _write(tmp, "src/about_page.py",
               "def about_handler(environ, sr):\n"
               "    sr('200 OK', [])\n    return [b'about']\n")
        node = {"_late_req": True,
                "title": "ADD AN ABOUT PAGE. Serve GET /about as an HTML page."}
        ok = e._late_req_delta_gate(node, "about_page", 4, "src/web_ui_about.py"
                                    if False else "src/about_page.py")
        assert ok is True
        assert e._doctor_states["about_page"]["last_cause"] is None


def test_pass_does_not_close_unrelated_cause():
    # if the open cause is NOT a delta cause, a passing delta gate must leave it
    # alone (it is a different, still-real problem).
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        e._doctor = _DoctorOn()
        e._doctor_states = {"n": {"last_cause": "weak_implementer"}}
        _write(tmp, "src/real.py", "def handler(req):\n    return 200, {}\n")
        ok = e._late_req_delta_gate({"_late_req": True}, "n", 4, "src/real.py")
        assert ok is True
        assert e._doctor_states["n"]["last_cause"] == "weak_implementer"
