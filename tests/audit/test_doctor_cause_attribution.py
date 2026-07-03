"""Audit rules S12.4-S12.5 (v160): the doctor ledger must ATTRIBUTE causes.

v160 evidence (2026-07-03T17-32-02__v160__p6-micro-notes): the assembled
product was fully green — the ONLY thing holding the root red was trace event
145 'doctor causes still open: product_entry:empty_delta'. The chain: event
128, the test-status gate found `tests/test_app.py asserts membership over
[200, 201]` on POST /notes; event 129, the doctor filed that finding under
the UNRELATED bucket `empty_delta` ('delivered nothing' — files WERE
delivered); events 130-138, rework fixed the assert and the node reached DONE
through contract_check + review_pass + verification — yet the mislabeled
cause was never closed and single-handedly flipped the terminal.

S12.4 — wrong cause attribution: a gate finding opens a cause NAMED BY ITS
  GATE (test_status / request_shape / ...), never shoved into empty_delta.
  RED: a test-status finding must diagnose a test-status-named cause.
  GREEN: a genuinely hollow delivery still diagnoses empty_delta.

S12.5 — causes close attributably: a cause opened by a gate MUST be closed
  when the node later reaches DONE and the OPENING gate re-runs clean on the
  CURRENT artifacts — with an attributable resolution event (the S10.15
  class applied to the doctor ledger). Never a blind auto-close.
  RED: open a cause via a gate finding, fix the artifact, complete the node
       -> cause closed with an event naming the gate; root no longer blocked.
  GREEN twin: artifact NOT fixed -> cause stays open (root stays red).
  GREEN 2: causes on non-DONE nodes are untouched.

Deterministic: unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import pathlib
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_diagnosers as dgn  # noqa: E402
import spec_flow_runner as sfr      # noqa: E402
from spec_flow_doctor import Context  # noqa: E402

_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [],
}

# the literal v160 finding text (event 128)
_V160_FINDING = ("tests/test_app.py asserts membership over [200, 201] after "
                 "`POST /notes` — assert exactly the contracted status 201")


# --- S12.4: cause id derives from the originating gate ------------------------

def test_test_status_finding_opens_test_status_cause_not_empty_delta():
    dg = dgn.Diagnosers()
    out = dg.run(node="product_entry", gate="test_status_gate", verdict="FAIL",
                 evidence={"scope_findings": [_V160_FINDING]},
                 context=Context(node="product_entry", gate="test_status_gate"))
    causes = [f.cause for f in out]
    assert "empty_delta" not in causes, (
        "a test-status violation filed as empty_delta is the v160 mislabel — "
        "files WERE delivered; the cause must be named by its gate")
    assert any("test_status" in c for c in causes), (
        "the cause id must derive from the originating gate id "
        f"(test_status_gate), got {causes}")


def test_request_shape_finding_opens_request_shape_cause():
    dg = dgn.Diagnosers()
    out = dg.run(node="notes_api", gate="request_shape_gate", verdict="FAIL",
                 evidence={"scope_findings": [
                     "src/api.py requires request field 'NOTES_DB' outside "
                     "the contracted request shape ['text']"]},
                 context=Context(node="notes_api", gate="request_shape_gate"))
    causes = [f.cause for f in out]
    assert "empty_delta" not in causes
    assert any("request_shape" in c for c in causes)


def test_genuinely_hollow_delivery_still_diagnoses_empty_delta():
    # GREEN twin: the delta gate reporting a hollow delivery keeps its bucket
    dg = dgn.Diagnosers()
    out = dg.run(node="about_page", gate="delta_gate", verdict="FAIL",
                 evidence={"scope_findings": [
                     "src/about_page.py adds no new symbol — empty delta"]},
                 context=Context(node="about_page", gate="delta_gate"))
    assert any(f.cause == "empty_delta" for f in out), (
        "a delivery that genuinely adds nothing is still an empty_delta")


def test_dup_surface_text_keeps_empty_delta_on_any_gate():
    # GREEN pin (v041 class): re-declared surface texts stay empty_delta even
    # when they arrive through spec_review — the text names the hollow delta
    dg = dgn.Diagnosers()
    out = dg.run(node="delete_note", gate="spec_review", verdict="FAIL",
                 evidence={"scope_findings": [
                     "route-redeclare: restates ['/notes'] and introduces "
                     "no new route"]},
                 context=Context(node="delete_note", gate="spec_review"))
    assert any(f.cause == "empty_delta" for f in out)


# --- S12.5: causes close attributably when the opening gate re-runs clean -----

# the cause-opening artifact: an EXACT-status mismatch (assert 200, contract
# 201). NOTE (S12.7, v161): the previous fixture smeared `in (200, 201)` —
# that class is now mechanically REPAIRED by the gate itself (autofix), so it
# no longer opens a cause; the exact-mismatch class stays an honest red
# (which of the two disagreeing guesses is right is not mechanical).
_WRONG = """\
    def _call(method, path):
        return 201


    def test_post_created():
        code = _call("POST", "/notes")
        assert code == 200
