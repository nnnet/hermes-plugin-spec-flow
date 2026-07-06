"""STAGE 16 extension (S16.7): the compiled OpenAPI is validated by the
THIRD-PARTY openapi-spec-validator, not only the hand-rolled lint_openapi
(node K1, plan 2026-07-04T00-45; user superpriority 2026-07-06 — "use the
OpenAPI 3.1 library").

Why: the engine hand-rolled OpenAPI lint on stdlib. The whole point of the
IR-as-OpenAPI form is that a MAINTAINED standard library can read it — an
external oracle disagreeing with our compiler is a signal, and a real
3.1 validator catches spec violations our lint never enumerated.

What is pinned here:
  * S16.7 `spec_openapi.validate_openapi_library(doc)` runs
    openapi-spec-validator over the document and returns standard-conformance
    errors (empty on a valid doc);
  * our `compile_openapi(ir)` output passes the library validator unmodified
    (the compiled form IS standard OpenAPI 3.1, not a private dialect);
  * a broken document (bad type / missing required OpenAPI field) is caught
    by the library.

Deterministic; the library is a dev/test oracle pinned in
tests/requirements-dev.txt.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_ir  # noqa: E402
import spec_openapi  # noqa: E402


def _frag(nid, paths):
    return {"openapi": "3.1.0", "info": {"title": nid, "version": "1"},
            "paths": paths}


def _op(handler, status="200", media="application/json"):
    return {"x-spec-flow-handler": handler,
            "responses": {status: {"description": "ok",
                                   "content": {media: {"schema": {}}}}}}


def _ir():
    return {"format": spec_ir.IR_FORMAT,
            "product": {"entry": "src/app.py", "callable": ["wsgi_app"]},
            "nodes": {"notes": {"files": ["src/notes.py"],
                                "openapi": _frag("notes", {"/notes": {
                                    "get": _op("get_notes")}})}}}


# ── S16.7 the library validator exists and accepts our compiled doc ─────────

def test_compiled_openapi_passes_the_library_validator():
    doc = spec_openapi.compile_openapi(_ir())
    errs = spec_openapi.validate_openapi_library(doc)
    assert errs == [], (
        "our compiled OpenAPI must be valid 3.1 by the STANDARD library, not "
        "only our hand-rolled lint: %r" % errs)


# ── S16.7 the library catches a broken document ─────────────────────────────

def test_library_catches_a_broken_document():
    broken = {"openapi": "3.1.0", "info": {"title": "x"},  # missing version
              "paths": {"/x": {"get": {"responses": {}}}}}
    assert spec_openapi.validate_openapi_library(broken), (
        "a document missing a required OpenAPI field must be caught by the "
        "library validator")


def test_library_catches_wrong_type():
    broken = {"openapi": "3.1.0", "info": {"title": "x", "version": "1"},
              "paths": "not-an-object"}
    assert spec_openapi.validate_openapi_library(broken), (
        "paths must be an object — the library validator catches the type "
        "violation")
