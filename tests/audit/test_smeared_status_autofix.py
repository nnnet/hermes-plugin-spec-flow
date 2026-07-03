"""Audit rule S12.7 (v161): a MECHANICALLY fixable test defect is fixed by the
engine, never round-tripped through a model that keeps failing to apply the
exact feedback.

v160+v161 (p6-micro-notes): the test-status gate found `tests/test_app.py
asserts membership over [200, 201] after POST /notes — assert exactly the
contracted status 201` and the doctor sent the SAME exact feedback into
rework in BOTH runs; the worker model delivered the SAME smear back both
times and the finding held the root red at the terminal. A smeared
membership assert whose set CONTAINS the contracted status carries zero
ambiguity — the contracted value is engine data (`_route_success_status`),
so the rewrite `assert code in (200, 201)` -> `assert code == 201` (and the
unittest twin `assertIn` -> `assertEqual`) is a deterministic AST-level edit
the engine performs itself, journaled as an event.

Contract pinned here:
  * RED (pre-fix): a leaf test smearing an OWNED route's success status with
    the contracted value IN the set is REWRITTEN in place — after the gate
    pass the file asserts exactly the contracted status, the gate is clean,
    and the autofix is journaled (loop entry + milestone event);
  * the unittest `assertIn(code, (200, 201))` twin is rewritten to
    `assertEqual(code, 201)`;
  * GREEN: an already-exact assertion is left byte-identical;
  * HONEST RED: a smeared set NOT containing the contracted value is NOT
    autofixed — the test is wrong in a way no mechanical edit can settle
    (which guess is right?), so the gate reds with the classic finding;
  * quiet mode (the S12.5 recheck) stays side-effect-free: verdict only,
    no rewrite.

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

_NODE = {"id": "notes_api", "title": "Notes API",
         "requirement": "own POST /notes and GET /notes"}
_REL = "tests/test_notes_api.py"


def _gate(tmp_path, test_body: str, quiet: bool = False):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    p = pathlib.Path(eng.workspace.root) / _REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(test_body), encoding="utf-8")
    ok = eng._leaf_test_status_gate(dict(_NODE), "notes_api", 1, _REL,
                                    quiet=quiet)
    return eng, ok, p


# --- RED (v161): the fixable smear is repaired by the engine itself -----------

def test_smeared_membership_with_contracted_value_is_autofixed(tmp_path):
    eng, ok, p = _gate(tmp_path, """\
        def _call(method, path):
            return 201


        def test_post_created():
            code = _call("POST", "/notes")
            assert code in (200, 201)
    """)
    text = p.read_text(encoding="utf-8")
    assert "assert code == 201" in text, (
        "v160/v161: the exact feedback failed to converge through the model "
        "TWICE — a smear whose set contains the contracted 201 is "
        f"mechanically fixable and the ENGINE must rewrite it; got:\n{text}")
    assert "in (200, 201)" not in text
    assert ok is True, (
        "after the mechanical repair the artifact satisfies the contract — "
        "the gate must be clean, not red on the pre-fix text")
    fixes = [l for l in eng.loops if l.get("type") == "test-status-autofix"]
    assert fixes, "the autofix must be journaled as a loop entry"
    assert "201" in fixes[0]["detail"] and _REL in fixes[0]["detail"], (
        f"the journal names the file and the contracted status: {fixes[0]}")
    ev = [e for e in eng.events if getattr(e, "gate", "") ==
          "test_status_autofix"]
    assert ev, "the autofix must be a visible milestone event"


def test_unittest_assertin_twin_is_autofixed(tmp_path):
    eng, ok, p = _gate(tmp_path, """\
        import unittest


        class T(unittest.TestCase):
            def _call(self, method, path):
                return 201

            def test_post_created(self):
                code = self._call("POST", "/notes")
                self.assertIn(code, (200, 201))
    """)
    text = p.read_text(encoding="utf-8")
    assert "self.assertEqual(code, 201)" in text, (
        f"the unittest smear twin must be rewritten too; got:\n{text}")
    assert "assertIn" not in text
    assert ok is True
    # the repaired file must still be valid python
    compile(text, str(p), "exec")


def test_autofixed_file_still_parses_and_keeps_other_lines(tmp_path):
    eng, ok, p = _gate(tmp_path, """\
        def _call(method, path):
            return 201


        def test_post_created():
            code = _call("POST", "/notes")
            assert code in (200, 201)


        def test_get_ok():
            code = _call("GET", "/notes")
            assert code == 200
    """)
    text = p.read_text(encoding="utf-8")
    compile(text, str(p), "exec")
    assert "assert code == 200" in text, "untouched assertions must survive"
    assert "assert code == 201" in text
    assert ok is True


# --- GREEN: exact assertions are byte-identical --------------------------------

def test_already_exact_assertion_is_untouched(tmp_path):
    body = """\
        def _call(method, path):
            return 201


        def test_post_created():
            code = _call("POST", "/notes")
            assert code == 201
    """
    eng, ok, p = _gate(tmp_path, body)
    assert ok is True
    assert p.read_text(encoding="utf-8") == textwrap.dedent(body), (
        "an already-exact file must not be rewritten at all")
    assert not [l for l in eng.loops if l.get("type") == "test-status-autofix"]


# --- HONEST RED: a smear NOT containing the contracted value stays red --------

def test_smear_without_contracted_value_is_not_autofixed(tmp_path):
    body = """\
        def _call(method, path):
            return 200


        def test_post_created():
            code = _call("POST", "/notes")
            assert code in (200, 202)
    """
    eng, ok, p = _gate(tmp_path, body)
    assert ok is False, (
        "a smeared set NOT containing the contracted 201 is wrong in a way "
        "no mechanical edit can settle — it must red honestly, never be "
        "'fixed' into an assertion the tester never intended")
    assert p.read_text(encoding="utf-8") == textwrap.dedent(body), (
        "no rewrite on the honest-red path")
    loops = [l for l in eng.loops if l.get("type") == "test-status-mismatch"]
    assert loops and "exactly" in loops[0]["detail"]


# --- quiet recheck stays side-effect-free --------------------------------------

def test_quiet_recheck_never_rewrites(tmp_path):
    body = """\
        def _call(method, path):
            return 201


        def test_post_created():
            code = _call("POST", "/notes")
            assert code in (200, 201)
    """
    eng, ok, p = _gate(tmp_path, body, quiet=True)
    assert ok is False, (
        "quiet mode is the S12.5 verdict-only re-run: the artifact IS "
        "smeared, so the verdict is red")
    assert p.read_text(encoding="utf-8") == textwrap.dedent(body), (
        "the quiet recheck is contractually side-effect-free (S12.5)")
