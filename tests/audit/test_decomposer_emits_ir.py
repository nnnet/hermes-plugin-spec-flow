"""Audit STAGE 15 — the decomposer emits IR; prose stops carrying the interface.

Node E1 of the spec-IR rearchitecture (plan 2026-07-04T00-45): the
decomposer's output gains a MACHINE part (`"ir"`, spec-flow IR v1 per
`spec_ir.py`) and the ENGINE validates it with `spec_ir.validate_ir` at the
exact seam where the output is received (`_expand_node`), BEFORE any
assembly starts. The rules:

- S15.1 an IR-capable decomposer output MISSING the machine part is a NAMED
  refusal (gate `decomposer_ir`, verdict FAIL, node id in the event) that
  drives ONE bounded re-ask carrying the exact errors — never a silent
  fallback to prose-only;
- S15.2 an INVALID machine part (unknown key, a route claimed by a node
  with children — the closed world of spec_ir) is refused with the node id
  and the offending value, and the rejected fragment never enters the
  engine's IR registry;
- S15.3 two nodes claiming one (method, path) across separate decomposer
  calls red at the SECOND call (the seam validates the MERGED document);
- S15.4 IR WINS OVER PROSE: when the decomposer supplied IR for a node,
  `_leaf_owned_routes` reads the IR openapi fragment, not the claim text,
  and journals `interface_source: ir`; without IR the existing prose
  derivation stays as the fallback and journals
  `interface_source: prose-derived`;
- S15.5 route facts follow the IR: request required fields and body media
  declared in an accepted fragment win over prose-derived guesses in
  `_route_request_fields` / `_route_media_map`;
- S15.6 the LLM decomposer PROMPT asks for the machine part including
  concrete example values in scenario `when.body`; the engine never invents
  a missing body — it stays an honest incompleteness finding (spec_ir).

Deterministic gates are tested with CRAFTED decomposer outputs at the seam
where the engine receives them (an injected agent / the seam method) — the
LLM door (`llm_backend.ask`) is never faked.
"""
from __future__ import annotations

import pathlib
import sys

_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from harness import run_engine as eng  # noqa: E402
import spec_ir  # noqa: E402


def _engine(tmp_path):
    e = eng.Engine(workspace=str(tmp_path / "wk"), depth=eng.DEPTH_SPEC)
    e._product_contract = lambda: {
        "entry": "src/app.py",
        "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
        "routes": [("GET", "/ui")],
    }
    return e


def _openapi(nid, method, path, status="200", media="application/json",
             required=None):
    op = {"responses": {str(status): {"description": "success",
                                      "content": {media: {"schema": {}}}}}}
    if required is not None:
        op["requestBody"] = {"required": True, "content": {
            "application/json": {"schema": {
                "type": "object", "required": list(required),
                "additionalProperties": False}}}}
    return {"openapi": spec_ir.OPENAPI_VERSION,
            "info": {"title": "node %s interface" % nid, "version": "1"},
            "paths": {path: {method.lower(): op}}}


def _machine_part(nodes):
    return {"format": spec_ir.IR_FORMAT, "nodes": nodes}


def _gate_events(e, gate="decomposer_ir", verdict=None):
    return [ev for ev in e.events
            if ev.gate == gate and (verdict is None or ev.verdict == verdict)]


# ── S15.1 missing machine part = named refusal + bounded re-ask ─────────────

def test_missing_machine_part_is_named_refusal_not_silent_prose(tmp_path):
    e = _engine(tmp_path)
    calls = []

    def dec(ctx):
        calls.append(ctx)
        return {"atomic": True, "metrics": {"tasks": 1},
                "acceptance": ["Given a note, when POST /notes, then 201"]}

    dec.emits_ir = True
    e.agents["decomposer"] = dec
    e._decompose_calls = 0
    e._decomposer_ctx = lambda node, depth, parent, ancestors: {
        "node": {"id": node["id"]}, "depth": depth}
    node = {"id": "core", "title": "core notes"}
    e._expand_node(node, 1, None, ())
    fails = _gate_events(e, verdict="FAIL")
    assert fails, ("an IR-capable decomposer that returned NO machine part "
                   "must be REFUSED by name (gate decomposer_ir), never "
                   "silently accepted as prose-only")
    assert fails[0].task == "core" and fails[0].level == eng.L_MILESTONE
    assert "machine part" in (fails[0].action + fails[0].detail)
    # the refusal DRIVES the retry chain: one bounded re-ask carrying errors
    assert len(calls) == 2, "the seam must re-ask the decomposer once"
    assert calls[1].get("ir_errors"), (
        "the re-ask context must carry the exact refusal errors so the "
        "worker's own llm_backend chain can correct the output")
    assert any(l.get("type") == "decomposer-ir-refused" for l in e.loops)


