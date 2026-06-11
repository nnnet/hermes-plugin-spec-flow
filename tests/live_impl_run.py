"""One live leaf through the real implementer (haiku). Opt-in, spends quota.

Runs a single-leaf project at depth='execute' with the LIVE llm_implementer
(claude -p --model haiku). Materialises into a timestamped runs-out workspace
and reports the generated code + whether the engine's own test run went green.
"""
from __future__ import annotations

import datetime
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from harness import run_engine as eng          # noqa: E402
from harness import llm_implementer as li      # noqa: E402

STAMP = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
WS = HERE / "runs-out" / f"{STAMP}__live-haiku-impl"

PROJECT = {
    "name": "live-haiku-impl",
    "goal": "a tiny in-memory key/value store with get/set",
    "target": "get returns what set stored; missing key -> None",
    "policy": {"measurable_target": True, "spend_per_action_usd": 1,
               "human_in_loop": False, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
    "tree": {
        "id": "kvstore", "title": "In-memory key/value store",
        "metrics": {"modules": 1, "tasks": 4, "interfaces": 1, "estimated_loc": 80,
                    "open_decisions": 0, "single_concern": True, "testable_criteria": True},
    },
}


def main() -> int:
    impl = li.make_implementer()  # live: claude -p --model haiku
    print(f"workspace: {WS}")
    res = eng.run_project(dict(PROJECT), workspace=str(WS), depth="execute",
                          agents={"implementer": impl})
    root = pathlib.Path(res.workspace_root)
    code = root / "src" / "kvstore.py"
    test = root / "tests" / "test_kvstore.py"
    print("\n=== generated src/kvstore.py ===")
    print(code.read_text(encoding="utf-8") if code.is_file() else "<missing>")
    print("=== generated tests/test_kvstore.py ===")
    print(test.read_text(encoding="utf-8") if test.is_file() else "<missing>")
    print("=== leaf status ===")
    print("kvstore:", res.tasks["kvstore"].status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
