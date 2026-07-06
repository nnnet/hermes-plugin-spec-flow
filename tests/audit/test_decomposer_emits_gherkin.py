"""Audit STAGE 32 — a NON-HTTP code leaf (storage/lib/behaviour, like the v166
``db_layer``) carries a MACHINE behaviour spec (a Gherkin feature + complete
``symbols.exposes``) as its PRIMARY interface, LIBRARY-validated at the
decomposer seam (node N3, plan 2026-07-06T17-05).

Why this stage exists (the hole N3 closes — the v166 class):
    K2 (Stage 22) made the machine OpenAPI document the primary carrier for
    HTTP nodes and library-refused an invalid one at ``_accept_decomposer_ir``.
    But a NON-HTTP leaf owns no routes, so it carries no ``openapi`` document —
    the K2 gate has nothing to validate. In run v166 the ``db_layer`` node
    (a SQLite storage module contracting three functions —
    ``connect``/``insert_note``/``list_notes``) arrived as PROSE: the function
    contract lived in the EARS text and the machine ``symbols.exposes`` was
    EMPTY. The seam accepted it with ZERO errors — no machine behaviour
    carrier, no typed signatures — and the hole only surfaced far downstream as
    ``spec_lint FAIL #20`` ("exposes NO contracted symbols"). A weak LLM handed
    that node has to GUESS every signature, return type and error — exactly the
    "not complete for a weak LLM" failure the single criterion forbids.

    N3 makes the machine behaviour spec the PRIMARY carrier for a non-HTTP code
    leaf: it MUST emit a Gherkin ``behavior`` feature (parsed by the
    ``gherkin-official`` library, the N1/S31 oracle) AND a COMPLETE
    ``symbols.exposes`` — every public callable with typed args, a return type
    and a declared error surface. A leaf that carries neither, or an INCOMPLETE
    carrier (exposes without types/returns/errors), is an HONEST NAMED refusal
    at the seam (milestone gate ``decomposer_gherkin`` FAIL — the
    ``decomposer_openapi`` precedent), never a silent pass to prose.

Ratchet (RED before code): the RED cases below MUST fail on the pre-N3 engine —
a non-HTTP leaf with a prose contract and empty ``symbols`` is accepted by
``_accept_decomposer_ir`` with ZERO errors (the v166 bug), and an incomplete
carrier (untyped exposes, no behaviour feature) does not red either. They go
GREEN once the seam requires a complete machine behaviour carrier for every
non-HTTP code leaf.

Both directions (v151 lesson): a COMPLETE non-HTTP carrier (full Gherkin
feature + typed exposes with returns and errors) must stay SILENT — the gate
must never false-red a legitimate storage/lib leaf.

Test: run ``python -m pytest tests/audit/test_decomposer_emits_gherkin.py -q``.
"""
from __future__ import annotations

import pathlib
import sys

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from harness import run_engine as eng  # noqa: E402
import spec_ir  # noqa: E402
import spec_gherkin  # noqa: E402


def _engine(tmp_path, interface_policy="ir-required"):
    """An engine whose product owns NO routes — a pure storage/lib product, so
    every leaf is a non-HTTP code leaf (the v166 db_layer shape)."""
    e = eng.Engine(workspace=str(tmp_path / "wk"), depth=eng.DEPTH_SPEC,
                   interface_policy=interface_policy)
    e._product_contract = lambda: {
        "entry": "src/app.py",
        "boot": {},
        "routes": [],
    }
    return e


def _machine_part(nodes):
    return {"format": spec_ir.IR_FORMAT, "nodes": nodes}


def _gate_events(e, gate, verdict=None):
    return [ev for ev in e.events
            if ev.gate == gate and (verdict is None or ev.verdict == verdict)]


def _prose_db_layer_node():
    """The v166 shape: a non-HTTP code leaf whose contract lives ONLY in prose.
    No ``openapi`` (owns no routes), no ``behavior`` feature, and an EMPTY
    ``symbols`` — the machine carrier the weak LLM needs is absent."""
    return {"files": ["src/db_layer.py"]}


def _complete_db_layer_node():
    """A COMPLETE non-HTTP behaviour carrier: a machine Gherkin feature naming
    each callable's behaviour AND typed ``symbols.exposes`` with return types
    and a declared error surface — enough for a weak LLM with no guessing."""
    feature = "\n".join([
        "Feature: db_layer sqlite storage",
        "  Scenario: connect opens a database handle",
        "    Given a filesystem path 'notes.db'",
        "    When connect(path) is called",
        "    Then a sqlite3.Connection is returned",
        "  Scenario: insert_note stores a note and returns its id",
        "    Given an open connection and body 'hello'",
        "    When insert_note(conn, body) is called",
        "    Then the new integer row id is returned",
        "  Scenario: list_notes returns all stored notes",
        "    Given an open connection with two notes",
        "    When list_notes(conn) is called",
        "    Then a list of note dicts is returned",
        "",
    ])
    exposes = [
        {"name": "connect", "args": ["path: str"],
         "returns": "sqlite3.Connection", "raises": ["sqlite3.OperationalError"]},
        {"name": "insert_note",
         "args": ["conn: sqlite3.Connection", "body: str"],
         "returns": "int", "raises": ["sqlite3.IntegrityError"]},
        {"name": "list_notes", "args": ["conn: sqlite3.Connection"],
         "returns": "list[dict]", "raises": []},
    ]
    return {"files": ["src/db_layer.py"],
            "effects": [],  # explicit placement/side-effect envelope (N6 architecture)
            "behavior": feature,
            "symbols": {"exposes": exposes}}


# -- premise: the node genuinely IS a non-HTTP code leaf ----------------------

