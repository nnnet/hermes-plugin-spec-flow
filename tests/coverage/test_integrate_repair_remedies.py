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
