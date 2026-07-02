"""Audit rule (investigator F1/F12, v150): a route's behavioural contract is
derived from ITS OWN requirement, never stamped from a method-generic template.

v150: ``_leaf_route_binding``'s behaviour text was keyed on the HTTP METHOD
only, so every GET route — /ping, /health, /ui — received the notes-collection
wording ("a list, newest-first, honour a `q` filter") verbatim: a neighbour's
contract copied into an unrelated route.

Contract enforced here:
  * the binding of a leaf whose requirement says nothing about collections or
    filters carries NO collection/filter wording — its own requirement drives
    the contract text;
  * two leaves with different requirements never share the exact behaviour
    sentence for their routes;
  * the deterministic gate (consistency.check_route_contract_distinct) flags
    the v150 artifacts (known-answer) and stays silent on distinct contracts.

Deterministic: engine unit calls + artifact scans, no LLM.
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import spec_flow_runner as sfr  # noqa: E402
import consistency  # noqa: E402

V150_RUN = (pathlib.Path(__file__).resolve().parents[1] / "runs-out"
            / "2026-07-02T20-18-57__v150__p6-micro-notes")

needs_v150 = pytest.mark.skipif(
    not V150_RUN.is_dir(), reason="v150 run dir not present on this checkout")

_CONTRACT = {
    "entry": "src/app.py",
    "boot": {"json_roundtrip": "/notes", "ok_route": "/ping"},
    "routes": [["GET", "/ui"]],
}


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    return eng


def _behaviour_of(binding: str, path: str) -> str:
    line = next(ln for ln in binding.splitlines() if "`GET %s`" % path in ln)
    return line.split("—", 1)[1].split("; SUCCESS STATUS", 1)[0].strip()


def test_ping_contract_carries_no_neighbour_collection_wording(tmp_path):
    eng = _engine(tmp_path)
    ping = {"id": "ping_text", "title": "Liveness ping",
            "requirement": 'Serve GET /ping returning the plain text "pong".'}
    binding = eng._leaf_route_binding(ping)
    assert binding, "the leaf owns GET /ping — a binding must be emitted"
    behaviour = _behaviour_of(binding, "/ping")
    assert "newest-first" not in behaviour, (
        "v150 template contamination: the notes-collection wording was "
        "stamped on /ping whose requirement never mentions a collection")
    assert "`q` filter" not in behaviour
    assert "pong" in behaviour, (
        "the /ping contract must be traceable to its OWN requirement")


def test_different_requirements_never_share_a_behaviour_sentence(tmp_path):
    eng = _engine(tmp_path)
    ping = {"id": "ping_text", "title": "Liveness ping",
            "requirement": 'Serve GET /ping returning the plain text "pong".'}
    ui = {"id": "web_ui", "title": "Web UI",
          "requirement": "Serve GET /ui — an HTML page that lists all notes "
                         "(newest first) with an add form."}
    b_ping = _behaviour_of(eng._leaf_route_binding(ping), "/ping")
    b_ui = _behaviour_of(eng._leaf_route_binding(ui), "/ui")
    assert b_ping != b_ui, (
        "two routes born of different requirements share the exact "
        "behaviour sentence: %r" % b_ping)


def test_requirement_that_asks_for_filtering_still_gets_the_filter_contract(tmp_path):
    eng = _engine(tmp_path)
    notes = {"id": "notes_api", "title": "Notes API",
             "requirement": "GET /notes lists all notes newest-first and can "
                            "be filtered by a q word."}
    binding = eng._leaf_route_binding(notes)
    assert "newest-first" in binding and "`q` filter" in binding, (
        "content semantics the requirement DOES ask for must stay contracted")


@needs_v150
def test_v150_contract_templating_is_flagged():
    findings = consistency.audit(V150_RUN)
    hits = [f for f in findings if f.kind == "route_contract_templated"]
    assert hits, "the v150 cross-spec template contamination must be flagged"
    assert any(re.search(r"/ping|/ui|/health", f.message) for f in hits)


def test_distinct_contracts_produce_no_finding(tmp_path):
    run = tmp_path / "run"
    specs = run / "workspace" / "specs"
    specs.mkdir(parents=True)
    (specs / "a.md").write_text(
        "- `GET /ping` -> `def get_ping(payload, query)` — return the plain "
        "text pong; SUCCESS STATUS 200\n", encoding="utf-8")
    (specs / "b.md").write_text(
        "- `GET /notes` -> `def get_notes(payload, query)` — return the "
        "notes list; SUCCESS STATUS 200\n", encoding="utf-8")
    assert consistency.check_route_contract_distinct(run) == []
