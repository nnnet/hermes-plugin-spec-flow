"""S35/N2: the machine-standard REGISTRY — a DATA table ``format -> validator``
that decides whether a node carries a valid machine standard, so the
``standardized_spec`` gate never hard-codes a per-format branch.

Why (user 2026-07-06, hard rule): a node's spec must be in a machine STANDARD
from a registry, never prose; and the registry must be EXTENSIBLE — a new
standard is a NEW ADAPTER ROW, not a rewrite of the gate. The engine already
grew two format-specific seams (``decomposer_openapi`` for OpenAPI, ``decomposer
_gherkin`` for Gherkin/symbols); N2 needs to reason over ALL formats uniformly
("is there ANY valid carrier?"), so the format->validator mapping is lifted out
of the gate into this data table.

What: ``FORMAT_VALIDATORS`` maps a standard name to an adapter record
``{standard, validator, active}``. The ACTIVE adapters (openapi / gherkin /
jsonschema) resolve ``validator`` to a real oracle callable already used
elsewhere (``spec_openapi.validate_openapi_library``, ``spec_ir.gherkin_errors``,
``spec_ir.jsonschema_errors``) — the registry COLLECTS them, it does not
re-implement validation. The STUB adapters (asyncapi / protobuf / graphql /
smithy / typespec) are declared with ``active=False`` and a no-op validator;
they light up when a decomposer first proposes a node of that class, without the
gate changing. ``node_carrier_format(node)`` classifies which registry standard a
node carries (or ``None`` == prose-only, no machine carrier).

Test: tests/audit/test_standardized_spec_gate.py (S35).
"""
from __future__ import annotations

from typing import Any, Callable, Optional


def _validate_openapi(node: Any) -> list:
    """Validate the node's OpenAPI 3.1 document with the maintained library
    oracle. Empty list == standard-valid (or oracle absent — dev-optional)."""
    try:
        import spec_openapi
    except ImportError:  # pragma: no cover - flat layout guarantees import
        return []
    doc = (node or {}).get("openapi") if isinstance(node, dict) else None
    if not isinstance(doc, dict) or not doc:
        return []
    return list(spec_openapi.node_openapi_library_errors(
        str((node or {}).get("id") or "?"), doc))


def _validate_gherkin(node: Any) -> list:
    """Validate the node's Gherkin behaviour grammar with the ready-made
    ``gherkin-official`` parser (via ``spec_ir.gherkin_errors`` over a one-node
    IR). Empty list == grammatical (or parser absent — degrade)."""
    try:
        import spec_ir
    except ImportError:  # pragma: no cover
        return []
    nid = str((node or {}).get("id") or "n") if isinstance(node, dict) else "n"
    return list(spec_ir.gherkin_errors({"nodes": {nid: node}}))


def _validate_jsonschema(node: Any) -> list:
    """Validate the node's data-shape carrier with the ``jsonschema`` oracle
    (2020-12) via ``spec_ir.jsonschema_errors`` over a one-node IR. Empty list ==
    structure-valid."""
    try:
        import spec_ir
    except ImportError:  # pragma: no cover
        return []
    nid = str((node or {}).get("id") or "n") if isinstance(node, dict) else "n"
    return list(spec_ir.jsonschema_errors(
        {"format": spec_ir.IR_FORMAT, "product": {}, "nodes": {nid: node}}))


def _stub_validator(_node: Any) -> list:
    """A stub adapter carries no live oracle yet; it is inert until activated,
    so it never fires (an inactive standard cannot be a node's carrier)."""
    return []


def _adapter(standard: str, validator: Callable[[Any], list],
             active: bool) -> dict:
    return {"standard": standard, "validator": validator, "active": active}


# The registry as DATA. A new standard = a new row here, never a gate branch.
FORMAT_VALIDATORS: dict = {
    # active adapters — real oracle libraries already in the codebase
    "openapi": _adapter("OpenAPI 3.1", _validate_openapi, True),
    "gherkin": _adapter("Gherkin", _validate_gherkin, True),
    "jsonschema": _adapter("JSON Schema 2020-12", _validate_jsonschema, True),
    # stub adapters — declared, inert until a node of the class appears
    "asyncapi": _adapter("AsyncAPI", _stub_validator, False),
    "protobuf": _adapter("Protocol Buffers", _stub_validator, False),
    "graphql": _adapter("GraphQL SDL", _stub_validator, False),
    "smithy": _adapter("Smithy", _stub_validator, False),
    "typespec": _adapter("TypeSpec", _stub_validator, False),
}


def node_carrier_format(node: Any) -> Optional[str]:
    """Which ACTIVE registry standard does this node carry as its machine
    interface, or ``None`` when it carries no machine carrier at all (prose-only)?

    Why: the standardized_spec gate must distinguish "carrier present, validate
    it" from "no carrier — prose-only, refuse". The class-to-carrier mapping is
    the same one the completeness model uses (``spec_ir._node_class``): an http
    node carries ``openapi``; a non-HTTP ``.py`` code leaf carries ``gherkin``
    (its behaviour feature/scenarios); a branch delegates and carries none of its
    own; anything else is prose-only.
    What: returns an ACTIVE format key from ``FORMAT_VALIDATORS`` or ``None``.
    Test: tests/audit/test_standardized_spec_gate.py.
    """
    if not isinstance(node, dict):
        return None
    if node.get("openapi"):
        return "openapi"
    if node.get("children"):
        # a branch delegates to children; it owns no leaf carrier of its own,
        # so it is not a prose-only leaf — the caller treats branches as n/a.
        return None
    try:
        import spec_gherkin
        is_code_leaf = spec_gherkin.is_code_leaf("?", node)
    except ImportError:  # pragma: no cover
        is_code_leaf = bool([f for f in (node.get("files") or [])
                             if str(f).endswith(".py")])
    if is_code_leaf and (node.get("behavior") or node.get("scenarios")):
        return "gherkin"
    return None


def validate_carrier(fmt: str, node: Any) -> list:
    """Run the registry adapter for ``fmt`` over ``node``; empty list == valid.
    Unknown or inactive format => empty (an inactive adapter never fires)."""
    rec = FORMAT_VALIDATORS.get(fmt)
    if not rec or not rec.get("active"):
        return []
    return list(rec["validator"](node))
