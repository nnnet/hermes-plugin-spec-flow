"""Platform-provided seed files survive the fresh-run workspace wipe."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from harness import run_engine as eng      # noqa: E402

CASE = {"name": "seed-case", "goal": "tiny", "target": "tiny"}
SMALL = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True, "testable_criteria": True}


def _decomposer(ctx):
    return {"metrics": dict(SMALL)}


def test_seed_files_survive_workspace_wipe(tmp_path):
    ws = tmp_path / "wk"
    # pre-existing junk proves the wipe happens AND the seeds land after it
    ws.mkdir()
    (ws / "stale.txt").write_text("junk", encoding="utf-8")
    eng.run_project(
        dict(CASE), workspace=str(ws), depth="spec",
        agents={"decomposer": _decomposer},
        seed_files={"src/registry.py": "ROUTES = {}\n",
                    "tests/smoke/test_smoke.py": "def test_x():\n    pass\n"})
    assert not (ws / "stale.txt").exists(), "fresh run must wipe the dir"
    assert (ws / "src" / "registry.py").read_text(encoding="utf-8") \
        == "ROUTES = {}\n"
    assert (ws / "tests" / "smoke" / "test_smoke.py").exists()


def test_seed_files_refuse_path_escape(tmp_path):
    ws = tmp_path / "wk"
    eng.run_project(dict(CASE), workspace=str(ws), depth="spec",
                    agents={"decomposer": _decomposer},
                    seed_files={"../escape.py": "x = 1\n"})
    assert not (tmp_path / "escape.py").exists()