def test_db_layer_is_recognised_as_a_non_http_code_leaf():
    """The premise of this stage: a node with src ``files`` but no ``openapi``
    and no ``children`` is a non-HTTP code leaf — the class that owns no route
    yet still contracts callable symbols. If this classifier stops holding the
    RED cases below no longer describe the v166 hole."""
    assert spec_gherkin.is_code_leaf("db_layer", _prose_db_layer_node()), (
        "a leaf with src files, no openapi, no children is a non-HTTP code "
        "leaf — the storage/lib class the v166 db_layer belongs to")
    # an HTTP node (owns an openapi document) is NOT a behaviour-carrier leaf
    assert not spec_gherkin.is_code_leaf("api", {
        "files": ["src/api.py"],
        "openapi": {"openapi": spec_ir.OPENAPI_VERSION, "info": {}, "paths": {}}})
    # a branch (has children) is not a leaf
    assert not spec_gherkin.is_code_leaf("parent", {
        "files": ["src/x.py"], "children": ["c1"]})


# -- S32.1 a prose-only non-HTTP leaf is a NAMED refusal, not a silent pass ----

def test_prose_non_http_leaf_is_named_refusal_not_silent(tmp_path):
    """RED on pre-N3: the v166 db_layer (prose contract, empty symbols, no
    behaviour feature) sails through ``_accept_decomposer_ir`` with ZERO errors.
    GREEN: the seam refuses it — a non-HTTP code leaf with no machine behaviour
    carrier is an attributable ``decomposer_gherkin`` FAIL milestone."""
    e = _engine(tmp_path)
    out = {"atomic": True,
           "ir": _machine_part({"db_layer": _prose_db_layer_node()})}
    errs = e._accept_decomposer_ir({"id": "db_layer"}, out)
    assert errs, (
        "a NON-HTTP code leaf carrying no machine behaviour spec (no Gherkin "
        "feature, empty symbols.exposes) MUST be refused at the seam — this is "
        "the exact v166 db_layer hole; a weak LLM cannot build it without "
        "guessing every signature")
    assert any("db_layer" in m for m in errs)
    fails = _gate_events(e, "decomposer_gherkin", verdict="FAIL")
    assert fails, (
        "the refusal must be a MILESTONE on gate decomposer_gherkin, the "
        "decomposer_openapi precedent — a visible honest FAIL, not a whisper")
    reg = e.__dict__.get("_decomposer_ir_nodes") or {}
    assert "db_layer" not in reg, (
        "a behaviour-refused fragment must NOT enter the IR registry")


# -- S32.2 an INCOMPLETE carrier (untyped, no errors) also reds ----------------

def test_incomplete_carrier_is_refused(tmp_path):
    """RED on pre-N3: a machine-ish carrier that is INCOMPLETE for a weak LLM —
    exposes without types/returns/errors and no behaviour feature — is not
    refused. GREEN: incompleteness (the single criterion) is a named refusal."""
    e = _engine(tmp_path)
    incomplete = {"files": ["src/db_layer.py"],
                  # names only: no arg types, no return type, no error surface,
                  # and no Gherkin behaviour feature — a weak LLM must guess
                  "symbols": {"exposes": [{"name": "connect", "args": []}]}}
    out = {"atomic": True, "ir": _machine_part({"db_layer": incomplete})}
    errs = e._accept_decomposer_ir({"id": "db_layer"}, out)
    assert errs, (
        "an INCOMPLETE non-HTTP carrier (untyped exposes, no return, no error "
        "surface, no behaviour feature) MUST red — completeness-for-a-weak-LLM "
        "is the single acceptance criterion")
    assert _gate_events(e, "decomposer_gherkin", verdict="FAIL"), (
        "incompleteness reds on the same named gate")


# -- S32.3 a COMPLETE carrier passes (GREEN — never false-red) -----------------

def test_complete_carrier_passes_the_seam(tmp_path):
    """Both directions: a COMPLETE non-HTTP behaviour carrier (full Gherkin
    feature + typed exposes with returns and errors) is accepted with no
    decomposer_gherkin FAIL — the gate must never false-red a legitimate
    storage/lib leaf. The decomposer_ir PASS still journals."""
    e = _engine(tmp_path)
    out = {"atomic": True,
           "ir": _machine_part({"db_layer": _complete_db_layer_node()})}
    errs = e._accept_decomposer_ir({"id": "db_layer"}, out)
    assert errs == [], (
        "a COMPLETE machine behaviour carrier must be accepted — the gate must "
        "not false-red a conforming non-HTTP leaf: %r" % errs)
    assert not _gate_events(e, "decomposer_gherkin", verdict="FAIL")
    assert _gate_events(e, "decomposer_ir", verdict="PASS"), (
        "an accepted fragment still journals the decomposer_ir PASS")
    reg = e.__dict__.get("_decomposer_ir_nodes") or {}
    assert "db_layer" in reg


# -- S32.4 the behaviour feature grammar is validated by the LIBRARY ----------

def test_behaviour_feature_grammar_is_library_validated():
    """The behaviour carrier is a MACHINE standard: a broken Gherkin feature is
    caught by the ready-made ``gherkin-official`` parser (the N1/S31 oracle),
    not a hand splitter. A valid feature stays silent; a broken one reds."""
    if not spec_gherkin.gherkin_available():
        import pytest
        pytest.skip("gherkin-official not installed — grammar oracle absent")
    good = _complete_db_layer_node()["behavior"]
    assert spec_gherkin.feature_library_errors(good) == [], (
        "a well-formed behaviour feature must parse clean")
    broken = "Feature: db\n    When something happens\n"  # step before Scenario
    assert spec_gherkin.feature_library_errors(broken), (
        "a grammatically broken feature must be caught by the parser library")
