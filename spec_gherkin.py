"""Machine behaviour-spec oracle for NON-HTTP code leaves (node N3, S32).

Why (v166, single criterion 2026-07-06):
    K2/S22 made the OpenAPI 3.1 document the PRIMARY, library-validated carrier
    for HTTP nodes. A non-HTTP leaf (storage/lib, like the v166 ``db_layer``)
    owns no route, so it carries no ``openapi`` document — the K2 gate has
    nothing to validate and the node's function contract silently degraded to
    PROSE with an EMPTY ``symbols.exposes`` (surfacing only far downstream as
    ``spec_lint FAIL #20``). A weak LLM handed such a node must guess every
    signature, return type and error — the exact "not complete for a weak LLM"
    failure the single acceptance criterion forbids.

    This module is the non-HTTP counterpart of ``spec_openapi``'s decomposer
    seam check: it recognises a non-HTTP code leaf and requires it to carry a
    COMPLETE machine behaviour spec — a Gherkin ``behavior`` feature (grammar
    validated by the ready-made ``gherkin-official`` parser, the N1/S31 oracle)
    AND a ``symbols.exposes`` list where every public callable has typed args,
    a return type and a declared error surface. A missing or incomplete carrier
    is an attributable NAMED refusal at the seam, never a silent pass to prose.

What: pure functions over an IR node dict; no engine state. The wiring into the
``_accept_decomposer_ir`` seam (milestone gate ``decomposer_gherkin``) lives in
``spec_flow_runner``.

Test: tests/audit/test_decomposer_emits_gherkin.py.
"""
from __future__ import annotations

from typing import Any, List


# Lazily-loaded gherkin-official parser pieces, mirroring spec_ir._gherkin_parser
# so the grammar oracle stays an optional dev/test dependency: None until first
# lookup, a tuple when present, False when the library is genuinely absent.
_GHERKIN_PARSER: Any = None


def _gherkin_parser() -> Any:
    """The gherkin-official (Parser, TokenScanner, (error types)) if importable,
    else None.

    Why: the parser LIBRARY is the oracle of Gherkin grammar — a dev/test
    dependency, not a hard runtime dependency of the shipped engine. Imported
    lazily and tolerating absence keeps the behaviour-carrier check usable in a
    bare environment (grammar oracle skipped, the deterministic completeness
    check still stands).
    What: imports once, memoises the tuple (or False on ImportError).
    Test: tests/audit/test_decomposer_emits_gherkin.py (S32.4)."""
    global _GHERKIN_PARSER
    if _GHERKIN_PARSER is None:
        try:
            from gherkin.parser import Parser
            from gherkin.token_scanner import TokenScanner
            from gherkin.errors import ParserError, CompositeParserException
            _GHERKIN_PARSER = (Parser, TokenScanner,
                               (ParserError, CompositeParserException))
        except Exception:  # noqa: BLE001 — optional dev/test oracle
            _GHERKIN_PARSER = False
    return _GHERKIN_PARSER or None


def gherkin_available() -> bool:
    """Whether the gherkin-official grammar oracle is importable.

    Why: callers/tests skip the grammar assertion (never false-refuse) when the
    optional library is absent — the S14.7 optional-oracle contract.
    What: True iff the parser library imports.
    Test: gated in tests/audit/test_decomposer_emits_gherkin.py (S32.4)."""
    return _gherkin_parser() is not None


def feature_library_errors(text: str) -> List[str]:
    """Validate a free Gherkin ``.feature`` document with the ready-made parser.

    Why: the behaviour carrier is a machine STANDARD (Gherkin); its grammar is
    enforced by the maintained ``gherkin-official`` library, never a home-grown
    splitter — the same oracle N1/S31 uses for closed HTTP scenarios, here over
    the free behaviour feature a non-HTTP leaf emits.
    What: parses ``text`` and requires a Feature carrying at least one Scenario;
    returns human-readable error strings ([] == grammatical). Absent library =>
    [] (degrade; the deterministic completeness check still runs).
    Test: tests/audit/test_decomposer_emits_gherkin.py (S32.4)."""
    parts = _gherkin_parser()
    if parts is None:
        return []
    Parser, TokenScanner, err_types = parts
    if not isinstance(text, str) or not text.strip():
        return ["behavior feature is empty"]
    try:
        doc = Parser().parse(TokenScanner(text))
    except err_types as exc:  # a broken Gherkin document
        return ["behavior feature gherkin grammar error: %s" % exc]
    feat = doc.get("feature") if isinstance(doc, dict) else None
    if not feat:
        return ["behavior feature declares no Feature"]
    scen = [c for c in (feat.get("children") or []) if c.get("scenario")]
    if not scen:
        return ["behavior feature declares no Scenario "
                "(no behaviour contracted)"]
    return []


