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
