"""Audit rules S17.3/S17.4: the write door REFUSES a skeleton edit and the
engine hands the skeleton to the coder.

Node C1 (plan 2026-07-04T00-45), the engine side:

  * the ONE write door (`_delivery_lint` seam) runs the skeleton
    conformance check for every delivered code file whose leaf carries an
    IR interface: a delivery that rewrites a signature, imports outside
    the allowed list or grows the public surface NEVER lands — refused
    with the existing refusal artifact (P4, attributable);
  * the engine COMPILES the skeleton at leaf time (`_ir_skeleton_for`),
    registers the conformance view on the workspace and hands the skeleton
    to the coder (ictx) — the model fills ONLY the bodies;
  * leaves WITHOUT an IR interface keep today's path untouched (fallback);
    the product entry module stays engine-synthesized, never skeletoned;
  * a late requirement that grows the module's route surface DROPS the
    stale registration on the IR re-dump (the S12.2 erasure gate owns the
    co-owned surface) — otherwise the honest rework that ADDS the late
    handler would be refused as "uncontracted" (a self-made deadlock).

Deterministic: engine unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import importlib
import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import spec_flow_runner as sfr  # noqa: E402
import spec_skeletons  # noqa: E402

_CONSTITUTION = [
    "The product entry src/app.py exposes wsgi_app (stdlib WSGI).",
    "POST /notes takes {text} and responds {id}.",
    "GET /notes responds {items: [{id, text}]} newest-first.",
    "GET /health responds 200.",
]

_DELETE_REQ = (
    "ALLOW REMOVING A NOTE (added by the human mid-run; binding). "
    "A reader must be able to delete a single note by its id. Extend the "
    "existing notes capability - do not add a separate store. Deleting a "
    "note then listing must no longer show it.")

# the same hand IR shape the compiler tests use — door tests judge the
# SEAM, not the derivation
_IR = {
    "format": "spec-flow ir v1",
    "product": {"kind": "web-service", "entry": "src/app.py",
                "callable": "wsgi_app"},
    "nodes": {"core": {
        "files": ["src/core.py"],
        "openapi": {
            "openapi": "3.1.0",
            "info": {"title": "t", "version": "1"},
            "paths": {"/notes": {
                "post": {"x-spec-flow-handler": "post_notes",
                         "responses": {"201": {"description": "ok"}}},
                "get": {"x-spec-flow-handler": "get_notes",
                        "responses": {"200": {"description": "ok"}}}}}},
        "symbols": {"exposes": [
            {"name": "post_notes", "args": ["payload", "query"]},
            {"name": "get_notes", "args": ["payload", "query"]}]}}}}


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._constitution = list(_CONSTITUTION)
    core = {"id": "core", "title": "core notes",
            "requirement": "serve POST /notes and GET /notes and GET /health"}
    assert ("POST", "/notes") in set(eng._leaf_owned_routes(core))
    return eng


def _attach(eng, nid, title, req):
    extra = {"id": nid, "title": title, "requirement": req,
             "_late_req": True}
    eng._amend_target = lambda e: "src/core.py"   # deterministic router seam
    eng._attach_late_req(extra, {"id": "L0"}, 1, "root", "L0", (), [])
    return extra


def _register(ws):
    ws.ir_skeletons = {"src/core.py": {"ir": _IR, "node": "core"}}


# ── S17.3 the door refuses a skeleton edit ──────────────────────────────────

def test_write_door_refuses_a_signature_rewrite(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    ws = eng.workspace
    _register(ws)
    bad = spec_skeletons.compile_skeleton(_IR, "core").replace(
        "def post_notes(payload, query):", "def post_notes(payload):")
    bad = bad.replace("raise NotImplementedError", "return 201, {}")
    ws._write("src/core.py", bad, "code")
    assert not (pathlib.Path(ws.root) / "src" / "core.py").exists(), (
        "a delivery that rewrites an engine-owned signature must never LAND")
    ref = [a for a in ws.artifacts
           if str(a.get("type", "")).startswith("refused")]
    assert ref and "post_notes" in str(ref[-1].get("reason", "")), (
        "the refusal artifact must name the offending symbol: %r" % ref)


def test_write_door_refuses_a_phantom_import(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    ws = eng.workspace
    _register(ws)
    body = spec_skeletons.compile_skeleton(_IR, "core").replace(
        "raise NotImplementedError", "return 200, {}")
    ws._write("src/core.py", "import requests\n" + body, "code")
    assert not (pathlib.Path(ws.root) / "src" / "core.py").exists(), (
        "the v157 phantom-import class dies AT THE DOOR")


def test_write_door_lands_a_bodies_only_delivery(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    ws = eng.workspace
    _register(ws)
    good = spec_skeletons.compile_skeleton(_IR, "core").replace(
        "raise NotImplementedError", "return 200, {}")
    ws._write("src/core.py", good, "code")
    assert (pathlib.Path(ws.root) / "src" / "core.py").exists(), (
        "the honest bodies-only delivery passes the same door untouched")


def test_unregistered_files_keep_todays_path(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    ws = eng.workspace
    _register(ws)
    ws._write("src/other.py", "import requests\n\n\ndef free():\n"
              "    return 1\n", "code")
    assert (pathlib.Path(ws.root) / "src" / "other.py").exists(), (
        "a file with NO registered skeleton is judged by the old gates "
        "only — the fallback path is untouched")


# ── S17.4 the engine compiles, registers and hands the skeleton ────────────

def test_engine_compiles_and_registers_the_skeleton(tmp_path):
    eng = _engine(tmp_path)
    skel = eng._ir_skeleton_for("core", "src/core.py")
    assert "def post_notes(payload, query):" in skel, (
        "the engine must compile the skeleton from its OWN datums via the "
        "IR: %r" % skel[:200])
    reg = getattr(eng.workspace, "ir_skeletons", None) or {}
    assert reg.get("src/core.py", {}).get("node") == "core", (
        "the conformance view must be registered on the workspace so the "
        "door can judge the delivery: %r" % reg)


def test_product_entry_is_never_skeletoned(tmp_path):
    eng = _engine(tmp_path)
    assert eng._ir_skeleton_for("core", "src/app.py") == "", (
        "the entry router is ENGINE-synthesized (v164 template revision "
        "lesson) — never handed to the coder as a skeleton")
    reg = getattr(eng.workspace, "ir_skeletons", None) or {}
    assert "src/app.py" not in reg


def test_leaf_without_ir_interface_falls_back(tmp_path):
    eng = _engine(tmp_path)
    assert eng._ir_skeleton_for("ghost", "src/ghost.py") == "", (
        "no IR entry -> no skeleton -> today's path, no behavior change")
    reg = getattr(eng.workspace, "ir_skeletons", None) or {}
    assert "src/ghost.py" not in reg


def test_route_growth_reregisters_the_module_surface(tmp_path):
    # S17.6 (node J1): a co-owned module whose surface GREW past the per-node
    # interface must NOT drop its write-door registration into a closed-world
    # gap (the v165 deadlock: a dropped registration let ANY public function
    # in while the honest late handler had no skeleton to belong to). It is
    # RE-REGISTERED against the module-surface UNION so the door ADMITS every
    # contracted handler of the whole module AND still refuses an uncontracted
    # one.
    eng = _engine(tmp_path)
    eng._write_ir()
    skel = eng._ir_skeleton_for("core", "src/core.py")
    assert skel and "src/core.py" in (eng.workspace.ir_skeletons or {})
    # the late requirement binds DELETE /notes INTO src/core.py: the module
    # surface now exceeds node core's own interface — the stale per-node
    # skeleton would refuse the honest rework that ADDS delete_notes
    _attach(eng, "delete_note", "ALLOW REMOVING A NOTE", _DELETE_REQ)
    reg = eng.workspace.ir_skeletons or {}
    assert "src/core.py" in reg, (
        "the co-owned surface must stay DEFENDED (re-registered against the "
        "module union), never dropped into a closed-world gap")
    ent = reg["src/core.py"]
    surface = spec_skeletons.contract_surface(ent["ir"], ent["node"])
    assert "delete_notes" in surface, (
        "the re-registered module surface must CONTAIN the late handler so "
        "the write door admits the honest rework: %s" % sorted(surface))
    for h in ("post_notes", "get_notes", "get_health"):
        assert h in surface, (
            "the union must keep the original owner's handlers too: %s"
            % sorted(surface))


def test_leaf_pipeline_hands_the_skeleton_to_the_coder():
    src = inspect.getsource(sfr.Engine._leaf_pipeline)
    assert "_ir_skeleton_for" in src, (
        "the leaf pipeline must compile the skeleton at leaf time")
    assert '"skeleton"' in src, (
        "the skeleton must travel to the implementer via ictx")


def test_skeleton_reaches_worker_prompts():
    rw = importlib.import_module("harness.role_worker")
    blk = rw._skeleton_block(
        {"skeleton": "def post_notes(payload, query):\n"
                     "    raise NotImplementedError\n"})
    assert "def post_notes(payload, query):" in blk, (
        "the worker must SEE the engine skeleton verbatim")
    assert "bod" in blk.lower(), (
        "the worker must be TOLD to fill only the bodies")
    assert rw._skeleton_block({}) == "", "no skeleton -> no block"
    src = pathlib.Path(rw.__file__).read_text(encoding="utf-8")
    assert src.count("_skeleton_block(ctx)") >= 2, (
        "both the chat and the claude implementer paths must carry the "
        "skeleton block")