"""

_EXACT = """\
    def _call(method, path):
        return 201


    def test_post_created():
        code = _call("POST", "/notes")
        assert code == 201
"""

_NID = "notes_api"
_TEST_REL = "tests/test_notes_api.py"


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC,
                     doctor_project={"doctor": {"enabled": True}})
    eng._product_contract = lambda: dict(_CONTRACT)
    return eng


def _write_test(eng, body: str) -> None:
    p = pathlib.Path(eng.workspace.root) / _TEST_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")


def _open_cause_via_gate(eng) -> None:
    node = {"id": _NID, "title": "Notes API",
            "requirement": "own POST /notes and GET /notes"}
    _write_test(eng, _WRONG)
    ok = eng._leaf_test_status_gate(node, _NID, 1, _TEST_REL)
    assert ok is False
    opened = eng._doctor_open_causes()
    assert opened, "the gate FAIL must open a doctor cause"
    assert any("test_status" in c for _n, c in opened), (
        f"S12.4: the cause must be named by its gate, got {opened}")


def _mark(eng, status: str) -> None:
    eng.tasks[_NID] = sfr.Task(id=_NID, title="Notes API", kind="impl",
                               profile="", skill="", status=status)


def test_fixed_artifact_on_done_node_closes_cause_attributably(tmp_path):
    eng = _engine(tmp_path)
    _open_cause_via_gate(eng)
    _write_test(eng, _EXACT)            # rework fixed the assert
    _mark(eng, "done")                  # node reached DONE through its gates
    eng._prune_stale_causes()           # root-gate evaluation re-derives
    assert eng._doctor_open_causes() == [], (
        "v160: the node passed the very gate that opened the cause — a still-"
        "open ledger entry lies about the product and flips a green terminal")
    res = [lp for lp in eng.loops if lp.get("type") == "doctor"
           and lp.get("outcome") == "resolved"]
    assert res and "test_status_gate" in str(res[-1].get("detail", "")), (
        "the close must be ATTRIBUTABLE: an event naming the gate that "
        f"re-ran clean, got {res}")


def test_unfixed_artifact_keeps_cause_open(tmp_path):
    # GREEN twin: no blind auto-close — the gate still fails on the artifact
    eng = _engine(tmp_path)
    _open_cause_via_gate(eng)
    _mark(eng, "done")                  # DONE claimed, artifact still smeared
    eng._prune_stale_causes()
    assert eng._doctor_open_causes(), (
        "the opening gate still fails on the current artifact — the cause "
        "must stay open and hold the root red")


def test_causes_on_non_done_nodes_untouched(tmp_path):
    # GREEN 2: re-derivation only applies to nodes that reached DONE
    eng = _engine(tmp_path)
    _open_cause_via_gate(eng)
    _write_test(eng, _EXACT)
    _mark(eng, "todo")                  # node never completed
    eng._prune_stale_causes()
    assert eng._doctor_open_causes(), (
        "a cause on a node that never reached DONE is still live work — "
        "completion re-derivation must not touch it")


# --- S12.8 (v161): EVERY re-runnable leaf gate registers its recheck ----------
# v161 final event 144: 'doctor causes still open: web_ui:handler' — yet the
# final artifact DID define get_ui (rework restored it and the node reached
# DONE). The cause stayed open because _leaf_handler_gate never registered a
# recheck, so _prune_stale_causes hit `continue` (no fn) and the honest-repair
# work was vetoed by a STALE ledger entry — the exact S12.5 class, one gate
# over. The S12.5 wiring is a per-gate obligation, not a per-incident patch.

_UI_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health",
             "html_route": "/ui"},
    "routes": [],
}
_UI_NID = "web_ui"
_UI_REL = "src/web_ui.py"
_UI_NODE = {"id": _UI_NID, "title": "Web UI", "requirement": "own GET /ui"}

_UI_MISSING = """\
    def render_page():
        return "<html><body><h1>Notes</h1></body></html>"
