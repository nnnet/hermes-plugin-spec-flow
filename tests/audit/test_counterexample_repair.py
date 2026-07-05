"""STAGE 19 (S19.1-S19.5): counterexample-driven ONE-FUNCTION repair — the
doctor extracts input/expected/got from a failing compiled-test run and the
remedy re-asks the model for ONE function body with FRESH context (node D1,
plan 2026-07-04T00-45).

Why: the existing repair paths re-ask with the WHOLE module plus the raw
pytest dump (_REPAIR_TASK / _REPAIR_DIFF_TASK carry full src + full test +
output tail), so a weak model is invited to rewrite everything it sees —
v159 "the core rework re-guessed the module and erased routes" and the v160+
eternal-open causes are one class: a repair whose degrees of freedom exceed
the defect. B3 compiled the leaf's contract tests FROM the IR and C1 made the
skeleton engine-owned; this stage composes them: when a compiled contract
test fails, the ENGINE (deterministic code, no LLM) extracts a COUNTEREXAMPLE
— which function, which input, what was expected, what came back — and the
re-ask carries ONLY that one function's slot plus the counterexample. The
write door accepts ONLY that function's body, so a full-file rewrite is
impossible BY CONSTRUCTION (model-independence via minimum degrees of
freedom). The loop is a Ralph loop: fresh context each iteration, ONE task
per iteration, objective exit = the REAL contract-test run going green.

What is pinned here:
  * S19.1 counterexample extraction is DETERMINISTIC ENGINE CODE parsing the
    compiled-test failure output — never an LLM call; the failing STEP is
    attributed (a scenario red on its given.state POST blames post_notes,
    not the when-step handler); a green run or garbage output yields no
    counterexample and no crash (total);
  * S19.2 the re-ask is ONE function with FRESH context: the prompt carries
    the target function's engine slot (def line + contract anchor) and the
    counterexample — never another function, never the module, never the
    raw pytest dump;
  * S19.3 the write door accepts ONLY that function's body: an unknown
    function, a module-shaped reply, a renamed def or a changed argument
    list are REFUSED with a named ValueError; a sneaky body smuggling a
    sibling's def lands as a harmless nested def while every byte outside
    the target block stays identical — the C1 skeleton_conformance door
    stays green over the splice;
  * S19.4 the Ralph loop's exit is OBJECTIVE: green means the REAL test run
    passed, never the model's claim; one counterexample per iteration;
    bounded rounds; a red run without an extractable counterexample stops
    honestly instead of looping;
  * S19.5 the knobs are DATA in the remedies config layering (max_rounds
    overridable per case), not literals in the loop.

Deterministic: known-answer IR dicts; the compiled tests run under REAL
pytest in a scratch workspace; the only LLM-shaped agent is an injected
local stub (the same injection seam the doctor's Classifier already uses).
"""
from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import spec_conformance  # noqa: E402
import spec_flow_remedies  # noqa: E402
import spec_ir  # noqa: E402
import spec_skeletons  # noqa: E402
from spec_flow_doctor import (  # noqa: E402  (RED before code: names absent)
    BODY_ONLY_RULE, Counterexample, apply_function_body,
    counterexample_repair, extract_counterexamples, function_slot,
    repair_prompt)


# ── known-answer IR fixtures (the S18 notes shape + explicit handlers) ───────

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


def _doc(nid, paths):
    return {"openapi": spec_ir.OPENAPI_VERSION,
            "info": {"title": "node %s interface" % nid, "version": "1"},
            "paths": paths}


def _notes_node():
    return {
        "files": ["src/notes_api.py"],
        "openapi": _doc("notes_api", {
            "/notes": {"post": _op("201", required=["text"],
                                   handler="post_notes"),
                       "get": _op("200", handler="get_notes")},
        }),
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


def _module(post_body=_GOOD_POST, get_body=_GOOD_GET):
    """The C1 skeleton with bodies spliced through the D1 door itself —
    the fixture path exercises the same by-construction seam it audits."""
    skel = spec_skeletons.compile_skeleton(_ir(), "notes_api")
    assert skel, "fixture IR must compile a skeleton (C1 contract)"
    src = "_ITEMS = []\n\n" + skel
    src = apply_function_body(src, "post_notes", post_body)
    return apply_function_body(src, "get_notes", get_body)


def _run_compiled(tmp_path, module_src):
    """REAL pytest over the REAL compiled contract tests — the objective
    oracle every gate below judges against (no LLM, no monkeypatch)."""
    tests = spec_conformance.compile_leaf_tests(_ir(), "notes_api")
    root = tmp_path / "wk"
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "src" / "notes_api.py").write_text(module_src, encoding="utf-8")
    (root / "tests" / "test_notes_api.py").write_text(tests, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src")
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_notes_api.py", "-q",
         "--no-header", "-p", "no:cacheprovider"],
        capture_output=True, text=True, timeout=120, cwd=str(root), env=env)
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def _tests_src():
    return spec_conformance.compile_leaf_tests(_ir(), "notes_api")


