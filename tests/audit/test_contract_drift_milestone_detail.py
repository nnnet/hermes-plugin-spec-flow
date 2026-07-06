#!/usr/bin/env python3
"""Audit STAGE 27 — the engine attaches the per-record contract drift (the
``contract_gap`` / ``duplicate_route`` / ``missing_endpoint`` / ``type_mismatch``
entries the validator emits) to the ``contract_check`` MILESTONE event as
STRUCTURED data, so the live dashboard renders per-file contract drift
generically (node M2, plan 2026-07-04T00-45).

Why this stage exists (the hole M2 closes):
    Node K4 (Stage 24) built ``tests/harness/openapi_diff.py`` on the compiled
    machine OpenAPI document: it prints a JSON list of drift RECORDS
    (``{"kind": "contract_gap", "endpoint": "GET /a", ...}``,
    ``{"kind": "type_mismatch", "endpoint": "GET /b", "field": "id", ...}``,
    ``{"kind": "duplicate_route", "detail": ...}``) and exits nonzero on drift.
    But when the engine runs that validator through ``contract_check`` and
    emits the ``contract_check`` MILESTONE event, it dropped the record list:
    only ``res["drift"][0]["detail"]`` — the RAW stdout dump of the FIRST
    validator — was stuffed into the event's free-text ``detail`` string. The
    machine records (which route drifted, which field, gap vs duplicate vs
    type) were buried inside an unparsed blob, and records from any second
    validator were lost entirely. The live dashboard renders milestones
    generically off ``level`` / ``verdict`` / structured fields, so per-file
    contract drift never surfaced as data — only as one opaque string.

    M2 makes the engine PARSE the validator records and attach them to the
    MILESTONE event as a structured ``details`` list (every drift record from
    EVERY validator, each carrying its ``kind`` and its route/field). The
    dashboard then shows the per-file contract drift with no dashboard change.

Ratchet (RED before code): on the pre-M2 engine the ``contract_check`` drift
milestone carries NO structured ``details`` — the record list is absent, so
this audit fails. It turns GREEN once the engine attaches the parsed records.

Deterministic: a real engine run (``run_project``) over the privacy-analytics
run with the real ``openapi_diff`` validator wired in. No LLM, no network.

Test: run ``python -m pytest tests/audit/test_contract_drift_milestone_detail.py -q``.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from harness import run_engine as eng  # noqa: E402


@pytest.fixture
def run(plugin, tmp_path):
    """A real engine run whose contract_check hits the real openapi_diff.

    Why: the drift milestone is emitted by the engine, not the tool; the audit
    must observe the engine's own event, wired to the same validator a live
    run uses.
    What: points CONTRACT_VALIDATORS['openapi'] at openapi_diff.py, runs the
    privacy-analytics project at the simplest depth with a contracts dir.
    Test: the assertions below read run.events for the contract_check drift.
    """
    plugin.tools.CONTRACT_VALIDATORS["openapi"] = [
        "python3", str(eng.OPENAPI_DIFF), "{contract}", "{code}"]
    return eng.run_project(
        eng.load_run(), workspace=str(tmp_path / "wk"), depth="spec",
        tools=plugin.tools, contracts_dir=str(eng.CONTRACTS))


def _drift_milestones(run):
    return [e for e in run.events
            if e.gate == "contract_check" and e.verdict == "drift"]


def test_a_drift_episode_occurred(run):
    """Precondition: the privacy-analytics run has a contract drift episode,
    otherwise there is no milestone to enrich.

    Why: a green audit must be earned by a real drift, not by its absence.
    What: at least one contract_check event with verdict 'drift'.
    Test: assert the drift-milestone list is non-empty.
    """
    assert _drift_milestones(run), (
        "the run must contain a contract_check drift milestone to audit")


def test_drift_milestone_carries_structured_records(run):
    """S27.1 THE DRIFT MILESTONE CARRIES THE PARSED RECORD LIST: the
    contract_check drift event exposes a structured ``details`` list of the
    validator's drift records, not just a raw-dump string.

    Why: the pre-M2 engine buried the records inside ``detail`` (the raw
    stdout of only the first validator); the dashboard renders structured
    milestone fields, so unparsed text never surfaced as per-file drift.
    What: the event's ``details`` is a non-empty list of record dicts.
    Test: assert ``e.details`` is a list with at least one dict record.
    """
    ev = _drift_milestones(run)[0]
    details = getattr(ev, "details", None)
    assert isinstance(details, list) and details, (
        "the contract_check drift milestone must carry a structured 'details' "
        f"list of validator records; got details={details!r}")
    assert all(isinstance(r, dict) for r in details), (
        f"each drift record must be a dict; got {details!r}")


def test_records_name_kind_and_route(run):
    """S27.2 EACH RECORD NAMES ITS KIND AND ITS LOCATION: every attached
    record carries the drift ``kind`` (contract_gap / duplicate_route /
    missing_endpoint / missing_field / type_mismatch) and, where the validator
    provides one, the route/field it drifted on.

    Why: the dashboard shows per-file contract drift by kind and route; a
    record without a kind is not renderable as drift.
    What: every record has a non-empty ``kind`` drawn from the known set, and
    at least one record locates a route (``endpoint``) or field.
    Test: assert kinds are known and non-empty; assert a locating field exists.
    """
    known = {"contract_gap", "duplicate_route", "missing_endpoint",
             "missing_field", "type_mismatch", "validator_error"}
    records = [r for e in _drift_milestones(run) for r in getattr(e, "details", [])]
    assert records, "no drift records attached to any drift milestone"
    assert all(str(r.get("kind") or "") in known for r in records), (
        f"every record must name a known drift kind; got {records!r}")
    located = [r for r in records
               if r.get("endpoint") or r.get("field") or r.get("detail")]
    assert located, (
        f"at least one record must locate its drift (route/field/detail); "
        f"got {records!r}")
