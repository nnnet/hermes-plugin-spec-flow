"""STAGE 19 (S19.6-S19.9): the D1 counterexample repair is WIRED into the
repairman seam of the harness (node D2, plan 2026-07-04T00-45).

Why: D1 built the mechanism — deterministic counterexample extraction, the
ONE-function slot prompt, the body-only write door, the bounded Ralph loop —
but left it unconnected: both repair seams (`_orchestra_run`'s fixer step and
`make_implementer`'s repair round) still re-ask with `_REPAIR_DIFF_TASK`,
which carries the FULL module + FULL test + raw pytest dump. For a leaf whose
interface tests are ENGINE-COMPILED (B3, `ctx['tests_precompiled']`) that is
exactly the degrees-of-freedom overdose D1 exists to kill: the failing run
reduces deterministically to a counterexample, so the repair can and must be
a one-function re-ask through the door that makes a full-file rewrite
impossible BY CONSTRUCTION.

What is pinned here:
  * S19.6 ENGAGEMENT (the RED case of the node's acceptance): with the
    tests_precompiled flag AND a failing compiled run, the repairman seam
    hands repair to the doctor's loop — the model receives the one-function
    slot + counterexample prompt (BODY_ONLY_RULE, no sibling function, no
    module body, no SEARCH/REPLACE diff task) and a good body turns the
    leaf green through the SAME two-tier leaf bar; the journal event
    `counterexample_repair` proves which function was re-asked;
  * S19.7 THE DOOR HOLDS AT THE SEAM: a module-shaped reply cannot land —
    the workspace module outside the blamed function stays byte-identical
    and the leaf ends honestly red (never the model's claim);
  * S19.8 FALLBACK IS BYTE-FOR-BYTE LEGACY (the GREEN case): without the
    flag, or with a red run that yields no extractable counterexample, the
    step declines (returns None) WITHOUT any LLM call, any write or any
    journal event — the caller walks the historical whole-file path
    unchanged;
  * S19.9 THE SEAMS ARE CONSULTED: both repair call sites resolve through
    `_counterexample_repair_step` BEFORE `_REPAIR_DIFF_TASK` — otherwise
    the wiring is dead code (the S18.5 `_active_team` lesson).

Deterministic: known-answer IR dicts (the S18/S19 notes shape); the compiled
tests run under REAL pytest in a scratch workspace; the only LLM-shaped agent
is an injected `ask` callable — the same injection seam the doctor's loop
already owns (no monkeypatch on any LLM path).
"""
from __future__ import annotations

import inspect
import pathlib
import sys
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
_TESTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))

import spec_conformance  # noqa: E402
import spec_ir  # noqa: E402
import spec_skeletons  # noqa: E402
from spec_flow_doctor import BODY_ONLY_RULE, apply_function_body  # noqa: E402
from harness import pytest_verifier as pv  # noqa: E402
from harness import role_worker as rw  # noqa: E402


# ── known-answer IR fixtures (the S19 notes shape) ───────────────────────────

def _op(status="200", media="application/json", required=None, handler=""):
    resp: dict = {"description": "contracted"}
    if media:
        resp["content"] = {media: {"schema": {}}}
    op: dict = {"responses": {str(status): resp}}
    if handler:
        op["x-spec-flow-handler"] = handler
    if required is not None:
        op["requestBody"] = {
            "required": bool(required),
            "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {f: {} for f in required},
                "required": list(required),
                "additionalProperties": False}}}}
    return op


def _notes_node():
    return {
        "files": ["src/notes_api.py"],
        "openapi": {"openapi": spec_ir.OPENAPI_VERSION,
                    "info": {"title": "node notes_api interface",
                             "version": "1"},
                    "paths": {"/notes": {
                        "post": _op("201", required=["text"],
                                    handler="post_notes"),
                        "get": _op("200", handler="get_notes")}}},
        "scenarios": [{
            "requirement": "notes-roundtrip",
            "given": {"state": [{"method": "POST", "path": "/notes",
                                 "body": {"text": "hello"}}]},
            "when": {"method": "GET", "path": "/notes"},
            "then": {"status": "200", "media": "application/json",
                     "body_check": {"json_subset": [{"text": "hello"}]}},
        }],
    }


