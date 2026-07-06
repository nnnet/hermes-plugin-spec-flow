"""Audit STAGE 35 — a SEPARATE engine gate asserts that EVERY node's spec is in a
machine STANDARD from the registry AND complete-for-a-weak-LLM, else the node is
not READY (node N2, plan 2026-07-06T17-05).

Why this stage exists (the hole N2 closes):
    K2/S22 (``decomposer_openapi``) validates ONLY http nodes — the OpenAPI 3.1
    document's grammar. N3/S32 (``decomposer_gherkin``) validates ONLY non-HTTP
    code leaves — the Gherkin behaviour carrier + typed exposes. Two holes remain
    that NEITHER gate owns, and the user asked this be checked "as a SEPARATE gate
    inside the engine":
      (1) FORMAT COVERAGE — a leaf that is NEITHER http NOR a ``.py`` code leaf
          (class ``other``: a prose-only node with no machine carrier from the
          registry at all) sails through both gates untouched. Prose is never a
          carrier; such a node must be a NAMED refusal.
      (2) COMPLETENESS — a node can carry a format-VALID machine carrier and
          still be un-buildable by a weak LLM: an HTTP route with only success
          responses (no error surface), a dependency imported with no traceable
          requirement, missing files/effects. ``spec_completeness_gaps`` (N6)
          models these as DATA, but no engine milestone ACTS on the gaps — N6 is
          a pure detector by design; N2 is the milestone that emits the verdict.

    N2 adds ONE gate, ``standardized_spec``, that for EVERY proposed node
    COLLECTS (does not re-derive): (a) is there a valid machine carrier from the
    registry ``spec_registry.FORMAT_VALIDATORS`` (prose-only => FAIL); (b)
    ``spec_ir.spec_completeness_gaps(node)`` is empty (any gap => FAIL). A miss is
    an attributable ``standardized_spec`` FAIL milestone (the
    ``decomposer_openapi``/``decomposer_gherkin`` precedent) and the fragment does
    NOT enter the IR registry — the node is not READY until the single criterion
    is met.

Ratchet (RED before code): the RED cases below MUST fail on the pre-N2 engine —
a prose-only ``other`` leaf and a format-valid-but-INCOMPLETE http node are both
accepted by ``_accept_decomposer_ir`` with ZERO errors and no
``standardized_spec`` milestone. They go GREEN once the gate collects the
registry-carrier + completeness verdict and refuses.

Both directions (v151 lesson): a COMPLETE node of each class (http route with an
error surface + traceable requirement; a typed non-HTTP code leaf) must stay
SILENT — the gate must never false-red a legitimate node.

Registry is DATA (a new standard = a new adapter row, never a gate rewrite): the
active adapters are OpenAPI / Gherkin / JSON Schema; AsyncAPI / Protobuf /
GraphQL / Smithy / TypeSpec are declared stub adapters, inert until a node of
that class appears.

Test: run ``python -m pytest tests/audit/test_standardized_spec_gate.py -q``.
"""
from __future__ import annotations

import pathlib
import sys

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from harness import run_engine as eng  # noqa: E402
import spec_ir  # noqa: E402


def _engine(tmp_path, interface_policy="ir-required"):
    """An engine whose product owns NO routes by default — leaves are non-HTTP;
    individual tests attach an ``openapi`` document to make an http node."""
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


def _prose_only_other_node():
    """Class ``other``: a leaf with NO ``.py`` source files (so not a code leaf),
    NO ``openapi`` document (not http) and NO ``children`` (not a branch). Every
    key is closed-world VALID (so it clears the decomposer_ir structural gate),
    yet it carries NO machine standard from the registry — its behaviour would
    live only in prose. Neither the openapi gate nor the gherkin gate touches
    this class; only standardized_spec does."""
    return {"files": ["docs/policy.md"], "effects": []}


def _http_scenario():
    """One closed ``{requirement, when, then}`` scenario — the behaviour carrier
    the N6 model requires for an http node (behavior aspect)."""
    return {"requirement": "list notes",
            "when": {"method": "GET", "path": "/notes"},
            "then": {"status": 200}}