def test_legacy_decomposer_without_capability_is_untouched(tmp_path):
    # GREEN direction: a simulated decomposer that never promised IR keeps
    # the historical contract — no refusal, no retry, no new FAIL events.
    e = _engine(tmp_path)
    calls = []

    def dec(ctx):
        calls.append(ctx)
        return {"atomic": True, "metrics": {"tasks": 1}}

    e.agents["decomposer"] = dec
    e._decompose_calls = 0
    e._decomposer_ctx = lambda node, depth, parent, ancestors: {
        "node": {"id": node["id"]}, "depth": depth}
    e._expand_node({"id": "core", "title": "core"}, 1, None, ())
    assert len(calls) == 1
    assert not _gate_events(e, verdict="FAIL")


# ── S15.2 invalid IR = refusal naming node id + offending value ─────────────

def test_route_on_node_with_children_is_refused(tmp_path):
    e = _engine(tmp_path)
    out = {"atomic": False,
           "children": [{"id": "core.a"}, {"id": "core.b"}],
           "ir": _machine_part({"core": {
               "children": ["core.a", "core.b"],
               "openapi": _openapi("core", "POST", "/notes", "201")}})}
    errs = e._accept_decomposer_ir({"id": "core"}, out)
    assert errs, "a route claimed by a node WITH children must be refused"
    assert any("core" in x for x in errs), "errors must name the node id"
    fails = _gate_events(e, verdict="FAIL")
    assert fails and fails[0].task == "core"
    assert not e.__dict__.get("_decomposer_ir_nodes"), (
        "a refused fragment must never enter the engine's IR registry")


def test_unknown_key_is_refused_naming_the_value(tmp_path):
    e = _engine(tmp_path)
    out = {"atomic": True,
           "ir": _machine_part({"core": {
               "files": ["src/core.py"],
               "routez": [["POST", "/notes"]]}})}
    errs = e._accept_decomposer_ir({"id": "core"}, out)
    assert errs and any("routez" in x for x in errs), (
        "the refusal must carry the offending key (attributable, P4)")


# ── S15.3 two nodes claiming one (method, path) across calls ────────────────

def test_duplicate_route_across_calls_is_refused(tmp_path):
    e = _engine(tmp_path)
    first = {"atomic": True, "ir": _machine_part({"core": {
        "files": ["src/core.py"],
        "openapi": _openapi("core", "POST", "/notes", "201",
                            required=["text"])}})}
    assert e._accept_decomposer_ir({"id": "core"}, first) == []
    assert _gate_events(e, verdict="PASS"), (
        "an accepted machine part must be journaled (attributable PASS)")
    rival = {"atomic": True, "ir": _machine_part({"web": {
        "files": ["src/web.py"],
        "openapi": _openapi("web", "POST", "/notes", "201",
                            required=["text"])}})}
    errs = e._accept_decomposer_ir({"id": "web"}, rival)
    assert errs and any("core" in x and "web" in x for x in errs), (
        "the merged-document validation must red the SECOND claim naming "
        "both owners")
    reg = e.__dict__.get("_decomposer_ir_nodes") or {}
    assert "web" not in reg and "core" in reg, (
        "first-owner-wins: the rival fragment stays out of the registry")


# ── S15.4 IR wins over prose in route ownership + journaled source ──────────

