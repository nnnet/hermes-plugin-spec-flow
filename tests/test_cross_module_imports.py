"""Cross-module export contract: distinct weak-model leaves agree on a module
but not on the symbol names it exports. One leaf writes `def create_jwt`,
another's test does `from auth import generate_jwt`. Each leaf is green alone;
the corpus aborts at collection with ImportError (and downstream modules that
import the broken one fail too, as a cascade). The deterministic detector
names the exact missing export so repair adds/aliases it at the owner — the
real incident: `from registry import registry` where registry.py defined only
`route`, taking down every module that imported it."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import pytest_verifier as pv  # noqa: E402


def _src(tmp, name, body):
    d = tmp / "src"; d.mkdir(exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")


def _test(tmp, name, body):
    d = tmp / "tests"; d.mkdir(exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")


def test_detects_missing_export(tmp_path):
    _src(tmp_path, "auth.py", "def create_jwt(uid):\n    return uid\n")
    _test(tmp_path, "test_order.py",
          "from auth import generate_jwt\n\ndef test_x():\n    generate_jwt(1)\n")
    v = pv._cross_module_import_violations(str(tmp_path))
    assert v == [("tests/test_order.py", "auth", "generate_jwt")]


def test_clean_when_symbol_exists(tmp_path):
    _src(tmp_path, "auth.py", "def generate_jwt(uid):\n    return uid\n")
    _test(tmp_path, "test_order.py", "from auth import generate_jwt\n")
    assert pv._cross_module_import_violations(str(tmp_path)) == []


def test_registry_object_missing(tmp_path):
    # the real incident: registry.py exports only `route`, not the object
    _src(tmp_path, "registry.py", "def route(method, path):\n    return method\n")
    _src(tmp_path, "catalog.py", "from registry import registry\n")
    v = pv._cross_module_import_violations(str(tmp_path))
    assert ("src/catalog.py", "registry", "registry") in v


def test_ignores_stdlib_and_third_party(tmp_path):
    _src(tmp_path, "svc.py",
         "from os import path\nfrom json import dumps\n"
         "from flask import Flask\n")
    # none of os/json/flask are local src modules → never flagged
    assert pv._cross_module_import_violations(str(tmp_path)) == []


def test_reexported_name_counts_as_export(tmp_path):
    # base.py re-exports helper; mid.py imports helper from base → legal
    _src(tmp_path, "helpers.py", "def helper():\n    return 1\n")
    _src(tmp_path, "base.py", "from helpers import helper\n")
    _src(tmp_path, "mid.py", "from base import helper\n")
    assert pv._cross_module_import_violations(str(tmp_path)) == []


def test_class_and_assignment_exports_recognized(tmp_path):
    _src(tmp_path, "models.py",
         "class Order:\n    pass\n\nDB_NAME = 'm'\n")
    _src(tmp_path, "use.py", "from models import Order, DB_NAME\n")
    assert pv._cross_module_import_violations(str(tmp_path)) == []


def test_star_import_not_flagged(tmp_path):
    _src(tmp_path, "a.py", "def f():\n    return 1\n")
    _src(tmp_path, "b.py", "from a import *\n")
    assert pv._cross_module_import_violations(str(tmp_path)) == []


def test_unparseable_owner_skipped(tmp_path):
    # a syntactically broken owner is a different class (non-ASCII/syntax);
    # this detector must not crash or false-flag on it
    _src(tmp_path, "broken.py", "def f(:\n    pass\n")
    _src(tmp_path, "user.py", "from broken import f\n")
    out = pv._cross_module_import_violations(str(tmp_path))
    assert out == []          # owner unparseable → not this detector's job


def test_module_exports_collects_all_binders(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("import os\nfrom x import y\nA = 1\n"
                 "def f():\n    pass\nclass C:\n    pass\n", encoding="utf-8")
    ex = pv._module_exports(p)
    assert {"os", "y", "A", "f", "C"} <= ex
