"""Audit stage S42 (node Q4, plan 2026-07-06T20-15 systemic-design-fixes):
conformance from the machine contract via a READY oracle. Catalog A/conformance
of the systemic review: the compiled leaf test pinned a response with a shallow,
hand-rolled top-level check (required present, primitive types, additional-
Properties:false). A body that satisfies that shallow pin but violates a NESTED
constraint — an enum value, a string format, an array item type — slipped
through green. S42 conforms the body against the FULL OpenAPI response schema
with the maintained openapi-schema-validator (Draft 2020-12 / OAS 3.1), a ready
library oracle, so the drift reds.

  * S42.1 — the compiled source wires the ready oracle (`_conform` +
    `OAS31Validator`) for every shaped response.
  * S42.2 — the oracle catches an enum violation the shallow pin misses (RED:
    valid body passes but the out-of-enum body must red; the shallow asserts
    provably do NOT mention the enum).
  * S42.3 — the oracle import is UNGUARDED (fail-closed, Q2/S38): a missing
    library is a HARD error, never a silent skip.

Deterministic: known-answer IR + exec of the emitted helper, no network.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import spec_conformance  # noqa: E402
import spec_ir  # noqa: E402


def _doc(nid, paths):
    return {"openapi": spec_ir.OPENAPI_VERSION,
            "info": {"title": "node %s interface" % nid, "version": "1"},
            "paths": paths}


def _enum_shape():
    # a CLOSED object response whose 'kind' is constrained to an enum — the
    # shallow pin checks presence + additionalProperties but NOT the enum.
    return {"type": "object",
            "properties": {"kind": {"enum": ["note", "task"]},
                           "id": {"type": "integer"}},
            "required": ["kind"],
            "additionalProperties": False}


def _enum_node():
    get = {"responses": {"200": {"description": "contracted",
            "content": {"application/json": {"schema": _enum_shape()}}}}}
    return {"files": ["src/kinds.py"], "effects": [],
            "openapi": _doc("kinds", {"/kinds": {"get": get}}),
            "scenarios": []}


def _ir(nodes):
    return {"format": spec_ir.IR_FORMAT, "product": {}, "nodes": nodes}


# ── S42.1: ready oracle wired into the compiled source ──────────────────────

def test_conform_oracle_wired_into_compiled_source():
    src = spec_conformance.compile_leaf_tests(_ir({"kinds": _enum_node()}),
                                              "kinds")
    assert "_conform(" in src, "the compiled test must call the ready oracle"
    assert "OAS31Validator" in src, \
        "the compiled test must use openapi-schema-validator (ready lib)"
    assert "iter_errors" in src, "the oracle must enumerate schema errors"


# ── S42.2: catches an enum violation the shallow pin misses ─────────────────

def _conform_ns():
    ns: dict = {}
    exec(spec_conformance._HELPER_CONFORM, ns)  # noqa: S102 — audited helper
    return ns


def test_oracle_catches_enum_violation():
    conform = _conform_ns()["_conform"]
    shape = _enum_shape()
    # a valid body passes silently
    conform(shape, {"kind": "note", "id": 1}, "GET /kinds")
    # an out-of-enum value (still a str, so the shallow type pin would pass)
    # must red through the library oracle
    import pytest
    with pytest.raises(AssertionError):
        conform(shape, {"kind": "zzz"}, "GET /kinds")


def test_shallow_pin_provably_misses_the_enum():
    # the shallow hand-rolled asserts never reference the enum — proof the
    # ready oracle is doing work the pin cannot.
    shallow = "\n".join(spec_conformance._shape_body_asserts(_enum_shape(),
                                                             "GET /kinds"))
    assert "note" not in shallow and "task" not in shallow, \
        "the shallow pin must NOT encode the enum — that is the oracle's job"


# ── S42.3: the oracle import is fail-closed (no silent skip) ─────────────────

def test_oracle_import_is_unguarded_fail_closed():
    helper = spec_conformance._HELPER_CONFORM
    assert "import" in helper and "OAS31Validator" in helper
    assert "except ImportError" not in helper and "pytest.skip" not in helper, \
        "a missing oracle must be a HARD error (fail-closed), never a skip"
