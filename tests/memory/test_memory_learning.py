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

def _ws(tmp_path, files):
    """A real workspace on disk so make_verifier runs a genuine pytest."""
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return str(tmp_path)


def test_verifier_failure_is_retained_with_diagnosis(tmp_path):
    from harness import pytest_verifier as pv
    prov, _ = _mgr()
    # a REAL red suite: the verdict is a genuine pytest run, not a stub
    _ws(tmp_path, {"tests/test_x.py": "def test_x():\n    assert 1 == 2\n"})
    verify = pv.make_verifier(model="stub", max_repair=0)
    out = verify({"workspace_root": str(tmp_path), "node": "beta"})
    assert out["status"] == "FAIL"
    notes = prov._banks.get(memory.role_bank("verifier"), [])
    assert notes and "beta" in notes[0] and "assert 1 == 2" in notes[0]


def test_verifier_repair_success_is_retained(tmp_path, fake_openai):
    from harness import pytest_verifier as pv
    from harness_fakeapi import ok
    prov, _ = _mgr()
    # a REAL red test naming the missing route; the repair answer comes from a
    # real local server and greens it under a genuine pytest re-run (no faked
    # verdict). A free model id keeps the free-only gate from blocking the call.
    _ws(tmp_path, {"tests/test_route.py":
                   'def test_route():\n    assert False, "no route GET /x"\n'})
    fake_openai([(200, ok('{"files": {"tests/test_route.py":'
                          ' "def test_route():\\n    assert True\\n"}}'))])
    verify = pv.make_verifier(model="openrouter/x:free", max_repair=2)
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
