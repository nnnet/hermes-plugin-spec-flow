"""#3: flaky-test quarantine. A test file whose isolated verdict flips
green↔red across reruns is flaky (proven non-deterministic); a consistently
red file is a real bug. Quarantine records the flaky file and excludes it
from the verdict — never silently, never masking a stable failure."""
import pathlib
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import pytest_verifier as pv  # noqa: E402


def _mk(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")


def _flaky_test(root, name, statefile):
    # passes/fails alternately by toggling a counter persisted on disk, so
    # successive isolated runs disagree → proven flaky
    _mk(root, f"tests/test_{name}.py", f"""
        import pathlib
        _s = pathlib.Path(__file__).parent / "{statefile}"
        def test_flip():
            n = int(_s.read_text()) if _s.exists() else 0
            _s.write_text(str(n + 1))
            assert n % 2 == 0
    """)


def test_detect_flaky_true_for_alternating(tmp_path):
    _flaky_test(tmp_path, "coin", ".coin")
    # across >=2 isolated runs the verdict differs → flaky
    assert pv.detect_flaky(str(tmp_path), "tests/test_coin.py", runs=4) is True


def test_detect_flaky_false_for_stable_red(tmp_path):
    _mk(tmp_path, "tests/test_bug.py", "def test_x():\n    assert False\n")
    # consistently red → a real bug, NOT flaky
    assert pv.detect_flaky(str(tmp_path), "tests/test_bug.py", runs=4) is False


def test_detect_flaky_false_for_stable_green(tmp_path):
    _mk(tmp_path, "tests/test_ok.py", "def test_x():\n    assert True\n")
    assert pv.detect_flaky(str(tmp_path), "tests/test_ok.py", runs=4) is False


def test_detect_flaky_needs_two_runs(tmp_path):
    _flaky_test(tmp_path, "coin", ".coin2")
    assert pv.detect_flaky(str(tmp_path), "tests/test_coin.py", runs=1) is False


def test_quarantine_separates_flaky_from_stable_red(tmp_path):
    _flaky_test(tmp_path, "coin", ".coin3")
    _mk(tmp_path, "tests/test_bug.py", "def test_x():\n    assert False\n")
    _mk(tmp_path, "tests/test_ok.py", "def test_x():\n    assert True\n")
    q = pv.quarantine_flaky(str(tmp_path), include_smoke=False, runs=4)
    assert "tests/test_coin.py" in q["flaky"]
    assert "tests/test_bug.py" in q["stable_red"]
    # the green file is neither
    assert "tests/test_ok.py" not in q["flaky"]
    assert "tests/test_ok.py" not in q["stable_red"]


def test_quarantine_off_below_two_runs(tmp_path):
    _mk(tmp_path, "tests/test_bug.py", "def test_x():\n    assert False\n")
    assert pv.quarantine_flaky(str(tmp_path), include_smoke=False, runs=1) == \
        {"flaky": [], "stable_red": []}