# ── S19.1 deterministic extraction from a REAL failing run ──────────────────

def test_counterexample_extracted_from_real_failing_contract_run(tmp_path):
    passed, out = _run_compiled(tmp_path, _module(post_body=_BAD_POST))
    assert not passed, "the wrong module must fail its compiled contract"
    cxs = extract_counterexamples(_tests_src(), out)
    assert cxs, "a failing compiled run must yield counterexamples"
    by_fn = {c.function for c in cxs}
    assert "post_notes" in by_fn, (
        "the broken handler is the counterexample's function")
    cx = next(c for c in cxs if c.test == "test_post_notes_status_201")
    assert cx.method == "POST" and cx.path == "/notes", (
        "the input names the contracted route the failing test invoked")
    assert cx.payload == {"text": "hello"}, (
        "the input payload is the IR scenario datum the compiler valued the "
        "request with — never an invented value")
    assert "201" in cx.expected, "expected quotes the contracted assert"
    assert "500" in cx.got, "got quotes the observed value from the run"
    # deterministic: same output + same source -> same counterexamples
    assert extract_counterexamples(_tests_src(), out) == cxs


def test_scenario_failure_blames_the_failing_step(tmp_path):
    """The roundtrip scenario reds on its given.state POST (500 < 400 is
    false) — the counterexample must blame post_notes, the step that FAILED,
    never the when-step handler that was never reached."""
    _passed, out = _run_compiled(tmp_path, _module(post_body=_BAD_POST))
    sc = [c for c in extract_counterexamples(_tests_src(), out)
          if c.test.startswith("test_scenario_")]
    assert sc, "the failing scenario test must yield a counterexample"
    assert sc[0].function == "post_notes" and sc[0].method == "POST", (
        "attribution is the FAILING step (given.state POST), not get_notes")


def test_green_run_yields_no_counterexamples(tmp_path):
    passed, out = _run_compiled(tmp_path, _module())
    assert passed, "the correct module must pass its compiled contract"
    assert extract_counterexamples(_tests_src(), out) == [], (
        "a green run has no counterexample — nothing to repair")


def test_extraction_is_total_on_garbage_output():
    for junk in ("", "no failures here", "FAILED but not pytest-shaped ::",
                 "E   assert orphan line with no section"):
        assert extract_counterexamples(_tests_src(), junk) == [], (
            "garbage output yields [] — extraction never crashes or invents")


# ── S19.2 the re-ask is ONE function slot with FRESH context ─────────────────

def _cx():
    return Counterexample(
        test="test_post_notes_status_201", function="post_notes",
        method="POST", path="/notes", payload={"text": "hello"},
        expected="assert status == 201", got="assert 500 == 201")


def test_function_slot_is_signature_and_anchor_only():
    slot = function_slot(_module(post_body=_BAD_POST), "post_notes")
    assert "def post_notes(payload, query):" in slot, (
        "the slot carries the engine-owned def line")
    assert "AICODE-NOTE" in slot and "POST /notes" in slot, (
        "the slot carries the C1 contract anchor naming the route contract")
    assert "boom" not in slot and "return" not in slot, (
        "the slot is the EMPTY body slot — the failed body is not replayed")
    assert "get_notes" not in slot, "one function means ONE function"


def test_repair_prompt_carries_one_function_and_no_module():
    module = _module(post_body=_BAD_POST)
    prompt = repair_prompt(_cx(), function_slot(module, "post_notes"))
    assert "post_notes" in prompt and "assert 500 == 201" in prompt, (
        "the prompt carries the counterexample: input/expected/got")
    assert "POST" in prompt and "/notes" in prompt and "hello" in prompt
    assert BODY_ONLY_RULE in prompt, (
        "the prompt states the body-only contract the door enforces")
    assert "def get_notes" not in prompt and "_ITEMS" not in prompt, (
        "FRESH context: no other function, no module body — the re-ask "
        "cannot invite a whole-file rewrite")
    assert "short test summary info" not in prompt, (
        "the raw pytest dump never travels — only the counterexample")


# ── S19.3 the write door accepts ONLY that function's body ──────────────────

def test_door_refuses_unknown_function():
    with pytest.raises(ValueError, match="ghost"):
        apply_function_body(_module(), "ghost", "return 1")


def test_door_refuses_module_shaped_reply():
    module = _module(post_body=_BAD_POST)
    shaped = ("import os\n\n"
              "def post_notes(payload, query):\n    return 201, {}\n\n"
              "def get_notes(payload, query):\n    return 200, []\n")
    with pytest.raises(ValueError):
        apply_function_body(module, "post_notes", shaped)
    with pytest.raises(ValueError):        # renamed def
        apply_function_body(module, "post_notes",
                            "def post_note(payload, query):\n    return 201, {}")
    with pytest.raises(ValueError):        # rewritten argument list
        apply_function_body(module, "post_notes",
                            "def post_notes(request):\n    return 201, {}")
    with pytest.raises(ValueError):        # not a function body at all
        apply_function_body(module, "post_notes", "return oops(")


