"""Audit rule S12.2 (v159): a rework/redelivery of an owner module must
PRESERVE the contracted route surface — dropping a landed handler is refused
at the write door, and the rework directive re-prints the route contract.

v159 (p6-micro-notes): the mid-run amend about_page LANDED `get_about` in
src/core.py (commit #6, checkpoint 008) — then the integrate doctor's
"module repair: rework core (acceptance blamed it)" rewrote src/core.py from
scratch and the weak model dropped `get_about` AND `delete_notes` (both
contracted, both previously delivered). Nothing refused the delivery: the
module SYMBOL contract (S11.3/S11.4) was EMPTY for core (no leaf importers),
and route handlers lived in no module-scoped datum the door could enforce.
GET /about 404-ed at assembly and the doctor ground on 'weak_implementer'
three times without ever naming the erasure.

Contract enforced (deterministic, the S11.3/S11.4 twin for ROUTES):
  * (method, path) -> owner MODULE is engine data
    (``_route_handler_modules``, recorded where route ownership is recorded
    — for an amend, the module it edits via ``code_target``);
  * the write door refuses a delivery to ``src/<stem>.py`` that does not
    bind every contracted route handler of that module at module level
    (``_delivery_lint`` ``route_handlers`` gate) — an erasing rework never
    lands;
  * the rework directive (``_remedy_rework_module``) re-prints the module's
    route contract (``_module_route_binding_text``) so the rewriting LLM
    sees the frozen surface — and the door enforces the same datum anyway;
  * GREEN edges: a delivery carrying every contracted handler lands; a
    module with no owned routes is untouched; tests/ files are never gated.

Deterministic: unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import pathlib
import re
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [("GET", "/about")],
    "media": {"/notes": "json", "/health": "json", "/about": "html"},
}
_CORE = {"id": "core", "title": "Product core",
         "requirement": "own POST /notes and GET /notes and GET /health"}
_ABOUT = {"id": "about_page", "title": "About page",
          "code_target": "src/core.py", "binds_route": ["GET", "/about"],
          "requirement": "serve GET /about as a small HTML page"}

_FULL_CORE = """\
    def post_notes(payload, query):
        return 201, {"id": 1}


    def get_notes(payload, query):
        return 200, {"items": []}


    def get_health(payload, query):
        return 200, {"status": "ok"}


    def get_about(payload, query):
        return 200, "<html>about</html>"
"""

# the v159 erasure: the rework re-guessed core WITHOUT the amend-landed route
_ERASED_CORE = """\
    def post_notes(payload, query):
        return 201, {"id": 1}


    def get_notes(payload, query):
        return 200, {"items": []}


    def get_health(payload, query):
        return 200, {"status": "ok"}
"""


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    # record ownership the way the run does: core's own routes, then the
    # amend's engine-bound route into the SAME module (code_target)
    eng._leaf_owned_routes(dict(_CORE))
    eng._leaf_owned_routes(dict(_ABOUT))
    return eng


def _deliver(eng, body: str):
    rel = "src/core.py"
    eng.workspace._write(rel, textwrap.dedent(body), "code")
    p = pathlib.Path(eng.workspace.root) / rel
    refused = [a for a in eng.workspace.artifacts
               if a.get("path") == rel and str(a.get("type", "")
                                               ).startswith("refused")]
    return p.is_file(), refused


# ---- RED: the v159 erasure --------------------------------------------------

def test_delivery_dropping_contracted_handler_is_refused(tmp_path):
    eng = _engine(tmp_path)
    landed, refused = _deliver(eng, _ERASED_CORE)
    assert refused and not landed, (
        "a delivery of src/core.py WITHOUT get_about — while the ownership "
        "datum contracts GET /about into this module — is the v159 erasure "
        "(the doctor's core rework wiped the amend-landed /about handler); "
        "the write door must refuse it")
    reason = refused[0].get("reason", "")
    assert "get_about" in reason, "the refusal must NAME the erased handler"
    assert "/about" in reason, "the refusal must NAME the erased route"


def test_rework_directive_reprints_route_contract(tmp_path):
    eng = _engine(tmp_path)
    block = eng._module_route_binding_text("core")
    assert "get_about" in block and "/about" in block, (
        "S11.4 twin: the rework directive must re-print the module's ROUTE "
        "contract — v159's rework prompt carried no trace of the amend-landed "
        "routes, so the weak model re-guessed the module without them")
    assert "about_page" in block, "the block must attribute the owner leaf"
    # reachability (S7.2): _remedy_rework_module must actually print it
    src = pathlib.Path(sfr.__file__).read_text(encoding="utf-8")
    body = src.split("def _remedy_rework_module", 1)[1].split("\n    def ")[0]
    assert "_module_route_binding_text" in body, (
        "_remedy_rework_module does not re-print the route contract block")


# ---- GREEN edges ------------------------------------------------------------

def test_delivery_with_full_surface_lands(tmp_path):
    eng = _engine(tmp_path)
    landed, refused = _deliver(eng, _FULL_CORE)
    assert landed and not refused, (
        "a delivery binding every contracted route handler must land")


def test_module_without_owned_routes_untouched(tmp_path):
    eng = _engine(tmp_path)
    rel = "src/helpers.py"
    eng.workspace._write(rel, "def util():\n    return 1\n", "code")
    assert (pathlib.Path(eng.workspace.root) / rel).is_file(), (
        "a module the ownership datum knows nothing about is never gated "
        "(leniency: one false positive sinks a run, v151)")
    assert not eng._module_route_binding_text("helpers")


def test_tests_files_never_gated(tmp_path):
    eng = _engine(tmp_path)
    rel = "tests/test_core.py"
    eng.workspace._write(rel, "def test_ok():\n    assert True\n", "code")
    assert (pathlib.Path(eng.workspace.root) / rel).is_file()


def test_alias_binding_counts_as_defined(tmp_path):
    # any real module-level binding satisfies the surface (S11.3 semantics)
    eng = _engine(tmp_path)
    landed, refused = _deliver(eng, _ERASED_CORE + """\

    def _about_impl(payload, query):
        return 200, "<html>about</html>"


    get_about = _about_impl
""")
    assert landed and not refused, (
        "a module-level assignment alias is a real binding of the handler")