def _http_node(openapi_doc, **extra):
    # a COMPLETE http node states every applicable N6 aspect: behaviour
    # (scenarios), placement/side-effect envelope (files+effects) and the
    # interface/data carrier (the OpenAPI document). Missing effects reads as an
    # architecture gap, a missing scenario as a behavior gap — declare both so a
    # GREEN node is complete by construction.
    node = {"files": ["src/api.py"], "effects": [],
            "scenarios": [_http_scenario()], "openapi": openapi_doc}
    node.update(extra)
    return node


def _openapi_doc(routes_with_errors: bool):
    """A grammatically VALID OpenAPI 3.1 document whose operation records a
    response body schema (the data contract). When ``routes_with_errors`` is
    False it declares ONLY a 200 response — format-valid yet an incomplete error
    surface (``spec_completeness_gaps`` -> errors_edges)."""
    ok = {"description": "ok",
          "content": {"application/json": {"schema": {"type": "array"}}}}
    responses = {"200": ok}
    if routes_with_errors:
        responses["404"] = {"description": "not found"}
    return {
        "openapi": spec_ir.OPENAPI_VERSION,
        "info": {"title": "notes", "version": "1.0.0"},
        "paths": {"/notes": {"get": {"responses": responses}}},
    }


# -- registry is DATA -------------------------------------------------------

def test_registry_is_a_data_table_of_format_to_validator():
    """The registry that decides "is there a valid machine carrier?" is DATA —
    a table ``format -> validator``, so a new standard is a new row, never a gate
    rewrite. The active formats (openapi/gherkin/jsonschema) resolve to a real
    callable oracle; the stub formats are declared but inert."""
    import spec_registry  # noqa: E402
    table = spec_registry.FORMAT_VALIDATORS
    assert isinstance(table, dict)
    for fmt in ("openapi", "gherkin", "jsonschema"):
        assert fmt in table, "active standard %r must be a registry row" % fmt
        assert callable(table[fmt].get("validator")), (
            "an active adapter must resolve to a callable oracle")
        assert table[fmt].get("active") is True
    for stub in ("asyncapi", "protobuf", "graphql", "smithy", "typespec"):
        assert stub in table, (
            "the stub adapter %r must be declared (extensible: activated when a "
            "node of that class appears) — not a hard-coded gate branch" % stub)
        assert table[stub].get("active") is False


# -- S35.1 a prose-only node (no registry carrier) is a NAMED refusal -------

def test_prose_only_node_is_named_refusal_not_silent(tmp_path):
    """RED on pre-N2: a class-``other`` prose-only leaf — no openapi, no code-leaf
    Gherkin carrier — is touched by NEITHER the openapi gate nor the gherkin gate
    and passes ``_accept_decomposer_ir`` with ZERO errors. GREEN: the
    standardized_spec gate refuses it — a node with no machine carrier from the
    registry is an attributable FAIL, never a silent prose pass."""
    e = _engine(tmp_path)
    out = {"atomic": True,
           "ir": _machine_part({"policy": _prose_only_other_node()})}
    errs = e._accept_decomposer_ir({"id": "policy"}, out)
    assert errs, (
        "a prose-only node carrying no machine standard from the registry MUST "
        "be refused — prose is never a carrier (the single criterion)")
    assert any("policy" in m for m in errs)
    fails = _gate_events(e, "standardized_spec", verdict="FAIL")
    assert fails, (
        "the refusal must be a MILESTONE on gate standardized_spec — a SEPARATE "
        "gate inside the engine, the decomposer_openapi/gherkin precedent")
    reg = e.__dict__.get("_decomposer_ir_nodes") or {}
    assert "policy" not in reg, (
        "a standard-refused fragment must NOT enter the IR registry")


# -- S35.2 a format-valid but INCOMPLETE carrier reds -----------------------

