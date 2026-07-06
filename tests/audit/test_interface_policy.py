"""STAGE 15 extension (S15.7-S15.10): the prose-derived interface fallback is
GATED by an explicit policy, not taken silently (node H8, plan
2026-07-04T00-45; principles-audit finding F4).

Why: when the decomposer supplies no machine IR fragment, ownership falls back
to reading the node's prose (S10.19b) — behaviour originates from prose, not
the spec, with the split only whispered in the journal (`interface_source:
prose-derived`). Nothing consented to it and nothing failed on it. Under the
"spec is the single source" thesis that silent channel must be an explicit,
visible decision.

What is pinned here:
  * S15.7 the engine carries an `interface_policy` (default `ir-required`);
    an unknown value is refused LOUDLY at construction (never a silent
    typo-to-default);
  * S15.8 under `ir-required`, a node whose interface was prose-derived is
    recorded as a policy violation and surfaces as a FAILING product check
    (NOT READY) naming the node — following the decomposer_ir FAIL precedent;
  * S15.9 under `allow-prose`, the historical fallback runs unchanged and
    produces NO violation — the consent is explicit and the journal still
    carries `interface_source: prose-derived`;
  * S15.10 an `ir`-sourced interface never violates any policy.

Deterministic: the seam (`_log_interface_source`, the verdict helper) is
driven with crafted state; the LLM door is never faked.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402


def _engine(tmp_path, **kw):
    return sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC,
                      **kw)


# ── S15.7 unknown policy is refused loudly ──────────────────────────────────

def test_unknown_interface_policy_is_refused(tmp_path):
    with pytest.raises((ValueError, RuntimeError)):
        _engine(tmp_path, interface_policy="permissive-ish")


def test_default_policy_is_ir_required(tmp_path):
    assert _engine(tmp_path).interface_policy == "ir-required"


# ── S15.8 prose-derived under ir-required is a violation → NOT READY ─────────

def test_prose_under_ir_required_is_a_failing_check(tmp_path):
    eng = _engine(tmp_path)                      # default ir-required
    eng._log_interface_source("orders", "prose-derived")
    fails = eng._interface_policy_failures()
    assert any("orders" in f for f in fails), (
        "a prose-derived interface under ir-required must surface as a "
        "failing product check naming the node (F4)")


# ── S15.9 allow-prose consents, no violation ────────────────────────────────

def test_allow_prose_consents_no_violation(tmp_path):
    eng = _engine(tmp_path, interface_policy="allow-prose")
    eng._log_interface_source("orders", "prose-derived")
    assert eng._interface_policy_failures() == [], (
        "allow-prose is explicit consent — the historical fallback runs with "
        "no violation")


# ── S15.10 an ir-sourced interface never violates ───────────────────────────

def test_ir_source_never_violates(tmp_path):
    eng = _engine(tmp_path)                       # ir-required
    eng._log_interface_source("orders", "ir")
    assert eng._interface_policy_failures() == [], (
        "an interface taken from the decomposer machine fragment is exactly "
        "what the policy demands — never a violation")
