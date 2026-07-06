"""STAGE 13 extension (S13.7): the IR can DECLARE third-party dependencies,
and only a declared dependency may be imported by a leaf (node H5, plan
2026-07-04T00-45; principles-audit finding F8 — the wall blocking "spec is
the single source" from ever asking for Flask/sqlite3/etc.).

Why: the IR schema had no notion of a dependency (`spec_ir` _PRODUCT_KEYS /
_NODE_KEYS), so a spec physically could not ask the product to build on a
library — stdlib-only was hard-wired at the schema wall. The third-party-
first principle needs the spec to be able to REQUEST a library, and the
closed import world to admit exactly the requested ones (no more).

What is pinned here:
  * S13.7 `product.requirements` is a list of dependencies (name + optional
    version); `node.dependencies` names, per node, which of them the node
    imports; an `effects` node key (the H6 datum) is also schema-legal;
  * a node dependency not present in product.requirements is a closed-world
    error (you cannot import what the product never requested);
  * a leaf importing a DECLARED dependency raises no phantom-import finding;
    an UNDECLARED import stays refused (the wall still stands for the
    unrequested).

Deterministic: hand IR + direct validate_ir / skeleton_conformance; no LLM.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_ir  # noqa: E402
import spec_skeletons  # noqa: E402


def _ir(nodes, product=None):
    return {"format": spec_ir.IR_FORMAT, "product": product or {},
            "nodes": nodes}


def _leaf(deps=None, effects=None):
    node = {"files": ["src/api.py"],
            "symbols": {"exposes": [{"name": "handler", "args": ["p", "q"]}],
                        "consumes": []}}
    if deps is not None:
        node["dependencies"] = list(deps)
    if effects is not None:
        node["effects"] = list(effects)
    return {"api": node}


# ── S13.7 schema accepts the dependency vocabulary ──────────────────────────

def test_product_requirements_accepted():
    ir = _ir(_leaf(), product={"requirements": [{"name": "flask",
                                                 "version": "3.0"},
                                                {"name": "sqlalchemy"}]})
    errs = spec_ir.validate_ir(ir)["errors"]
    assert errs == [], ("product.requirements must be a legal schema key — "
                        "the wall (F8) is here: %r" % errs)


def test_node_dependencies_and_effects_accepted():
    ir = _ir(_leaf(deps=["flask"], effects=["fs-write"]),
             product={"requirements": [{"name": "flask"}]})
    errs = spec_ir.validate_ir(ir)["errors"]
    assert errs == [], ("node.dependencies and node.effects must be legal "
                        "schema keys: %r" % errs)


def test_undeclared_node_dependency_is_a_closed_world_error():
    ir = _ir(_leaf(deps=["requests"]),
             product={"requirements": [{"name": "flask"}]})
    errs = spec_ir.validate_ir(ir)["errors"]
    assert any("requests" in e for e in errs), (
        "a node cannot import what the product never requested — closed "
        "world: %r" % errs)


def test_bare_string_requirement_accepted():
    ir = _ir(_leaf(deps=["flask"]),
             product={"requirements": ["flask"]})
    assert spec_ir.validate_ir(ir)["errors"] == [], (
        "a requirement may be a bare name string, not only {name: ...}")


# ── S13.7 the import door admits declared deps, refuses the rest ────────────

_MOD = ("import flask\n\n\n"
        "def handler(p, q):\n"
        "    return 200, {'ok': True}\n")


def test_declared_dependency_import_is_allowed():
    ir = _ir(_leaf(deps=["flask"]),
             product={"requirements": [{"name": "flask"}]})
    findings = spec_skeletons.skeleton_conformance(ir, "api", _MOD)
    assert not any("flask" in f and "import" in f.lower() for f in findings), (
        "a DECLARED dependency import must not be a phantom-import finding: "
        "%r" % findings)


def test_undeclared_dependency_import_still_refused():
    ir = _ir(_leaf(deps=[]), product={"requirements": []})
    findings = spec_skeletons.skeleton_conformance(ir, "api", _MOD)
    assert any("flask" in f for f in findings), (
        "an UNDECLARED import stays refused — the wall still stands for the "
        "unrequested: %r" % findings)


# ── S13.7 requirements.txt is compiled from the requested deps ──────────────

def test_requirements_txt_compiled_from_the_spec():
    ir = _ir(_leaf(deps=["flask"]),
             product={"requirements": [{"name": "flask", "version": "3.0"},
                                       "sqlalchemy"]})
    txt = spec_ir.requirements_txt(ir)
    assert "flask==3.0" in txt and "sqlalchemy" in txt, (
        "the pip manifest is compiled from product.requirements — versions "
        "pinned, bare names kept: %r" % txt)


def test_no_requirements_no_manifest():
    assert spec_ir.requirements_txt(_ir(_leaf())) == "", (
        "a spec that requested nothing gets no manifest — stdlib-only stays "
        "the default")
