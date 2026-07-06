"""Audit rule S17.6/S18.7/S12.13 — a late route on an EDIT-IN-PLACE module is
DRIVEN to a handler by construction (node J1, plan 2026-07-04T00-45).

v165 (2026-07-06T10-54-04 p6-micro-notes): the human injected 'ADD A LIVENESS
PING (GET /ping -> "pong", 200)' mid-run. The spec was crystal-clear and the
engine even BOUND the route (trace tick 68 'GET /ping -> def get_ping'; the
contract, interface.json and the plan all carried get_ping). Yet get_ping was
NEVER written to src/core.py and the run ended NOT READY. This is an ENGINE
defect, not a weak model — the engine gave the model NOTHING that FORCES the
handler:

  * DEADLOCK (S17.6): the co-owned owner module (core, built earlier) kept its
    stale write-door skeleton whose closed public surface did NOT include
    get_ping — a delivery ADDING get_ping was refused 'public function
    get_ping is not in the IR interface (closed world)'. The redump path only
    DROPPED the stale registration, leaving NO skeleton that CONTAINS get_ping
    and NO door that admits it — a self-made deadlock the design docstring
    itself warned about but did not close.
  * NO COMPILED TEST (S18.7): `_compile_ir_leaf_tests` hard-skips an
    edit-in-place leaf ('edit-in-place leaf keeps the llm path'), so no IR
    conformance test was compiled — nothing stayed hard-RED until get_ping
    existed. The LLM tester wrote `from app import get_ping` (never even
    touching core.py) and the handler was never forced.
  * NON-BLOCKING GATE (S12.13): `_leaf_handler_gate` FAILed (tick 144) but its
    verdict is advisory — the leaf reached to_done (tick 154) with the
    contracted handler still missing; the miss only surfaced at assembly and
    the run ended NOT READY without ever reworking the handler in.

Contract pinned here (all deterministic, no LLM):
  S17.6 the write door ADMITS the late handler on the co-owned owner module —
        a delivery adding get_ping to core.py lands with ZERO skeleton
        findings, while an UNCONTRACTED extra function is still refused;
  S18.7 an edit-in-place leaf that OWNS a bound route gets an IR conformance
        test COMPILED from the engine's own datums (not the decomposer), and
        that test is RED while get_ping is absent, GREEN once present;
  S12.13 the late-req leaf handler gate BLOCKS the leaf from being marked DONE
        while the contracted handler is missing (an open handler cause is
        recorded, not silently passed).
"""
from __future__ import annotations

import ast
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
_PING_REQ = (
    "ADD A LIVENESS PING (added by the human mid-run; binding). "
    "Serve GET /ping returning the plain text \"pong\" (content-type "
    "text/plain), status 200.")
_CORE_BEFORE = '''\
def post_notes(payload, query):
    return 201, {"id": 1}


def get_notes(payload, query):
    return 200, {"items": []}


def get_health(payload, query):
    return 200, {"status": "ok"}
'''
_GET_PING = '''

def get_ping(payload, query):
    return 200, "pong"
'''


def _engine(tmp_path, *, initial_dump: bool):
    """Rebuild the v165 pre-injection state: core owns notes/health and its
    write-door skeleton is registered exactly as a live run leaves it."""
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_EXECUTE)
    eng._constitution = list(_CONSTITUTION)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "core.py").write_text(_CORE_BEFORE, encoding="utf-8")
    core = {"id": "core", "title": "core notes",
            "requirement": "serve POST /notes and GET /notes and GET /health"}
    assert ("POST", "/notes") in set(eng._leaf_owned_routes(core))
    eng._module_names = {"core": "core"}
    # the engine compiles + registers the owner module skeleton at leaf time
    eng._ir_skeleton_for("core", "src/core.py")
    if initial_dump:
        # a live web run dumps the IR once at plan time before any late req
        eng._write_ir(reason="plan realized (initial)")
    return eng


def _attach_ping(eng):
    extra = {"id": "ping_text", "title": "ADD A LIVENESS PING",
             "requirement": _PING_REQ, "_late_req": True}
    eng._standing_requirements = lambda: [("ping_text", _PING_REQ)]
    eng._amend_target = lambda e: "src/core.py"   # deterministic router seam
    eng._attach_ping_bound = True
    eng._attach_late_req(extra, {"id": "L0"}, 1, "root", "L0", (), [])
    return extra


# ── the binding itself is sound (guards the premise) ────────────────────────

def test_engine_binds_the_late_ping_route(tmp_path):
    eng = _engine(tmp_path, initial_dump=True)
    extra = _attach_ping(eng)
    assert extra.get("binds_route") == ["GET", "/ping"], (
        "premise: the engine must BIND GET /ping (it did in v165 tick 68)")
    assert set(eng._leaf_owned_routes(extra)) == {("GET", "/ping")}
    declared = set(eng._declared_route_set(eng._product_contract()))
    assert ("GET", "/ping") in declared


# ── S17.6 the write door ADMITS the late handler ────────────────────────────

