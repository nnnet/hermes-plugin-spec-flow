"""Audit STAGE 34 — a MACHINE model of node-spec completeness for the weak-LLM
criterion: ``spec_ir.spec_completeness_gaps(node)`` names, per node class, every
REQUIRED spec aspect that is present-but-empty (node N6, plan 2026-07-06T17-05).

Why this stage exists (the hole N6 closes):
    The engine already owns FORMAT oracles — ``validate_ir`` (closed world),
    ``gherkin_errors``/``feature_library_errors`` (Gherkin grammar),
    ``validate_openapi_library``/``jsonschema_errors`` (OpenAPI/JSON Schema).
    They all answer "is the carrier the node DOES have VALID?". None answers the
    orthogonal, prior question the single criterion actually turns on: "does the
    node CARRY every aspect a weak LLM needs, or is a mandatory aspect simply
    ABSENT?". A node can pass every format oracle and still be un-buildable by a
    weak model because it declares no requirements, no error/edge cases, or
    exposes symbols with untyped signatures — none of which is a FORMAT defect,
    so the format oracles stay silent (a green-but-hollow spec).

    N6 formalises "complete for a weak LLM" as DATA: a pure detector that (1)
    classifies the node (HTTP leaf vs non-HTTP code leaf vs branch), (2) decides
    which content aspects are APPLICABLE to that class, and (3) returns the
    APPLICABLE-yet-EMPTY aspects as structured gaps ``{aspect, why}``. An aspect
    that does not apply to the class is EXPLICITLY ``n/a`` and never a gap — a
    non-HTTP leaf missing an HTTP interface is legitimate, not a hole. This is
    the DEFINITION of completeness as data; the gate that EMITS a milestone from
    it is N2 (a later node) — this node only detects.

Ratchet (RED before code): the RED cases below MUST fail on the pre-N6 engine —
either ``spec_completeness_gaps`` does not exist yet, or (once a naive stub is
imagined) a node with a VALID carrier but NO requirements / NO error-or-edge
cases / UNTYPED exposed signatures is not reported incomplete. They go GREEN
once the detector names each such missing mandatory aspect.

Both directions (v151 lesson): a COMPLETE node — every applicable aspect
present — must yield ZERO gaps, and an INAPPLICABLE aspect (HTTP interface on a
storage leaf, public API on an HTTP leaf) must NEVER appear as a gap. A detector
audited only for misses could sink a run with one false positive.

Test: run ``python -m pytest tests/audit/test_spec_completeness_gaps.py -q``.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_ir  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: hand-built IR nodes, one per class, complete and holed.
# ---------------------------------------------------------------------------

_COMPLETE_BEHAVIOR = (
    "Feature: list notes\n"
    "  Scenario: two notes stored\n"
    "    Given a store with two notes\n"
    "    When list_notes is called\n"
    "    Then it returns both notes\n"
    "\n"
    "  Scenario Outline: empty store edge\n"
    "    Given an empty store\n"
    "    When list_notes is called\n"
    "    Then it returns <result>\n"
    "\n"
    "    Examples:\n"
    "      | result |\n"
    "      | []     |\n"
)


def _complete_code_leaf():
    """A non-HTTP storage/lib leaf that carries EVERY applicable aspect."""
    return {
        "files": ["src/db_layer.py"],
        "behavior": _COMPLETE_BEHAVIOR,
        "symbols": {
            "exposes": [
                {
                    "name": "list_notes",
                    "args": ["conn: sqlite3.Connection"],
                    "returns": "list[dict]",
                    "raises": ["sqlite3.OperationalError"],
                },
            ],
        },
        "dependencies": [],
        "effects": ["read_fs"],
        "requirements": [{"name": "sqlite-utils", "version": "3.0"}],
    }


def _complete_http_leaf():
    """An HTTP leaf that owns a route with a typed body and an error response."""
    return {
        "files": ["src/notes_api.py"],
        "openapi": {
            "openapi": spec_ir.OPENAPI_VERSION,
            "info": {"title": "notes", "version": "1"},
            "paths": {
                "/notes": {
                    "post": {
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"body": {"type": "string"}},
                                    }
                                }
                            },
                        },
                        "responses": {
                            "201": {
                                "description": "created",
                                "content": {
                                    "application/json": {
                                        "schema": {"type": "object"}
                                    }
                                },
                            },
                            "422": {
                                "description": "invalid body",
                                "content": {
                                    "application/json": {
                                        "schema": {"type": "object"}
                                    }
                                },
                            },
                        },
                    }
                }
            },
        },
        "scenarios": [{"when": {"method": "POST", "path": "/notes"},
                       "then": {"status": "201"}}],
        "dependencies": [],
        "effects": [],
        "requirements": ["flask"],
    }


def _branch():
    """A branch node — delegates to children, executes nothing itself."""
    return {"children": ["a", "b"], "files": []}


# ---------------------------------------------------------------------------
# The detector must exist and be a pure function over a node dict.
# ---------------------------------------------------------------------------

def test_detector_exists():
    assert hasattr(spec_ir, "spec_completeness_gaps"), (
        "N6: spec_ir must expose spec_completeness_gaps(node) — the machine "
        "model of node-spec completeness for the weak-LLM criterion")


def _aspects(gaps):
    return {g["aspect"] for g in gaps}


# ---------------------------------------------------------------------------
# GREEN direction — a complete node of each class yields ZERO gaps.
# ---------------------------------------------------------------------------

def test_complete_code_leaf_has_no_gaps():
    gaps = spec_ir.spec_completeness_gaps(_complete_code_leaf())
    assert gaps == [], (
        "a non-HTTP code leaf carrying behaviour + typed exposes + reqs + "
        "edge Examples must be complete, got gaps: %r" % gaps)


def test_complete_http_leaf_has_no_gaps():
    gaps = spec_ir.spec_completeness_gaps(_complete_http_leaf())
    assert gaps == [], (
        "an HTTP leaf owning a typed route with an error response must be "
        "complete, got gaps: %r" % gaps)


# ---------------------------------------------------------------------------
# Applicability — an inapplicable aspect is n/a, NEVER a gap.
# ---------------------------------------------------------------------------

def test_http_interface_not_required_of_a_storage_leaf():
    gaps = spec_ir.spec_completeness_gaps(_complete_code_leaf())
    assert "http_interface" not in _aspects(gaps), (
        "a non-HTTP storage leaf owns no route — the HTTP interface aspect is "
        "n/a and must not be reported as a gap")


def test_public_api_not_required_of_an_http_leaf():
    gaps = spec_ir.spec_completeness_gaps(_complete_http_leaf())
    assert "public_api" not in _aspects(gaps), (
        "an HTTP leaf's contract lives in its OpenAPI document, not in "
        "symbols.exposes — the public-API aspect is n/a for it")


def test_branch_requires_no_behaviour_or_interface():
    gaps = _aspects(spec_completeness := spec_ir.spec_completeness_gaps(_branch()))
    assert "behavior" not in gaps and "http_interface" not in gaps \
        and "public_api" not in gaps, (
        "a branch executes nothing itself — behaviour/interface/public-API are "
        "n/a; got %r" % spec_completeness)


# ---------------------------------------------------------------------------
# RED direction — each mandatory-yet-empty aspect is named as a gap.
# ---------------------------------------------------------------------------

def test_missing_requirements_is_a_gap():
    node = _complete_code_leaf()
    node["requirements"] = []
    node["dependencies"] = ["sqlite-utils"]  # imported but never requested
    gaps = spec_ir.spec_completeness_gaps(node)
    assert "requirements" in _aspects(gaps), (
        "a node that imports a dependency but declares no traceable "
        "requirement is incomplete — a weak LLM cannot resolve it")


def test_missing_edge_cases_is_a_gap():
    node = _complete_code_leaf()
    node["behavior"] = (
        "Feature: list notes\n"
        "  Scenario: two notes stored\n"
        "    Given a store with two notes\n"
        "    When list_notes is called\n"
        "    Then it returns both notes\n"
    )  # a single happy path, no Examples / edge Scenario
    gaps = spec_ir.spec_completeness_gaps(node)
    assert "errors_edges" in _aspects(gaps), (
        "a behaviour spec with only a happy path declares no error/edge "
        "cases — a weak LLM will not invent them")


def test_untyped_exposes_is_a_public_api_gap():
    node = _complete_code_leaf()
    node["symbols"]["exposes"] = [
        {"name": "list_notes", "args": ["conn"], "returns": "", "raises": []},
    ]  # untyped arg, no return type
    gaps = spec_ir.spec_completeness_gaps(node)
    assert "public_api" in _aspects(gaps), (
        "an exposed callable with untyped args and no return type is not a "
        "usable contract for a weak LLM")


def test_empty_behaviour_is_a_behavior_gap():
    node = _complete_code_leaf()
    node["behavior"] = ""
    gaps = spec_ir.spec_completeness_gaps(node)
    assert "behavior" in _aspects(gaps), (
        "an executable node with no behaviour carrier has nothing to build "
        "against")


def test_http_leaf_without_error_response_is_an_edge_gap():
    node = _complete_http_leaf()
    node["openapi"]["paths"]["/notes"]["post"]["responses"].pop("422")
    gaps = spec_ir.spec_completeness_gaps(node)
    assert "errors_edges" in _aspects(gaps), (
        "an HTTP route declaring only a success response leaves the error "
        "surface unspecified — a weak LLM will not invent the failure modes")


def test_missing_architecture_is_a_gap():
    node = _complete_code_leaf()
    node.pop("files")
    node.pop("effects")
    gaps = spec_ir.spec_completeness_gaps(node)
    assert "architecture" in _aspects(gaps), (
        "a node without declared files/effects gives a weak LLM no placement "
        "or side-effect envelope")


# ---------------------------------------------------------------------------
# Structure — each gap is a {aspect, why} record, why non-empty.
# ---------------------------------------------------------------------------

def test_each_gap_is_a_structured_record_with_a_reason():
    node = _complete_code_leaf()
    node["behavior"] = ""
    node["requirements"] = []
    node["dependencies"] = ["sqlite-utils"]
    gaps = spec_ir.spec_completeness_gaps(node)
    assert gaps, "expected gaps for a holed node"
    for g in gaps:
        assert isinstance(g, dict), "a gap must be a mapping, got %r" % (g,)
        assert g.get("aspect"), "a gap must name its aspect: %r" % (g,)
        assert str(g.get("why") or "").strip(), (
            "a gap must state WHY the aspect is mandatory and empty: %r" % (g,))