def is_code_leaf(nid: str, node: Any) -> bool:
    """Whether ``node`` is a NON-HTTP code leaf — the class that owns no route
    yet still contracts callable symbols (the v166 db_layer / storage / lib).

    Why: only this class needs the behaviour carrier; an HTTP node already
    carries an ``openapi`` document (K2), and a branch delegates to children.
    What: True iff the node builds source ``files``, declares NO ``openapi``
    document and has NO ``children``.
    Test: tests/audit/test_decomposer_emits_gherkin.py
    (test_db_layer_is_recognised_as_a_non_http_code_leaf)."""
    if not isinstance(node, dict):
        return False
    if node.get("openapi"):
        return False
    if node.get("children"):
        return False
    files = node.get("files") or []
    return bool([f for f in files if str(f).endswith(".py")])


def _arg_is_typed(arg: Any) -> bool:
    """An exposed arg is COMPLETE for a weak LLM only when it carries a type.

    What: accepts either the ``"name: type"`` string form or a mapping carrying
    a non-empty ``type``. A bare name (no type) is incomplete."""
    if isinstance(arg, str):
        return ":" in arg and arg.split(":", 1)[1].strip() != ""
    if isinstance(arg, dict):
        return bool(str(arg.get("type") or "").strip())
    return False


def _expose_incompleteness(ent: Any) -> List[str]:
    """The ways ONE exposes entry falls short of complete-for-a-weak-LLM.

    Why: a weak LLM must not guess a signature — every public callable needs a
    name, typed args, a return type and an explicit error surface.
    What: returns error fragments ([] == complete). ``raises`` may be an empty
    list (an explicit "raises nothing" decision) but the KEY must be present."""
    out: List[str] = []
    if not isinstance(ent, dict):
        return ["exposes entry is not a mapping"]
    name = str(ent.get("name") or "").strip()
    if not name:
        out.append("exposes entry has no name")
    args = ent.get("args")
    if not isinstance(args, list):
        out.append("%s: args is not a list" % (name or "?"))
    else:
        untyped = [a for a in args if not _arg_is_typed(a)]
        if untyped:
            out.append("%s: args carry no types %r (a weak LLM must guess)"
                       % (name or "?", untyped))
    if not str(ent.get("returns") or "").strip():
        out.append("%s: no return type declared" % (name or "?"))
    if "raises" not in ent or not isinstance(ent.get("raises"), list):
        out.append("%s: no error surface declared (raises: [] if none)"
                   % (name or "?"))
    return out


def node_behavior_carrier_errors(nid: str, node: Any) -> List[str]:
    """Refuse a NON-HTTP code leaf that lacks a COMPLETE machine behaviour spec.

    Why (N3/S32, the v166 hole): such a leaf's PRIMARY carrier is a Gherkin
    ``behavior`` feature plus a complete ``symbols.exposes`` — not prose. A leaf
    with neither, or an incomplete one, cannot be built by a weak LLM without
    guessing, so it is refused at the decomposer seam (the ``decomposer_openapi``
    precedent for HTTP nodes).
    What: returns a list of "node <nid>: ..." error strings ([] == complete or
    not a non-HTTP code leaf). The Gherkin grammar is library-validated; the
    exposes completeness is deterministic (runs even without the library).
    Test: tests/audit/test_decomposer_emits_gherkin.py (S32.1-S32.3)."""
    if not is_code_leaf(nid, node):
        return []
    errors: List[str] = []
    prefix = "node %s: " % nid

    feature = node.get("behavior")
    if not isinstance(feature, str) or not feature.strip():
        errors.append(prefix + "non-HTTP code leaf carries NO machine behaviour "
                      "feature (Gherkin) — its contract must not live in prose")
    else:
        for err in feature_library_errors(feature):
            errors.append(prefix + err)

    symbols = node.get("symbols") if isinstance(node.get("symbols"), dict) else {}
    exposes = symbols.get("exposes") or []
    if not isinstance(exposes, list) or not exposes:
        errors.append(prefix + "non-HTTP code leaf exposes NO contracted "
                      "symbols — every public callable must be declared with a "
                      "typed signature (the v166 spec_lint #20 class)")
    else:
        for ent in exposes:
            for frag in _expose_incompleteness(ent):
                errors.append(prefix + "exposes " + frag)

    return errors
