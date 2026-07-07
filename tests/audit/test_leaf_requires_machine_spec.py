"""Audit stage S50 (node Q10, plan 2026-07-06T20-15): a leaf may NOT pass
spec_lint without a validated MACHINE spec. HARD project rule (owner, emphatic):
"нельзя фаллбек на прозу, машинный спек обязателен; нода не может пройти
spec_lint без валидированного машинного спека — это ошибка".

Observed on v174/web_ui: a late-injected HTML-page leaf reached spec_lint PASS
while its IR node was `{children: []}` — no route, no scenarios, no typed
exposes. The card gate `_card_completeness_findings` was INERT for a leaf that
exposes nothing (it only demanded acceptance when a surface was already present),
so a carrier-less leaf sailed through. S50 closes it: an atomic leaf that is
neither a branch nor an amend MUST carry a machine spec — an HTTP route
(openapi paths), executable scenarios/behaviour (Gherkin), or typed exposes.
None => a card gap (fillable by the decomposer rework, or the leaf is
re-decomposed); prose is not a spec.

  * S50.1 — a carrier-less atomic leaf yields a machine-spec card gap.
  * S50.2 — a leaf with ANY machine carrier (route / scenarios / behaviour /
    typed exposes) yields NO such gap.
  * S50.3 — a branch (children) and an amend (code_target) are exempt.

Deterministic: pure gate call on hand-built nodes, no LLM, no run.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import spec_flow_runner as sfr  # noqa: E402

_MARK = "machine spec"          # the S50 finding names the missing machine spec


def _eng():
    e = sfr.Engine.__new__(sfr.Engine)
    e.__dict__["_module_importers"] = {}
    e.__dict__["_module_contracts"] = {}
    e.__dict__["_node_registry"] = {}
    return e


def _findings(node):
    return sfr.Engine._card_completeness_findings(_eng(), node)


def _has_machine_gap(node):
    return any(_MARK in f for f in _findings(node))


# ── S50.1: carrier-less leaf is a machine-spec gap ──────────────────────────

def test_carrierless_leaf_flagged():
    node = {"id": "web_ui", "children": []}      # no route/scenarios/exposes
    assert _has_machine_gap(node), \
        "a leaf with no machine carrier must FAIL the card gate (spec mandatory)"


def test_prose_only_leaf_flagged():
    node = {"id": "make_nice", "spec_markdown": "# make it pretty\nprose only"}
    assert _has_machine_gap(node), "prose is not a machine spec"


# ── S50.2: any real carrier clears the gap ──────────────────────────────────

def test_route_carrier_ok():
    node = {"id": "notes", "openapi": {"paths": {"/notes": {"get": {}}}}}
    assert not _has_machine_gap(node), "an HTTP route is a machine spec"


def test_scenarios_carrier_ok():
    node = {"id": "flow", "scenarios": [{"when": "x", "then": "y"}]}
    assert not _has_machine_gap(node), "executable scenarios are a machine spec"


def test_behavior_carrier_ok():
    node = {"id": "beh", "behavior": "Feature: x\n  Scenario: s\n    Then t"}
    assert not _has_machine_gap(node), "Gherkin behaviour is a machine spec"


def test_typed_exposes_carrier_ok():
    node = {"id": "lib", "symbols": {"exposes": [{"name": "connect", "args": None}]}}
    assert not _has_machine_gap(node), "typed exposes are a machine spec"


# ── S50.3: branch and amend are exempt ──────────────────────────────────────

def test_branch_exempt():
    node = {"id": "L0", "children": [{"id": "a"}, {"id": "b"}]}
    assert not _has_machine_gap(node), "a branch delegates to children"


def test_amend_exempt():
    node = {"id": "req_x", "code_target": "src/core.py", "children": []}
    assert not _has_machine_gap(node), "an amend edits an owner — no own carrier"
