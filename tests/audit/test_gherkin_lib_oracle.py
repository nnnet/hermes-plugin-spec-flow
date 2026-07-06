"""Audit rule S31 (node N1): the Gherkin behavioural spec is validated by a
READY-MADE parser library, not a home-grown scenario splitter.

Why (user 2026-07-06, hard rule): a spec must be in a machine STANDARD and its
grammar enforced by a maintained oracle LIBRARY, never by our own string logic.
The closed Given/When/Then scenario schema (`spec_ir._check_scenario`) checked
dict keys by hand and consciously declared "not Gherkin, no parser surface".
That left a whole GRAMMATICAL class invisible: a scenario whose closed structure
is well-formed can still be a broken Gherkin document (a value injecting a
`Feature:`/`Scenario:` line, a step text that cannot be a Gherkin step).

What this pins (both directions, S28/v151 clause):
  * S31.1 — a Gherkin oracle exists in the scenario validation path and it uses
    the `gherkin-official` parser library (an `import gherkin`), NOT a hand
    splitter. `spec_ir.gherkin_errors(ir)` must be that oracle.
  * S31.2 RED direction — a closed-schema-VALID but grammatically-broken
    scenario (a route path carrying an embedded `\nFeature:` line) is well-formed
    to the hand `_check_scenario` yet reds through the library oracle.
  * S31.2 GREEN direction — every engine-built scenario renders to VALID Gherkin
    and the oracle stays silent (no false positive on a legitimate node).

This test is RED before N1: `spec_ir` has no `gherkin_errors` and does not import
`gherkin` in the scenario validation path, so the closed-valid-but-broken
scenario passes unflagged (the hand-only world). It goes GREEN once the library
oracle is wired in beside the hand cross-rules (the S13.8 / S14.7 pattern).

Deterministic: engine unit calls over a tmp workspace + a synthetic scenario;
no LLM.
"""
from __future__ import annotations

import inspect
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402
import spec_ir  # noqa: E402

# the same deterministic engine fixture the S13.4 schema audit uses, so the
# GREEN direction runs over REAL engine-built scenarios, not a hand mock.
_GOAL = (
    'A tiny notes service. Two HTTP endpoints over a WSGI app: POST /notes '
    'accepts {"text": "..."} and stores it, returning {"id": <int>}; '
    'GET /notes returns {"items": [...]} newest-first.')
_CONSTITUTION = [
    "Standard library ONLY: HTTP through a WSGI app (src/app.py exposes "
    "`wsgi_app`).",
]
_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [],
    "media": {"/notes": "json", "/health": "json"},
}
_NODE = {"id": "core", "title": "Product core",
         "requirement": "own POST /notes and GET /notes and GET /health"}


def _built(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    eng._goal = _GOAL
    eng._constitution = list(_CONSTITUTION)
    assert eng._leaf_owned_routes(dict(_NODE))
    return eng, spec_ir.build_ir(eng)


def _gherkin_available() -> bool:
    try:
        import gherkin  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ---- S31.1 the oracle exists and is LIBRARY-backed ----------------------------

def test_scenario_validation_has_a_gherkin_library_oracle():
    assert hasattr(spec_ir, "gherkin_errors"), (
        "the Gherkin grammar of a scenario must be validated by a ready-made "
        "parser oracle `spec_ir.gherkin_errors`, not by a home-grown splitter")


def test_gherkin_oracle_uses_the_parser_library_not_hand_logic():
    src = inspect.getsource(spec_ir)
    assert "import gherkin" in src or "from gherkin" in src, (
        "the oracle must PARSE with the gherkin-official library — a hand "
        "string splitter is exactly what the user forbade (2026-07-06)")


# ---- S31.2 RED direction: library catches what the hand check misses -----------

def _hijack_scenario():
    # a closed-schema-VALID scenario over a REAL declared route (/health): when
    # has {method, path}, then has {status, body_check} — the hand
    # `_check_scenario` is fully satisfied and reds NOTHING. But the requirement
    # id (which seeds the Scenario title) carries an embedded newline that opens
    # a SECOND `Scenario:` at feature-child indent. The canonical Gherkin
    # rendering therefore parses into TWO scenarios, the first empty of steps —
    # a grammatical break the parser AST exposes and the hand key-check cannot.
    return {"requirement": "core\n  Scenario: hijack",
            "when": {"method": "GET", "path": "/health"},
            "then": {"status": 200, "body_check": {"equals": {"status": "ok"}}}}


@pytest.mark.skipif(not _gherkin_available(),
                    reason="gherkin-official (dev/test oracle) not installed")
def test_broken_gherkin_scenario_that_hand_check_passes_is_caught(tmp_path):
    _, ir = _built(tmp_path)
    ir["nodes"]["core"]["scenarios"] = [_hijack_scenario()]

    # precondition — the HAND world (library oracle disabled) is grammar-blind:
    # with the parser unavailable, validate_ir reds NOTHING on this closed-valid
    # scenario. This both proves the break is invisible to the hand check AND
    # exercises the S31.3 degrade contract (absent library => hand rules stand).
    saved = spec_ir._GHERKIN_PARSER
    spec_ir._GHERKIN_PARSER = False  # force the absent-library path
    try:
        hand = spec_ir.validate_ir(ir)["errors"]
    finally:
        spec_ir._GHERKIN_PARSER = saved
    assert hand == [], (
        "precondition: with the library oracle disabled the hand check passes "
        "this closed-valid scenario clean — the grammatical break is invisible "
        "to it; got %r" % hand)

    # the LIBRARY oracle DOES red on the broken grammar:
    lib = spec_ir.gherkin_errors(ir)
    assert lib, (
        "a closed-valid but grammatically-broken scenario must red through "
        "the gherkin-official parser oracle")
    assert any("core" in e for e in lib), (
        "the oracle names the offending node: %r" % lib)


# ---- S31.2 GREEN direction: no false positive on engine-built scenarios --------

@pytest.mark.skipif(not _gherkin_available(),
                    reason="gherkin-official (dev/test oracle) not installed")
def test_engine_built_scenarios_render_to_valid_gherkin(tmp_path):
    _, ir = _built(tmp_path)
    assert spec_ir.gherkin_errors(ir) == [], (
        "every engine-built scenario must render to VALID Gherkin — the oracle "
        "never false-flags a legitimate node (both-directions clause)")


def test_validate_ir_folds_in_the_gherkin_oracle(tmp_path):
    # the oracle is not an orphan helper — validate_ir must FOLD its verdict in
    # so the closed-world gate reds on a broken-grammar scenario (the S13.8 /
    # S14.7 wiring pattern: a library oracle sits BESIDE the hand rules).
    _, ir = _built(tmp_path)
    ir["nodes"]["core"]["scenarios"] = [_hijack_scenario()]
    errs = spec_ir.validate_ir(ir)["errors"]
    assert any("gherkin" in e.lower() for e in errs), (
        "validate_ir must surface the gherkin oracle's verdict, not leave it "
        "as an unreached helper: %r" % errs)