def test_door_full_file_rewrite_impossible_by_construction():
    """A body smuggling a sibling's def cannot touch the sibling: it lands
    as a harmless NESTED def while every byte outside the target block is
    unchanged — the rewrite is impossible by construction, not by review."""
    module = _module(post_body=_BAD_POST)
    sneaky = ("def get_notes(payload, query):\n"
              "    return 200, {'hacked': True}\n"
              "return 201, {'id': 1}")
    out = apply_function_body(module, "post_notes", sneaky)
    before, after = ast.parse(module), ast.parse(out)

    def _seg(tree, src, name):
        fd = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == name)
        return ast.get_source_segment(src, fd)

    assert _seg(before, module, "get_notes") == _seg(after, out, "get_notes"), (
        "the sibling function is byte-identical — only the target changed")
    tops = [n.name for n in after.body if isinstance(n, ast.FunctionDef)]
    assert tops == [n.name for n in before.body
                    if isinstance(n, ast.FunctionDef)], (
        "the module-level surface is unchanged (no def added or lost)")


def test_door_green_splice_keeps_engine_surface():
    module = _module(post_body=_BAD_POST)
    for reply in (_GOOD_POST,                       # bare body
                  "```python\n" + _GOOD_POST + "\n```"):   # fenced body
        out = apply_function_body(module, "post_notes", reply)
        tree = ast.parse(out)                        # the result parses
        fd = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "post_notes")
        assert [a.arg for a in fd.args.args] == ["payload", "query"], (
            "the engine-owned signature survives the splice")
        assert "AICODE-NOTE: skeleton-contract post_notes" in out, (
            "the C1 contract anchor survives the splice")
        assert spec_skeletons.skeleton_conformance(_ir(), "notes_api",
                                                   out) == [], (
            "the C1 write door stays green over a D1 body splice")
        assert "boom" not in out, "the failed body was actually replaced"


# ── S19.4 Ralph loop: fresh context, one task, objective exit ────────────────

def test_ralph_loop_exits_green_on_the_real_test_run(tmp_path):
    def run_tests(src):
        return _run_compiled(tmp_path / "green", src)

    def fix(_prompt):
        return _GOOD_POST

    res = counterexample_repair(_module(post_body=_BAD_POST), _tests_src(),
                                run_tests=run_tests, ask=fix)
    assert res["green"] is True, "the objective exit: the REAL run is green"
    applied = [r for r in res["records"] if r.get("applied")]
    assert len(applied) == 1 and applied[0]["function"] == "post_notes", (
        "one iteration, one function — the Ralph discipline")
    assert "201" in res["module"], "the repaired module carries the fix"


def test_ralph_loop_one_task_per_iteration_and_never_claims_green(tmp_path):
    prompts: list = []
    round_no = [0]

    def run_tests(src):
        return _run_compiled(tmp_path / ("red%d" % len(prompts)), src)

    def still_wrong(prompt):
        prompts.append(prompt)
        round_no[0] += 1
        return "return %d, {'error': 'still wrong'}" % (500 + round_no[0])

    res = counterexample_repair(_module(post_body=_BAD_POST), _tests_src(),
                                run_tests=run_tests, ask=still_wrong,
                                max_rounds=2)
    assert res["green"] is False, (
        "the model answered every round yet the loop NEVER claims green — "
        "the verdict is the real run, not the model's cooperation")
    assert len(res["records"]) == 2 and len(prompts) == 2, (
        "bounded: exactly max_rounds iterations, one task each")
    for p in prompts:
        assert "def get_notes" not in p and "_ITEMS" not in p, (
            "every iteration re-asks ONE function with fresh context")
    assert "501" in prompts[1], (
        "iteration 2's counterexample quotes the NEW observed value — the "
        "context is re-extracted from the latest run, never replayed")


def test_ralph_loop_stops_honestly_without_counterexample():
    calls = {"ask": 0}

    def red_junk(_src):
        return False, "boot crashed before pytest could report"

    def never(_prompt):
        calls["ask"] += 1
        return ""

    res = counterexample_repair(_module(post_body=_BAD_POST), _tests_src(),
                                run_tests=red_junk, ask=never, max_rounds=3)
    assert res["green"] is False and calls["ask"] == 0, (
        "a red run with no extractable counterexample stops WITHOUT an LLM "
        "call — the remedy never guesses which function to blame")
    assert any("counterexample" in str(r.get("detail", "")).lower()
               for r in res["records"]), "the stop reason is journaled"


# ── S19.5 knobs are data in the remedies layering ────────────────────────────

def test_counterexample_config_is_layered_data():
    cfg = spec_flow_remedies.counterexample_config()
    assert int(cfg["max_rounds"]) >= 1, "a sane factory default exists"
    over = spec_flow_remedies.counterexample_config(
        {"counterexample": {"max_rounds": 5}})
    assert int(over["max_rounds"]) == 5, (
        "the case YAML layer overrides the factory default (5-layer merge)")
