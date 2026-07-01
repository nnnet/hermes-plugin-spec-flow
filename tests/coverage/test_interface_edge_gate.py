"""C2 — deterministic PLAN gate over typed dependency edges.

A node declares ``exposes: [symbol]`` (its public surface) and typed needs
``needs: [{"from": node_id, "symbols": [...]}]``. Every needed symbol must be in
the producer's declared exposes — a need on a symbol NO node produces is a broken
interface edge, caught before any code is written. Medium-agnostic (symbols, not
routes) and pure (no LLM). Undeclared edges are lenient (back-compat).
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import spec_flow_runner as sfr  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from harness import role_worker as rw  # noqa: E402


def test_clean_edge_passes():
    tree = {"id": "root", "children": [
        {"id": "store", "exposes": ["save(text)", "load()"]},
        {"id": "cli", "needs": [{"from": "store", "symbols": ["save(text)"]}]},
    ]}
    assert sfr._interface_edge_violations(tree) == []


def test_needed_symbol_not_exposed_is_flagged():
    tree = {"id": "root", "children": [
        {"id": "store", "exposes": ["create_note(text)"]},
        # cli guessed 'save_note' — the producer exposes 'create_note'
        {"id": "cli", "needs": [{"from": "store", "symbols": ["save_note"]}]},
    ]}
    viol = sfr._interface_edge_violations(tree)
    assert len(viol) == 1
    assert "save_note" in viol[0] and "store" in viol[0]


def test_need_on_unknown_node_is_flagged():
    tree = {"id": "root", "children": [
        {"id": "cli", "needs": [{"from": "ghost", "symbols": ["x"]}]},
    ]}
    viol = sfr._interface_edge_violations(tree)
    assert viol and "not a node" in viol[0]


def test_undeclared_edges_are_lenient():
    # no exposes/needs anywhere → back-compat no-op (a decomposer that does not
    # populate the fields changes nothing).
    tree = {"id": "root", "children": [
        {"id": "a"}, {"id": "b", "depends_on": ["a"]}]}
    assert sfr._interface_edge_violations(tree) == []


def test_declared_contract_reaches_the_coder():
    # the plan's declared exposes + typed needs must reach the coder prompt so a
    # producer exposes the agreed names and a consumer imports them verbatim.
    block = rw._interfaces_block({
        "declared_exposes": ["create_note(text)", "list_notes()"],
        "declared_needs": [{"from": "store", "symbols": ["create_note(text)"]}],
    })
    assert "MUST EXPOSE EXACTLY" in block
    assert "create_note(text)" in block
    assert "from store import create_note(text)" in block


def test_medium_agnostic_no_http_assumption():
    # a NON-WEB pipeline: symbols only, no routes anywhere.
    tree = {"id": "root", "children": [
        {"id": "engine", "exposes": ["run(rows)", "load(path)"]},
        {"id": "report", "needs": [
            {"from": "engine", "symbols": ["run(rows)", "summarize"]}]},
    ]}
    viol = sfr._interface_edge_violations(tree)
    # 'run(rows)' is fine; 'summarize' is not exposed → exactly one finding
    assert len(viol) == 1 and "summarize" in viol[0]