"""

_UI_FIXED = """\
    def get_ui(payload, query):
        return 200, "<html><body><h1>Notes</h1></body></html>"
"""


def _ui_engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC,
                     doctor_project={"doctor": {"enabled": True}})
    eng._product_contract = lambda: dict(_UI_CONTRACT)
    return eng


def _write_module(eng, body: str) -> None:
    p = pathlib.Path(eng.workspace.root) / _UI_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")


def _open_handler_cause(eng) -> None:
    _write_module(eng, _UI_MISSING)
    ok = eng._leaf_handler_gate(dict(_UI_NODE), _UI_NID, 1, _UI_REL)
    assert ok is False, "the module does not define get_ui — the gate reds"
    opened = eng._doctor_open_causes()
    assert any(n == _UI_NID for n, _c in opened), (
        f"the gate FAIL must open a doctor cause on {_UI_NID}, got {opened}")


def _mark_ui(eng, status: str) -> None:
    eng.tasks[_UI_NID] = sfr.Task(id=_UI_NID, title="Web UI", kind="impl",
                                  profile="", skill="", status=status)


def test_restored_handler_on_done_node_closes_cause_attributably(tmp_path):
    eng = _ui_engine(tmp_path)
    _open_handler_cause(eng)
    _write_module(eng, _UI_FIXED)       # rework restored the handler
    _mark_ui(eng, "done")               # node reached DONE through its gates
    eng._prune_stale_causes()           # root-gate evaluation re-derives
    assert eng._doctor_open_causes() == [], (
        "v161 event 144: get_ui WAS restored and the node was DONE, yet "
        "'web_ui:handler' stayed open and held the root red — the handler "
        "gate must register its S12.5 recheck like every re-runnable gate")
    res = [lp for lp in eng.loops if lp.get("type") == "doctor"
           and lp.get("outcome") == "resolved"]
    assert res and "handler_gate" in str(res[-1].get("detail", "")), (
        "the close must be ATTRIBUTABLE: an event naming the gate that "
        f"re-ran clean, got {res}")


def test_still_missing_handler_keeps_cause_open(tmp_path):
    # GREEN twin: no blind auto-close — get_ui is still absent at completion
    eng = _ui_engine(tmp_path)
    _open_handler_cause(eng)
    _mark_ui(eng, "done")               # DONE claimed, handler still missing
    eng._prune_stale_causes()
    assert eng._doctor_open_causes(), (
        "the opening gate still fails on the current artifact — the cause "
        "must stay open and hold the root red (honest ledger)")


def test_handler_gate_quiet_recheck_is_side_effect_free(tmp_path):
    # the S12.5 recheck contract: verdict only — no events, loops or doctor
    eng = _ui_engine(tmp_path)
    _write_module(eng, _UI_MISSING)
    before_loops = len(eng.loops)
    before_events = len(eng.events)
    ok = eng._leaf_handler_gate(dict(_UI_NODE), _UI_NID, 1, _UI_REL,
                                quiet=True)
    assert ok is False
    assert len(eng.loops) == before_loops and \
        len(eng.events) == before_events, (
        "quiet mode must not journal, emit or open causes")
    assert eng._doctor_open_causes() == []
