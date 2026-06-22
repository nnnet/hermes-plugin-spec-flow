"""The integrate gate's only physical lever is to rebuild the assembled entry
via a real worker. So _attempt_integrate_repair must enact the whole rebuild
family the doctor can prescribe (reconcile_check / rework / escalate_tier) — not
only reconcile_check — else a fail diagnosed as weak_implementer (ladder
rework→escalate) is analysed but never acted on (the v050 RED). escalate_tier
rebuilds on the strong tier."""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from spec_flow_runner import Engine   # noqa: E402


def _eng(remedy_kind):
    e = Engine.__new__(Engine)
    e._product_contract = lambda: {"entry": "src/app.py"}
    e._doctor_advise = lambda *a, **k: types.SimpleNamespace(kind=remedy_kind)
    e._reconcile_calls = []
    e._remedy_reconcile_check = (
        lambda entry, escalate=False: e._reconcile_calls.append((entry, escalate)) or True)
    return e


def test_reconcile_check_triggers_rebuild():
    e = _eng("reconcile_check")
    assert e._attempt_integrate_repair("boot RED /ui 404") is True
    assert e._reconcile_calls == [("src/app.py", False)]


def test_rework_triggers_rebuild():
    e = _eng("rework")
    assert e._attempt_integrate_repair("ping 404") is True
    assert e._reconcile_calls == [("src/app.py", False)]


def test_escalate_tier_rebuilds_on_strong():
    e = _eng("escalate_tier")
    assert e._attempt_integrate_repair("weak_implementer") is True
    assert e._reconcile_calls == [("src/app.py", True)]   # escalate=True


def test_non_rebuild_remedy_does_nothing():
    e = _eng("ask_human")
    assert e._attempt_integrate_repair("blocked") is False
    assert e._reconcile_calls == []


def test_no_contract_no_repair():
    e = _eng("reconcile_check")
    e._product_contract = lambda: {}        # non-web / no entry derivable
    assert e._attempt_integrate_repair("x") is False
    assert e._reconcile_calls == []


# --- module-targeted heal: blame the feature module, not only the entry -------

_TRACEBACK = (
    "FAILED tests/test_app.py::TestApp::test_post_notes_creates_note\n"
    "  File \"src/app.py\", line 27, in wsgi_app\n"
    "    status, resp = notes_api.create_note(payload, q)\n"
    "  File \"src/notes_api.py\", line 20, in create_note\n"
    "    conn.execute('INSERT INTO notes ...')\n"
    "sqlite3.OperationalError: no such table: notes\n")


def _eng_blame():
    e = Engine.__new__(Engine)
    e.tasks = {"notes_api": object(), "web_ui": object()}
    return e


def test_blamed_module_is_deepest_owned_non_entry():
    e = _eng_blame()
    assert e._blamed_module_from_failure(_TRACEBACK, "app") == "notes_api"


def test_blame_ignores_entry_and_tests():
    e = _eng_blame()
    only_entry = ("FAILED tests/test_app.py::x\n  File \"src/app.py\", line 5\n"
                  "AssertionError: 404")
    assert e._blamed_module_from_failure(only_entry, "app") is None


def test_feature_failure_reworks_module_not_entry():
    e = _eng("rework")
    e.tasks = {"notes_api": object()}
    e._reworked = []
    e._remedy_rework_module = (
        lambda mod, reason: e._reworked.append(mod) or True)
    assert e._attempt_integrate_repair("no such table", _TRACEBACK) is True
    assert e._reworked == ["notes_api"]
    assert e._reconcile_calls == []          # entry NOT rebuilt for a module bug


def test_entry_failure_still_reconciles():
    e = _eng("reconcile_check")
    e.tasks = {}
    e._reworked = []
    e._remedy_rework_module = lambda mod, reason: e._reworked.append(mod) or True
    only_entry = "  File \"src/app.py\", line 5\nAssertionError: '200' in '404'"
    assert e._attempt_integrate_repair("ui 404", only_entry) is True
    assert e._reworked == []
    assert e._reconcile_calls == [("src/app.py", False)]
