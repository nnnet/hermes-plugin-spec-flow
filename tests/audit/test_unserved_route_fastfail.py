"""Audit rule S12.3 (v159): a DECLARED route with NO resolvable handler must
fail FAST at the assembly barrier — a named FAIL milestone (route + owner
leaf), never only a generic 404 inside the suite.

v159 (p6-micro-notes): GET /about was in the contract from early on; the
entry synthesis serves declared routes only when a handler exists ("a
missing HTML page or extra route is simply omitted — it will 404"), so the
run reached final assembly with a 404 and only the LAST plan check (tick
170, after the final integrate FAIL) said "route GET /about: no owner leaf
(orphan)". Honest, but LATE — the doctor spent its repair rounds on
'weak_implementer' with no route-level attribution.

Contract enforced (deterministic, cheap):
  * at the re-verify barrier (``_verify_tests``, right after the entry is
    synthesized, BEFORE the suite runs), ``_unserved_route_gate`` resolves
    every declared route (``_resolve_route_handlers``) and emits a named
    FAIL milestone per unresolved route: route, canonical handler, owner
    leaf and its module — feeding the doctor at the FIRST assembly;
  * the inlined health route (an unresolved ok_route is synthesized as a
    trivial 200 by the entry) is NOT flagged — it IS served;
  * GREEN edges: an all-served plan is silent; a non-web project ({} product
    contract) is a clean no-op.

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
_SERVED = """\
    def post_notes(payload, query):
        return 201, {"id": 1}


    def get_notes(payload, query):
        return 200, {"items": []}


    def get_health(payload, query):
        return 200, {"status": "ok"}
"""


def _engine(tmp_path, core_body=_SERVED, contract=_CONTRACT):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(contract)
    eng._leaf_owned_routes({"id": "core", "requirement":
                            "own POST /notes and GET /notes and GET /health"})
    eng._leaf_owned_routes({"id": "about_page", "code_target": "src/core.py",
                            "binds_route": ["GET", "/about"]})
    root = pathlib.Path(eng.workspace.root)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src/core.py").write_text(textwrap.dedent(core_body),
                                      encoding="utf-8")
    return eng


# ---- RED: the v159 late-404 shape -------------------------------------------

def test_declared_route_without_handler_fails_fast(tmp_path):
    eng = _engine(tmp_path)          # get_about landed nowhere
    findings = eng._unserved_route_gate()
    loops = [l for l in eng.loops if l["type"] == "unserved-route"]
    assert findings and loops, (
        "GET /about is DECLARED but no module defines get_about — the "
        "assembly barrier must produce a named FAIL milestone at the FIRST "
        "assembly, not a generic 404 in the suite (v159: the orphan finding "
        "surfaced only in the last plan check, after the final FAIL)")
    detail = loops[0]["detail"]
    assert "GET" in detail and "/about" in detail, (
        "the finding must NAME the route")
    assert "about_page" in detail, "the finding must NAME the owner leaf"
    assert "get_about" in detail, "the finding must NAME the missing handler"


def test_inlined_health_route_is_not_flagged(tmp_path):
    # an unresolved ok_route is synthesized inline (trivial 200) — served
    body = _SERVED.replace(
        "def get_health(payload, query):\n"
        "        return 200, {\"status\": \"ok\"}\n", "")
    eng = _engine(tmp_path, core_body=body,
                  contract={**_CONTRACT, "routes": []})
    assert eng._unserved_route_gate() == [], (
        "the entry synthesizes the missing health route inline — flagging "
        "it would be a false positive (v151 lesson)")


# ---- GREEN edges ------------------------------------------------------------

def test_all_served_plan_is_silent(tmp_path):
    eng = _engine(tmp_path, core_body=_SERVED + """\


    def get_about(payload, query):
        return 200, "<html>about</html>"
""")
    assert eng._unserved_route_gate() == []
    assert not [l for l in eng.loops if l["type"] == "unserved-route"]


def test_non_web_project_is_noop(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: {}
    assert eng._unserved_route_gate() == []


def test_gate_runs_at_the_reverify_barrier():
    # S7.2: the gate must fire where assembly happens — inside _verify_tests,
    # after entry synthesis and BEFORE the suite runs
    src = pathlib.Path(sfr.__file__).read_text(encoding="utf-8")
    body = src.split("def _verify_tests", 1)[1].split("\n    def ")[0]
    gate_at = body.find("_unserved_route_gate")
    suite_at = body.find("_run_suite()")
    assert gate_at != -1, "_verify_tests never calls _unserved_route_gate"
    assert suite_at == -1 or gate_at < suite_at, (
        "the gate must fire at the FIRST assembly, before the suite verdict")
