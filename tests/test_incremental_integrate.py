"""#4: incremental integrate. A branch gate runs only its subtree's test
files; the root (L0) runs the whole corpus. run_suite honors a targets list;
the engine computes the subtree targets and passes them for branches only."""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import pytest_verifier as pv  # noqa: E402
from harness import run_engine as eng  # noqa: E402


def _mk(root, rel, body):
    p = pathlib.Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


# ─── run_suite scoping ────────────────────────────────────────────────

def test_run_suite_runs_only_targets(tmp_path):
    root = str(tmp_path)
    _mk(root, "tests/test_a.py", "def test_a():\n    assert True\n")
    _mk(root, "tests/test_b.py", "def test_b():\n    assert False\n")  # would fail
    # scope to a only → green despite b being broken
    ok, out = pv.run_suite(root, include_smoke=False,
                           targets=["tests/test_a.py"])
    assert ok is True
    # full corpus → red (b fails)
    ok_all, _ = pv.run_suite(root, include_smoke=False)
    assert ok_all is False


def test_run_suite_ignores_missing_targets(tmp_path):
    root = str(tmp_path)
    _mk(root, "tests/test_a.py", "def test_a():\n    assert True\n")
    # a non-existent target is filtered; falls back to the full dir
    ok, _ = pv.run_suite(root, include_smoke=False,
                         targets=["tests/test_ghost.py"])
    assert ok is True


# ─── engine subtree target computation ────────────────────────────────

def _engine():
    return eng.Engine(workspace=tempfile.mkdtemp())


def test_subtree_targets_collects_existing_module_tests():
    e = _engine()
    root = pathlib.Path(e.workspace.root)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    for m in ("cart", "pay"):
        (root / "tests" / f"test_{m}.py").write_text("def test_x():\n    assert True\n")
    e._module_names = {"cart_node": "cart", "pay_node": "pay", "branch": "branch"}
    node = {"id": "branch", "children": [
        {"id": "cart_node", "children": []},
        {"id": "pay_node", "children": []}]}
    targets = e._subtree_test_targets(node)
    assert set(targets) == {"tests/test_cart.py", "tests/test_pay.py"}


def test_subtree_targets_skips_modules_without_test_file():
    e = _engine()
    root = pathlib.Path(e.workspace.root)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_cart.py").write_text("def test_x():\n    assert True\n")
    e._module_names = {"cart_node": "cart", "ghost_node": "ghost"}
    node = {"id": "b", "children": [
        {"id": "cart_node", "children": []},
        {"id": "ghost_node", "children": []}]}
    # ghost has no test file on disk → excluded
    assert e._subtree_test_targets(node) == ["tests/test_cart.py"]


def test_incremental_flag_default_on():
    e = _engine()
    assert e._incremental_integrate is True
