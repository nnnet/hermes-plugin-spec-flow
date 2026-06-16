"""Memory MANAGEMENT: case-YAML modes for the two tiers + worker wiring.

roles.mode   accumulate | readonly | fresh | off   (craft across runs)
project.mode fresh | resume | readonly | off       (one case's decisions)
"""
import json as _json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import memory as mem    # noqa: E402


@pytest.fixture(autouse=True)
def clean_manager():
    mem.MANAGER = None
    yield
    mem.MANAGER = None


def _seeded_provider():
    f = mem.FakeMemory()
    f.retain(mem.role_bank("implementer"), "craft: payouts via amount_cents")
    f.retain(mem.project_bank("p4-case"), "decision: flat paths only")
    return f


# ─── start-of-run modes ───────────────────────────────────────────────

def test_project_fresh_clears_project_but_keeps_roles():
    f = _seeded_provider()
    m = mem.MemoryManager(f, "p4-case", roles_mode="accumulate",
                          project_mode="fresh")
    m.setup()
    assert f.recall(mem.role_bank("implementer"), "payouts craft")
    assert f.recall(mem.project_bank("p4-case"), "flat paths") == []


def test_project_resume_keeps_everything():
    f = _seeded_provider()
    mem.MemoryManager(f, "p4-case", project_mode="resume").setup()
    assert f.recall(mem.project_bank("p4-case"), "flat paths decision")


def test_roles_fresh_clears_role_banks():
    f = _seeded_provider()
    mem.MemoryManager(f, "p4-case", roles_mode="fresh",
                      project_mode="resume").setup()
    assert f.recall(mem.role_bank("implementer"), "payouts craft") == []


def test_readonly_blocks_retain_allows_recall():
    f = _seeded_provider()
    m = mem.MemoryManager(f, "p4-case", roles_mode="readonly",
                          project_mode="readonly")
    m.setup()
    assert m.retain_role("implementer", "new fact") is False
    assert m.retain_project("new decision") is False
    assert "payouts" in m.recall_role("implementer", "payouts craft")
    assert "flat paths" in m.recall_project("flat paths decision")


def test_off_silences_both_directions():
    f = _seeded_provider()
    m = mem.MemoryManager(f, "p4-case", roles_mode="off",
                          project_mode="off")
    assert m.recall_role("implementer", "payouts") == ""
    assert m.retain_role("implementer", "x") is False
    assert m.recall_project("flat paths") == ""


def test_unknown_modes_rejected():
    with pytest.raises(ValueError, match="roles.mode"):
        mem.MemoryManager(mem.FakeMemory(), "c", roles_mode="wipe")
    with pytest.raises(ValueError, match="project.mode"):
        mem.MemoryManager(mem.FakeMemory(), "c", project_mode="append")


# ─── configure() from the case YAML block ─────────────────────────────

def test_configure_installs_manager_and_applies_modes(monkeypatch):
    seeded = _seeded_provider()
    monkeypatch.setattr(mem, "make_provider", lambda cfg: seeded)
    m = mem.configure({"provider": "fake",
                       "roles": {"mode": "accumulate"},
                       "project": {"mode": "fresh"}}, "p4-case")
    assert mem.MANAGER is m
    assert seeded.recall(mem.project_bank("p4-case"), "flat paths") == []
    block = mem.recall_block_for("implementer", "payouts craft amount")
    assert "amount_cents" in block


def test_configure_none_disables_everything():
    assert mem.configure(None, "p4-case") is None
    assert mem.recall_block_for("implementer", "anything") == ""
    assert mem.retain_role("implementer", "x") is False


# ─── worker wiring (chat-only, offline) ───────────────────────────────

_LEAF = {"modules": 1, "tasks": 3, "interfaces": 1, "estimated_loc": 80,
         "open_decisions": 0, "single_concern": True,
         "testable_criteria": True}


def test_decomposer_prompt_carries_memory_block(monkeypatch, fake_openai):
    from harness import role_worker as rw
    from harness_fakeapi import ok
    f = mem.FakeMemory()
    f.retain(mem.role_bank("decomposer"),
             "craft: marketplace specs need explicit payout requirements")
    mem.MANAGER = mem.MemoryManager(f, "p4-case")
    # real local server answers; we then read the prompt the harness sent it.
    monkeypatch.setattr(rw, "_model_for",
                        lambda role, specialty="": "openrouter/x:free")
    srv = fake_openai([(200, ok(_json.dumps(
        {"atomic": True, "metrics": dict(_LEAF),
         "spec_markdown": "## Requirements\n- x"})))])
    dec = rw.make_decomposer()
    dec({"project": {"goal": "marketplace with payouts", "target": "t",
                     "constitution": []},
         "node": {"id": "payouts", "title": "Seller payouts"},
         "parent": "L0", "depth": 3, "ancestors": [], "existing_nodes": []})
    sent = " ".join(m.get("content", "")
                    for m in srv.requests[0]["messages"])
    assert "RELEVANT EXPERIENCE" in sent
    assert "payout requirements" in sent


def test_green_leaf_retained_in_both_tiers(tmp_path, monkeypatch, fake_openai):
    from harness import role_worker as rw
    from harness_fakeapi import ok
    f = mem.FakeMemory()
    mem.MANAGER = mem.MemoryManager(f, "p4-case")
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "pay.md").write_text("spec", encoding="utf-8")

    monkeypatch.setattr(rw, "_model_for",
                        lambda role, specialty="": "openrouter/x:free")
    fake_openai([(200, ok(_json.dumps({"files": {
        "src/pay.py": "def ok():\n    return True\n",
        "tests/test_pay.py":
            "import sys, pathlib\n"
            "sys.path.insert(0, str(pathlib.Path(__file__)"
            ".resolve().parents[1] / 'src'))\n"
            "import pay\n"
            "def test_ok():\n    assert pay.ok()\n"}})))])
    impl = rw.make_implementer()
    class W:
        root = str(tmp_path)
        enabled = True

        def _write(self, rel, body, kind):
            f = tmp_path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body, encoding="utf-8")
            return rel

    impl({"node": "pay", "title": "Seller payouts", "workspace": W(),
          "spec": "specs/pay.md"})
    role_hits = f.recall(mem.role_bank("implementer"),
                         "leaf payouts landed green")
    proj_hits = f.recall(mem.project_bank("p4-case"),
                         "payouts implemented src")
    assert role_hits and "pay.py" in role_hits[0]
    assert proj_hits and "src/pay.py" in proj_hits[0]
