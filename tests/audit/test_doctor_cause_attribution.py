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

_SMEARED = """\
    def _call(method, path):
        return 201


    def test_post_created():
        code = _call("POST", "/notes")
        assert code in (200, 201)
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
    _write_test(eng, _SMEARED)
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
