"""Memory LEARNING: all four roles retain, failures are retained with a
diagnosis, mental models are distilled at end of run and read back into
prompts.

Live picture that motivated this (Hindsight UI, 2026-06-13): only the
implementer bank existed, only successes were retained, zero mental
models — a memory that sees only victories learns nothing."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import memory  # noqa: E402


def _mgr(roles="accumulate", project="resume"):
    prov = memory.FakeMemory()
    mgr = memory.MemoryManager(prov, "case-x", roles_mode=roles,
                               project_mode=project)
    memory.MANAGER = mgr
    return prov, mgr


def teardown_function(_fn):
    memory.MANAGER = None


# ─── mental models: provider contract ─────────────────────────────────

def test_fake_mental_model_lifecycle():
    prov = memory.FakeMemory()
    assert prov.ensure_mental_model("b", "m1", "Topic", "review reject")
    assert prov.mental_models("b")[0]["content"] == ""
    prov.retain("b", "review reject happened on ghost criteria")
    assert prov.refresh_mental_model("b", "m1")
    assert "ghost criteria" in prov.mental_models("b")[0]["content"]


def test_refresh_unknown_model_is_false():
    prov = memory.FakeMemory()
    assert not prov.refresh_mental_model("b", "nope")


# ─── finalize_run: distillation per active banks ──────────────────────

def test_finalize_run_builds_models_for_roles_and_project():
    prov, mgr = _mgr()
    mgr.retain_role("reviewer", "Spec review REJECT at node 'a': AC without"
                    " REQ; vague metric")
    mgr.retain_project("Feature 'cart' is implemented by src/cart.py")
    done = memory.finalize_run()
    assert memory.role_bank("reviewer") in done
    assert memory.project_bank("case-x") in done
    models = prov.mental_models(memory.role_bank("reviewer"))
    assert any("REJECT" in m["content"] for m in models)


def test_finalize_run_respects_off_and_readonly():
    _, mgr = _mgr(roles="readonly", project="off")
    assert memory.finalize_run() == {}


def test_finalize_run_noop_without_manager():
    memory.MANAGER = None
    assert memory.finalize_run() == {}


# ─── recall: mental models reach the worker prompt ────────────────────

def test_recall_block_includes_distilled_models():
    prov, mgr = _mgr()
    mgr.retain_role("implementer", "Leaf 'cart' landed green as src/cart.py")
    memory.finalize_run()
    block = memory.recall_block_for("implementer", "cart module")
    assert "MENTAL MODELS" in block
    assert "src/cart.py" in block


def test_mental_block_is_cached_per_role():
    prov, mgr = _mgr()
    mgr.retain_role("implementer", "Leaf 'x' landed green as src/x.py")
    memory.finalize_run()
    first = mgr.mental_block("implementer")
    # new memories after the first fetch do NOT alter the cached block
    prov.retain(memory.role_bank("implementer"), "late noise")
    assert mgr.mental_block("implementer") == first


# ─── failure retention: every role records its misses ─────────────────

def test_verifier_failure_is_retained_with_diagnosis(tmp_path, monkeypatch):
    from harness import pytest_verifier as pv
    prov, _ = _mgr()
    monkeypatch.setattr(pv, "run_suite",
                        lambda root, smoke, targets=None: (False, "E assert 1 == 2"))
    verify = pv.make_verifier(model="stub", max_repair=0)
    out = verify({"workspace_root": str(tmp_path), "node": "beta"})
    assert out["status"] == "FAIL"
    notes = prov._banks.get(memory.role_bank("verifier"), [])
    assert notes and "beta" in notes[0] and "assert 1 == 2" in notes[0]


def test_verifier_repair_success_is_retained(tmp_path, monkeypatch):
    from harness import pytest_verifier as pv
    prov, _ = _mgr()
    calls = {"n": 0}

    def suite(root, smoke, targets=None):
        calls["n"] += 1
        return (calls["n"] >= 2, "E no route GET /x" if calls["n"] < 2
                else "all green")

    monkeypatch.setattr(pv, "run_suite", suite)
    monkeypatch.setattr(pv.llm_backend, "ask",
                        lambda *a, **k: '{"files": {"src/fix.py": "x = 1"}}')
    verify = pv.make_verifier(model="stub", max_repair=2)
    out = verify({"workspace_root": str(tmp_path), "node": "gamma"})
    assert out["status"] == "PASS"
    notes = prov._banks.get(memory.role_bank("verifier"), [])
    assert notes and "repair round" in notes[0] and "/x" in notes[0]


def test_demotion_sweep_feeds_decomposer_bank():
    # mirrors the run_cases post-run sweep: an engine demotion event
    # becomes a decomposer lesson
    prov, _ = _mgr()
    memory.retain_role(
        "decomposer",
        "Split of 'big' proposed branch-sized metrics but NO children —"
        " the engine demoted it to a leaf.",
        context="failed split", tags=["demotion"])
    block = memory.recall_block_for("decomposer", "branch children split")
    assert "demoted" in block
