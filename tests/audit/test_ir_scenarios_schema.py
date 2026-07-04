"""Audit rule S13.4 (Phase A, spec-IR): scenarios are FIRST-CLASS IR data
with a tiny CLOSED Given/When/Then schema owned by the engine.

Plan 2026-07-04T00-45, Addendum 1: the G-W-T that stayed prose in cards
since v150 becomes engine data — NOT full Gherkin (a parser would become its
own failure source), but exactly:

    given {env: {name: value}, state: [prior when-steps]}
    when  {method, path, body}
    then  {status, media, body_check (one of equals|contains|json_subset)}

Scenarios group by requirement id. The builder derives them from the same
datums the openapi fragment reads (`_route_owners`,
`_route_success_status`, `_route_media_map`, `_route_request_fields`,
`_route_fixed_body`, the boot roundtrip) — one source, so a scenario and the
interface can never disagree by construction. Values the engine never
recorded (request body values) stay ABSENT — an honest incompleteness
finding, never a guessed default.

Also pins the plan-time dump: the engine writes ir.json ONCE into the
workspace (journal event `ir_written`) so future runs carry the artifact.

Deterministic: engine unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402
import spec_ir  # noqa: E402

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


def _scenario(ir, method, path):
    for s in ir["nodes"]["core"]["scenarios"]:
        w = s.get("when") or {}
        if w.get("method") == method and w.get("path") == path:
            return s
    raise AssertionError("no scenario for %s %s" % (method, path))


# ---- scenarios are first-class, grouped by requirement ------------------------

def test_every_owned_route_yields_a_scenario(tmp_path):
    _, ir = _built(tmp_path)
    scs = ir["nodes"]["core"]["scenarios"]
    got = {(s["when"]["method"], s["when"]["path"]) for s in scs}
    assert got == {("POST", "/notes"), ("GET", "/notes"),
                   ("GET", "/health")}, (
        "scenarios are first-class: one per owned route, from the "
        "ownership datum")
    assert all(s["requirement"] == "core" for s in scs), (
        "scenarios group by requirement id — the owning node")


def test_then_carries_status_and_media_from_datums(tmp_path):
    _, ir = _built(tmp_path)
    post = _scenario(ir, "POST", "/notes")
    assert post["then"]["status"] == 201, (
        "then.status is `_route_success_status` — the same datum the "
        "openapi responses key reads")
    assert post["then"]["media"] == "application/json", (
        "then.media is the `_route_media_map` datum")


def test_fixed_body_route_gets_equals_body_check(tmp_path):
    _, ir = _built(tmp_path)
    health = _scenario(ir, "GET", "/health")
    assert health["then"]["body_check"] == {"equals": {"status": "ok"}}, (
        "GET /health has the ONE contracted body (`_route_fixed_body`) — "
        "the scenario asserts it with body_check.equals")


def test_roundtrip_get_carries_prior_post_state(tmp_path):
    _, ir = _built(tmp_path)
    get = _scenario(ir, "GET", "/notes")
    state = (get.get("given") or {}).get("state") or []
    assert any(st.get("method") == "POST" and st.get("path") == "/notes"
               for st in state), (
        "the boot json_roundtrip datum means POST-then-GET — the GET "
        "scenario's given.state must carry the prior POST when-step")


def test_unrecorded_body_values_stay_absent(tmp_path):
    _, ir = _built(tmp_path)
    post = _scenario(ir, "POST", "/notes")
    assert "body" not in post["when"], (
        "the engine records the request SHAPE (['text']) but no VALUES — "
        "when.body must be ABSENT, never a guessed example")
    rep = spec_ir.validate_ir(ir)
    assert any("POST /notes" in f and "values" in f.lower()
               for f in rep["incomplete"]), (
        "the absent values are an honest incompleteness finding: %r"
        % rep["incomplete"])


# ---- the schema is CLOSED ------------------------------------------------------

def test_given_unknown_key_is_error(tmp_path):
    _, ir = _built(tmp_path)
    get = _scenario(ir, "GET", "/notes")
    get.setdefault("given", {})["mood"] = "hopeful"
    errs = spec_ir.validate_ir(ir)["errors"]
    assert any("mood" in e for e in errs), (
        "given carries exactly {env, state} — free grammar is the failure "
        "source the closed schema exists to prevent: %r" % errs)


def test_body_check_with_two_kinds_is_error(tmp_path):
    _, ir = _built(tmp_path)
    health = _scenario(ir, "GET", "/health")
    health["then"]["body_check"] = {"equals": {"status": "ok"},
                                    "contains": "ok"}
    errs = spec_ir.validate_ir(ir)["errors"]
    assert errs, "body_check is EXACTLY ONE of equals|contains|json_subset"


def test_body_check_unknown_kind_is_error(tmp_path):
    _, ir = _built(tmp_path)
    health = _scenario(ir, "GET", "/health")
    health["then"]["body_check"] = {"matches_regex": ".*ok.*"}
    errs = spec_ir.validate_ir(ir)["errors"]
    assert any("matches_regex" in e for e in errs), (
        "no regex surface, no parser surface — the schema is closed: %r"
        % errs)


def test_scenario_missing_when_or_then_is_error(tmp_path):
    _, ir = _built(tmp_path)
    ir["nodes"]["core"]["scenarios"].append({"requirement": "core"})
    errs = spec_ir.validate_ir(ir)["errors"]
    assert errs, "a scenario without when/then is not a scenario"


def test_engine_built_scenarios_validate_clean(tmp_path):
    _, ir = _built(tmp_path)
    assert spec_ir.validate_ir(ir)["errors"] == [], (
        "builder and validator read the same datums — engine-built "
        "scenarios must always validate clean")


# ---- plan-time dump: one write, journal event ir_written -----------------------

def test_engine_dumps_ir_json_once_with_journal_event(tmp_path):
    eng, _ = _built(tmp_path)
    eng._write_ir()
    p = pathlib.Path(eng.workspace.root) / "ir.json"
    assert p.is_file(), (
        "the engine must dump ir.json into the workspace at plan time so "
        "future runs carry the artifact")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["format"] == spec_ir.IR_FORMAT
    assert "core" in data["nodes"]
    evs = [e for e in eng.events if e.gate == "ir_written"]
    assert len(evs) == 1, (
        "one write, one `ir_written` journal event — greppable in "
        "trace.jsonl")


def test_write_ir_wired_into_the_pipeline():
    # S7.2 pattern: the dump must be CALLED where the plan lands, not
    # orphaned as a method nothing reaches.
    src = pathlib.Path(sfr.__file__).read_text(encoding="utf-8")
    assert "self._write_ir()" in src, (
        "_write_ir is never invoked by the pipeline")
