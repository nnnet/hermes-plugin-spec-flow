"""Audit rule S10.6 (layer 1, task #148): a leaf TEST may assert SUCCESS only
on routes the leaf OWNS, and must assert EXACTLY the contracted status.

v150 layer-1 findings: test_app hedged ``assertIn(code, (200, 201))`` instead
of reading the card's contracted status, and leaf tests asserted 2xx on
routes their leaf never owned — two independent guesses that go red only at
assembly, where the doctor cannot fix the card.

Contract enforced by ``_leaf_test_status_gate``:
  * success (2xx-only) assertion after calling a route the leaf does NOT own
    (foreign or undeclared) -> FAIL at the leaf;
  * negative checks (404/405) on any route stay legal;
  * a smeared success set on an OWNED route (``in (200, 201)`` /
    ``assertIn``) -> FAIL «assert exactly the contracted status».

Deterministic: unit calls over a tmp workspace, no LLM.
"""
from __future__ import annotations

import pathlib
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import spec_flow_runner as sfr  # noqa: E402

_CONTRACT = {
    "entry": "src/app.py",
    "callable": ["wsgi_app"],
    "boot": {"json_roundtrip": "/notes", "ok_route": "/health"},
    "routes": [],
}


def _gate(tmp_path, test_body: str) -> "tuple[bool, list]":
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    node = {"id": "notes_api", "title": "Notes API",
            "requirement": "own POST /notes and GET /notes"}
    rel = "tests/test_notes_api.py"
    p = pathlib.Path(eng.workspace.root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(test_body), encoding="utf-8")
    ok = eng._leaf_test_status_gate(node, "notes_api", 1, rel)
    return ok, [l for l in eng.loops if l["type"] == "test-status-mismatch"]


def test_success_assert_on_foreign_route_is_red(tmp_path):
    ok, loops = _gate(tmp_path, """\
        def _call(method, path):
            return 200


        def test_health_ok():
            code = _call("GET", "/health")
            assert code == 200
    """)
    assert not ok and loops, (
        "a leaf test asserting SUCCESS on a route the leaf does not own "
        "(/health belongs to another leaf) must red at the leaf")
    assert "does NOT own" in loops[0]["detail"]


def test_success_assert_on_undeclared_route_is_red(tmp_path):
    ok, loops = _gate(tmp_path, """\
        def _call(method, path):
            return 200


        def test_mystery_ok():
            code = _call("GET", "/mystery")
            assert code in (200, 201)
    """)
    assert not ok and loops, (
        "asserting success on a route nobody declared is double-guessing")


def test_negative_check_on_foreign_route_is_legal(tmp_path):
    ok, loops = _gate(tmp_path, """\
        def _call(method, path):
            return 404


        def test_unknown_404():
            code = _call("GET", "/health")
            assert code == 404


        def test_bad_method_405():
            code = _call("DELETE", "/health")
            assert code in (404, 405)
    """)
    assert ok and not loops, "negative checks (404/405) must stay legal"


def test_smeared_success_set_on_own_route_never_survives(tmp_path):
    # S12.7 (v161) superseded the plain red: a smear whose set CONTAINS the
    # contracted status is mechanically repaired by the engine (journaled),
    # so the artifact that leaves the gate asserts EXACTLY the contract.
    # The class invariant is unchanged: the smear cannot pass silently.
    ok, loops = _gate(tmp_path, """\
        def _call(method, path):
            return 201


        def test_post_created():
            code = _call("POST", "/notes")
            assert code in (200, 201)
    """)
    assert ok and not loops, (
        "the fixable smear is repaired in place — the gate is clean over "
        "the REPAIRED artifact, not red over the pre-fix text")


def test_smeared_set_without_contracted_status_is_red(tmp_path):
    # the HONEST-RED twin: no contracted member = no mechanical answer —
    # the classic finding («assert exactly ...») must still fire
    ok, loops = _gate(tmp_path, """\
        def _call(method, path):
            return 200


        def test_post_created():
            code = _call("POST", "/notes")
            assert code in (200, 202)
    """)
    assert not ok and loops, (
        "a smear NOT containing the contracted 201 is wrong in a way no "
        "mechanical edit can settle — it must red honestly")
    assert "exactly" in loops[0]["detail"]


def test_unittest_assertin_smear_never_survives(tmp_path):
    ok, loops = _gate(tmp_path, """\
        import unittest


        class T(unittest.TestCase):
            def _call(self, method, path):
                return 201

            def test_post_created(self):
                code = self._call("POST", "/notes")
                self.assertIn(code, (200, 201))
    """)
    assert ok and not loops, (
        "the unittest assertIn smear is the same fixable class — repaired "
        "to assertEqual(code, 201), gate clean")


def test_exact_contracted_status_on_own_route_is_green(tmp_path):
    ok, loops = _gate(tmp_path, """\
        def _call(method, path):
            return 201


        def test_post_created():
            code = _call("POST", "/notes")
            assert code == 201


        def test_get_ok():
            code = _call("GET", "/notes")
            assert code == 200
    """)
    assert ok and not loops, "exact contracted statuses must pass the gate"
