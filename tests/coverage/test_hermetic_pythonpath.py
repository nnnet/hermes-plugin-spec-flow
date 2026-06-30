"""v131 root — the product must be judged on its OWN src only.

v131's boot-gate failed with ``cannot import name 'insert_note' from 'db'
(/some/other/repo/.../db/__init__.py)``: a FOREIGN ``db`` package on the
inherited ``PYTHONPATH`` shadowed the product's own ``src/db.py`` (which was
mid-build at that moment). A foreign same-named module must neither shadow a
product module (misleading RED) nor satisfy a product import (false green).

``hermetic_env`` makes the product's ``src/`` the sole ``PYTHONPATH`` entry for
every product subprocess (pytest + boot-gate), so imports always resolve to the
assembled modules regardless of the ambient path.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "harness"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from harness import pytest_verifier as pv  # noqa: E402


def test_hermetic_env_puts_product_src_first(tmp_path):
    env = pv.hermetic_env(str(tmp_path))
    assert env["PYTHONPATH"] == str(tmp_path / "src")


def _product(root):
    src = root / "src"
    src.mkdir()
    (src / "db.py").write_text(
        "def insert_note(text):\n    return 1\n")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_db.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))\n"
        "from db import insert_note\n"
        "def test_insert():\n"
        "    assert insert_note('x') == 1\n")


def test_foreign_db_on_pythonpath_cannot_shadow_product(tmp_path, monkeypatch):
    # a hostile foreign `db` package that lacks insert_note — importing it would
    # raise ImportError, exactly the v131 failure
    foreign = tmp_path / "foreign"
    (foreign / "db").mkdir(parents=True)
    (foreign / "db" / "__init__.py").write_text("# no insert_note here\n")
    monkeypatch.setenv("PYTHONPATH", str(foreign))

    root = tmp_path / "ws"
    root.mkdir()
    _product(root)

    passed, out = pv.run_suite(str(root), include_smoke=False)
    assert passed, out          # product's own db.py wins → green
    assert "ImportError" not in out