def test_valid_but_incomplete_http_carrier_is_refused(tmp_path):
    """RED on pre-N2: an http node with a grammatically VALID OpenAPI document
    that declares ONLY a success response — format-valid, so decomposer_openapi
    passes — is INCOMPLETE for a weak LLM (no error surface). No gate acts on the
    completeness gap. GREEN: standardized_spec collects
    ``spec_completeness_gaps`` and refuses the incomplete carrier."""
    e = _engine(tmp_path)
    node = _http_node(_openapi_doc(routes_with_errors=False))
    out = {"atomic": True, "ir": _machine_part({"api": node})}
    # sanity: the completeness detector (N6) DOES see the hole
    assert any(g["aspect"] == "errors_edges"
               for g in spec_ir.spec_completeness_gaps(node)), (
        "premise: an http route with only a success response is an errors_edges "
        "gap by the N6 model")
    errs = e._accept_decomposer_ir({"id": "api"}, out)
    assert errs, (
        "a format-VALID but INCOMPLETE carrier (no error surface) MUST red — "
        "completeness-for-a-weak-LLM is the single criterion the gate enforces")
    assert _gate_events(e, "standardized_spec", verdict="FAIL"), (
        "incompleteness reds on the standardized_spec gate")


# -- S35.3 a COMPLETE http node passes (GREEN — never false-red) ------------

def test_complete_http_node_passes_the_gate(tmp_path):
    """Both directions: a COMPLETE http node — a valid OpenAPI document with an
    error response and a traceable requirement — is accepted with no
    standardized_spec FAIL. The gate must not false-red a conforming node."""
    e = _engine(tmp_path)
    node = _http_node(_openapi_doc(routes_with_errors=True))
    out = {"atomic": True, "ir": _machine_part({"api": node})}
    assert spec_ir.spec_completeness_gaps(node) == [], (
        "premise: a route with an error response has no completeness gap")
    errs = e._accept_decomposer_ir({"id": "api"}, out)
    assert errs == [], (
        "a COMPLETE standardized node must be accepted — the gate must not "
        "false-red a conforming http node: %r" % errs)
    assert not _gate_events(e, "standardized_spec", verdict="FAIL")
    assert _gate_events(e, "standardized_spec", verdict="PASS"), (
        "a node that cleared the standard+completeness gate emits a NAMED PASS "
        "(symmetry with decomposer_openapi/gherkin M3)")
    assert _gate_events(e, "decomposer_ir", verdict="PASS")
    reg = e.__dict__.get("_decomposer_ir_nodes") or {}
    assert "api" in reg


# -- S35.4 a COMPLETE non-HTTP code leaf passes (GREEN, other class) --------

def test_complete_code_leaf_passes_the_gate(tmp_path):
    """The gherkin-carrier class must ALSO clear the standardized_spec gate: a
    typed non-HTTP code leaf with a behaviour feature, typed exposes and edge
    cases has a registry carrier (gherkin) and zero completeness gaps — the gate
    stays silent (never double-reds what N3 already accepts)."""
    e = _engine(tmp_path)
    feature = "\n".join([
        "Feature: cache",
        "  Scenario: get returns a stored value",
        "    Given a cache with key 'k'",
        "    When get(key) is called",
        "    Then the stored value is returned",
        "  Scenario: get raises on a missing key",
        "    Given an empty cache",
        "    When get(key) is called",
        "    Then a KeyError is raised",
        "",
    ])
    node = {"files": ["src/cache.py"],
            "effects": [],  # explicit placement/side-effect envelope (N6)
            "behavior": feature,
            "symbols": {"exposes": [
                {"name": "get", "args": ["key: str"],
                 "returns": "object", "raises": ["KeyError"]}]}}
    out = {"atomic": True, "ir": _machine_part({"cache": node})}
    assert spec_ir.spec_completeness_gaps(node) == [], (
        "premise: a typed code leaf with edge cases has no completeness gap")
    errs = e._accept_decomposer_ir({"id": "cache"}, out)
    assert errs == [], (
        "a COMPLETE non-HTTP code leaf must clear the standardized_spec gate "
        "too: %r" % errs)
    assert not _gate_events(e, "standardized_spec", verdict="FAIL")
