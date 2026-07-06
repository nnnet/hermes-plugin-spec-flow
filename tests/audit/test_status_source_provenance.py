"""STAGE 13 extension (S13.6): the ENGINE marks which interface facts it
INVENTED by convention vs which the spec declared (node H2, plan
2026-07-04T00-45; principles-audit finding F5).

Why: `_route_success_status` applies a REST convention (POST->201, else 200)
and `_route_fixed_body` inlines engine defaults (/health -> {"status":"ok"}).
Both are declared in the compiled OpenAPI (good) — but their ORIGIN was
invisible: reading ir.json you could not tell what the spec asked for from
what the engine made up. "Spec is the single source" needs the invented
bits VISIBLE so they can be audited and, later, demanded from the spec.

What is pinned here:
  * S13.6 a success response derived from the engine's status CONVENTION
    carries `x-spec-flow-status-source: convention` on the operation; a fixed
    body the engine inlined carries `x-spec-flow-body-source: convention`;
  * a decomposer machine fragment (the IR path) does NOT flow through
    `_node_openapi`, so its responses carry no convention marker — absence
    reads as spec-declared.

Deterministic: `_node_openapi` is called directly with known-answer datum
functions; no LLM.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_ir  # noqa: E402


def _build(routes, media=None, fixed=None, reqf=None):
    return spec_ir._node_openapi(
        "core", routes,
        reqf or {}, media or {},
        status_fn=lambda m: 201 if m == "POST" else 200,
        fixed_fn=lambda m, p: (fixed or {}).get((m, p)),
        handler_fn=lambda m, p: "h_%s" % p.strip("/"))


def _op(doc, path, method):
    return doc["paths"][path][method]


# ── S13.6 the convention-derived status is marked ───────────────────────────

def test_success_status_from_convention_is_marked():
    doc = _build([("POST", "/notes")], media={"/notes": "json"})
    op = _op(doc, "/notes", "post")
    assert op.get("x-spec-flow-status-source") == "convention", (
        "a status the engine derived by REST convention must be marked so "
        "ir.json shows the spec did not ask for it (F5)")


def test_get_status_also_marked_convention():
    doc = _build([("GET", "/notes")], media={"/notes": "json"})
    assert _op(doc, "/notes", "get").get(
        "x-spec-flow-status-source") == "convention"


# ── S13.6 an engine-inlined fixed body is marked ────────────────────────────

def test_fixed_body_from_engine_is_marked():
    doc = _build([("GET", "/health")],
                 fixed={("GET", "/health"): {"status": "ok"}})
    op = _op(doc, "/health", "get")
    assert op.get("x-spec-flow-body-source") == "convention", (
        "a body the engine inlined (/health -> {status: ok}) must be marked "
        "as an engine convention, not a spec datum")


def test_non_fixed_body_has_no_body_source_marker():
    doc = _build([("GET", "/notes")], media={"/notes": "json"})
    assert "x-spec-flow-body-source" not in _op(doc, "/notes", "get"), (
        "only an engine-inlined fixed body carries the body-source marker")
