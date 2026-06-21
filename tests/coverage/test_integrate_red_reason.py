"""The integrate verdict must say WHAT failed, not 'FAIL (scaffolds)'. The
engine distils a RED dump into one human line — the boot-gate route when the
assembled product 404s, else the pytest FAILED line + its assertion."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from spec_flow_runner import Engine   # noqa: E402


def _eng():
    return Engine.__new__(Engine)


def test_boot_gate_route_is_the_reason():
    e = _eng()
    boot = ("BOOTGATE_OK preflight\n"
            "- boot-gate RED: GET /ui -> 404 / not HTML\n")
    r = e._concise_red_reason("45 passed", boot)
    assert "/ui" in r and "404" in r
    assert "scaffold" not in r.lower()


def test_pytest_failure_line_and_assertion():
    e = _eng()
    out = ("tests/test_ping.py::test_ping_routing FAILED\n"
           "FAILED tests/test_ping.py::test_ping_routing\n"
           "E       AssertionError: '200' not found in '404 Not Found'\n"
           "1 failed, 45 passed in 0.22s\n")
    r = e._concise_red_reason(out, "")
    assert "test_ping" in r
    assert "404 Not Found" in r


def test_never_bare_scaffolds():
    e = _eng()
    r = e._concise_red_reason("collection error, nothing useful", "")
    assert r and "scaffold" not in r.lower()
