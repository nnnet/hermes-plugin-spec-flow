"""Audit stage S48 (node Q9, plan 2026-07-06T20-15): HITL injection triage. An
injection can be ANYTHING — a refinement of an existing surface, a new feature,
a change of scope, or plain NOISE (a poem, a joke, something unrelated to the
software). The engine must analyse each injection and fold it into the plan so
the original task is met WITH the meaningful injections — never blindly fork a
carrier-less leaf per injection (the v170/v171 hollow blockers delete_note /
req_a54f9144 / note_search).

The owner-attach machinery already exists (`_amend_find_owner` +
`SPEC_FLOW_AMEND_LLM`). S48 adds the two missing branches:
  * S48.1 — a NOISE injection (no meaningful overlap with the product concern)
    is DROPPED with a recorded reason, never materialised as a node.
  * S48.2 — a RELEVANT injection (shares the product's concern) is kept for the
    normal attach/carrier path — the conservative rule only drops CLEAR noise,
    never a real requirement.
  * S48.3 — the semantic owner router is armed by DEFAULT whenever amend is on
    (an injection's owner is analysed, not left to deterministic tokens alone),
    unless explicitly disabled.

Deterministic: pure token helpers + env-state assertion, no LLM, no network.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import spec_flow_runner as sfr  # noqa: E402

_CONCERN = ("a tiny notes service: POST /notes stores a note, GET /notes "
            "returns notes newest-first, sqlite3 persistence, GET /health")


# ── S48.1: clear noise is noise ─────────────────────────────────────────────

def test_poem_is_noise():
    assert sfr._injection_is_noise(
        "Roses are red, violets are blue, here is a little rhyme for you",
        _CONCERN), "an off-topic poem shares no product concern -> noise"


def test_joke_is_noise():
    assert sfr._injection_is_noise(
        "Why did the chicken cross the road? To get to the other side!",
        _CONCERN), "an unrelated joke -> noise"


# ── S48.2: a real refinement/feature is NOT noise ───────────────────────────

def test_make_notes_nice_is_not_noise():
    assert not sfr._injection_is_noise(
        "Make the notes nice to read: a clear heading and a tidy list",
        _CONCERN), "a refinement that names 'notes' shares the concern"


def test_delete_feature_is_not_noise():
    assert not sfr._injection_is_noise(
        "Add a way to delete a note by its id", _CONCERN), \
        "a new feature about notes shares the concern"


def test_empty_concern_never_noise():
    # with nothing to compare against, never drop (conservative — keep it).
    assert not sfr._injection_is_noise("anything at all", "")


# ── S48.3: the semantic owner router is armed by default under amend ─────────

def test_amend_llm_armed_by_default(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_REQ_AMEND", "1")
    monkeypatch.delenv("SPEC_FLOW_AMEND_LLM", raising=False)
    assert sfr._amend_llm_enabled(), \
        "when amend is on, the semantic owner router is armed by default"


def test_amend_llm_explicit_off(monkeypatch):
    monkeypatch.setenv("SPEC_FLOW_REQ_AMEND", "1")
    monkeypatch.setenv("SPEC_FLOW_AMEND_LLM", "0")
    assert not sfr._amend_llm_enabled(), \
        "an explicit SPEC_FLOW_AMEND_LLM=0 disables the router"


def test_amend_llm_off_when_amend_off(monkeypatch):
    monkeypatch.delenv("SPEC_FLOW_REQ_AMEND", raising=False)
    monkeypatch.delenv("SPEC_FLOW_AMEND_LLM", raising=False)
    assert not sfr._amend_llm_enabled(), \
        "no amend -> no router"