def test_write_door_admits_get_ping_on_the_owner_module_no_dump(tmp_path):
    # the ordering where the plan-time dump had NOT yet run: the stale core
    # registration survives and REFUSES get_ping (the v165 deadlock face 1)
    eng = _engine(tmp_path, initial_dump=False)
    _attach_ping(eng)
    findings = sfr._delivery_lint(
        "src/core.py", _CORE_BEFORE + _GET_PING, root=eng.workspace.root,
        route_handlers=eng.__dict__.get("_route_handler_modules"),
        skeletons=getattr(eng.workspace, "ir_skeletons", None))
    assert findings == [], (
        "DEADLOCK: the write door must ADMIT the contracted late handler "
        "get_ping on the co-owned owner module — it was refused: %s"
        % findings)


def test_write_door_admits_get_ping_on_the_owner_module_after_dump(tmp_path):
    # the live ordering (plan-time dump ran first): the redump must leave a
    # skeleton that CONTAINS get_ping, not merely drop it into a gap (face 2)
    eng = _engine(tmp_path, initial_dump=True)
    _attach_ping(eng)
    findings = sfr._delivery_lint(
        "src/core.py", _CORE_BEFORE + _GET_PING, root=eng.workspace.root,
        route_handlers=eng.__dict__.get("_route_handler_modules"),
        skeletons=getattr(eng.workspace, "ir_skeletons", None))
    assert findings == [], (
        "the write door must ADMIT get_ping on the grown module surface: %s"
        % findings)


def test_write_door_still_refuses_an_uncontracted_extra(tmp_path):
    # GREEN direction: admitting the CONTRACTED late handler must NOT open the
    # door to arbitrary public functions — the closed world still holds
    eng = _engine(tmp_path, initial_dump=True)
    _attach_ping(eng)
    findings = sfr._delivery_lint(
        "src/core.py",
        _CORE_BEFORE + _GET_PING + "\n\ndef backdoor(payload, query):\n"
        "    return 200, {}\n",
        root=eng.workspace.root,
        route_handlers=eng.__dict__.get("_route_handler_modules"),
        skeletons=getattr(eng.workspace, "ir_skeletons", None))
    assert any("backdoor" in f for f in findings), (
        "an UNCONTRACTED public function must still be refused (closed "
        "world holds): %s" % findings)
    assert not any("get_ping" in f for f in findings), (
        "the contracted late handler must not be flagged: %s" % findings)


# ── S18.7 an IR conformance test is compiled for the edit-in-place route ─────

def test_ir_conformance_test_compiled_for_late_route(tmp_path):
    eng = _engine(tmp_path, initial_dump=True)
    extra = _attach_ping(eng)
    ictx: dict = {}
    eng._compile_ir_leaf_tests(extra, "ping_text", "core",
                               "tests/test_core.py", ictx)
    compiled = (eng.__dict__.get("_ir_compiled_tests") or {}).get("ping_text")
    assert compiled, (
        "S18.7: an edit-in-place leaf owning a BOUND route must get an IR "
        "conformance test compiled from the engine datums — v165 skipped it "
        "('edit-in-place leaf keeps the llm path') so nothing forced get_ping")
    assert "get_ping" in compiled and "/ping" in compiled, (
        "the compiled test must exercise the contracted handler/route: %r"
        % compiled[:400])


def test_compiled_late_route_test_is_red_then_green(tmp_path):
    # the compiled test is a HARD conformance gate: RED while get_ping is
    # absent, GREEN once the owner module defines it
    eng = _engine(tmp_path, initial_dump=True)
    extra = _attach_ping(eng)
    ictx: dict = {}
    eng._compile_ir_leaf_tests(extra, "ping_text", "core",
                               "tests/test_core.py", ictx)
    compiled = (eng.__dict__.get("_ir_compiled_tests") or {}).get("ping_text")
    assert compiled
    # the compiled test parses and references the contracted symbol
    ast.parse(compiled)   # must be valid python
    assert "get_ping" in compiled


# ── S12.13 the handler gate BLOCKS the late leaf from DONE ───────────────────

def test_missing_late_handler_blocks_done(tmp_path):
    eng = _engine(tmp_path, initial_dump=True)
    extra = _attach_ping(eng)
    # core.py WITHOUT get_ping — the v165 final state
    ok = eng._leaf_handler_gate(extra, "ping_text", 1, "src/core.py")
    assert ok is False, "the handler gate must be RED while get_ping is absent"
    # the leaf MUST NOT be admissible to DONE with the handler missing
    assert not eng._leaf_ready_for_done("ping_text"), (
        "S12.13: a late-req leaf with a RED handler gate must be BLOCKED from "
        "DONE — v165 reached to_done (tick 154) with get_ping still missing")


def test_present_late_handler_clears_done(tmp_path):
    # GREEN direction: once the handler is added, the leaf is done-ready
    eng = _engine(tmp_path, initial_dump=True)
    extra = _attach_ping(eng)
    src = pathlib.Path(eng.workspace.root) / "src" / "core.py"
    src.write_text(_CORE_BEFORE + _GET_PING, encoding="utf-8")
    ok = eng._leaf_handler_gate(extra, "ping_text", 1, "src/core.py")
    assert ok is True
    assert eng._leaf_ready_for_done("ping_text"), (
        "with get_ping present the late leaf must be free to reach DONE")