def test_ir_wins_over_prose_for_route_ownership(tmp_path):
    e = _engine(tmp_path)
    # the node's PROSE names /notes (a dependency) — the v154 trap
    node = {"id": "web_ui", "title": "web page over /notes",
            "requirement": "render the notes list (GET /notes data) on "
                           "GET /ui as server-side HTML"}
    out = {"atomic": True, "ir": _machine_part({"web_ui": {
        "files": ["src/web_ui.py"],
        "openapi": _openapi("web_ui", "GET", "/ui", "200",
                            media="text/html")}})}
    assert e._accept_decomposer_ir(node, out) == []
    owned = e._leaf_owned_routes(node)
    assert owned == [("GET", "/ui")], (
        "with a supplied IR the openapi fragment is the ownership source — "
        "prose-matched /notes must NOT be claimed; got %r" % (owned,))
    srcs = [ev for ev in e.events if "interface_source: ir" in ev.detail]
    assert srcs and srcs[0].task == "web_ui", (
        "the journal must record interface_source: ir for the node")


def test_prose_fallback_survives_and_is_journaled(tmp_path):
    # GREEN direction: a run WITHOUT decomposer IR keeps the existing prose
    # derivation and records its source honestly.
    e = _engine(tmp_path)
    node = {"id": "core", "title": "core notes",
            "requirement": "serve POST /notes and GET /notes"}
    owned = set(e._leaf_owned_routes(node))
    assert owned == {("POST", "/notes"), ("GET", "/notes")}
    srcs = [ev for ev in e.events
            if "interface_source: prose-derived" in ev.detail]
    assert srcs and srcs[0].task == "core"


# ── S15.5 request fields / media follow the accepted IR ─────────────────────

def test_ir_request_fields_and_media_win(tmp_path):
    e = _engine(tmp_path)
    out = {"atomic": True, "ir": _machine_part({
        "core": {"files": ["src/core.py"],
                 "openapi": _openapi("core", "POST", "/notes", "201",
                                     required=["text", "author"])},
        "web_ui": {"files": ["src/web_ui.py"],
                   "openapi": _openapi("web_ui", "GET", "/ui", "200",
                                       media="text/html")}})}
    assert e._accept_decomposer_ir({"id": "core"}, out) == []
    reqf = e._route_request_fields()
    assert reqf.get(("POST", "/notes")) == ["text", "author"], (
        "IR-declared required fields must win over prose guessing; got %r"
        % (reqf.get(("POST", "/notes")),))
    media = e._route_media_map()
    assert media.get("/ui") == "html", (
        "IR-declared response media must feed the media datum; got %r"
        % (media.get("/ui"),))


# ── S15.6 the prompt asks for values; the engine never invents them ─────────

def test_prompt_asks_for_machine_part_with_example_bodies():
    from harness import llm_decomposer as dec
    assert '"ir"' in dec.PROMPT, "the prompt must ask for the machine part"
    assert spec_ir.IR_FORMAT in dec.PROMPT
    assert "when.body" in dec.PROMPT, (
        "the decomposer is ASKED to author concrete example values into "
        "scenario when.body (Phase A open question #1)")
    assert getattr(dec.decompose, "emits_ir", False) is True, (
        "the live decomposer must declare the IR capability so the engine "
        "seam can require the machine part")


def test_missing_body_stays_an_honest_gap_never_invented(tmp_path):
    e = _engine(tmp_path)
    frag = {"files": ["src/core.py"],
            "openapi": _openapi("core", "POST", "/notes", "201",
                                required=["text"]),
            "scenarios": [{"requirement": "core",
                           "when": {"method": "POST", "path": "/notes"},
                           "then": {"status": "201"}}]}
    out = {"atomic": True, "ir": _machine_part({"core": frag})}
    assert e._accept_decomposer_ir({"id": "core"}, out) == [], (
        "a scenario without a body is INCOMPLETE, not invalid")
    reg = e.__dict__.get("_decomposer_ir_nodes") or {}
    sc = (reg.get("core") or {}).get("scenarios")[0]
    assert "body" not in sc["when"], (
        "the engine must never inject a guessed body into the scenario")
    passes = _gate_events(e, verdict="PASS")
    assert passes and "incomplete: 1" in passes[-1].detail, (
        "the accept event must surface the incompleteness tally honestly")
