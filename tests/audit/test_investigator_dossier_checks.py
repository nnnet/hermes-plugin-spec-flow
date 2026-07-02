"""Audit rule (investigator dossier, v150): the dossier carries the CHECKED
VALUES from workspace tests — route calls plus the literal status/body
assertions next to them — so the «double guessing» class is catchable on
SUCCESS codes too, not only on failures.

v150: assertIn(code, (200, 201)) and the thrice-defined /health body were
invisible to the lenses because the dossier held no test-side literals.

No LLM here — pure dossier assembly over a synthetic run dir.
"""
from __future__ import annotations

import pathlib
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import investigator  # noqa: E402

V150_RUN = (pathlib.Path(__file__).resolve().parents[1] / "runs-out"
            / "2026-07-02T20-18-57__v150__p6-micro-notes")


def _run_dir(tmp_path) -> pathlib.Path:
    run = tmp_path / "run"
    tests = run / "workspace" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_core.py").write_text(textwrap.dedent('''\
        import json
        import unittest


        def _call(method, path, body=b""):
            return 201, b'{"id": 1}'


        def test_post_created():
            code, raw = _call("POST", "/notes", b'{"text": "hi"}')
            assert code == 201


        def test_health_body():
            code, raw = _call("GET", "/health")
            assert code == 200
            assert json.loads(raw) == {"status": "ok"}


        class TestSmear(unittest.TestCase):
            def test_hedged(self):
                code, raw = _call("POST", "/notes")
                self.assertIn(code, (200, 201))
    '''), encoding="utf-8")
    return run


def test_digest_carries_routes_and_pinned_statuses(tmp_path):
    digest = investigator._test_assert_digest(_run_dir(tmp_path))
    assert "POST /notes" in digest and "status == 201" in digest, digest
    assert "GET /health" in digest and "status == 200" in digest, digest


def test_digest_carries_the_smeared_success_set(tmp_path):
    digest = investigator._test_assert_digest(_run_dir(tmp_path))
    assert "status in (200, 201)" in digest, (
        "the assertIn(code, (200, 201)) smear must be visible verbatim: "
        + digest)


def test_digest_lines_name_file_and_test_function(tmp_path):
    digest = investigator._test_assert_digest(_run_dir(tmp_path))
    assert "tests/test_core.py::test_post_created:" in digest, digest
    assert "tests/test_core.py::test_hedged:" in digest, (
        "TestCase methods must be scanned too: " + digest)


def test_dossier_contains_the_new_section(tmp_path):
    dossier = investigator.build_dossier(_run_dir(tmp_path))
    assert "LEAF TEST CHECKS" in dossier
    assert "status in (200, 201)" in dossier


def test_empty_run_degrades_gracefully(tmp_path):
    (tmp_path / "empty").mkdir()
    assert investigator._test_assert_digest(tmp_path / "empty") \
        == "(no workspace tests)"
