"""П4-bis: bisection names the poisoner in the 'green alone, red together'
class. A test file that corrupts shared state passes by itself but reddens
the suite; removing it turns the suite green — that is the signature the
bisector keys on, handing the repair worker a precise target."""
import pathlib
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import pytest_verifier as pv  # noqa: E402


def _mk(root: pathlib.Path, rel: str, body: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")


def _poisoned_workspace(root: pathlib.Path):
    """A shared module with mutable global state; one test mutates it and
    never restores it, breaking a second test that assumes the default.
    Each test passes ALONE (load order), the pair is red together."""
    _mk(root, "src/state.py", """
        VALUE = 0
        def get():
            return VALUE
    """)
    # victim: assumes the default 0 — green alone, red after the poisoner ran
    _mk(root, "tests/test_victim.py", """
        import sys, pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))
        import state
        def test_default():
            assert state.get() == 0
    """)
    # poisoner: mutates the shared global and never resets it
    _mk(root, "tests/test_poison.py", """
        import sys, pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))
        import state
        def test_mutates():
            state.VALUE = 999
            assert state.get() == 999
    """)


def test_bisector_names_the_poisoner(tmp_path):
    _poisoned_workspace(tmp_path)
    diag = pv.bisect_poisoners(str(tmp_path), include_smoke=False)
    # pytest runs files alphabetically: test_poison before test_victim, so the
    # mutated global breaks the victim → suite red, both green alone.
    assert "tests/test_poison.py" in diag["poisoners"]
    assert set(diag["green_alone"]) == {"tests/test_poison.py",
                                        "tests/test_victim.py"}


def test_no_poisoner_when_all_green(tmp_path):
    _mk(tmp_path, "src/x.py", "def f():\n    return 1\n")
    _mk(tmp_path, "tests/test_a.py", "def test_a():\n    assert True\n")
    _mk(tmp_path, "tests/test_b.py", "def test_b():\n    assert 1 == 1\n")
    diag = pv.bisect_poisoners(str(tmp_path), include_smoke=False)
    # a clean green suite has no poisoner to name
    assert diag.get("poisoners") == []


def test_genuine_bugs_are_not_called_poisoners(tmp_path):
    # a file that FAILS alone is a real bug, not cross-test interference
    _mk(tmp_path, "tests/test_ok.py", "def test_ok():\n    assert True\n")
    _mk(tmp_path, "tests/test_bug.py", "def test_bug():\n    assert False\n")
    diag = pv.bisect_poisoners(str(tmp_path), include_smoke=False)
    assert diag["poisoners"] == []
    assert "tests/test_bug.py" in diag["victims"]


def test_bisect_skipped_for_single_file(tmp_path):
    _mk(tmp_path, "tests/test_only.py", "def test_x():\n    assert True\n")
    # fewer than 2 files → nothing to bisect
    assert pv.bisect_poisoners(str(tmp_path), include_smoke=False) == {}


def test_bisect_skipped_above_file_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "BISECT_MAX_FILES", 2)
    for i in range(3):
        _mk(tmp_path, f"tests/test_{i}.py", f"def test_{i}():\n    assert True\n")
    # 3 files over a ceiling of 2 → skipped (cost guard)
    assert pv.bisect_poisoners(str(tmp_path), include_smoke=False) == {}
