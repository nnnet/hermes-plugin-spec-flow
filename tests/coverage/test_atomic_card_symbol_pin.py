"""#113 Phase 0 slice 1 — the leaf's public symbol is DATA the engine pins, and
BOTH the coder and the tester read that ONE name, so a test can never import a
symbol the coder did not write (v143: tester imported `delete_notes` while the
coder wrote `delete_note` -> unrepairable ImportError).

Pure/deterministic: no LLM, no HTTP call.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from harness import role_worker as rw  # noqa: E402


def _engine(tmp_path):
    return sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)


def test_exposed_symbol_derived_deterministically_from_owned_route(tmp_path):
    eng = _engine(tmp_path)
    # a route the human declared; the entry file + callable named (so a contract
    # is derivable) — the exposed symbol is the CANONICAL handler for that route,
    # not whatever a model happens to call it. The value is CONSISTENCY: both
    # coder and tester read this one name, so they cannot diverge.
    eng._constitution = [
        "POST /notes takes {text} responds {id}. The entry src/app.py exposes"
        " wsgi_app."]
    eng._goal = "notes"
    node = {"id": "notes_post", "title": "handle POST /notes",
            "requirement": "create a note via POST /notes"}
    owned = eng._leaf_owned_routes(node)
    assert owned, "the leaf must own the declared route"
    # the exposed symbol is EXACTLY the canonical handler of each owned route —
    # pure derivation, no invention. Whatever method the contract resolves, the
    # symbol both roles read is this one.
    syms = eng._leaf_exposed_symbols(node)
    assert syms == [sfr._canonical_handler_symbol(m, p) + "(payload, query)"
                    for m, p in owned]
    assert syms  # non-empty for a route-owning leaf


def test_exposed_symbols_union_declared_exposes_for_a_library(tmp_path):
    eng = _engine(tmp_path)
    eng._constitution = ["A library, no HTTP."]
    eng._goal = "lib"
    node = {"id": "wordlib", "exposes": ["word_count(s)", "split_words(s)"]}
    assert eng._leaf_exposed_symbols(node) == ["word_count(s)", "split_words(s)"]


def test_exposed_symbols_empty_for_pure_edit_leaf(tmp_path):
    eng = _engine(tmp_path)
    eng._constitution = ["POST /notes takes {text}. src/app.py exposes wsgi_app."]
    eng._goal = "notes"
    # a leaf that owns no route (text mentions no declared path) and declares
    # nothing -> empty, so the gate stays inert (back-compat, no false red).
    node = {"id": "tweak", "title": "polish the wording"}
    assert eng._leaf_exposed_symbols(node) == []


def test_tester_rule_pins_the_symbol_and_forbids_pluralising():
    rule = rw._tester_symbol_rule(
        {"declared_exposes": ["delete_note(payload, query)"]})
    assert "delete_note" in rule
    assert "pluralise" in rule.lower()
    # the exact bug name must NOT be presented as acceptable
    assert "delete_notes" not in rule


def test_tester_rule_without_exposes_still_forbids_invention():
    rule = rw._tester_symbol_rule({})
    low = rule.lower()
    assert "never invent" in low and "pluralise" in low
