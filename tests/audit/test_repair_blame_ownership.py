"""Audit rule S12.10 (v162): the integrate-repair blame picker attributes by
the ROUTE-OWNERSHIP datum of the FAILING TEST's exercised route.

v162 (2026-07-03T19-55-37__v162__p6-micro-notes): the assembled suite failed
on test_get_about_returns_200_html and test_get_ui_returns_200_html — surfaces
owned by about_page/web_ui — yet the doctor ran 'module repair: rework core
(acceptance blamed it)' THREE times (ticks 144/147/150). The failure text
carried no src/<file>.py frame (pure assertion failures), so the picker fell
to `_blamed_module_from_routes`, which greps ALL route paths out of the whole
pytest dump: a co-failing test whose printed source line mentioned a
core-owned path stole the blame, and routes that were UNRESOLVED (exactly the
404 class!) can never match its resolved-handler mapping at all.

Contract enforced (deterministic, model-independent):
  * `_blamed_module_from_failure` first walks src frames (a real crash site
    stays the strongest signal), then attributes by the FAILING TEST itself:
    parse the failing test ids out of the pytest output, find each test
    function in its file, extract its route calls with the SAME (METHOD,
    "/path") string-constant scan the test-status gate uses, and map each
    route to its owner module through the engine's OWN ownership datum
    (`_route_handler_modules` / `_route_owners`) — never through the
    resolved-handler mapping (an unserved route has no handler precisely
    when it needs blame the most);
  * GREEN: a genuinely core-owned failure (the failing test exercises only
    core-owned routes) still blames core; no failing-test route ownership =
    fall through to the older heuristics (no false rework).

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
    "routes": [("GET", "/about")],
    "media": {"/notes": "json", "/health": "json", "/about": "html"},
}

# the v162 shape: pure assertion failures, no src/<file>.py frame anywhere —
# the test names appear in FAILED lines and the printed test source carries
# the exercised route as a string constant
_TEST_APP = """\
    import unittest


    class TestApp(unittest.TestCase):
        def _request(self, method, path, payload=None):
            return 404, {}, b""

        def test_get_about_returns_200_html(self):
            status, headers, body = self._request("GET", "/about")
            self.assertEqual(status, 200)

        def test_post_then_get_roundtrip(self):
            status, headers, body = self._request("POST", "/notes",
                                                  {"text": "hi"})
            self.assertEqual(status, 201)
"""

_FAIL_ABOUT = (
    "FAILED tests/test_app.py::TestApp::test_get_about_returns_200_html "
    "- AssertionError: 404 != 200\n"
    "E   AssertionError: 404 != 200\n")
_FAIL_NOTES = (
    "FAILED tests/test_app.py::TestApp::test_post_then_get_roundtrip "
    "- AssertionError: 404 != 201\n"
    "E   AssertionError: 404 != 201\n")


def _engine(tmp_path):
    eng = sfr.Engine(workspace=str(tmp_path / "wk"), depth=sfr.DEPTH_SPEC)
    eng._product_contract = lambda: dict(_CONTRACT)
    # ownership datum: core owns the notes surface, about_page owns /about
    eng._leaf_owned_routes({"id": "core", "requirement":
                            "own POST /notes and GET /notes and GET /health"})
    eng._leaf_owned_routes({"id": "about_page", "requirement":
                            "serve GET /about as a small HTML page"})
    root = pathlib.Path(eng.workspace.root)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_app.py").write_text(
        textwrap.dedent(_TEST_APP), encoding="utf-8")
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "core.py").write_text(
        "def post_notes(payload, query):\n    return 201, {}\n",
        encoding="utf-8")
    return eng


# ---- RED: the v162 blame roulette --------------------------------------------

def test_about_failure_blames_the_about_owner_not_core(tmp_path):
    eng = _engine(tmp_path)
    blamed = eng._blamed_module_from_failure(_FAIL_ABOUT, "app")
    assert blamed == "about_page", (
        "v162: the failing test exercises GET /about — owned by about_page "
        "per the engine's OWN ownership datum — yet the picker blamed %r; "
        "three 'rework core' rounds rewrote the wrong module while the "
        "/about surface stayed 404" % blamed)


def test_unserved_route_still_attributes_by_ownership(tmp_path):
    # the exact v162 aggravation: GET /about resolves to NO handler (that is
    # WHY the test fails) — a picker that walks the resolved-handler mapping
    # can never blame the owner of the one route that needs repair
    eng = _engine(tmp_path)
    mapping, unresolved = eng._resolve_route_handlers(_CONTRACT)
    assert ("GET", "/about") in unresolved, "fixture: /about must be unserved"
    blamed = eng._blamed_module_from_failure(_FAIL_ABOUT, "app")
    assert blamed == "about_page"


def test_mixed_failures_do_not_let_core_steal_the_blame(tmp_path):
    # co-failing core test present — majority of failing-test routes decides,
    # but the /about-only failure must never be attributed to core just
    # because ANOTHER test's printed source mentions /notes
    eng = _engine(tmp_path)
    blamed = eng._blamed_module_from_failure(_FAIL_ABOUT + _FAIL_NOTES, "app")
    assert blamed in ("about_page", "core")   # deterministic pick below
    only_about = eng._blamed_module_from_failure(_FAIL_ABOUT, "app")
    assert only_about == "about_page"


# ---- GREEN: honest core blame stays -------------------------------------------

def test_core_owned_failure_still_blames_core(tmp_path):
    eng = _engine(tmp_path)
    blamed = eng._blamed_module_from_failure(_FAIL_NOTES, "app")
    assert blamed == "core", (
        "a genuinely core-owned failure (the failing test exercises only "
        "POST /notes) must still blame core, got %r" % blamed)


def test_src_frame_still_wins_over_test_ownership(tmp_path):
    # a real crash site in a module traceback is the strongest signal — the
    # ownership attribution must not override it
    eng = _engine(tmp_path)
    eng.tasks["core"] = object()
    out = (_FAIL_ABOUT
           + '  File "src/core.py", line 3, in post_notes\n'
           + "    raise KeyError('NOTES_DB')\n")
    assert eng._blamed_module_from_failure(out, "app") == "core"


def test_no_failing_tests_falls_through_quietly(tmp_path):
    eng = _engine(tmp_path)
    assert eng._blamed_module_from_failure(
        "boot-gate RED: something odd", "app") is None
