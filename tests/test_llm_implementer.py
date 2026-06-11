"""Battle test — the live implementer adapter, exercised OFFLINE (roadmap D2).

D2 is a live implementer that asks a model to write real leaf code. The model
call is injectable, so here we drive the WHOLE adapter — prompt build, reply
parsing, file layout and the engine's own test run — with a deterministic stub
that spends NO quota. A real run (haiku) is opt-in and intentionally not part of
the suite.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import run_engine as eng  # noqa: E402
from harness import llm_implementer as li  # noqa: E402

LEAF = {
    "name": "impl-case",
    "goal": "a tiny health endpoint",
    "target": "x correct",
    "policy": {"measurable_target": True, "spend_per_action_usd": 1,
               "human_in_loop": False, "involves_outreach": False,
               "consent_obtained": True, "legal_exposure": False,
               "legality_reviewed": True},
    "tree": {
        "id": "health", "title": "Health endpoint",
        "metrics": {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 60,
                    "open_decisions": 0, "single_concern": True, "testable_criteria": True},
    },
}


def _stub_reply(prompt: str) -> str:
    # a model would return this; the fn name is fixed by the leaf id 'health'
    return (
        '{"code": "def health(payload=None):\\n'
        '    out = {\\"status\\": \\"ok\\", \\"node\\": \\"health\\"}\\n'
        '    if payload is not None:\\n        out[\\"payload\\"] = payload\\n'
        '    return out\\n", '
        '"test": "import sys\\nfrom pathlib import Path\\n'
        'sys.path.insert(0, str(Path(__file__).resolve().parent.parent / \\"src\\"))\\n'
        'from health import health\\n\\n'
        'def test_ok():\\n    assert health()[\\"status\\"] == \\"ok\\"\\n'
        'def test_payload():\\n    assert health({\\"p\\": 1})[\\"payload\\"] == {\\"p\\": 1}\\n"}'
    )


# ─── reply parsing ────────────────────────────────────────────────────


def test_parse_json_reply():
    out = li._parse('{"code": "a", "test": "b"}')
    assert out == {"code": "a", "test": "b"}


def test_parse_fenced_blocks():
    text = "```python\ncode here\n```\nthen\n```python\ntest here\n```"
    out = li._parse(text)
    assert out["code"] == "code here" and out["test"] == "test here"


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        li._parse("no code at all")


# ─── full adapter through the engine (stubbed model, no quota) ────────


def test_adapter_writes_real_files_and_tests_pass(plugin, tmp_path):
    impl = li.make_implementer(ask=_stub_reply)
    res = eng.run_project(dict(LEAF), workspace=str(tmp_path / "wk"),
                          depth="execute", tools=plugin.tools,
                          agents={"implementer": impl})
    root = pathlib.Path(res.workspace_root)
    code = root / "src" / "health.py"
    test = root / "tests" / "test_health.py"
    assert code.is_file() and test.is_file()
    assert "def health(" in code.read_text(encoding="utf-8")
    # depth=execute >= verify, so the engine ran the produced test — a verify
    # result event exists and the leaf reached done
    assert res.tasks["health"].status == "done"


def test_adapter_prompt_carries_leaf_identity(tmp_path):
    seen = {}

    def spy(prompt):
        seen["prompt"] = prompt
        return _stub_reply(prompt)

    impl = li.make_implementer(ask=spy)

    class _WS:
        def _write(self, *a, **k):
            pass

    impl({"node": "health", "title": "Health endpoint",
          "workspace": _WS(), "spec": "specs/health.md", "goal": "x"})
    assert "health" in seen["prompt"]
    assert "src/health.py" in seen["prompt"]
    assert "tests/test_health.py" in seen["prompt"]