def _ir():
    return {"format": spec_ir.IR_FORMAT, "product": {},
            "nodes": {"notes_api": _notes_node()}}


_GOOD_POST = ("_ITEMS.append(dict(payload or {}))\n"
              "return 201, {'id': len(_ITEMS)}")
_GOOD_GET = "return 200, list(_ITEMS)"
_BAD_POST = "return 500, {'error': 'boom'}"


def _module(post_body=_BAD_POST, get_body=_GOOD_GET):
    """The C1 skeleton with bodies spliced through the D1 door itself."""
    skel = spec_skeletons.compile_skeleton(_ir(), "notes_api")
    assert skel, "fixture IR must compile a skeleton (C1 contract)"
    src = "_ITEMS = []\n\n" + skel
    src = apply_function_body(src, "post_notes", post_body)
    return apply_function_body(src, "get_notes", get_body)


class _DiskWS:
    """A workspace door that REALLY writes — the loop's run_tests must judge
    the spliced module on disk, so an in-memory recorder would green-wash."""

    def __init__(self, root: str):
        self.root = root
        self.writes: list = []

    def _write(self, rel, body, kind):
        p = Path(self.root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        self.writes.append((rel, kind))
        return rel


def _workspace(tmp_path, post_body=_BAD_POST) -> str:
    """A REAL scratch workspace: broken leaf module + engine-compiled tests."""
    root = tmp_path / "wk"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "notes_api.py").write_text(
        _module(post_body), encoding="utf-8")
    (root / "tests" / "test_notes_api.py").write_text(
        spec_conformance.compile_leaf_tests(_ir(), "notes_api"),
        encoding="utf-8")
    return str(root)


def _red_run(root: str) -> tuple[str, int]:
    """The failing compiled run + the pre-repair suite baseline — exactly the
    datums the production call sites hold when the repair step fires."""
    passed, out = rw._run_pytest(root, "tests/test_notes_api.py")
    assert not passed, "the fixture leaf must be red before repair"
    b_passed, b_out = pv.run_suite(root, include_smoke=False)
    return out, pv._badness(b_passed, b_out)


_CTX = {"tests_precompiled": "tests/test_notes_api.py"}


# ── S19.6 engagement: one-function re-ask, green through the real bar ───────

def test_red_compiled_leaf_repairs_via_one_function_reask(tmp_path,
                                                          monkeypatch):
    logged: list = []
    monkeypatch.setattr(rw.llm_log, "log", lambda ev: logged.append(ev))
    root = _workspace(tmp_path)
    test_out, baseline = _red_run(root)
    prompts: list = []

    def ask(prompt):
        prompts.append(prompt)
        return _GOOD_POST

    res = rw._counterexample_repair_step(
        dict(_CTX), _DiskWS(root), root, "n1", "notes_api",
        test_out, baseline, pv, ask=ask)
    assert res is not None, (
        "a red engine-compiled leaf must ENGAGE the counterexample step")
    passed, out = res
    assert passed, "a correct one-function body must turn the leaf green "\
                   "through the real two-tier bar: " + out[-300:]
    # the workspace module now carries the fix — judged by artifacts
    src_now = (Path(root) / "src" / "notes_api.py").read_text(
        encoding="utf-8")
    assert "boom" not in src_now and "_ITEMS.append" in src_now


def test_reask_prompt_is_one_function_not_the_diff_task(tmp_path,
                                                        monkeypatch):
    monkeypatch.setattr(rw.llm_log, "log", lambda ev: None)
    root = _workspace(tmp_path)
    test_out, baseline = _red_run(root)
    prompts: list = []
    rw._counterexample_repair_step(
        dict(_CTX), _DiskWS(root), root, "n1", "notes_api",
        test_out, baseline, pv,
        ask=lambda p: (prompts.append(p) or _GOOD_POST))
    assert prompts, "the engaged step must re-ask the model"
    for p in prompts:
        assert BODY_ONLY_RULE in p, (
            "the re-ask is the doctor's body-only prompt — the same "
            "contract sentence the door enforces")
        # the whole-file diff task is NEVER offered to the model
        assert "SEARCH" not in p and "REPLACE" not in p
        assert "def get_notes" not in p, "no sibling function travels"
        assert "_ITEMS = []" not in p, "no module body travels"
        assert "def test_" not in p, "no test source / raw dump travels"


