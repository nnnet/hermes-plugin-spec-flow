"""Audit STAGE 38 — FAIL-CLOSED: absence is never success (node Q2, plan
2026-07-06T20-15, catalog C).

Why this stage exists (the fail-open holes it closes):
    The engine was built fail-open "so the flow never tears": a missing datum, an
    un-run oracle, or a silent model all resolved to the GREEN branch. Four sites
    made "absence == success" the default (catalog C):
      C1 (``spec_registry.validate_carrier``) — an unknown/inactive format returns
         ``[]`` == valid, so a node whose carrier is not actually checked reads as
         checked-and-clean (not-checked collapsed into ok).
      C2 (LLM verdicts) — a reviewer/approver that returns a dict with the verdict
         field ABSENT (the model went silent / drifted the schema) defaulted to
         PASS / approved=True: a silent model turned green.
      C3 (hollow spec) — a node with a syntactically valid but EMPTY spec (no
         behaviour carrier AND no interface/typed contract: 0 requirements, 0
         error surface, 0 edge cases) passed ``validate_ir`` with zero errors.
         ``spec_completeness_gaps`` (N6) merely RECORDED the gap; the closed-world
         oracle never blocked on it.
      C4 (oracle degrade) — when the optional grammar/schema library is absent the
         oracle degraded to ``[]`` (== ok), so "the oracle did not run" was
         indistinguishable from "the oracle ran and found nothing".

Ratchet (RED before code): every RED case below asserts the OLD fail-open answer
so it fails on the pre-Q2 engine, then goes GREEN once absence is a NAMED refusal
(a hollow node is a ``validate_ir`` error; an inactive/unknown carrier is a
not-checked FAIL, not ``[]``; a verdict-less LLM reply is not PASS; a library-less
oracle returns a NOT-CHECKED sentinel, never a bare ``[]``).

Both directions (v151 lesson): a COMPLETE node, an ACTIVE carrier with a clean
node, an EXPLICIT ``approved: True`` / ``verdict: PASS`` reply, and an oracle whose
library IS present must all stay SILENT/GREEN — fail-closed must never false-red a
legitimate present-and-clean datum.

Test: run ``python -m pytest tests/audit/test_fail_closed_absence.py -q``.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_ir  # noqa: E402
import spec_registry  # noqa: E402
import spec_flow_runner as sfr  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────

def _complete_http_node() -> dict:
    """A format-valid AND complete HTTP node — the GREEN control (must stay
    silent under every fail-closed gate)."""
    return {
        "id": "api",
        "files": ["src/api.py"],
        "openapi": {"openapi": "3.1.0", "info": {"title": "t", "version": "1"},
                    "paths": {"/ping": {"get": {
                        "responses": {
                            "200": {"description": "ok",
                                    "content": {"application/json": {
                                        "schema": {"type": "object"}}}},
                            "500": {"description": "err"}}}}}},
        "behavior": "Scenario: ping\n  Given up\n  When GET /ping\n  Then 200",
        "architecture": "flask blueprint mounts /ping",
        "dependencies": ["flask"],
        "effects": [],
    }


def _complete_ir() -> dict:
    """An IR whose single node is complete — validate_ir must be silent."""
    node = _complete_http_node()
    return {"format": spec_ir.IR_FORMAT,
            "product": {"entry": "src/app.py", "callable": ["wsgi_app"],
                        "requirements": [{"name": "flask", "version": "3.0"}]},
            "nodes": {"api": node}}


def _hollow_ir() -> dict:
    """An executable node with a valid shell but NO behaviour carrier and NO
    interface/typed contract — the catalog-C3 hollow spec."""
    return {"format": spec_ir.IR_FORMAT,
            "product": {"entry": "src/app.py", "callable": ["wsgi_app"],
                        "requirements": []},
            "nodes": {"api": {
                "files": ["src/api.py"],
                # no openapi, no behavior, no scenarios, no typed exposes
                "symbols": {"exposes": [], "consumes": []},
                "env": [],
                "dependencies": [],
                "effects": []}}}


# ── C3: hollow spec is a BLOCKING validate_ir error ─────────────────────────

def test_hollow_spec_is_validate_ir_error():
    errs = spec_ir.validate_ir(_hollow_ir())["errors"]
    assert any("hollow" in e.lower() for e in errs), (
        "C3 fail-open: a hollow executable node (no behaviour, no interface) "
        "passed validate_ir with zero errors; absence of content must be a "
        "NAMED blocking error, not a silent PASS. errors=%r" % errs)


def test_complete_node_is_not_flagged_hollow():
    errs = spec_ir.validate_ir(_complete_ir())["errors"]
    assert not any("hollow" in e.lower() for e in errs), (
        "fail-closed must not false-red a complete node. errors=%r" % errs)


# ── C1: an inactive/unknown carrier is NOT-CHECKED, never []==valid ─────────

def test_inactive_carrier_is_not_checked_fail():
    # asyncapi is a declared-but-inactive stub adapter
    out = spec_registry.validate_carrier("asyncapi", {"id": "n"})
    assert out, (
        "C1 fail-open: an inactive carrier returned []==valid; a standard that "
        "is not actually validated must be a NAMED not-checked refusal, not "
        "silence. out=%r" % out)
    assert spec_ir.is_not_checked(out[0]), (
        "the inactive-carrier refusal must carry the NOT-CHECKED marker so a "
        "consumer never mistakes not-checked for clean. out=%r" % out)


def test_unknown_carrier_is_not_checked_fail():
    out = spec_registry.validate_carrier("does-not-exist", {"id": "n"})
    assert out and spec_ir.is_not_checked(out[0]), (
        "C1 fail-open: an unknown format returned []==valid. out=%r" % out)


def test_active_clean_carrier_stays_silent():
    # an active carrier over a clean node must still return [] (green control)
    node = _complete_http_node()
    out = spec_registry.validate_carrier("openapi", node)
    assert out == [], (
        "fail-closed must not turn a real, active, passing carrier into a "
        "false refusal. out=%r" % out)


# ── C4: a library-less oracle returns NOT-CHECKED, not []==ok ───────────────

def test_gherkin_oracle_absent_library_is_not_checked(monkeypatch):
    # force the optional library (resolved by spec_ir._gherkin_parser) to read
    # as absent
    monkeypatch.setattr(spec_ir, "_gherkin_parser", lambda: None)
    ir = {"format": spec_ir.IR_FORMAT,
          "product": {"entry": "src/app.py", "callable": ["a"],
                      "requirements": []},
          "nodes": {"api": {"files": ["src/api.py"],
                            "scenarios": [{"id": "s",
                                           "given": [{"state": "up"}],
                                           "when": {"call": "GET /x"},
                                           "then": {"status": 200}}]}}}
    out = spec_ir.gherkin_errors(ir)
    assert out and any(spec_ir.is_not_checked(e) for e in out), (
        "C4 fail-open: absent gherkin library degraded to []==ok; a skipped "
        "oracle must report NOT-CHECKED, never silence. out=%r" % out)


def test_gherkin_oracle_present_library_stays_silent():
    # with the real library present, a valid IR is silent (green control)
    ir = {"format": spec_ir.IR_FORMAT,
          "product": {"entry": "src/app.py", "callable": ["a"],
                      "requirements": []},
          "nodes": {"api": {"files": ["src/api.py"],
                            "scenarios": [{"id": "s",
                                           "given": [{"state": "up"}],
                                           "when": {"call": "GET /x"},
                                           "then": {"status": 200}}]}}}
    out = [e for e in spec_ir.gherkin_errors(ir) if spec_ir.is_not_checked(e)]
    assert out == [], (
        "with the library present the oracle must run, not report not-checked. "
        "out=%r" % out)


# ── C2: a verdict-less LLM reply is FAIL/not-approved, never a silent PASS ───

def test_review_reply_without_verdict_is_not_pass():
    # a reviewer worker that returned a dict but no `verdict` field = the model
    # went silent / drifted the schema — must NOT resolve to PASS
    assert sfr._review_verdict_from_reply({"reasons": []}) != "PASS", (
        "C2 fail-open: a reviewer reply missing `verdict` defaulted to PASS; "
        "a silent model must not turn green.")


def test_review_reply_explicit_pass_stays_pass():
    assert sfr._review_verdict_from_reply({"verdict": "PASS"}) == "PASS", (
        "an explicit PASS reply must stay PASS (green control).")


def test_review_reply_explicit_reject_is_reject():
    assert sfr._review_verdict_from_reply({"verdict": "REJECT"}) == "REJECT"


def test_approved_reply_without_field_is_not_approved():
    # an approver reply lacking `approved` = silence — must be False
    assert sfr._approved_from_reply({"reason": "x"}) is False, (
        "C2 fail-open: an approver reply missing `approved` defaulted to True; "
        "a silent model must not approve.")


def test_approved_reply_explicit_true_is_approved():
    # the autonomous default carries an explicit approved:True (green control)
    assert sfr._approved_from_reply({"approved": True, "reason": "auto"}) is True
