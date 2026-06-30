"""#82 — deterministic autofix for the generated 'no third-party imports'
acceptance test that misclassifies the product's OWN src module as a
third-party dependency.

A weak model writes a test that parses a src module and asserts every import's
top name is in ``sys.stdlib_module_names``. When the product's ``core.py`` does
``from app import application`` (``app`` is the synthesized entry — its own
module), the test fails with ``non-stdlib import: app`` though that is NOT a
third-party dependency. ``autofix_local_import_stdlib_test`` widens the test's
allow-set with the real ``src/*.py`` module names, discovered at runtime.

The fix is MONOTONIC and honest: a genuine pip import (not a ``src/*.py`` stem)
still fails — the criterion's true intent ('no pip installs') is preserved.
"""

import pathlib
import subprocess
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from harness import pytest_verifier as pv  # noqa: E402


_STDLIB_TEST_GETATTR = textwrap.dedent('''\
    import ast, sys
    from pathlib import Path

    def test_ac_no_third_party_imports():
        src = Path(__file__).resolve().parent.parent / "src" / "core.py"
        tree = ast.parse(src.read_text())
        stdlib = getattr(sys, "stdlib_module_names", None) or {
            "json", "os", "sys", "pathlib", "ast",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                assert top in stdlib, f"non-stdlib import: {node.module}"
''')

_STDLIB_TEST_ATTR = textwrap.dedent('''\
    import ast, sys
    from pathlib import Path

    def test_ac_no_third_party_imports():
        src = Path(__file__).resolve().parent.parent / "src" / "core.py"
        tree = ast.parse(src.read_text())
        stdlib = sys.stdlib_module_names
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                assert top in stdlib, f"non-stdlib import: {node.module}"
''')


def _mk_product(tmp_path, core_src, test_src):
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("application = lambda e, s: [b'ok']\n")
    (src / "core.py").write_text(core_src)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text(test_src)
    return tmp_path


def _run(tmp_path):
    # run the test exactly as the suite would, from the product root
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_core.py", "-q"],
        cwd=str(tmp_path), capture_output=True, text=True)


def test_local_import_false_positive_is_fixed(tmp_path):
    core = "from app import application as _entry\nNOTES = []\n"
    root = _mk_product(tmp_path, core, _STDLIB_TEST_GETATTR)
    # RED before: 'app' is flagged as non-stdlib
    assert _run(root).returncode != 0
    fixed = pv.autofix_local_import_stdlib_test(str(root))
    assert fixed == ["tests/test_core.py"]
    # GREEN after: the product's own module is no longer a "third-party" import
    assert _run(root).returncode == 0


def test_genuine_third_party_still_fails(tmp_path):
    # importing a real pip package (NOT a src/*.py stem) must STILL be flagged —
    # the fix is monotonic, it never weakens the criterion's true intent
    core = "from requests import get\nNOTES = []\n"
    root = _mk_product(tmp_path, core, _STDLIB_TEST_GETATTR)
    pv.autofix_local_import_stdlib_test(str(root))
    res = _run(root)
    assert res.returncode != 0
    assert "requests" in (res.stdout + res.stderr)


def test_attribute_form_is_matched(tmp_path):
    # the allow-set may be seeded via `sys.stdlib_module_names` (Attribute)
    core = "from app import application as _entry\n"
    root = _mk_product(tmp_path, core, _STDLIB_TEST_ATTR)
    assert pv.autofix_local_import_stdlib_test(str(root)) == ["tests/test_core.py"]
    assert _run(root).returncode == 0


def test_unrelated_test_is_untouched(tmp_path):
    core = "from app import application as _entry\n"
    plain = "def test_ok():\n    assert 1 + 1 == 2\n"
    root = _mk_product(tmp_path, core, plain)
    before = (root / "tests" / "test_core.py").read_text()
    assert pv.autofix_local_import_stdlib_test(str(root)) == []
    assert (root / "tests" / "test_core.py").read_text() == before


def test_uncompilable_test_left_for_other_fixers(tmp_path):
    core = "from app import application as _entry\n"
    broken = _STDLIB_TEST_GETATTR + "\n    this is not python(\n"
    root = _mk_product(tmp_path, core, broken)
    before = (root / "tests" / "test_core.py").read_text()
    assert pv.autofix_local_import_stdlib_test(str(root)) == []
    assert (root / "tests" / "test_core.py").read_text() == before


def test_rewrite_stays_compilable(tmp_path):
    core = "from app import application as _entry\n"
    root = _mk_product(tmp_path, core, _STDLIB_TEST_GETATTR)
    pv.autofix_local_import_stdlib_test(str(root))
    body = (root / "tests" / "test_core.py").read_text()
    compile(body, "test_core.py", "exec")          # must not raise