def test_engagement_is_journaled_with_the_blamed_function(tmp_path,
                                                          monkeypatch):
    logged: list = []
    monkeypatch.setattr(rw.llm_log, "log", lambda ev: logged.append(ev))
    root = _workspace(tmp_path)
    test_out, baseline = _red_run(root)
    rw._counterexample_repair_step(
        dict(_CTX), _DiskWS(root), root, "n1", "notes_api",
        test_out, baseline, pv, ask=lambda p: _GOOD_POST)
    evs = [ev for ev in logged
           if ev.get("event") == "counterexample_repair"]
    assert evs and evs[0].get("node") == "n1", (
        "the one-function repair is journaled, never silent")
    assert evs[0].get("green") is True
    assert "post_notes" in (evs[0].get("functions") or []), (
        "the journal names WHICH function was re-asked — the proof the "
        "re-ask was one-function-scoped")


# ── S19.7 the door holds at the seam: a rewrite cannot land ──────────────────

def test_module_shaped_reply_cannot_land_through_the_seam(tmp_path,
                                                          monkeypatch):
    monkeypatch.setattr(rw.llm_log, "log", lambda ev: None)
    root = _workspace(tmp_path)
    before = (Path(root) / "src" / "notes_api.py").read_text(
        encoding="utf-8")
    test_out, baseline = _red_run(root)
    # a cooperative rewriter: answers every round with a WHOLE module that
    # would fix post_notes but also rewrites get_notes — the v159 class
    evil = _module(post_body=_GOOD_POST, get_body="return 500, []")
    res = rw._counterexample_repair_step(
        dict(_CTX), _DiskWS(root), root, "n1", "notes_api",
        test_out, baseline, pv, ask=lambda p: evil)
    assert res is not None
    passed, _out = res
    assert not passed, "a refused rewrite ends honestly red"
    after = (Path(root) / "src" / "notes_api.py").read_text(encoding="utf-8")
    assert after == before, (
        "the module is byte-identical — the door refused the rewrite, "
        "nothing landed")


# ── S19.8 fallback: no flag / no counterexample -> historical, untouched ────

def test_without_flag_the_step_declines_without_any_call(tmp_path,
                                                         monkeypatch):
    logged: list = []
    monkeypatch.setattr(rw.llm_log, "log", lambda ev: logged.append(ev))
    root = _workspace(tmp_path)
    test_out, baseline = _red_run(root)
    calls: list = []
    ws = _DiskWS(root)
    res = rw._counterexample_repair_step(
        {}, ws, root, "n1", "notes_api", test_out, baseline, pv,
        ask=lambda p: calls.append(p))
    assert res is None, "no flag -> the historical path runs unchanged"
    assert not calls and not ws.writes, (
        "declining must cost zero LLM calls and zero writes")
    assert not any(ev.get("event") == "counterexample_repair"
                   for ev in logged)


def test_without_counterexample_the_step_declines(tmp_path, monkeypatch):
    logged: list = []
    monkeypatch.setattr(rw.llm_log, "log", lambda ev: logged.append(ev))
    root = _workspace(tmp_path)
    _out, baseline = _red_run(root)
    calls: list = []
    ws = _DiskWS(root)
    res = rw._counterexample_repair_step(
        dict(_CTX), ws, root, "n1", "notes_api",
        "Segmentation fault (core dumped)", baseline, pv,
        ask=lambda p: calls.append(p))
    assert res is None, (
        "a red run with no extractable counterexample walks the historical "
        "path — the mechanism never guesses which function to blame")
    assert not calls and not ws.writes
    assert not any(ev.get("event") == "counterexample_repair"
                   for ev in logged)


# ── S19.9 both repair seams consult the step BEFORE the diff task ────────────

def test_repair_seams_consult_counterexample_step_first():
    for seam in (rw._orchestra_run, rw.make_implementer):
        src = inspect.getsource(seam)
        assert "_counterexample_repair_step(" in src, (
            f"{seam.__name__} must resolve repair through the "
            "counterexample step — otherwise the D2 wiring is dead code")
        assert (src.index("_counterexample_repair_step(")
                < src.index("_REPAIR_DIFF_TASK")), (
            f"{seam.__name__}: the one-function step is consulted BEFORE "
            "the whole-file diff task, which stays fallback-only")
