"""Audit rule (investigator F2, v150): the GET /health response body is ONE
engine-declared datum, read by every consumer — never re-invented per artifact.

v150 defined /health's body three incompatible ways (the handler, the test and
the smoke contract) because the datum lived nowhere machine-readable. The
single source is ``_route_fixed_body`` ('GET /health' -> {"status": "ok"}):
the machine interface contract carries it, the leaf route binding prints it,
and the synthesized entry inlines the same value.

Deterministic: engine unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [],
}


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    return eng


def test_interface_contract_carries_the_health_body(tmp_path):
    eng = _engine(tmp_path)
    pathlib.Path(eng.workspace.root).mkdir(parents=True, exist_ok=True)
    eng._write_interface_contract()
    data = json.loads(
        (pathlib.Path(eng.workspace.root) / "contracts" / "interface.json")
        .read_text(encoding="utf-8"))
    health = next(r for r in data["routes"]
                  if r["path"] == "/health" and r["method"] == "GET")
    assert health.get("body") == {"status": "ok"}, (
        "the machine contract must carry /health's ONE fixed body, got: %r"
        % health)
    # requirement-owned routes carry no invented fixed body
    notes = [r for r in data["routes"] if r["path"] == "/notes"]
    assert notes and all("body" not in r for r in notes)


def test_binding_prints_the_same_health_body(tmp_path):
    eng = _engine(tmp_path)
    node = {"id": "health_leaf", "title": "Liveness",
            "requirement": "Serve GET /health for liveness checks."}
    binding = eng._leaf_route_binding(node)
    line = next(ln for ln in binding.splitlines() if "`GET /health`" in ln)
    assert '{"status": "ok"}' in line, (
        "the binding must print the contracted /health body verbatim: %r"
        % line)


def test_synthesized_entry_inlines_the_same_health_body(tmp_path):
    eng = _engine(tmp_path)
    src = pathlib.Path(eng.workspace.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "notes.py").write_text(
        "def post_notes(payload, query):\n    return (201, {'id': 1})\n\n\n"
        "def get_notes(payload, query):\n    return (200, {'items': []})\n")
    mapping, unresolved = eng._resolve_route_handlers(_CONTRACT)
    code = eng._synthesize_entry_code(_CONTRACT, mapping, unresolved)
    assert code and ("GET", "/health") in unresolved
    want = sfr._route_fixed_body("GET", "/health")
    assert repr(want) in code, (
        "the inline liveness body must read the single-source datum")


def test_fixed_body_is_health_only():
    assert sfr._route_fixed_body("GET", "/health") == {"status": "ok"}
    assert sfr._route_fixed_body("GET", "/healthz") == {"status": "ok"}
    assert sfr._route_fixed_body("POST", "/health") is None
    assert sfr._route_fixed_body("GET", "/notes") is None
    assert sfr._route_fixed_body("GET", "/ping") is None
