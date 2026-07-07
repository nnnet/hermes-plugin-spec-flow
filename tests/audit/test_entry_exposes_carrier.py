"""Audit stage S47 (node Q8, plan 2026-07-06T20-15): the ASSEMBLY entry is not
hollow. Root cause of the v170 false-hollow on `product_entry`: the entry node
owns the product entry file (src/app.py) and wires every module's routes into
ONE product callable (wsgi_app), but its `symbols.exposes` was never recorded —
so `_node_class` saw an "other" node with no carrier and the Q2 hollow check
(S38) flagged it. The entry DOES expose the declared product callable; recording
it makes the entry a code node with a real typed contract, not a carrier-less
shell. Single-source: the callable names come from the product contract the
engine already declared, never invented.

  * S47.1 — `_entry_exposes` returns the product callable(s) as typed exposes
    for the entry node (id 'product_entry' or the owner of the entry file); an
    empty list for any other node or when no callable is declared.
  * S47.2 — a node carrying those exposes is NOT hollow (`_hollow_node_reason`
    returns None).
  * S47.3 — build_ir wires `_entry_exposes` so the entry node gets its exposes
    when the module contract recorded none.

Deterministic: pure helper + classifier + source-wiring assertion, no run.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import spec_ir  # noqa: E402

_CONTRACT = {"entry": "src/app.py", "callable": ["wsgi_app"]}


# ── S47.1: the entry exposes its product callable ───────────────────────────

def test_entry_exposes_by_id():
    ex = spec_ir._entry_exposes("product_entry", ["src/app.py"], _CONTRACT)
    assert ex == [{"name": "wsgi_app", "args": None}]


def test_entry_exposes_by_file_owner():
    ex = spec_ir._entry_exposes("root", ["src/app.py", "src/root.py"], _CONTRACT)
    assert ex == [{"name": "wsgi_app", "args": None}], \
        "the node owning the product entry file is the assembler"


def test_non_entry_node_gets_nothing():
    assert spec_ir._entry_exposes("db", ["src/db.py"], _CONTRACT) == []


def test_no_callable_declared_yields_nothing():
    assert spec_ir._entry_exposes("product_entry", ["src/app.py"],
                                  {"entry": "src/app.py"}) == []


# ── S47.2: an entry with exposes is not hollow ──────────────────────────────

def test_entry_with_exposes_is_not_hollow():
    node = {"files": ["src/app.py"],
            "symbols": {"exposes": [{"name": "wsgi_app", "args": None}]}}
    assert spec_ir._node_class(node) == "code"
    assert spec_ir._hollow_node_reason("product_entry", node) is None, \
        "an entry that exposes the product callable is a real contract, not hollow"


def test_entry_without_exposes_is_hollow():
    # the SAME entry node BEFORE the fix records exposes -> genuinely hollow,
    # proving the verdict hinges on the exposes being recorded.
    node = {"files": ["src/app.py"]}
    assert spec_ir._hollow_node_reason("product_entry", node) is not None


# ── S47.3: build_ir wires the helper ────────────────────────────────────────

def test_build_ir_wires_entry_exposes():
    src = pathlib.Path(spec_ir.__file__).read_text(encoding="utf-8")
    assert "_entry_exposes(" in src, "build_ir must call _entry_exposes"
    # the call must feed the node's exposes when the module contract had none
    assert src.count("_entry_exposes") >= 2, \
        "helper defined AND called (def + use)"
