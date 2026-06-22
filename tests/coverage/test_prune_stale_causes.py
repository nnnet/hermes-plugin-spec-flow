"""Re-validate open doctor causes against the FINAL artifact (v067/v068 false
RED). A cause opened mid-run but reworked-real on a path that never re-ran its
gate must be CLOSED at completion — else integrate vetoes a product that serves
every route. Honest: dropped only when the artifact positively satisfies the
cause, never blanket-cleared."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from spec_flow_runner import Engine, Workspace   # noqa: E402


class _DoctorOn:
    enabled = True


def _engine(tmp):
    e = Engine.__new__(Engine)
    e.workspace = Workspace(root=tmp, enabled=True).open()
    e._doctor = _DoctorOn()
    e.loops = []
    e.emit = lambda *a, **k: None
    e._module_for = lambda nid: nid
    return e


def _write(tmp, rel, body):
    p = os.path.join(tmp, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write(body)


# --- _module_has_real_code ----------------------------------------------------

def test_real_code_true():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/m.py", "def f():\n    return 1\n")
        assert e._module_has_real_code("src/m.py") is True


def test_comment_only_false():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/m.py", "# nothing\n\n")
        assert e._module_has_real_code("src/m.py") is False


def test_missing_false():
    with tempfile.TemporaryDirectory() as tmp:
        assert _engine(tmp)._module_has_real_code("src/gone.py") is False


def test_route_handler_present_true():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/web.py", "def about_handler(e, s):\n    return [b'x']\n")
        assert e._module_has_real_code("src/web.py", ["/about"]) is True


def test_route_handler_missing_false():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/web.py", "def index(e, s):\n    return [b'x']\n")
        assert e._module_has_real_code("src/web.py", ["/about"]) is False


# --- _prune_stale_causes ------------------------------------------------------

def test_stale_empty_delta_closed():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/api.py", "def create_note(t):\n    return 1\n")
        e._doctor_states = {"note_search": {"last_cause": "empty_delta",
                                            "delta_path": "src/api.py"}}
        e._prune_stale_causes()
        assert e._doctor_states["note_search"]["last_cause"] is None


def test_genuine_empty_delta_stays_open():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/api.py", "# still hollow\n")
        e._doctor_states = {"n": {"last_cause": "empty_delta",
                                  "delta_path": "src/api.py"}}
        e._prune_stale_causes()
        assert e._doctor_states["n"]["last_cause"] == "empty_delta"


def test_declared_route_still_missing_stays_open():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/web.py", "def index(e, s):\n    return [b'x']\n")
        e._doctor_states = {"about": {"last_cause": "empty_delta",
                                      "delta_path": "src/web.py",
                                      "delta_routes": ["/about"]}}
        e._prune_stale_causes()
        assert e._doctor_states["about"]["last_cause"] == "empty_delta"


def test_vague_spec_entry_closed_when_boots():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        e._assembled_product_boots = lambda: (True, "")
        e._doctor_states = {"product_entry": {"last_cause": "vague_spec"}}
        e._prune_stale_causes()
        assert e._doctor_states["product_entry"]["last_cause"] is None


def test_vague_spec_entry_stays_when_not_boots():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        e._assembled_product_boots = lambda: (False, "GET /about -> 404")
        e._doctor_states = {"product_entry": {"last_cause": "vague_spec"}}
        e._prune_stale_causes()
        assert e._doctor_states["product_entry"]["last_cause"] == "vague_spec"


def test_unrelated_cause_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        _write(tmp, "src/x.py", "def f():\n    return 1\n")
        e._doctor_states = {"n": {"last_cause": "weak_implementer",
                                  "delta_path": "src/x.py"}}
        e._prune_stale_causes()
        assert e._doctor_states["n"]["last_cause"] == "weak_implementer"


def test_disabled_doctor_noop():
    with tempfile.TemporaryDirectory() as tmp:
        e = _engine(tmp)
        e._doctor = None
        e._doctor_states = {"n": {"last_cause": "empty_delta",
                                  "delta_path": "src/api.py"}}
        e._prune_stale_causes()                 # must not raise
        assert e._doctor_states["n"]["last_cause"] == "empty_delta"
